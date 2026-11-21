import json
from collections import defaultdict
from datetime import datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from functools import wraps

from django import forms
from django.contrib.auth import login
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import Permission, User
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import PermissionDenied
from django.db.models import Count
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.translation import gettext as _, gettext_lazy
from django.views.decorators.http import require_POST

from . import services
from .models import (Company, ManufacturingOrder, Partner, PosLine, PosOrder, PosSession,
                     PosShop, Product, Profile, PurchaseOrder, SaleLine, SaleOrder, StockQuant)


def profile_of(user):
    return getattr(user, "profile", None) if user.is_authenticated else None


def company_required(view):
    @login_required
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if not profile_of(request.user):
            raise PermissionDenied
        return view(request, *args, **kwargs)
    return wrapper


def manager_required(view):
    @company_required
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if request.user.profile.role != Profile.MANAGER:
            raise PermissionDenied
        return view(request, *args, **kwargs)
    return wrapper


def add_user(company, username, password, role):
    """Managers get the back office; cashiers get only the POS screen."""
    user = User.objects.create_user(username, password=password,
                                    is_staff=(role == Profile.MANAGER))
    if role == Profile.MANAGER:
        user.user_permissions.set(Permission.objects.filter(content_type__app_label="erp"))
    Profile.objects.create(user=user, company=company, role=role)
    return user


def home(request):
    if request.user.is_superuser and not profile_of(request.user):
        return redirect("owner")
    profile = profile_of(request.user)
    if not profile:
        return redirect("login")
    return redirect("dashboard" if profile.role == Profile.MANAGER else "pos")


class UserForm(forms.Form):
    username = forms.CharField(label=gettext_lazy("Username"), max_length=150)
    password = forms.CharField(label=gettext_lazy("Password"), widget=forms.PasswordInput)

    def clean_username(self):
        name = self.cleaned_data["username"]
        if User.objects.filter(username=name).exists():
            raise forms.ValidationError(_("Username taken."))
        return name

    def clean_password(self):
        validate_password(self.cleaned_data["password"])
        return self.cleaned_data["password"]


class SignupForm(UserForm):
    company = forms.CharField(label=gettext_lazy("Company name"), max_length=120)
    field_order = ["company", "username", "password"]


class TeamForm(UserForm):
    role = forms.ChoiceField(label=gettext_lazy("Role"), choices=Profile.ROLES,
                             initial=Profile.CASHIER)


def signup(request):
    form = SignupForm(request.POST or None)
    if form.is_valid():
        company = services.setup_company(form.cleaned_data["company"])
        PosShop.objects.create(company=company, name=_("Shop"),
                               warehouse=company.warehouse_set.first())
        user = add_user(company, form.cleaned_data["username"], form.cleaned_data["password"],
                        Profile.MANAGER)
        login(request, user)
        return redirect("dashboard")
    return render(request, "erp/signup.html", {"form": form})


@manager_required
def team(request):
    company = request.user.profile.company
    form = TeamForm(request.POST or None)
    if form.is_valid():
        add_user(company, form.cleaned_data["username"], form.cleaned_data["password"],
                 form.cleaned_data["role"])
        return redirect("team")
    members = Profile.objects.filter(company=company).select_related("user")
    return render(request, "erp/team.html", {"form": form, "members": members})


@company_required
def pos(request):
    company = request.user.profile.company
    session = PosSession.objects.filter(company=company, user=request.user,
                                        state=PosSession.OPEN).first()
    if request.method == "POST":
        try:
            amount = Decimal(request.POST.get("cash") or 0)
        except InvalidOperation:
            amount = Decimal(0)
        if session:
            diff = services.close_session(session, amount)
            return render(request, "erp/pos_closed.html", {"session": session, "diff": diff})
        shop = get_object_or_404(PosShop, company=company, pk=request.POST["shop"])
        PosSession.objects.create(company=company, shop=shop, user=request.user,
                                  opening_cash=amount)
        return redirect("pos")
    if not session:
        return render(request, "erp/pos_open.html",
                      {"shops": PosShop.objects.filter(company=company)})
    products = Product.objects.filter(company=company, can_be_sold=True, sale_price__gt=0)
    return render(request, "erp/pos.html", {
        "session": session,
        "products": [{"id": p.id, "name": p.name, "price": str(p.sale_price)} for p in products],
    })


@company_required
@require_POST
def pos_order(request):
    company = request.user.profile.company
    session = get_object_or_404(PosSession, company=company, user=request.user,
                                state=PosSession.OPEN)
    try:
        data = json.loads(request.body)
        items = [(get_object_or_404(Product, company=company, pk=i["id"]), Decimal(str(i["qty"])))
                 for i in data["items"]]
        if not items or any(q <= 0 for _p, q in items):
            raise services.StockError(_("Add at least one product."))
        order = services.pos_sell(session, items, data.get("method", "cash"),
                                  client_ref=str(data.get("ref", ""))[:40])
    except (services.StockError, KeyError, InvalidOperation, ValueError, TypeError) as e:
        return JsonResponse({"error": str(e)}, status=400)
    return JsonResponse({"order": str(order), "id": order.pk,
                         "total": f"{order.total:,.2f}"})


@manager_required
def invoice(request, pk):
    order = get_object_or_404(SaleOrder, company=request.user.profile.company, pk=pk)
    return render(request, "erp/invoice.html", {"order": order, "title": _("Invoice"),
                                                "partner": order.partner})


@company_required
def receipt(request, pk):
    order = get_object_or_404(PosOrder, company=request.user.profile.company, pk=pk)
    return render(request, "erp/invoice.html", {"order": order, "title": _("Receipt"),
                                                "partner": order.customer})


def _day(value, end=False):
    try:
        d = datetime.strptime(value, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None
    return timezone.make_aware(datetime.combine(d, time.max if end else time.min))


@manager_required
def reports(request):
    company = request.user.profile.company
    today = timezone.localdate()
    start = _day(request.GET.get("from")) or _day(today.replace(day=1).isoformat())
    end = _day(request.GET.get("to"), end=True) or _day(today.isoformat(), end=True)
    period = {"order__date__range": (start, end), "order__company": company}

    sales = defaultdict(lambda: [Decimal(0), Decimal(0)])  # product -> [qty, amount]
    for line in SaleLine.objects.filter(order__state="done", **period).select_related(
            "product", "order"):
        sales[line.product.name][0] += line.quantity
        sales[line.product.name][1] += line.subtotal * (line.order.rate or 1)  # company currency
    for line in PosLine.objects.filter(**period).select_related("product"):
        sales[line.product.name][0] += line.quantity
        sales[line.product.name][1] += line.subtotal
    sales_rows = sorted(sales.items(), key=lambda kv: -kv[1][1])

    purchases = sum((po.total_base for po in PurchaseOrder.objects.filter(
        company=company, state="done", date__range=(start, end))), Decimal(0))
    produced = defaultdict(Decimal)
    for mo in ManufacturingOrder.objects.filter(company=company, state="done",
                                                date__range=(start, end)).select_related("bom__product"):
        produced[mo.bom.product.name] += mo.quantity
    stock = [(q.product.name, q.location, q.quantity, q.quantity * q.product.cost)
             for q in StockQuant.objects.filter(company=company, quantity__gt=0)
             .select_related("product", "location__warehouse")]
    return render(request, "erp/reports.html", {
        "start": start.date(), "end": end.date(), "sales_rows": sales_rows,
        "sales_total": sum((v[1] for v in sales.values()), Decimal(0)),
        "purchases": purchases, "produced": sorted(produced.items()),
        "stock": stock, "stock_value": sum((r[3] for r in stock), Decimal(0)),
    })


def _dashboard_company(request):
    """Managers see their company; the Beauty-Grid owner (superuser) can open any with ?company=."""
    if request.user.is_superuser and request.GET.get("company"):
        return get_object_or_404(Company, pk=request.GET["company"])
    profile = profile_of(request.user)
    if profile and profile.role == Profile.MANAGER:
        return profile.company
    if request.user.is_superuser:
        return None
    raise PermissionDenied


def company_stats(company, since=None):
    sos = SaleOrder.objects.filter(company=company).exclude(stage=SaleOrder.CANCELLED_S)
    pos = PosOrder.objects.filter(company=company)
    if since:
        sos, pos = sos.filter(date__gte=since), pos.filter(date__gte=since)
    revenue = sum((o.total_base for o in sos.filter(state="done")), Decimal(0)) + \
        sum((o.total for o in pos), Decimal(0))
    return {"orders": sos.count() + pos.count(), "revenue": revenue}


@login_required
def dashboard(request):
    company = _dashboard_company(request)
    if company is None:
        return redirect("owner")
    month = company_stats(company, timezone.now() - timedelta(days=30))
    stages = dict(SaleOrder.objects.filter(company=company).values_list("stage")
                  .annotate(n=Count("pk")).values_list("stage", "n"))
    low = StockQuant.objects.filter(company=company, quantity__lte=5,
                                    location__kind="internal").select_related("product")
    return render(request, "erp/dashboard.html", {
        "company": company, "all": company_stats(company), "month": month,
        "customers": Partner.objects.filter(company=company, is_customer=True).count(),
        "products": Product.objects.filter(company=company).count(),
        "stages": [(label, stages.get(key, 0), key) for key, label in SaleOrder.STAGES],
        "low": low.order_by("quantity")[:10], "low_count": low.count(),
        "waiting_mo": ManufacturingOrder.objects.filter(company=company, state="draft").count(),
        "draft_po": PurchaseOrder.objects.filter(company=company, state="draft").count(),
        "online": SaleOrder.objects.filter(company=company, source=SaleOrder.ONLINE)
        .select_related("partner").order_by("-date")[:5],
    })


def owner_required(view):
    @login_required
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_superuser:
            raise PermissionDenied
        return view(request, *args, **kwargs)
    return wrapper


@owner_required
def owner(request):
    """Beauty-Grid owner panel: every company, its numbers, switch it on or off."""
    if request.method == "POST":
        company = get_object_or_404(Company, pk=request.POST.get("company"))
        company.active = not company.active
        company.save(update_fields=["active"])
        return redirect("owner")
    q = request.GET.get("q", "").strip()
    companies = Company.objects.order_by("name")
    if q:
        companies = companies.filter(name__icontains=q)
    rows = [(c, company_stats(c), Partner.objects.filter(company=c, is_customer=True).count(),
             Profile.objects.filter(company=c).count()) for c in companies]
    return render(request, "erp/owner.html", {"rows": rows, "q": q})
