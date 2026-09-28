# ME Vendor Hub — Handoff

**As of:** 28 Sep 2026 · **Repo:** https://github.com/abhijain2903/amazonreco (branch `main`)
**Client:** Modern Electronics (ME), KSA — Amazon Vendor Central digitalisation · **Built by:** Iksula

Read this first when picking the project up in Claude Code. `CLAUDE.md` has the day-to-day commands and coding conventions;
`README.md` has setup, deployment and the API list.

---

## 1. What the hub does

ME sells to Amazon.sa as a vendor. Today the work runs on email, Excel and re-typing between Vendor Central, SAP and
Salesforce. The requirement (ME's pptx) had two 10-step processes sharing one data key (Model # / Quantity / Value / Amazon #):

| Flow | Steps | Rules |
|---|---|---|
| F1 Confirm PO | Amazon PO in → price check vs agreed list, stock check vs free stock → PIC decides per line → acknowledgement file | R1 price, R2 stock, R3 confirm deadline |
| F2 Book & release | Sales order in SAP (+ Salesforce log) → credit control releases | — |
| F3 Ship | SAP delivery → ASN (must equal delivery) → Carrier Central slot → delivered | R4 ASN = delivery, R5 slot before dispatch |
| F4 Invoice | SAP billing → Amazon invoice, blocked if qty/price differs from ASN/PO | R6 |
| Payments | Remittance matched to invoices; short pay → dispute / link to DN / accept | R7 |
| F5 Promotions | Product team request → submit to Amazon → agreement # (unique) → promo runs | R8 unique agreement |
| F6 Debit notes | DN due end + 30 days; validated vs sold units × agreed support | R9 DN late, R10 DN match |
| F7 Claims | Claim to product team → credit note reconciled; shortfall chased or written off | R11 CN = claim |

R12 = global tolerance (SAR 1). BRD worked example used in seed and tests: 180 units sold × SAR 50 support, DN charges 192 units → +SAR 600.

## 2. Documents and links (claude.ai, private to the owner — share from each page's Share menu)

- BRD (layman language): https://claude.ai/artifact/QQDiqNVCXMWhJvbTwH7e6g
- Prototype Spec (screens, upload types U1-U9, permission matrix §10): https://claude.ai/artifact/KvFVzANRB5cWDXYXU74iDi
- Technical Design & Build Plan (updated to Django + HTMX): https://claude.ai/artifact/9pWH7kyMgW6ToXvsywgZRe
- Clickable prototype (single HTML, not the build): https://claude.ai/artifact/LN1BL6rHdxHKo3eTkaM8dz
- Build screenshots, 24 screens from the running app: https://claude.ai/artifact/5VZrPxUEAcCYqXqDd1LTsM

## 3. Decisions already taken (don't reopen without a reason)

- **Python end to end, simplest scalable stack**: the user asked for Python front and back and "simpler yet scalable".
  → Django 5.2 LTS + HTMX/Alpine (server-rendered, no JS build) + PostgreSQL 16 + Procrastinate (jobs in Postgres, **no Redis**).
  Earlier options (Next.js/Prisma, Drizzle) were dropped.
- Modular monolith: one Django app per business area; services layer shared by screens and the JSON API.
- Money in integer halalas; Asia/Riyadh; append-only audit trail enforced by a DB trigger; optimistic locking via `version`.
- File mode first: every source works by upload (U1-U9) and generated files; live connectors are adapter stubs.
- Demo mode (`HUB_DEMO_SIMULATIONS=true`) simulates SAP order/delivery/billing and Amazon sold units so every flow clicks through.
- Delivery via GitHub repo `abhijain2903/amazonreco` (the Claude GitHub app now has push access).

## 4. Current status

**Done and verified**
- All flows F1-F7, payments/disputes, Action Center (per role), dashboard, global search (Ctrl+K), notifications,
  live refresh (SSE), record drawers with 10-step stepper, reference chain, checks, timeline and notes.
- Promotion wizard, board and timeline views; DN validation with split view, override (reason required) and dispute;
  unlinked-DN linking with best-match suggestion; claims + CN record/chase/write-off.
- Upload wizard (CSV/XLSX, auto column mapping with synonyms, remembered mappings, row preview with errors/warnings, import,
  duplicate-file warning, sample files built from live data, templates).
- Settings: SKU master, price list, rules R1-R12 (edit → background re-check + audit), categories, FCs, users & permission matrix,
  notifications (display only), numbering (display only). Integrations page + connector drawer (modes, fields, test/sync/save,
  secrets never stored in DB).
- JSON API (`/api/v1/…`, OpenAPI at `/api/v1/docs`), session or Bearer `HUB_API_TOKEN`.
- Entra ID sign-in wired (roles from groups via `OIDC_GROUP_ROLE_MAP`); dev user-picker login with optional access code.
- Tests: `pytest` → 47 passed (rules, flows, uploads for all 9 types, API, smoke of every page/drawer/dialog for admin + PIC).
  Playwright e2e: 6 passed against a running seeded server.
- Packaging: Dockerfile (`bin/start.sh`: migrate → optional seed → optional worker → gunicorn/uvicorn on `$PORT`),
  `docker-compose.yml` (db, migrate, web, worker), `render.yaml` one-click demo, `.env.example`.

**Not verified yet**
- Docker image build (no Docker daemon in the build sandbox; the same start commands were run and work outside Docker).
- Entra ID sign-in against a real tenant.
- Render deploy (blueprint written and the start sequence simulated on an empty DB; not deployed).
- `/api/v1/docs` loads Swagger UI from a CDN — blank without internet.

## 5. Hosting — where it stands

No live URL yet. Two routes:

1. **Render (fastest demo)**: https://render.com/deploy?repo=https://github.com/abhijain2903/amazonreco → set `HUB_DEMO_PASSWORD`
   → ~10 min. Free-plan limits: sleeps after 15 min idle, files lost on restart, free DB expires after 30 days.
2. **AWS EC2 via SSM (user's preferred)**: account `231811141843`, region `ap-south-1`, instance `i-00c8ba9d4b11f9a59`.
   The user's instructions for this server:
   - Connect with **SSM Session Manager only — no SSH**. Touch **no other EC2 instance or AWS resource**.
   - **First only verify** (credentials valid, account ID matches, region, instance online in SSM, session can be opened),
     make **no changes**, reply exactly "SSM connection established successfully." and **wait**; the user gives commands one at a time.
   - The earlier cloud session could not do this (placeholder AWS keys, AWS endpoints blocked). From Claude Code on the user's
     machine, check `aws sts get-caller-identity`, `aws ssm describe-instance-information --filters Key=InstanceIds,Values=i-00c8ba9d4b11f9a59 --region ap-south-1`,
     and that `session-manager-plugin` is installed before opening a session.
   - Likely deploy once allowed (only when the user asks): install Docker + compose plugin → clone repo → `.env` from `.env.example`
     (`DJANGO_ALLOWED_HOSTS`, `DJANGO_CSRF_TRUSTED_ORIGINS`, `HUB_DEV_LOGIN=true`, `HUB_DEMO_PASSWORD`, `HUB_DEMO_SIMULATIONS=true`,
     `HUB_SEED_DEMO=true`) → `docker compose up -d --build`. With no TLS/domain, set `DJANGO_DEBUG=true` or `DJANGO_SSL_REDIRECT=false`
     and note secure cookies need HTTPS (put CloudFront or an ALB with a certificate in front, or use a domain + Caddy).
     Security group must allow the web port; for CloudFront forward `CloudFront-Forwarded-Proto`/Host.

## 6. Open items (suggested order)

1. Get a live URL (section 5) and walk ME through it; collect feedback.
2. UAT data: load ME's real SKU master (U1) and agreed price list (U2); replace example FC codes with real Amazon.sa ship-to codes.
3. Confirm with ME IT which live integrations come first and build them behind the existing adapters
   (`integrations/connectors.py`): Amazon SP-API for vendors or EDI 850/855/856/810/820; SAP OData (S/4) or IDoc/BAPI (ECC);
   Salesforce order log; SMTP/Microsoft 365 email (daily digest currently only logs).
4. Make "Settings → Notifications" and "Numbering" editable (today display-only); per-user notification preferences.
5. Production hardening: CI (GitHub Actions running pytest against Postgres), Docker build in CI, S3/Azure Blob for media
   (`STORAGES`), backups, Sentry/logging, rate limits on the API, vendor Swagger UI assets locally.
6. Arabic/RTL is not in scope yet — confirm with ME.

## 7. Gotchas learned during the build

- HTMX events from a response fire on the element that made the request; a `closeModal` that removes the modal first swallows a
  following `openDrawer`. `hub.js` defers `closeModal` with `setTimeout` — keep it that way.
- Re-rendering an open drawer/modal skips the open animation (`hub.js` adds a `still` class in `htmx:beforeSwap`).
- Buttons inside clickable table rows: `hub.js` cancels the row's request in `htmx:confirm` when the click came from an inner control.
- SSE `/events/` is async under ASGI (production) and a sync generator under runserver; don't make it async-only.
- WhiteNoise under ASGI emits a "must consume synchronous iterators" warning — silenced in settings, harmless.
- `seed_demo` is deterministic (seed 20260926) and relative to "now"; e2e tests change data, so re-seed before each e2e run.
- Seed and uploads use `next_number()` counters; suggested CN numbers only advance the counter when the suggestion is used.
- Upload type U9 is **credit notes**, not a sales report; sold units come from demo simulation or the DN upload sample.
- The audit trigger makes any UPDATE of `core_auditevent` fail — compute timestamps before writing events (seed bug fixed this way).

## 8. Where things are

```
config/        settings (env-driven), urls, asgi/wsgi, api.py (Django Ninja)
core/          Base model, audit, notes, notifications, files, counters, Action Center, dashboard, search, SSE,
               htmx helpers, middleware, tasks (Procrastinate), templatetags/hub.py, seed_demo command
identity/      User (roles ArrayField), permissions matrix, Entra backend
rules/         engine.py (R1-R12 pure functions), RuleConfig + services
catalog/ orders/ fulfilment/ billing/ payments/ promotions/ debitnotes/ claims/   business apps (models, services, views, urls)
uploads/       types.py (U1-U9), services.py (parse → map → preview → commit), views
integrations/  connectors.py (adapters), models (Connector, SyncRun), views
templates/     pages/ records/ dialogs/ partials/ registration/
static/        css/hub.css, js/hub.js, js/htmx.min.js, js/alpine.min.js
tests/         conftest (seeds once per session), test_rules, test_flows, test_uploads, test_api, test_smoke, e2e/test_ui
bin/start.sh   container entrypoint · Dockerfile · docker-compose.yml · render.yaml · .env.example
```
