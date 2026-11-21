import json
from decimal import Decimal as D

from django.contrib.auth.models import User
from django.test import TestCase

from . import services
from .models import (BoM, InventoryCount, ManufacturingOrder, Partner, PosSession, Product,
                     PurchaseOrder, SaleOrder)


class Flows(TestCase):
    def setUp(self):
        self.client.post("/signup/", {"company": "Indigo", "username": "owner",
                                      "password": "secret-pass-1"})
        self.user = User.objects.get(username="owner")
        self.co = self.user.profile.company
        self.wh = self.co.warehouse_set.first()
        self.stock = services.stock_location(self.wh)
        mk = lambda name, **kw: Product.objects.create(company=self.co, name=name, **kw)
        self.oil = mk("Shea butter", uom="g")
        self.jar = mk("Jar 50ml")
        self.cream = mk("Aura cream", sale_price=D("5000"))
        self.vendor = Partner.objects.create(company=self.co, name="Supplier", is_vendor=True)
        self.customer = Partner.objects.create(company=self.co, name="Shop A")

    def buy(self, product, qty):
        po = PurchaseOrder.objects.create(company=self.co, partner=self.vendor, warehouse=self.wh)
        po.lines.create(product=product, quantity=qty, price=1)
        services.receive_purchase(po)

    def qty(self, product):
        return services.on_hand(product, self.stock)

    def test_signup_creates_company_and_admin_access(self):
        self.assertEqual(self.co.name, "Indigo")
        self.assertTrue(self.user.has_perm("erp.add_product"))
        self.assertEqual(self.client.get("/admin/erp/product/").status_code, 200)

    def test_purchase_receipt_adds_stock_once(self):
        self.buy(self.oil, 1000)
        self.assertEqual(self.qty(self.oil), 1000)
        po = PurchaseOrder.objects.get()
        with self.assertRaises(services.StockError):
            services.receive_purchase(po)  # no double receipt

    def test_manufacture_consumes_recipe(self):
        bom = BoM.objects.create(company=self.co, product=self.cream, quantity=10)
        bom.lines.create(component=self.oil, quantity=500)  # 50 g per jar
        bom.lines.create(component=self.jar, quantity=10)
        mo = ManufacturingOrder.objects.create(company=self.co, bom=bom, quantity=20,
                                               warehouse=self.wh)
        self.assertEqual(len(services.missing_components(mo)), 2)
        with self.assertRaises(services.StockError):
            services.produce(mo)
        self.buy(self.oil, 1500)
        self.buy(self.jar, 30)
        services.produce(mo)
        self.assertEqual(self.qty(self.oil), 500)
        self.assertEqual(self.qty(self.jar), 10)
        self.assertEqual(self.qty(self.cream), 20)

    def test_sale_cannot_oversell(self):
        self.buy(self.cream, 5)
        so = SaleOrder.objects.create(company=self.co, partner=self.customer, warehouse=self.wh)
        so.lines.create(product=self.cream, quantity=6, price=5000)
        with self.assertRaises(services.StockError):
            services.deliver_sale(so)
        self.assertEqual(self.qty(self.cream), 5)  # rolled back
        so.lines.update(quantity=5)
        services.deliver_sale(so)
        self.assertEqual(self.qty(self.cream), 0)

    def test_inventory_count_sets_quantity(self):
        self.buy(self.jar, 10)
        count = InventoryCount.objects.create(company=self.co, location=self.stock)
        count.lines.create(product=self.jar, counted=7)
        services.apply_count(count)
        self.assertEqual(self.qty(self.jar), 7)

    def test_pos_session_flow(self):
        self.buy(self.cream, 3)
        shop = self.co.posshop_set.first()
        self.client.post("/pos/", {"shop": shop.pk, "cash": "1000"})
        r = self.client.post("/pos/order/", json.dumps({"items": [{"id": self.cream.pk, "qty": 2}]}),
                             content_type="application/json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(self.qty(self.cream), 1)
        r = self.client.post("/pos/order/", json.dumps({"items": [{"id": self.cream.pk, "qty": 2}]}),
                             content_type="application/json")
        self.assertEqual(r.status_code, 400)  # out of stock
        self.client.post("/pos/", {"cash": "11000"})
        s = PosSession.objects.get()
        self.assertEqual(s.state, "closed")
        self.assertEqual(s.expected_cash, D("11000"))

    def test_companies_are_isolated(self):
        self.client.logout()
        self.client.post("/signup/", {"company": "Other", "username": "other",
                                      "password": "secret-pass-2"})
        r = self.client.get("/admin/erp/product/")
        self.assertNotContains(r, "Aura cream")
        r = self.client.get(f"/admin/erp/product/{self.cream.pk}/change/")
        self.assertNotEqual(r.status_code, 200)


import threading
from unittest import skipUnless

from django.db import connection
from django.test import TransactionTestCase


@skipUnless(connection.vendor == "postgresql", "row locking needs PostgreSQL")
class Concurrency(TransactionTestCase):
    def test_two_sales_cannot_take_the_last_unit(self):
        co = services.setup_company("Race")
        wh = co.warehouse_set.first()
        p = Product.objects.create(company=co, name="Last jar")
        services.move(p, D(1), services.virtual(co, "vendor"), services.stock_location(wh), "init")
        results = []

        def sell():
            try:
                services.move(p, D(1), services.stock_location(wh),
                              services.virtual(co, "customer"), "race")
                results.append("ok")
            except services.StockError:
                results.append("refused")
            finally:
                connection.close()

        threads = [threading.Thread(target=sell) for _ in range(5)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(sorted(results), ["ok"] + ["refused"] * 4)
        self.assertEqual(services.on_hand(p, services.stock_location(wh)), 0)


import gzip
import tempfile
from pathlib import Path

from django.core.management import call_command


class Fixes(Flows):
    def login_as(self, role):
        from .views import add_user
        add_user(self.co, role, "another-pass-99", role)
        self.client.logout()
        self.client.login(username=role, password="another-pass-99")

    def test_cashier_only_gets_pos(self):
        self.login_as("cashier")
        self.assertEqual(self.client.get("/pos/").status_code, 200)
        self.assertNotEqual(self.client.get("/admin/erp/product/").status_code, 200)
        self.assertEqual(self.client.get("/reports/").status_code, 403)
        self.assertEqual(self.client.get("/team/").status_code, 403)
        self.assertRedirects(self.client.get("/"), "/pos/")

    def test_manager_adds_team_member(self):
        r = self.client.post("/team/", {"username": "amna", "password": "kasala-2026!",
                                        "role": "cashier"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(User.objects.get(username="amna").profile.role, "cashier")
        self.assertFalse(User.objects.get(username="amna").is_staff)

    def test_weak_password_refused(self):
        r = self.client.post("/team/", {"username": "x", "password": "123", "role": "cashier"})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(User.objects.filter(username="x").exists())

    def test_offline_retry_counts_once(self):
        self.buy(self.cream, 5)
        self.client.post("/pos/", {"shop": self.co.posshop_set.first().pk, "cash": "0"})
        body = json.dumps({"items": [{"id": self.cream.pk, "qty": 1}], "ref": "abc-1"})
        for _ in range(3):
            r = self.client.post("/pos/order/", body, content_type="application/json")
            self.assertEqual(r.status_code, 200)
        self.assertEqual(self.qty(self.cream), 4)
        r = self.client.get(f"/receipt/{r.json()['id']}/")
        self.assertContains(r, "Aura cream")

    def test_bad_pos_input_rejected(self):
        self.buy(self.cream, 5)
        self.client.post("/pos/", {"shop": self.co.posshop_set.first().pk, "cash": "0"})
        for body in ('{"items": [{"id": %d, "qty": -2}]}' % self.cream.pk, "not json",
                     '{"items": [{"id": %d, "qty": 1}], "method": "gold"}' % self.cream.pk):
            r = self.client.post("/pos/order/", body, content_type="application/json")
            self.assertEqual(r.status_code, 400, body)
        self.assertEqual(self.qty(self.cream), 5)

    def test_invoice_and_reports(self):
        self.buy(self.cream, 5)
        so = SaleOrder.objects.create(company=self.co, partner=self.customer, warehouse=self.wh)
        so.lines.create(product=self.cream, quantity=2, price=5000)
        services.deliver_sale(so)
        self.assertContains(self.client.get(f"/invoice/{so.pk}/"), "10000.00")
        r = self.client.get("/reports/")
        self.assertContains(r, "Aura cream")
        self.assertContains(r, "10000.00")

    def test_arabic_by_default_and_english_switch(self):
        self.assertContains(self.client.get("/reports/"), 'dir="rtl"')
        self.assertContains(self.client.get("/admin/erp/product/"), "المنتجات")
        self.client.post("/i18n/setlang/", {"language": "en", "next": "/reports/"})
        r = self.client.get("/admin/erp/product/")
        self.assertContains(r, "products")

    def test_backup_file(self):
        with tempfile.TemporaryDirectory() as d:
            call_command("backup", dir=d, stdout=open("/dev/null", "w"))
            files = list(Path(d).glob("*.json.gz"))
            self.assertEqual(len(files), 1)
            self.assertIn("Aura cream", gzip.open(files[0], "rt").read())


from .models import Currency, ReorderRule


class NewFeatures(Flows):
    def test_min_stock_creates_purchase_when_low(self):
        ReorderRule.objects.create(company=self.co, product=self.jar, warehouse=self.wh,
                                   min_qty=10, max_qty=50, vendor=self.vendor)
        self.buy(self.jar, 12)
        count = InventoryCount.objects.create(company=self.co, location=self.stock)
        count.lines.create(product=self.jar, counted=8)  # drops below 10
        services.apply_count(count)
        po = PurchaseOrder.objects.filter(state="draft").get()
        self.assertEqual(po.lines.get().quantity, 42)  # back up to 50
        services.check_reorder(self.jar)  # already on the way: no duplicate
        self.assertEqual(PurchaseOrder.objects.filter(state="draft").count(), 1)

    def test_min_stock_creates_manufacturing_without_vendor(self):
        bom = BoM.objects.create(company=self.co, product=self.cream, quantity=1)
        bom.lines.create(component=self.jar, quantity=1)
        ReorderRule.objects.create(company=self.co, product=self.cream, warehouse=self.wh,
                                   min_qty=5, max_qty=20)
        self.buy(self.cream, 6)
        so = SaleOrder.objects.create(company=self.co, partner=self.customer, warehouse=self.wh)
        so.lines.create(product=self.cream, quantity=3, price=5000)
        services.deliver_sale(so)
        self.assertEqual(ManufacturingOrder.objects.get().quantity, 17)

    def test_order_in_foreign_currency(self):
        usd = Currency.objects.create(company=self.co, code="USD", rate=D("600"))
        po = PurchaseOrder.objects.create(company=self.co, partner=self.vendor, warehouse=self.wh,
                                          currency=usd)
        po.lines.create(product=self.oil, quantity=10, price=D("2.5"))
        po.refresh_from_db()
        self.assertEqual(po.rate, D("600"))
        self.assertEqual(po.total, D("25.00"))
        self.assertEqual(po.total_base, D("15000.00"))
        usd.rate = D("700"); usd.save()
        po.refresh_from_db()
        self.assertEqual(po.total_base, D("15000.00"))  # rate fixed when the order was made
        services.receive_purchase(po)
        self.assertContains(self.client.get("/reports/"), "15000.00")

    def test_online_store_order(self):
        self.cream.published = True; self.cream.save()
        self.buy(self.cream, 3)
        shop = f"/shop/{self.co.slug}/"
        self.client.logout()
        self.assertContains(self.client.get(shop), "Aura cream")
        self.assertNotContains(self.client.get(shop), "Shea butter")  # not published
        self.client.post(shop + "cart/update/", {"product": self.cream.pk, "qty": 5, "add": 1})
        r = self.client.get(shop + "cart/")
        self.assertContains(r, 'value="3"')  # capped at stock
        token = r.context["token"]
        data = {"name": "Huda", "phone": "0911222333", "address": "Khartoum 2",
                "payment": "cod", "token": token}
        r = self.client.post(shop + "cart/", data)
        self.client.post(shop + "cart/", data)  # double click: no second order
        self.assertEqual(SaleOrder.objects.count(), 1)
        so = SaleOrder.objects.get(source="online")
        self.assertRedirects(r, f"{shop}order/{so.pk}/")
        self.assertEqual(so.partner.name, "Huda")
        self.assertEqual(so.total, D("15000.00"))
        self.assertContains(self.client.get(f"{shop}order/{so.pk}/"), str(so))
        self.client.logout()  # a stranger can't read someone's confirmation
        self.assertEqual(self.client.get(f"{shop}order/{so.pk}/").status_code, 404)

    def test_store_can_be_closed_and_rejects_bad_redirect(self):
        self.cream.published = True; self.cream.save()
        r = self.client.post(f"/shop/{self.co.slug}/cart/update/",
                             {"product": self.cream.pk, "next": "https://evil.example"})
        self.assertFalse(r["Location"].startswith("https://evil"))
        self.co.store_enabled = False; self.co.save()
        self.assertEqual(self.client.get(f"/shop/{self.co.slug}/").status_code, 404)

    def test_arabic_company_name_gets_store_address(self):
        co = services.setup_company("إنديجو")
        self.assertEqual(co.slug, "إنديجو")
        self.assertEqual(self.client.get(f"/shop/{co.slug}/").status_code, 200)


from django.test import override_settings

from .models import Category


class RevanParity(Flows):
    def sale(self, qty=1):
        so = SaleOrder.objects.create(company=self.co, partner=self.customer, warehouse=self.wh)
        so.lines.create(product=self.cream, quantity=qty, price=5000)
        return so

    def test_order_stages_and_invoice_numbers(self):
        self.buy(self.cream, 5)
        so = self.sale()
        services.set_stage(so, "processing")
        services.set_stage(so, "confirmed")
        with self.assertRaises(services.StockError):
            services.set_stage(so, "delivered")  # must ship first
        services.set_stage(so, "shipped")
        self.assertEqual((so.stage, so.invoice_number, self.qty(self.cream)), ("shipped", "INV-00001", 4))
        services.set_stage(so, "delivered")
        with self.assertRaises(services.StockError):
            services.set_stage(so, "cancelled")
        other = self.sale()
        services.set_stage(other, "cancelled")
        with self.assertRaises(services.StockError):
            services.deliver_sale(other)
        third = self.sale()
        services.deliver_sale(third)
        self.assertEqual(third.invoice_number, "INV-00002")
        self.assertContains(self.client.get(f"/invoice/{third.pk}/"), "INV-00002")

    def test_sku_category_and_variants_in_store(self):
        self.assertEqual(self.cream.sku, f"BG-{self.cream.pk:05d}")
        cat = Category.objects.create(company=self.co, name="Creams")
        self.cream.published, self.cream.category = True, cat
        self.cream.save()
        big = Product.objects.create(company=self.co, name="Aura cream", variant_of=self.cream,
                                     variant_name="100 ml", sale_price=D("9000"))
        self.buy(big, 2)
        self.client.logout()
        r = self.client.get(f"/shop/{self.co.slug}/?category={cat.pk}")
        self.assertContains(r, "100 ml")
        self.assertContains(r, "Creams")
        self.client.post(f"/shop/{self.co.slug}/cart/update/", {"product": big.pk, "add": 1, "qty": 1})
        self.assertContains(self.client.get(f"/shop/{self.co.slug}/cart/"), "Aura cream – 100 ml")

    def test_stock_level_filter_and_customer_stats(self):
        self.buy(self.jar, 3)
        self.buy(self.oil, 100)
        r = self.client.get("/admin/erp/stockquant/?level=low")
        self.assertContains(r, "Jar 50ml")
        self.assertNotContains(r, "Shea butter")
        self.buy(self.cream, 2)
        services.deliver_sale(self.sale(2))
        self.assertContains(self.client.get("/admin/erp/partner/"), "10,000.00")

    def test_dashboard_numbers(self):
        self.buy(self.cream, 5)
        services.deliver_sale(self.sale(2))
        r = self.client.get("/dashboard/")
        self.assertEqual(r.context["all"], {"orders": 1, "revenue": D("10000.00")})
        self.assertEqual(self.client.get("/").url, "/dashboard/")

    def test_owner_panel_switches_company_off(self):
        boss = User.objects.create_superuser("boss", password="owner-pass-1")
        self.client.logout()
        self.client.login(username="boss", password="owner-pass-1")
        self.assertContains(self.client.get("/owner/"), "Indigo")
        self.assertEqual(self.client.get(f"/dashboard/?company={self.co.pk}").status_code, 200)
        self.client.post("/owner/", {"company": self.co.pk})
        self.co.refresh_from_db()
        self.assertFalse(self.co.active)
        self.assertEqual(self.client.get(f"/shop/{self.co.slug}/").status_code, 404)
        self.client.logout()
        self.client.login(username="owner", password="secret-pass-1")
        self.assertRedirects(self.client.get("/pos/"), "/login/?inactive=1", fetch_redirect_response=False)
        self.client.login(username="boss", password="owner-pass-1")  # managers can't see /owner/
        self.client.logout(); self.client.login(username="owner", password="secret-pass-1")
        self.co.active = True; self.co.save()
        self.assertEqual(self.client.get("/owner/").status_code, 403)

    @override_settings(STORE_DOMAIN="beauty-grid.com", ALLOWED_HOSTS=[".beauty-grid.com", "testserver"])
    def test_store_on_subdomain(self):
        self.cream.published = True; self.cream.save()
        self.client.logout()
        r = self.client.get("/", HTTP_HOST=f"{self.co.slug}.beauty-grid.com")
        self.assertContains(r, "Aura cream")
        r = self.client.get("/cart/", HTTP_HOST=f"{self.co.slug}.beauty-grid.com")
        self.assertEqual(r.status_code, 200)
