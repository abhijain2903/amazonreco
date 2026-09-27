# ME Vendor Hub

Amazon order-to-cash and promotion-to-claim for **Modern Electronics (KSA)**, built by Iksula.

The hub replaces the email, Excel and copy-paste steps between Amazon Vendor Central, SAP and Salesforce with one
place where each team sees its next action, every number is checked against the agreement, and every step is recorded.

| Flow | What happens | Rules |
|---|---|---|
| F1 Confirm PO | Amazon PO arrives, price and stock are checked per line, PIC confirms, acknowledgement file is produced | R1, R2, R3 |
| F2 Book & release | Sales order created in SAP (and logged in Salesforce), credit control releases | – |
| F3 Ship | SAP delivery becomes the ASN, delivery slot is booked, delivery recorded | R4, R5 |
| F4 Invoice | SAP billing becomes the Amazon invoice, blocked if it differs from the ASN or PO price | R6 |
| Payments | Remittance matched to invoices, short payments disputed or accepted | R7 |
| F5 Promotions | Promotion tracker from product-team request to Amazon agreement # | R8 |
| F6 Debit notes | Each Amazon DN checked against sold units × agreed support | R9, R10 |
| F7 Claims | Claim to the product team, credit note reconciled, shortfall chased or written off | R11 |

R12 is the global tolerance (default SAR 1). All rules can be switched and tuned in **Settings → Rules & tolerances**.

---

## Stack

Deliberately small: one Python codebase, one database, no front-end build step.

- **Django 5.2 LTS** (Python 3.12): pages, business logic, admin
- **HTMX 2 + Alpine.js** (vendored in `static/js`): drawers, dialogs, inline edits without a SPA
- **PostgreSQL 16**: data, audit trail (append-only, enforced by a trigger) and the job queue
- **Procrastinate**: background and scheduled jobs stored in PostgreSQL (no Redis)
- **Django Ninja**: JSON API with OpenAPI docs at `/api/v1/docs`
- **mozilla-django-oidc**: Microsoft Entra ID sign-in, roles from Entra groups
- **gunicorn + uvicorn workers** (ASGI) in production, WhiteNoise for static files

Scale path: add web containers behind the load balancer and more `worker` containers; PostgreSQL is the only stateful
service. Uploaded and generated files go to `HUB_MEDIA_ROOT` (a volume), or to S3/Azure Blob by switching the storage backend.

## Quick start (Docker)

```bash
cp .env.example .env            # set DJANGO_SECRET_KEY and POSTGRES_PASSWORD at least
# for a demo / UAT box also set: DJANGO_DEBUG=true HUB_DEV_LOGIN=true HUB_DEMO_SIMULATIONS=true
docker compose up -d --build
docker compose exec web python manage.py seed_demo      # example data (350 SKUs, POs at every stage, 30 promotions)
open http://localhost:8000
```

## Quick start (local)

```bash
python3.12 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
createdb mehub                          # PostgreSQL 16 running locally
export POSTGRES_HOST=localhost POSTGRES_USER=$USER POSTGRES_PASSWORD=
python manage.py migrate
python manage.py seed_demo              # add --reset to wipe and reload
python manage.py runserver              # http://127.0.0.1:8000
python manage.py procrastinate worker   # second terminal: background + scheduled jobs
```

With `HUB_DEV_LOGIN=true` (the default when `DJANGO_DEBUG=true`) the sign-in page lets you pick a person.
Each example user has one role, so you see that role's view:

| User | Role | Lands on |
|---|---|---|
| faisal | Amazon account (PIC) | Action Center |
| noura | Planning | Purchase Orders |
| omar | Credit control | Purchase Orders |
| khalid | Logistics | Shipments |
| reem | Product team | Promotions |
| priya | Finance | Payments |
| tariq | Manager | Dashboard |
| admin | Admin (superuser) | Settings |

All example passwords are `demo` (Django admin at `/admin/`). Switch user from the menu at the top right.

## Demo mode vs. file mode

`HUB_DEMO_SIMULATIONS=true` simulates the SAP and Amazon side (SAP order numbers, deliveries, billing, sold units) so
every flow can be clicked end to end. With it off, the same data arrives through **Uploads** (file mode) or, later,
the live connectors. Nothing else changes.

## Tests

```bash
pytest                                            # 44 tests: rules, flows F1-F7, uploads U1-U9, API
python manage.py seed_demo --reset && python manage.py runserver &
E2E_BASE_URL=http://127.0.0.1:8000 pytest tests/e2e -m e2e -p no:django    # browser tests (Playwright)
```

## Project layout

```
config/          settings, URLs, ASGI/WSGI, JSON API (api.py)
core/            base models, audit trail, notifications, files, Action Center, dashboard, search, HTMX helpers,
                 background jobs (tasks.py), template tags, seed_demo command
identity/        user model with roles, permission matrix, Entra ID backend
rules/           R1-R12 as pure functions (engine.py) + their settings (RuleConfig)
catalog/         SKUs, ASIN map, agreed prices, fulfilment centres
orders/          purchase orders and lines (F1, F2)
fulfilment/      SAP deliveries, ASN, delivery slots (F3)
billing/         SAP billing and Amazon invoices (F4)
payments/        remittances, matching, disputes
promotions/      promotion tracker (F5)
debitnotes/      debit-note validation (F6)
claims/          claims and credit notes (F7)
uploads/         upload types U1-U9: parse, map columns, preview, import
integrations/    connector adapters (Amazon VC, SAP, Salesforce, Finance, Email, SSO)
templates/       pages/, records/ (drawers), dialogs/, partials/
static/          hub.css, hub.js, htmx, alpine
tests/           pytest suite; tests/e2e for the browser
```

Each business app follows the same pattern: `models.py` (data), `services.py` (commands: permission check, rule check,
change, audit entry, all in one transaction), `views.py` (pages and HTMX endpoints that call the services).
Screens and the JSON API share the services, so a rule or permission is enforced the same way everywhere.

## Integrations (placeholders)

Every external system sits behind one adapter in `integrations/connectors.py` with `test_connection`, `pull`, `push`
and `sync`. Modes per connector: **off**, **file** (works today through Uploads and generated files), **manual**, or a
live mode (Amazon SP-API/EDI, SAP OData/IDoc/BAPI, Salesforce API, SMTP/Microsoft 365, Entra ID/SAML).
Live methods raise `NotImplementedError` with a note on the real call, ready to be filled in once ME IT confirms access.
Secrets are read from the environment, never stored in the database.

## JSON API

Session or `Authorization: Bearer $HUB_API_TOKEN` (acts as `HUB_API_USER`). Docs: `/api/v1/docs`.

- `GET /api/v1/purchase-orders`, `GET /api/v1/purchase-orders/{po}`, `POST /api/v1/purchase-orders/{po}/confirm`
- `GET /api/v1/promotions`, `GET /api/v1/debit-notes`, `GET /api/v1/action-items`
- `POST /api/v1/uploads/{U1..U9}` (multipart file), `POST /api/v1/uploads/{id}/commit`

## Scheduled jobs (Procrastinate, times in Asia/Riyadh)

| Job | When | What |
|---|---|---|
| `sweep_slots` | hourly | R5: shipments leaving within 48 h without a slot |
| `sweep_dn_timing` | 07:00 | R9: debit notes overdue |
| `daily_digest` | 08:00 | per-role summary of open actions |
| `rerun_open_checks` | on rule change | re-runs checks on open records |

## Production checklist

- `DJANGO_DEBUG=false`, `HUB_DEV_LOGIN=false`, `HUB_DEMO_SIMULATIONS=false`, a strong `DJANGO_SECRET_KEY`
- Entra ID: app registration with the `groups` claim; set `OIDC_*` and `OIDC_GROUP_ROLE_MAP`
- TLS at the load balancer (`X-Forwarded-Proto` is trusted); `DJANGO_ALLOWED_HOSTS` and `DJANGO_CSRF_TRUSTED_ORIGINS`
- Nightly PostgreSQL backups; the media volume holds uploads and generated files
- Load the real master data with Uploads U1 (SKU master) and U2 (price list) before go-live
