"""Public online store: one per company at /shop/<slug>/. Orders arrive as online sale orders."""
import secrets
from decimal import Decimal

from django import forms
from django.db import transaction
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.translation import gettext as _, gettext_lazy
from django.views.decorators.http import require_POST

from . import services
from django.db.models import Q

from .models import Category, Company, Partner, Product, SaleOrder


def _company(slug):
    company = get_object_or_404(Company, slug=slug)
    if not (company.store_enabled and company.active):
        raise Http404
    return company


def _products(company):
    """Everything a customer may buy: published products and the variants of published products."""
    return Product.objects.filter(
        Q(published=True, variant_of=None) | Q(variant_of__published=True),
        company=company, can_be_sold=True, sale_price__gt=0)


def _cards(company, category=None):
    """One card per published product; its options are itself and its variants (if sellable)."""
    parents = Product.objects.filter(company=company, published=True, variant_of=None)
    if category:
        parents = parents.filter(category=category)
    sellable = set(_products(company).values_list("pk", flat=True))
    cards = []
    for parent in parents.prefetch_related("variants").select_related("category"):
        options = [o for o in [parent, *parent.variants.all()] if o.pk in sellable]
        if options:
            cards.append((parent, [(o, _available(o, company)) for o in options]))
    return cards


def _available(product, company):
    if not product.tracks_stock:
        return None  # services are always available
    wh = company.warehouse_set.order_by("pk").first()
    return services.on_hand(product, services.stock_location(wh))


def _cart(request, slug):
    return request.session.setdefault("carts", {}).setdefault(slug, {})


def _cart_lines(request, company):
    cart = _cart(request, company.slug)
    products = {p.pk: p for p in _products(company).filter(pk__in=cart.keys())}
    lines = [(products[int(pk)], int(q)) for pk, q in cart.items() if int(pk) in products]
    return lines, sum((p.sale_price * q for p, q in lines), Decimal(0))


def shop(request, slug):
    company = _company(slug)
    category = Category.objects.filter(company=company, pk=request.GET.get("category") or 0).first()
    lines, total = _cart_lines(request, company)
    categories = Category.objects.filter(
        company=company, product__published=True, product__variant_of=None).distinct()
    return render(request, "store/shop.html", {
        "company": company, "cards": _cards(company, category), "categories": categories,
        "category": category, "cart_count": sum(q for _p, q in lines)})


@require_POST
def cart_update(request, slug):
    company = _company(slug)
    product = get_object_or_404(_products(company), pk=request.POST.get("product"))
    cart = _cart(request, slug)
    try:
        qty = max(0, min(int(request.POST.get("qty", 1)), 999))
    except ValueError:
        qty = 0
    if request.POST.get("add"):
        qty += int(cart.get(str(product.pk), 0))
    available = _available(product, company)
    if available is not None:
        qty = min(qty, int(available))
    if qty:
        cart[str(product.pk)] = qty
    else:
        cart.pop(str(product.pk), None)
    request.session.modified = True
    nxt = request.POST.get("next", "")
    if url_has_allowed_host_and_scheme(nxt, allowed_hosts={request.get_host()}):
        return redirect(nxt)
    return redirect("cart", slug=slug)


class CheckoutForm(forms.Form):
    name = forms.CharField(label=gettext_lazy("Your name"), max_length=120)
    phone = forms.CharField(label=gettext_lazy("Phone"), max_length=40)
    address = forms.CharField(label=gettext_lazy("Delivery address"), widget=forms.Textarea(attrs={"rows": 3}))
    payment = forms.ChoiceField(label=gettext_lazy("Payment"), choices=[
        ("cod", gettext_lazy("Cash on delivery")), ("bank", gettext_lazy("Bank transfer"))])
    note = forms.CharField(label=gettext_lazy("Note"), required=False,
                           widget=forms.Textarea(attrs={"rows": 2}))


def cart(request, slug):
    company = _company(slug)
    lines, total = _cart_lines(request, company)
    form = CheckoutForm(request.POST or None)
    if request.method == "POST":
        tokens = request.session.get("checkout_tokens", [])
        if request.POST.get("token") not in tokens:  # second click on "Place order"
            mine = request.session.get("my_orders", [])
            return redirect("order_done", slug=slug, pk=mine[-1]) if mine else redirect("cart", slug=slug)
    if request.method == "POST" and lines and form.is_valid():
        short = [p.name for p, q in lines
                 if (a := _available(p, company)) is not None and a < q]
        if short:
            form.add_error(None, _("Not enough stock for: %s") % ", ".join(short))
        else:
            order = _place_order(company, lines, form.cleaned_data)
            request.session["checkout_tokens"].remove(request.POST["token"])
            request.session["carts"][slug] = {}
            request.session.setdefault("my_orders", []).append(order.pk)
            request.session.modified = True
            return redirect("order_done", slug=slug, pk=order.pk)
    token = secrets.token_urlsafe(16)
    request.session["checkout_tokens"] = request.session.get("checkout_tokens", [])[-9:] + [token]
    return render(request, "store/cart.html", {"company": company, "lines": lines,
                                               "total": total, "form": form, "token": token})


@transaction.atomic
def _place_order(company, lines, data):
    phone = data["phone"].strip()
    partner = Partner.objects.filter(company=company, phone=phone).first() or \
        Partner.objects.create(company=company, name=data["name"], phone=phone, is_customer=True)
    order = SaleOrder.objects.create(
        company=company, partner=partner, warehouse=company.warehouse_set.order_by("pk").first(),
        source=SaleOrder.ONLINE, delivery_address=data["address"], customer_note=data["note"],
        payment=dict(CheckoutForm.base_fields["payment"].choices)[data["payment"]])
    for product, qty in lines:
        order.lines.create(product=product, quantity=qty, price=product.sale_price)
    return order


def order_done(request, slug, pk):
    company = _company(slug)
    if pk not in request.session.get("my_orders", []):
        raise Http404  # only the person who ordered sees the confirmation
    order = get_object_or_404(SaleOrder, company=company, pk=pk, source=SaleOrder.ONLINE)
    return render(request, "store/done.html", {"company": company, "order": order})
