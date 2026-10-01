# ME Vendor Hub — working notes for Claude Code

Amazon order-to-cash and promotion-to-claim hub for Modern Electronics (KSA), built by Iksula.
Read `HANDOFF.md` for project history, current status and the open-items list before starting new work.

## Stack
Django 5.2 · HTMX 2 + Alpine (vendored in `static/js`, no build step) · PostgreSQL 16 · Procrastinate (jobs in Postgres, no Redis)
· Django Ninja API (`config/api.py`) · mozilla-django-oidc (Entra ID) · gunicorn + uvicorn workers (ASGI).

## Commands
```bash
. .venv/bin/activate                         # python 3.11+ (Docker image uses 3.12)
pip install -r requirements-dev.txt
python manage.py migrate
python manage.py seed_demo --reset           # deterministic example data; users faisal/noura/omar/khalid/reem/priya/tariq/admin, password "demo"
python manage.py runserver                   # http://127.0.0.1:8000 (dev user-picker login)
python manage.py procrastinate worker        # background + periodic jobs
pytest                                       # 255 tests: rules, flows F1-F7, uploads U1-U10, API, access matrix, matching, phases A-C, review regressions, smoke — needs Postgres
E2E_BASE_URL=http://127.0.0.1:8000 pytest tests/e2e -m e2e -p no:django   # Playwright; re-seed first
```
DB settings come from `POSTGRES_*` env vars (default db `mehub`, host `localhost`). `DJANGO_DEBUG` defaults to true.

## Layout and conventions
- One Django app per business area: `orders` (F1-F2), `fulfilment` (F3), `billing` (F4), `payments`, `promotions` (F5),
  `debitnotes` (F6), `claims` (F7), `returns` (RTV), plus `catalog`, `rules`, `uploads`, `integrations`, `matching`, `identity`, `core`.
- **Commands live in `services.py`**: `require(user, perm)` → state/rule checks → change → `audit(...)`, inside
  `@transaction.atomic`, using `select_for_update()` and `check_version()` (optimistic locking via `Base.version`).
  Business errors raise `core.services.CommandError`; `CommandErrorMiddleware` turns them into a red toast (HTMX) or flash.
- **Views are thin**: render a page/drawer/dialog, or call a service and return `core.htmx.done(request, toast, file=..., close_modal=..., open_drawer=...)`
  which answers 204 + `HX-Trigger` (toast / refresh / drawerReload / closeModal / openDrawer) or retargets a generated file to `#modal`.
- **Rules R1-R12 are pure functions** in `rules/engine.py` (`Cfg` from `rules.services.get_cfg()`, cached 5 s). Keep them DB-free and unit-tested in `tests/test_rules.py`.
- **Money is integer halalas** (`*_h` fields, SAR × 100). Format with template filters `sar`, `n`, `n2`, `signed`. Time zone Asia/Riyadh.
- **Audit trail is append-only** (Postgres trigger, migration `core/0003`). Never update/delete `AuditEvent`; timelines read from it.
- Promotion stages `live / waiting_dn / dn_overdue / dn_received` are **derived** (`promotions.services.stage_of`), not stored.
- Action Center is a query (`core/actions.py`), not a table. Due dates use the Saudi working calendar (`core/workcal.py`).
- A PO can have several deliveries / shipments / invoices (`seq`); `po.shipment`, `po.invoice` etc. are the latest one.
  Set a PO to paid only via `payments.services.settle_po`.
- Alerts go through `core.services.notify(...)` (routed to roles / people); lists offer Excel export via `core/exports.py`
  (`partials/export_btn.html` + a `wants_export(request)` branch in the view).
- Permissions: `identity/permissions.py` (`PERMS`, `can`, `caps`); in templates use `{% perm 'confirm' %}` on buttons.
- Templates: `pages/` (full pages extending `base.html`, content inside `#view`), `records/` (drawers extending `records/_drawer.html`),
  `dialogs/` (extending `dialogs/_modal.html`), `partials/`. Custom tags/filters in `core/templatetags/hub.py` (builtins, no `{% load %}` needed).
- Client JS is only `static/js/hub.js` (toasts, drawer/modal events, palette, SSE, drag-drop). Keep new UI server-rendered.
- Upload types U1-U10: columns, synonyms, validation, import and sample files all in `uploads/types.py`.
- External systems only through adapters in `integrations/connectors.py`; live methods raise `NotImplementedError` until built.
- Demo switches: `HUB_DEMO_SIMULATIONS` (simulate SAP/Amazon steps), `HUB_DEV_LOGIN` (user picker), `HUB_DEMO_PASSWORD` (access code),
  `HUB_DJANGO_ADMIN` (false on client-facing hosts: no `/admin/`, no Admin console link).
- Query-string choices (`tab`, `view`) go through `core.htmx.pick(request, key, allowed, default)` — never index a dict with raw input.
- Read access follows the same permission as writes: pages for Admin-only areas call `require(user, "settings")`, and the
  sidebar hides entries via `core.nav.NAV_PERMS`.
- Every command form sends the record's `version`; autosaving forms get the new version back via `htmx.done(..., version=(input_id, v))`.
- Line models are ordered by `created_at` so seeded demo data is identical on every database (ids are random UUIDs).
- CSP (`core.middleware.SecurityHeadersMiddleware`) allows only self-hosted script files, no inline scripts or eval: put
  behaviour in `static/js/hub.js`, not in `onclick=` / Alpine expressions / htmx `hx-on`.
- Master data (SKUs, FCs) is never created by a transaction import; unknown codes are row errors. Sample files skip SKUs
  on flagged lines of new POs (the live R1/R2 demo examples).

## Before finishing a change
Run `pytest`; for UI changes also open the affected pages (and the e2e suite after `seed_demo --reset`).
Add a test in `tests/test_flows.py` for any new command or rule behaviour.
