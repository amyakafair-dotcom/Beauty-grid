"""Stock rules. Every stock change goes through move()."""
from decimal import Decimal

from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext as _

from django.utils.text import slugify

from .models import (BoM, Company, InventoryCount, Location, ManufacturingOrder, Order,
                     PosOrder, PosSession, Product, PurchaseLine, PurchaseOrder, ReorderRule,
                     SaleOrder, StockMove, StockQuant, Warehouse)


class StockError(Exception):
    pass


def setup_company(name):
    """New company with one warehouse and the virtual locations the rules need."""
    base = slugify(name, allow_unicode=True)[:50] or "store"
    slug, n = base, 1
    while Company.objects.filter(slug=slug).exists():
        n += 1
        slug = f"{base}-{n}"
    company = Company.objects.create(name=name, slug=slug)
    wh = Warehouse.objects.create(company=company, name="Main")
    Location.objects.create(company=company, name="Stock", warehouse=wh)
    for kind in (Location.VENDOR, Location.CUSTOMER, Location.PRODUCTION, Location.ADJUSTMENT):
        Location.objects.create(company=company, name=kind.title(), kind=kind)
    return company


def virtual(company, kind):
    return Location.objects.get(company=company, kind=kind)


def stock_location(warehouse):
    return warehouse.location_set.filter(kind=Location.INTERNAL).order_by("pk").first()


def on_hand(product, location):
    q = StockQuant.objects.filter(product=product, location=location).first()
    return q.quantity if q else Decimal(0)


def _locked_quant(product, location):
    """The quant row, locked until the transaction ends, so two sales can't both take the last unit."""
    StockQuant.objects.get_or_create(company=product.company, product=product, location=location)
    return StockQuant.objects.select_for_update().get(product=product, location=location)


@transaction.atomic
def move(product, qty, source, destination, reference):
    if not product.tracks_stock or qty == 0:
        return
    if qty < 0:
        raise StockError(_("Quantity cannot be negative."))
    if source.kind == Location.INTERNAL:
        q = _locked_quant(product, source)
        if q.quantity < qty:
            raise StockError(_("Not enough %(product)s in %(location)s: %(have)s available, "
                               "%(need)s needed.") % {"product": product, "location": source,
                                                      "have": f"{q.quantity.normalize():f}",
                                                      "need": f"{Decimal(qty).normalize():f}"})
        q.quantity -= qty
        q.save()
    if destination.kind == Location.INTERNAL:
        q = _locked_quant(product, destination)
        q.quantity += qty
        q.save()
    StockMove.objects.create(company=product.company, product=product, quantity=qty,
                             source=source, destination=destination, reference=reference)
    if source.kind == Location.INTERNAL:
        check_reorder(product)


def incoming(product, warehouse):
    """Quantity already on the way: draft purchases and draft manufacturing orders."""
    bought = sum((l.quantity for l in PurchaseLine.objects.filter(
        product=product, order__warehouse=warehouse, order__state=Order.DRAFT)), Decimal(0))
    made = sum((mo.quantity for mo in ManufacturingOrder.objects.filter(
        bom__product=product, warehouse=warehouse, state=ManufacturingOrder.DRAFT)), Decimal(0))
    return bought + made


def check_reorder(product):
    """Below the minimum (counting what's on the way)? Create a draft order up to the maximum."""
    created = []
    for rule in ReorderRule.objects.filter(product=product).select_related("warehouse", "vendor"):
        have = on_hand(product, stock_location(rule.warehouse)) + incoming(product, rule.warehouse)
        if have >= rule.min_qty:
            continue
        need = rule.max_qty - have
        if rule.vendor:
            po = PurchaseOrder.objects.create(company=product.company, partner=rule.vendor,
                                              warehouse=rule.warehouse)
            po.lines.create(product=product, quantity=need, price=product.cost)
            created.append(po)
        else:
            bom = BoM.objects.filter(product=product).first()
            if bom:
                created.append(ManufacturingOrder.objects.create(
                    company=product.company, bom=bom, quantity=need, warehouse=rule.warehouse))
    return created


def _check_draft(obj):
    if obj.state != "draft":
        raise StockError(_("%(obj)s is already %(state)s.") % {"obj": obj, "state": _(obj.state)})


@transaction.atomic
def receive_purchase(po: PurchaseOrder):
    _check_draft(po)
    src, dst = virtual(po.company, Location.VENDOR), stock_location(po.warehouse)
    for line in po.lines.all():
        move(line.product, line.quantity, src, dst, str(po))
    po.state = Order.DONE
    po.save()


@transaction.atomic
def deliver_sale(so: SaleOrder):
    """Ship: stock leaves, the invoice gets its number."""
    _check_draft(so)
    if so.stage == SaleOrder.CANCELLED_S:
        raise StockError(_("%s is cancelled.") % so)
    src, dst = stock_location(so.warehouse), virtual(so.company, Location.CUSTOMER)
    for line in so.lines.all():
        move(line.product, line.quantity, src, dst, str(so))
    Company.objects.select_for_update().get(pk=so.company_id)  # one invoice number at a time
    n = SaleOrder.objects.filter(company=so.company).exclude(invoice_number="").count() + 1
    so.invoice_number = f"INV-{n:05d}"
    so.state, so.stage = Order.DONE, SaleOrder.SHIPPED
    so.save()


@transaction.atomic
def set_stage(so: SaleOrder, stage):
    if stage not in SaleOrder.NEXT[so.stage]:
        raise StockError(_("%(order)s can't go from %(a)s to %(b)s.") % {
            "order": so, "a": so.get_stage_display(), "b": dict(SaleOrder.STAGES)[stage]})
    if stage == SaleOrder.SHIPPED:
        return deliver_sale(so)
    so.stage = stage
    if stage == SaleOrder.CANCELLED_S:
        so.state = Order.CANCELLED
    so.save()


def component_needs(bom: BoM, qty):
    """[(component, needed quantity)] to make qty of the BoM product."""
    factor = Decimal(qty) / bom.quantity
    return [(l.component, l.quantity * factor) for l in bom.lines.all()]


def missing_components(mo: ManufacturingOrder):
    loc = stock_location(mo.warehouse)
    return [(c, need, on_hand(c, loc)) for c, need in component_needs(mo.bom, mo.quantity)
            if c.tracks_stock and on_hand(c, loc) < need]


@transaction.atomic
def produce(mo: ManufacturingOrder):
    _check_draft(mo)
    missing = missing_components(mo)
    if missing:
        raise StockError(_("Missing components: %s") % ", ".join(
            f"{c} ({have} / {need})" for c, need, have in missing))
    stock, prod = stock_location(mo.warehouse), virtual(mo.company, Location.PRODUCTION)
    for component, need in component_needs(mo.bom, mo.quantity):
        move(component, need, stock, prod, str(mo))
    move(mo.bom.product, mo.quantity, prod, stock, str(mo))
    mo.state = ManufacturingOrder.DONE
    mo.save()


@transaction.atomic
def apply_count(count: InventoryCount):
    _check_draft(count)
    adj = virtual(count.company, Location.ADJUSTMENT)
    for line in count.lines.all():
        diff = line.counted - on_hand(line.product, count.location)
        if diff > 0:
            move(line.product, diff, adj, count.location, str(count))
        elif diff < 0:
            move(line.product, -diff, count.location, adj, str(count))
    count.state = InventoryCount.APPLIED
    count.save()


@transaction.atomic
def pos_sell(session: PosSession, items, payment_method=PosOrder.CASH, customer=None,
             client_ref=""):
    """items: [(product, qty)]. Price comes from the product.
    client_ref makes a retried offline order count once."""
    if client_ref:
        done = PosOrder.objects.filter(company=session.company, client_ref=client_ref).first()
        if done:
            return done
    if session.state != PosSession.OPEN:
        raise StockError(_("Session is closed."))
    if payment_method not in dict(PosOrder.METHODS):
        raise StockError(_("Unknown payment method."))
    order = PosOrder.objects.create(company=session.company, session=session,
                                    payment_method=payment_method, customer=customer,
                                    client_ref=client_ref)
    src = stock_location(session.shop.warehouse)
    dst = virtual(session.company, Location.CUSTOMER)
    for product, qty in items:
        order.lines.create(product=product, quantity=qty, price=product.sale_price)
        move(product, Decimal(qty), src, dst, str(order))
    return order


def close_session(session: PosSession, counted_cash):
    session.closing_cash = counted_cash
    session.state = PosSession.CLOSED
    session.closed_at = timezone.now()
    session.save()
    return session.closing_cash - session.expected_cash  # cash difference
