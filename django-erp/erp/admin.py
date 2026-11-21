from django.contrib import admin, messages
from django.urls import reverse
from django.utils.html import format_html
from django.utils.translation import gettext, gettext_lazy as _

from . import services
from .models import (BoM, BoMLine, Category, Company, Currency, InventoryCount, ReorderRule, InventoryCountLine, Location,
                     ManufacturingOrder, Partner, PosLine, PosOrder, PosSession, PosShop,
                     Product, Profile, PurchaseLine, PurchaseOrder, SaleLine, SaleOrder,
                     StockMove, StockQuant, Warehouse)

admin.site.site_header = "Beauty-Grid ERP"
admin.site.site_title = "Beauty-Grid ERP"
admin.site.index_title = _("The tech behind your beauty")


def store_url(company):
    from django.conf import settings
    if settings.STORE_DOMAIN:
        return f"https://{company.slug}.{settings.STORE_DOMAIN}/"
    return reverse("shop", args=[company.slug])


def company_of(request):
    profile = getattr(request.user, "profile", None)
    return profile.company if profile else None


class CompanyScoped:
    """Each login sees and creates only its own company's records."""

    def _scope(self, request, qs, field="company"):
        if request.user.is_superuser and not company_of(request):
            return qs
        return qs.filter(**{field: company_of(request)})

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        model = db_field.related_model
        if any(f.name == "company" for f in model._meta.fields):
            kwargs["queryset"] = self._scope(request, model._default_manager.all())
        return super().formfield_for_foreignkey(db_field, request, **kwargs)


class ScopedAdmin(CompanyScoped, admin.ModelAdmin):
    def get_queryset(self, request):
        return self._scope(request, super().get_queryset(request))

    def save_model(self, request, obj, form, change):
        if not change:
            obj.company = company_of(request)
        super().save_model(request, obj, form, change)


class ScopedInline(CompanyScoped, admin.TabularInline):
    extra = 1


def run(service, label):
    """Admin action that calls a service on each selected record."""
    def action(modeladmin, request, queryset):
        for obj in queryset:
            try:
                service(obj)
                modeladmin.message_user(request, gettext("%(obj)s: %(action)s done.") % {"obj": obj, "action": label})
            except services.StockError as e:
                modeladmin.message_user(request, f"{obj}: {e}", messages.ERROR)
    action.short_description = label
    action.__name__ = service.__name__
    return action


def stage_action(stage):
    def to_stage(order):
        services.set_stage(order, stage)
    to_stage.__name__ = f"stage_{stage}"
    return to_stage


@admin.register(Category)
class CategoryAdmin(ScopedAdmin):
    search_fields = ("name",)


class StockLevel(admin.SimpleListFilter):
    title = _("stock level")
    parameter_name = "level"

    def lookups(self, request, model_admin):
        return [("low", _("Low (1–5)")), ("out", _("Out (0)"))]

    def queryset(self, request, queryset):
        if self.value() == "low":
            return queryset.filter(quantity__gt=0, quantity__lte=5)
        if self.value() == "out":
            return queryset.filter(quantity__lte=0)
        return queryset


@admin.register(Partner)
class PartnerAdmin(ScopedAdmin):
    list_display = ("name", "phone", "email", "is_customer", "is_vendor", "orders", "spent")

    @admin.display(description=_("orders"))
    def orders(self, obj):
        return obj.saleorder_set.exclude(stage="cancelled").count() + obj.posorder_set.count()

    @admin.display(description=_("spent"))
    def spent(self, obj):
        total = sum(o.total_base for o in obj.saleorder_set.filter(state="done"))
        return f"{total + sum(o.total for o in obj.posorder_set.all()):,.2f}"
    list_filter = ("is_customer", "is_vendor")
    search_fields = ("name", "phone")


@admin.register(Product)
class ProductAdmin(ScopedAdmin):
    list_display = ("__str__", "sku", "category", "type", "uom", "sale_price", "cost",
                    "total_on_hand", "published")
    list_filter = ("category", "type", "can_be_sold", "can_be_purchased", "published")
    search_fields = ("name", "sku", "variant_name")
    fieldsets = [
        (None, {"fields": ["name", "sku", "category", "type", "uom", "sale_price", "cost"]}),
        (_("Variant"), {"fields": ["variant_of", "variant_name"], "classes": ["collapse"]}),
        (_("Online store"), {"fields": ["published", "description", "image"]}),
        (_("Options"), {"fields": ["can_be_sold", "can_be_purchased"]}),
    ]

    @admin.display(description=_("on hand"))
    def total_on_hand(self, obj):
        return sum(q.quantity for q in obj.stockquant_set.all()) if obj.tracks_stock else "-"


@admin.register(Currency)
class CurrencyAdmin(ScopedAdmin):
    list_display = ("code", "rate")


@admin.register(ReorderRule)
class ReorderRuleAdmin(ScopedAdmin):
    list_display = ("product", "warehouse", "min_qty", "max_qty", "vendor", "on_hand_now")
    actions = ["check_now"]

    @admin.display(description=_("on hand"))
    def on_hand_now(self, obj):
        return services.on_hand(obj.product, services.stock_location(obj.warehouse)).normalize()

    @admin.action(description=_("Check now and create orders"))
    def check_now(self, request, queryset):
        created = [o for p in {r.product for r in queryset} for o in services.check_reorder(p)]
        self.message_user(request, gettext("Orders created: %s") % (
            ", ".join(map(str, created)) or "0"))


@admin.register(Warehouse)
class WarehouseAdmin(ScopedAdmin):
    pass


@admin.register(Location)
class LocationAdmin(ScopedAdmin):
    list_display = ("__str__", "kind")
    list_filter = ("kind",)


@admin.register(StockQuant)
class StockQuantAdmin(ScopedAdmin):
    list_display = ("product", "sku", "location", "quantity")
    list_filter = (StockLevel, "location")

    @admin.display(description=_("SKU"))
    def sku(self, obj):
        return obj.product.sku
    search_fields = ("product__name",)

    def has_add_permission(self, request):
        return False  # stock changes only through receipts, MOs, counts...

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(StockMove)
class StockMoveAdmin(StockQuantAdmin):
    list_display = ("date", "reference", "product", "quantity", "source", "destination")
    list_filter = ()
    search_fields = ("reference", "product__name")


class CountLineInline(ScopedInline):
    model = InventoryCountLine


@admin.register(InventoryCount)
class InventoryCountAdmin(ScopedAdmin):
    list_display = ("__str__", "location", "date", "state")
    inlines = [CountLineInline]
    actions = [run(services.apply_count, _("Apply count"))]


class BoMLineInline(ScopedInline):
    model = BoMLine


@admin.register(BoM)
class BoMAdmin(ScopedAdmin):
    list_display = ("product", "quantity")
    inlines = [BoMLineInline]


@admin.register(ManufacturingOrder)
class ManufacturingOrderAdmin(ScopedAdmin):
    list_display = ("__str__", "bom", "quantity", "state", "components")
    list_filter = ("state",)
    actions = [run(services.produce, _("Produce"))]

    @admin.display(description=_("components"))
    def components(self, obj):
        if obj.state == "done":
            return gettext("consumed")
        missing = services.missing_components(obj)
        return gettext("ready") if not missing else gettext("missing: %s") % ", ".join(
            str(c) for c, _n, _h in missing)


class PurchaseLineInline(ScopedInline):
    model = PurchaseLine


@admin.register(PurchaseOrder)
class PurchaseOrderAdmin(ScopedAdmin):
    list_display = ("__str__", "partner", "date", "total", "currency_code", "state")
    list_filter = ("state",)
    inlines = [PurchaseLineInline]
    actions = [run(services.receive_purchase, _("Receive goods"))]


class SaleLineInline(ScopedInline):
    model = SaleLine


@admin.register(SaleOrder)
class SaleOrderAdmin(ScopedAdmin):
    list_display = ("__str__", "partner", "source", "date", "total", "currency_code", "stage",
                    "invoice_number", "print_link")
    list_filter = ("stage", "source")
    readonly_fields = ("source", "stage", "invoice_number")
    search_fields = ("partner__name", "partner__phone", "invoice_number")
    date_hierarchy = "date"
    inlines = [SaleLineInline]

    @admin.display(description=_("invoice"))
    def print_link(self, obj):
        return format_html('<a href="{}" target="_blank">{}</a>', reverse("invoice", args=[obj.pk]),
                           gettext("Print"))
    actions = [run(stage_action(SaleOrder.PROCESSING), _("Mark as processing")),
               run(stage_action(SaleOrder.CONFIRMED_S), _("Confirm")),
               run(services.deliver_sale, _("Ship (stock leaves)")),
               run(stage_action(SaleOrder.DELIVERED), _("Mark as delivered")),
               run(stage_action(SaleOrder.CANCELLED_S), _("Cancel"))]


@admin.register(PosShop)
class PosShopAdmin(ScopedAdmin):
    pass


class PosLineInline(ScopedInline):
    model = PosLine
    extra = 0


@admin.register(PosOrder)
class PosOrderAdmin(ScopedAdmin):
    list_display = ("__str__", "session", "customer", "payment_method", "total", "date",
                    "print_link")
    inlines = [PosLineInline]

    @admin.display(description=_("receipt"))
    def print_link(self, obj):
        return format_html('<a href="{}" target="_blank">{}</a>', reverse("receipt", args=[obj.pk]),
                           gettext("Print"))

    def has_add_permission(self, request):
        return False  # sold from the cashier screen


@admin.register(PosSession)
class PosSessionAdmin(ScopedAdmin):
    list_display = ("__str__", "user", "state", "opening_cash", "expected_cash",
                    "closing_cash", "opened_at", "closed_at")

    def has_add_permission(self, request):
        return False  # opened from the cashier screen


@admin.register(Company)
class CompanyAdmin(admin.ModelAdmin):
    """Managers edit their own company and online store settings."""
    list_display = ("name", "currency", "store_link")

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        return qs if request.user.is_superuser else qs.filter(pk=getattr(company_of(request), "pk", 0))

    def has_add_permission(self, request):
        return request.user.is_superuser

    def has_delete_permission(self, request, obj=None):
        return request.user.is_superuser

    @admin.display(description=_("online store"))
    def store_link(self, obj):
        if not obj.slug:
            return "-"
        url = store_url(obj)
        return format_html('<a href="{}" target="_blank">{}</a>', url, url)


@admin.register(Profile)
class ProfileAdmin(admin.ModelAdmin):
    def has_module_permission(self, request):
        return request.user.is_superuser

    list_display = ("user", "company", "role")
