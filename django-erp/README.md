# Beauty-Grid ERP

ERP for small cosmetics producers: products, stock, recipes and manufacturing,
purchases, wholesale sales, a point-of-sale screen, an online store, minimum stock
rules, multiple currencies, invoices and reports.
Arabic (default) and English. Each company sees only its own data.

## Run locally

```bash
pip install -r requirements.txt
python manage.py migrate
python manage.py runserver
```

Open http://127.0.0.1:8000/signup/ to create a company. The first account is
the manager.

## Who sees what

| Role | Gets |
| --- | --- |
| Beauty-Grid owner (superuser) | `/owner/`: every company with orders, revenue, customers, users; open its dashboard; switch it off (its users can't log in, its store closes) |
| Manager | dashboard (`/dashboard/`), back office (`/admin/`), reports, team, POS |
| Cashier | POS screen only |

Managers add people at `/team/`. Passwords must pass Django's strength rules.

## How it works

| Area | Where | What you do |
| --- | --- | --- |
| Customers / vendors | back office | add contacts |
| Products | back office | type, unit, price, cost. Only products with a sale price appear in the POS |
| Recipes | back office | components needed to make N units |
| Production | Manufacturing orders | shows ready / missing components; action **Produce** |
| Buying | Purchase orders | action **Receive goods** adds stock |
| Sale orders | Sale orders | stages New → Processing → Confirmed → Shipped → Delivered (or Cancelled). **Ship** removes stock, refuses to oversell and gives the invoice its number (INV-00001); **Print** opens the invoice |
| Dashboard | `/dashboard/` | total and 30-day orders and revenue, customers, products, low stock, orders by stage, latest online orders, orders waiting for review |
| Catalogue | Products, Categories | SKU (auto BG-00001 if empty), category, variants (e.g. Aura 100 ml is a variant of Aura, with its own price and stock) |
| Stock | Stock on hand | filter Low (1–5) / Out (0) |
| Customers | Customers and vendors | orders count and total spent per customer |
| Counting | Stock counts | enter counted quantities; action **Apply count** |
| Shop | `/pos/` | open with cash, sell, print receipt, close with counted cash and see the difference |
| Reports | `/reports/` | sales by product, purchases, production, stock value, for any dates (in the company currency) |
| Minimum stock | Minimum stock rules | min and max per product. When stock plus what's already ordered drops below min, a draft purchase order (if a vendor is set) or manufacturing order (if not) is created up to max. Also action **Check now** |
| Currencies | Currencies, then the order's currency field | e.g. USD with rate 600. The order keeps the rate it was made with; totals in reports are converted |
| Online store | `/shop/<store-address>/` | tick **show in online store** on a product and add a photo. Customers order with name, phone and address (cash on delivery or bank transfer); the order appears in Sale orders with source "Online store". Store address, phone and open/closed: back office → Companies. Categories become filters, variants a drop-down; a double click on "Place order" makes one order |

Every stock change is written to Stock moves with its reference (PO00001, MO00003, ...).
Stock rows are locked while they change, so two cashiers can't sell the same last unit
(tested on PostgreSQL).

**POS without internet:** if the connection drops, sales are kept in the browser and
sent automatically when it returns; each sale is counted once even if sent twice.
Keep the POS page open while offline (reloading it needs the internet), and the
session can't be closed until waiting sales are sent.

**Store on its own subdomain:** set `STORE_DOMAIN=beauty-grid.com`, add `.beauty-grid.com` to
`ALLOWED_HOSTS`, and point a wildcard DNS record `*.beauty-grid.com` at the server. Then
`indigo-cosmetics.beauty-grid.com` opens that company's store.

**Light / dark:** the ◐ button on every page (the back office has its own switch).

**Online store limits:** no online card payment (no payment gateway connected) and no
stock reservation until you deliver. Product photos are served by the app itself, fine for
a small store; move them to a storage bucket when traffic grows.

## Language

Arabic by default for everyone. The English / العربية button switches and is
remembered. Translations live in `locale/ar/LC_MESSAGES/django.po`; after
editing run `python manage.py compilemessages`.

## Deploy (beauty-grid.com)

Environment variables:

```
SECRET_KEY=<long random string>
DEBUG=0
ALLOWED_HOSTS=beauty-grid.com,www.beauty-grid.com
CSRF_TRUSTED_ORIGINS=https://beauty-grid.com,https://www.beauty-grid.com
DATABASE_URL=postgres://user:password@host:5432/dbname
```

Optional: `EMAIL_HOST`, `EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD` for email;
`SECURE_SSL_REDIRECT=0` if your host already forces HTTPS.

Build / start:

```bash
pip install -r requirements.txt
python manage.py collectstatic --noinput
python manage.py migrate
gunicorn config.wsgi
```

With `DEBUG=0` the app refuses to start without a real `SECRET_KEY`, forces HTTPS,
uses secure cookies and HSTS. `python manage.py check --deploy` passes (only the
optional HSTS preload notice remains).

## Backups

```bash
python manage.py backup                 # backups/backup-YYYYmmdd-HHMMSS.json.gz, keeps the newest 30
python manage.py loaddata backups/<file>.json.gz   # restore into an empty database
```

Run the backup daily (cron or your host's scheduler) and copy the `backups/`
folder off the server. With PostgreSQL, your host's database snapshots are a
second layer.

## Tests

```bash
python manage.py test erp                                        # 49 tests
DATABASE_URL=postgres://... python manage.py test erp            # adds the two-cashiers race test
```

## Not included yet

Lots and expiry dates (left out on purpose), product variants, backorders, taxes,
accounting, online card payment.
