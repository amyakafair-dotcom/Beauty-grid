from decimal import Decimal

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _

Q3 = {"max_digits": 14, "decimal_places": 3}   # quantities
MONEY = {"max_digits": 12, "decimal_places": 2}
CENT = Decimal("0.01")


class Company(models.Model):
    name = models.CharField(_("name"), max_length=120)
    currency = models.CharField(_("currency"), max_length=8, default="SDG")
    slug = models.SlugField(_("store address"), max_length=60, unique=True, null=True,
                            allow_unicode=True)
    store_enabled = models.BooleanField(_("online store open"), default=True)
    store_phone = models.CharField(_("store phone"), max_length=40, blank=True)
    active = models.BooleanField(_("active"), default=True,
                                 help_text=_("Inactive companies can't log in and their store is closed."))

    class Meta:
        verbose_name = _("company")
        verbose_name_plural = _("companies")

    def __str__(self):
        return self.name


class Profile(models.Model):
    """Links a login to the company whose data it sees."""
    MANAGER, CASHIER = "manager", "cashier"
    ROLES = [(MANAGER, _("Manager")), (CASHIER, _("Cashier"))]
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                                verbose_name=_("user"))
    company = models.ForeignKey(Company, on_delete=models.CASCADE, verbose_name=_("company"))
    role = models.CharField(_("role"), max_length=10, choices=ROLES, default=MANAGER)

    class Meta:
        verbose_name = _("team member")
        verbose_name_plural = _("team members")

    def __str__(self):
        return f"{self.user} @ {self.company}"


class CompanyOwned(models.Model):
    company = models.ForeignKey(Company, on_delete=models.CASCADE, editable=False)

    class Meta:
        abstract = True


class Currency(CompanyOwned):
    """A foreign currency. rate = how much 1 unit is worth in the company currency."""
    company = models.ForeignKey(Company, on_delete=models.CASCADE, editable=False,
                                related_name="currencies")
    code = models.CharField(_("code"), max_length=8)
    rate = models.DecimalField(_("rate to company currency"), max_digits=16, decimal_places=6,
                               default=1)

    class Meta:
        verbose_name = _("currency")
        verbose_name_plural = _("currencies")

    def __str__(self):
        return self.code


class Partner(CompanyOwned):
    name = models.CharField(_("name"), max_length=120)
    phone = models.CharField(_("phone"), max_length=40, blank=True)
    email = models.EmailField(_("email"), blank=True)
    is_customer = models.BooleanField(_("customer"), default=True)
    is_vendor = models.BooleanField(_("vendor"), default=False)

    class Meta:
        verbose_name = _("customer / vendor")
        verbose_name_plural = _("customers and vendors")

    def __str__(self):
        return self.name


class Category(CompanyOwned):
    name = models.CharField(_("name"), max_length=120)

    class Meta:
        verbose_name = _("category")
        verbose_name_plural = _("categories")
        ordering = ["name"]

    def __str__(self):
        return self.name


class Product(CompanyOwned):
    STORABLE, CONSUMABLE, SERVICE = "storable", "consumable", "service"
    TYPES = [(STORABLE, _("Storable")), (CONSUMABLE, _("Consumable")), (SERVICE, _("Service"))]
    UOMS = [("unit", _("unit")), ("g", _("g")), ("kg", _("kg")), ("ml", _("ml")), ("L", _("L"))]

    name = models.CharField(_("name"), max_length=120)
    sku = models.CharField(_("SKU"), max_length=40, blank=True,
                           help_text=_("Your own short code. Left empty, one is made for you."))
    category = models.ForeignKey(Category, null=True, blank=True, on_delete=models.SET_NULL,
                                 verbose_name=_("category"))
    variant_of = models.ForeignKey("self", null=True, blank=True, on_delete=models.CASCADE,
                                   related_name="variants", verbose_name=_("variant of"),
                                   help_text=_("e.g. Aura 100 ml is a variant of Aura."))
    variant_name = models.CharField(_("variant name"), max_length=60, blank=True,
                                    help_text=_("e.g. 100 ml, Rose"))
    type = models.CharField(_("type"), max_length=12, choices=TYPES, default=STORABLE)
    uom = models.CharField(_("unit of measure"), max_length=8, choices=UOMS, default="unit")
    sale_price = models.DecimalField(_("sale price"), default=0, **MONEY)
    cost = models.DecimalField(_("cost"), default=0, **MONEY)
    can_be_sold = models.BooleanField(_("can be sold"), default=True)
    can_be_purchased = models.BooleanField(_("can be purchased"), default=True)
    published = models.BooleanField(_("show in online store"), default=False)
    description = models.TextField(_("description"), blank=True)
    image = models.ImageField(_("image"), upload_to="products/", blank=True)

    class Meta:
        verbose_name = _("product")
        verbose_name_plural = _("products")

    def __str__(self):
        return f"{self.name} – {self.variant_name}" if self.variant_name else self.name

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        if not self.sku:  # readable default code, e.g. BG-00042
            self.sku = f"BG-{self.pk:05d}"
            super().save(update_fields=["sku"])

    @property
    def tracks_stock(self):
        return self.type == self.STORABLE


class Warehouse(CompanyOwned):
    name = models.CharField(_("name"), max_length=120)

    class Meta:
        verbose_name = _("warehouse")
        verbose_name_plural = _("warehouses")

    def __str__(self):
        return self.name


class Location(CompanyOwned):
    INTERNAL, VENDOR, CUSTOMER, PRODUCTION, ADJUSTMENT = (
        "internal", "vendor", "customer", "production", "adjustment")
    KINDS = [(INTERNAL, _("Internal")), (VENDOR, _("Vendor")), (CUSTOMER, _("Customer")),
             (PRODUCTION, _("Production")), (ADJUSTMENT, _("Adjustment"))]

    name = models.CharField(_("name"), max_length=120)
    kind = models.CharField(_("kind"), max_length=12, choices=KINDS, default=INTERNAL)
    warehouse = models.ForeignKey(Warehouse, null=True, blank=True, on_delete=models.CASCADE,
                                  verbose_name=_("warehouse"))

    class Meta:
        verbose_name = _("location")
        verbose_name_plural = _("locations")

    def __str__(self):
        return f"{self.warehouse}/{self.name}" if self.warehouse else self.name


class StockQuant(CompanyOwned):
    """Quantity of a product sitting in an internal location."""
    product = models.ForeignKey(Product, on_delete=models.CASCADE, verbose_name=_("product"))
    location = models.ForeignKey(Location, on_delete=models.CASCADE, verbose_name=_("location"))
    quantity = models.DecimalField(_("quantity"), default=0, **Q3)

    class Meta:
        unique_together = ("product", "location")
        verbose_name = _("stock on hand")
        verbose_name_plural = _("stock on hand")


class StockMove(CompanyOwned):
    """Every change of stock, kept as history."""
    product = models.ForeignKey(Product, on_delete=models.PROTECT, verbose_name=_("product"))
    quantity = models.DecimalField(_("quantity"), **Q3)
    source = models.ForeignKey(Location, on_delete=models.PROTECT, related_name="+",
                               verbose_name=_("from"))
    destination = models.ForeignKey(Location, on_delete=models.PROTECT, related_name="+",
                                    verbose_name=_("to"))
    reference = models.CharField(_("reference"), max_length=60)
    date = models.DateTimeField(_("date"), auto_now_add=True)

    class Meta:
        ordering = ["-date"]
        verbose_name = _("stock move")
        verbose_name_plural = _("stock moves")


class InventoryCount(CompanyOwned):
    DRAFT, APPLIED = "draft", "applied"
    STATES = [(DRAFT, _("draft")), (APPLIED, _("applied"))]
    location = models.ForeignKey(Location, on_delete=models.PROTECT, verbose_name=_("location"),
                                 limit_choices_to={"kind": "internal"})
    state = models.CharField(_("state"), max_length=10, choices=STATES, default=DRAFT,
                             editable=False)
    date = models.DateTimeField(_("date"), auto_now_add=True)

    class Meta:
        verbose_name = _("stock count")
        verbose_name_plural = _("stock counts")

    def __str__(self):
        return f"CNT{self.pk:05d}"


class InventoryCountLine(models.Model):
    count = models.ForeignKey(InventoryCount, on_delete=models.CASCADE, related_name="lines")
    product = models.ForeignKey(Product, on_delete=models.PROTECT, verbose_name=_("product"))
    counted = models.DecimalField(_("counted"), **Q3)

    class Meta:
        verbose_name = _("line")
        verbose_name_plural = _("lines")


class BoM(CompanyOwned):
    """Recipe: how much of each component makes `quantity` of the product."""
    product = models.ForeignKey(Product, on_delete=models.CASCADE, verbose_name=_("product"))
    quantity = models.DecimalField(_("batch size"), default=1, **Q3)

    class Meta:
        verbose_name = _("recipe")
        verbose_name_plural = _("recipes")

    def __str__(self):
        return f"{self.product} × {self.quantity.normalize():f}"


class BoMLine(models.Model):
    bom = models.ForeignKey(BoM, on_delete=models.CASCADE, related_name="lines")
    component = models.ForeignKey(Product, on_delete=models.PROTECT, verbose_name=_("component"))
    quantity = models.DecimalField(_("quantity"), **Q3)

    class Meta:
        verbose_name = _("component")
        verbose_name_plural = _("components")


class Order(CompanyOwned):
    """Shared shape of purchase and sale orders."""
    DRAFT, CONFIRMED, DONE, CANCELLED = "draft", "confirmed", "done", "cancelled"
    STATES = [(DRAFT, _("draft")), (CONFIRMED, _("confirmed")), (DONE, _("done")),
              (CANCELLED, _("cancelled"))]

    partner = models.ForeignKey(Partner, on_delete=models.PROTECT, verbose_name=_("partner"))
    warehouse = models.ForeignKey(Warehouse, on_delete=models.PROTECT, verbose_name=_("warehouse"))
    state = models.CharField(_("state"), max_length=10, choices=STATES, default=DRAFT,
                             editable=False)
    currency = models.ForeignKey(Currency, null=True, blank=True, on_delete=models.PROTECT,
                                 verbose_name=_("currency"),
                                 help_text=_("Empty = company currency."))
    rate = models.DecimalField(_("exchange rate"), max_digits=16, decimal_places=6, null=True,
                               blank=True, help_text=_("Filled from the currency if left empty."))
    date = models.DateTimeField(_("date"), auto_now_add=True)

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        if self.rate is None:
            self.rate = self.currency.rate if self.currency else Decimal(1)
        super().save(*args, **kwargs)

    @property
    def currency_code(self):
        return self.currency.code if self.currency else self.company.currency

    @property
    def total(self):
        return sum((l.subtotal for l in self.lines.all()), Decimal(0)).quantize(CENT)

    @property
    def total_base(self):
        """Total in the company currency."""
        return (self.total * (self.rate or 1)).quantize(CENT)


class OrderLine(models.Model):
    product = models.ForeignKey(Product, on_delete=models.PROTECT, verbose_name=_("product"))
    quantity = models.DecimalField(_("quantity"), **Q3)
    price = models.DecimalField(_("unit price"), **MONEY)

    class Meta:
        abstract = True
        verbose_name = _("line")
        verbose_name_plural = _("lines")

    @property
    def subtotal(self):
        return (self.quantity * self.price).quantize(CENT)


class PurchaseOrder(Order):
    class Meta:
        verbose_name = _("purchase order")
        verbose_name_plural = _("purchase orders")

    def __str__(self):
        return f"PO{self.pk:05d}"


class PurchaseLine(OrderLine):
    order = models.ForeignKey(PurchaseOrder, on_delete=models.CASCADE, related_name="lines")


class SaleOrder(Order):
    NEW, PROCESSING, CONFIRMED_S, SHIPPED, DELIVERED, CANCELLED_S = (
        "new", "processing", "confirmed", "shipped", "delivered", "cancelled")
    STAGES = [(NEW, _("New")), (PROCESSING, _("Processing")), (CONFIRMED_S, _("Confirmed")),
              (SHIPPED, _("Shipped")), (DELIVERED, _("Delivered")), (CANCELLED_S, _("Cancelled"))]
    NEXT = {NEW: [PROCESSING, CONFIRMED_S, CANCELLED_S], PROCESSING: [CONFIRMED_S, CANCELLED_S],
            CONFIRMED_S: [SHIPPED, CANCELLED_S], SHIPPED: [DELIVERED], DELIVERED: [],
            CANCELLED_S: []}
    stage = models.CharField(_("stage"), max_length=12, choices=STAGES, default=NEW,
                             editable=False)
    invoice_number = models.CharField(_("invoice number"), max_length=20, blank=True,
                                      editable=False)
    BACKOFFICE, ONLINE = "backoffice", "online"
    SOURCES = [(BACKOFFICE, _("Back office")), (ONLINE, _("Online store"))]
    source = models.CharField(_("source"), max_length=12, choices=SOURCES, default=BACKOFFICE,
                              editable=False)
    delivery_address = models.TextField(_("delivery address"), blank=True)
    customer_note = models.TextField(_("customer note"), blank=True)
    payment = models.CharField(_("payment"), max_length=40, blank=True)

    class Meta:
        verbose_name = _("sale order")
        verbose_name_plural = _("sale orders")

    def __str__(self):
        return f"SO{self.pk:05d}"


class SaleLine(OrderLine):
    order = models.ForeignKey(SaleOrder, on_delete=models.CASCADE, related_name="lines")


class ReorderRule(CompanyOwned):
    """Keep a product between min and max: below min, a draft purchase (if a vendor is set)
    or manufacturing order (if a recipe exists) is created up to max."""
    product = models.ForeignKey(Product, on_delete=models.CASCADE, verbose_name=_("product"),
                                limit_choices_to={"type": "storable"})
    warehouse = models.ForeignKey(Warehouse, on_delete=models.CASCADE, verbose_name=_("warehouse"))
    min_qty = models.DecimalField(_("minimum"), **Q3)
    max_qty = models.DecimalField(_("maximum"), **Q3)
    vendor = models.ForeignKey(Partner, null=True, blank=True, on_delete=models.SET_NULL,
                               verbose_name=_("vendor"), limit_choices_to={"is_vendor": True},
                               help_text=_("Empty = manufacture with the product's recipe."))

    class Meta:
        unique_together = ("product", "warehouse")
        verbose_name = _("minimum stock rule")
        verbose_name_plural = _("minimum stock rules")

    def __str__(self):
        return f"{self.product}: {self.min_qty.normalize():f} – {self.max_qty.normalize():f}"

    def clean(self):
        from django.core.exceptions import ValidationError
        if self.min_qty is not None and self.max_qty is not None and self.max_qty < self.min_qty:
            raise ValidationError(_("Maximum must be at least the minimum."))


class ManufacturingOrder(CompanyOwned):
    DRAFT, DONE = "draft", "done"
    STATES = [(DRAFT, _("draft")), (DONE, _("done"))]
    bom = models.ForeignKey(BoM, on_delete=models.PROTECT, verbose_name=_("recipe"))
    quantity = models.DecimalField(_("quantity"), **Q3)
    warehouse = models.ForeignKey(Warehouse, on_delete=models.PROTECT, verbose_name=_("warehouse"))
    state = models.CharField(_("state"), max_length=10, choices=STATES, default=DRAFT,
                             editable=False)
    date = models.DateTimeField(_("date"), auto_now_add=True)

    class Meta:
        verbose_name = _("manufacturing order")
        verbose_name_plural = _("manufacturing orders")

    def __str__(self):
        return f"MO{self.pk:05d}"


class PosShop(CompanyOwned):
    name = models.CharField(_("name"), max_length=120)
    warehouse = models.ForeignKey(Warehouse, on_delete=models.PROTECT, verbose_name=_("warehouse"))

    class Meta:
        verbose_name = _("POS shop")
        verbose_name_plural = _("POS shops")

    def __str__(self):
        return self.name


class PosSession(CompanyOwned):
    OPEN, CLOSED = "open", "closed"
    STATES = [(OPEN, _("open")), (CLOSED, _("closed"))]
    shop = models.ForeignKey(PosShop, on_delete=models.PROTECT, verbose_name=_("shop"))
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
                             verbose_name=_("cashier"))
    state = models.CharField(_("state"), max_length=10, choices=STATES, default=OPEN)
    opening_cash = models.DecimalField(_("opening cash"), default=0, **MONEY)
    closing_cash = models.DecimalField(_("counted cash"), null=True, blank=True, **MONEY)
    opened_at = models.DateTimeField(_("opened at"), auto_now_add=True)
    closed_at = models.DateTimeField(_("closed at"), null=True, blank=True)

    class Meta:
        verbose_name = _("POS session")
        verbose_name_plural = _("POS sessions")

    def __str__(self):
        return f"#{self.pk} ({self.shop})"

    def cash_sales(self):
        return sum((o.total for o in self.orders.filter(payment_method=PosOrder.CASH)), Decimal(0))

    @property
    def expected_cash(self):
        return (self.opening_cash + self.cash_sales()).quantize(CENT)


class PosOrder(CompanyOwned):
    CASH, BANK = "cash", "bank"
    METHODS = [(CASH, _("Cash")), (BANK, _("Bank transfer"))]
    session = models.ForeignKey(PosSession, on_delete=models.PROTECT, related_name="orders",
                                verbose_name=_("session"))
    customer = models.ForeignKey(Partner, null=True, blank=True, on_delete=models.PROTECT,
                                 verbose_name=_("customer"))
    payment_method = models.CharField(_("payment method"), max_length=8, choices=METHODS,
                                      default=CASH)
    client_ref = models.CharField(max_length=40, blank=True, db_index=True, editable=False)
    date = models.DateTimeField(_("date"), auto_now_add=True)

    class Meta:
        verbose_name = _("POS order")
        verbose_name_plural = _("POS orders")

    def __str__(self):
        return f"POS{self.pk:05d}"

    @property
    def total(self):
        return sum((l.subtotal for l in self.lines.all()), Decimal(0)).quantize(CENT)


class PosLine(OrderLine):
    order = models.ForeignKey(PosOrder, on_delete=models.CASCADE, related_name="lines")


# column titles in the back office
for _prop, _label in ((Order.total, _("total")), (Order.total_base, _("total in company currency")), (Order.currency_code, _("currency")), (PosOrder.total, _("total")),
                      (PosSession.expected_cash, _("expected cash"))):
    _prop.fget.short_description = _label
