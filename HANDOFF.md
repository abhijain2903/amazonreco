# ME Vendor Hub — Handoff

**As of:** 1 Oct 2026 (build `a2a48b3`) · **Repo:** https://github.com/abhijain2903/amazonreco (branch `main`)
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
| Returns (RTV) | Amazon return request → authorise / refuse → goods received (count, condition) → deduction matched; overcharge disputed | — |

R12 = global tolerance (SAR 1). BRD worked example used in seed and tests: 180 units sold × SAR 50 support, DN charges 192 units → +SAR 600.

## 2. Documents and links (claude.ai, private to the owner — share from each page's Share menu)

- BRD (layman language): https://claude.ai/artifact/QQDiqNVCXMWhJvbTwH7e6g
- Prototype Spec (screens, upload types U1-U9, permission matrix §10): https://claude.ai/artifact/KvFVzANRB5cWDXYXU74iDi
- Technical Design & Build Plan (updated to Django + HTMX): https://claude.ai/artifact/9pWH7kyMgW6ToXvsywgZRe
- Clickable prototype (single HTML, not the build): https://claude.ai/artifact/LN1BL6rHdxHKo3eTkaM8dz
- Build screenshots, 24 screens from the running app: https://claude.ai/artifact/5VZrPxUEAcCYqXqDd1LTsM
- Process gap map (every real-world step vs the hub; status after phases A–C): https://claude.ai/artifact/SbJz7LphBEXfK43Q6my9Hd
- Client objections & answers (demo prep, incl. what Amazon's APIs allow): https://claude.ai/artifact/UCq5t5qZwSuL8whQ5miGTC
- Glossary of every term, stage and code in the hub: https://claude.ai/artifact/81X8D9Nhswqgr7vh8oHUfQ

## 3. Decisions already taken (don't reopen without a reason)

- **Python end to end, simplest scalable stack**: the user asked for Python front and back and "simpler yet scalable".
  → Django 5.2 LTS + HTMX/Alpine (server-rendered, no JS build) + PostgreSQL 16 + Procrastinate (jobs in Postgres, **no Redis**).
  Earlier options (Next.js/Prisma, Drizzle) were dropped.
- Modular monolith: one Django app per business area; services layer shared by screens and the JSON API.
- Money in integer halalas; Asia/Riyadh; append-only audit trail enforced by a DB trigger; optimistic locking via `version`.
- File mode first: every source works by upload (U1-U10) and generated files; live connectors are adapter stubs.
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
- AI-assisted matching (`matching/`): payment→invoice, deduction→debit note, debit note→promotion and deduction
  classification with scores and reasons; "Ask AI" per record; provider Anthropic / OpenAI / Bedrock, key entered in
  Settings → Matching (Fernet-encrypted, write-only) or from the server env. Nothing is applied without a person.
- Access audit: every command checked against the permission matrix (`tests/test_access.py`).
- Gap-map fixes, phases A–C (1 Oct 2026), everything except Arabic and live connectors:
  - A: short-paid footer, Documents tab, Excel export everywhere, Saudi working calendar (Fri–Sat, holidays in Settings),
    price valid on the PO date, case packs, single SKU/price edit, credit hold, POD prompt, dispute case ID + partial win,
    promotion types, several credit notes per claim, go-live guide on Uploads.
  - B: stock reserved across POs, Amazon PO changes/cancellations, SSCC carton labels, slot reschedule / missed / refused,
    collect freight, invoice rejected/on hold + resubmit, credit memos, chargeback/returns/co-op deduction types,
    recoveries in later remittances, ageing report, fixed fees, several DNs per agreement (instalments), amendments,
    budgets, batch claims, per-person alerts, assignment + @mentions, Reports page, vendor codes.
  - C: backorders and split shipments (several deliveries/ASNs/slots/invoices per PO, stage "Backorder open"),
    returns (RTV) app with U10 upload.
- ME's three trackers (PO tracker per booking portion, sell-out tracker from Amazon sales / inventory / forecast reports via
  U11 / U12, claim tracker) as dashboard tabs with ME's exact columns and Excel export (`core/trackers.py`). RFPO per PO,
  sales order per booking portion, RRP and model status on SKUs, Salesforce MECL ref / second ref / sub-category on promotions.
- Tests: `pytest` → 270 passed (rules, flows, uploads U1-U10, API, access matrix, matching, defects, phases A–C, review
  regressions, smoke). Playwright e2e: 17 passed against a running seeded server (`test_ui` + `test_scenarios`, the real-world cases).
- Reports page: six tabs (summary with period-on-period and money leaked/protected, orders & stock, shipping, cash &
  deductions, promotions, team speed), 12-week trends, one-workbook export. Demo seed adds realistic history for it.
- Packaging: Dockerfile (`bin/start.sh`: migrate → optional seed → optional worker → gunicorn/uvicorn on `$PORT`),
  `docker-compose.yml` (db, migrate, web, worker), `render.yaml` one-click demo, `.env.example`.

**Not verified yet**
- Docker image build (no Docker daemon in the build sandbox; the same start commands were run and work outside Docker).
- Entra ID sign-in against a real tenant.
- Render deploy (blueprint written and the start sequence simulated on an empty DB; not deployed).
- `/api/v1/docs` loads Swagger UI from a CDN — blank without internet.

## 5. Hosting — live

Live since 28 Sep 2026 at **https://amazonhub.iksulalive.com** on AWS EC2 `i-00c8ba9d4b11f9a59` (account `231811141843`,
`ap-south-1`, Ubuntu 24.04), sharing the box with a separate app (Supplier Portal, `/opt/spp`) that must not be touched.

- Native layout, no Docker: code `/opt/mehub/app` (branch `main`), venv `/opt/mehub/venv`, system user `mehub`, own Postgres
  db/role `mehub`, settings `/etc/mehub/mehub.env` (root-only), helper `sudo mehub-manage <cmd>`, systemd `mehub-web`
  (gunicorn/uvicorn on 127.0.0.1:8010) and `mehub-worker`, nginx vhost with a Let's Encrypt certificate. Only ports 80/443.
- Access is **SSM Session Manager only**; the user's IAM user can open interactive sessions only, so server steps are given as
  single commands for the user to paste. Deploy = merge to `main`, then:
  `sudo -u mehub git -C /opt/mehub/app pull -q && sudo mehub-manage migrate --noinput && sudo mehub-manage collectstatic --noinput && sudo systemctl restart mehub-web mehub-worker`
  (add `pip install -r requirements.txt` when requirements change).
- Demo settings on the server: demo simulations on, dev login with access code, `HUB_DJANGO_ADMIN=false`. The live AI
  provider is OpenAI, connected in Settings → Matching.
- `seed_demo --reset` wipes the live demo data — only on request.

## 6. Open items (suggested order)

1. Settings only ME can give: GS1 company prefix (`HUB_GS1_PREFIX`, SSCC labels), payment terms (`HUB_PAYMENT_TERMS_DAYS`,
   default 60), real Amazon.sa FC codes and vendor codes, this year's Eid holidays (Settings → Calendar), freight terms
   (prepaid / collect), budgets per category or per brand.
2. UAT data: ME's real SKU master (U1) and price list (U2), then the go-live guide on Uploads.
3. Live integrations behind the existing adapters (`integrations/connectors.py`): Amazon SP-API for vendors (orders,
   shipments, invoices + Invoices API status, Finance Remittance API, returns, vendor sales report) or EDI; SAP OData (S/4)
   or IDoc/BAPI (ECC); Salesforce order log; Microsoft 365 / SMTP email for alerts.
4. Small follow-ups: one-click "Add to SKU master" from a rejected PO row; partial credit release if ME uses it.
5. Production hardening: CI (GitHub Actions running pytest against Postgres), S3 for media (`STORAGES`), backups,
   Sentry/logging, API rate limits, vendor Swagger UI assets locally, Entra ID sign-in against ME's tenant.
6. Arabic/RTL is not in scope — confirm with ME.

## 7. Gotchas learned during the build

- HTMX events from a response fire on the element that made the request; a `closeModal` that removes the modal first swallows a
  following `openDrawer`. `hub.js` defers `closeModal` with `setTimeout` — keep it that way.
- Re-rendering an open drawer/modal skips the open animation (`hub.js` adds a `still` class in `htmx:beforeSwap`).
- Buttons inside clickable table rows: `hub.js` cancels the row's request in `htmx:confirm` when the click came from an inner control.
- SSE `/events/` is async under ASGI (production) and a sync generator under runserver; don't make it async-only.
- WhiteNoise under ASGI emits a "must consume synchronous iterators" warning — silenced in settings, harmless.
- `seed_demo` is deterministic (seed 20260926) and relative to "now"; e2e tests change data, so re-seed before each e2e run.
  New seed steps go at the end of `handle()` so earlier random draws don't shift.
- Seed and uploads use `next_number()` counters; suggested CN numbers only advance the counter when the suggestion is used.
- Upload type U9 is **credit notes**, not a sales report; U10 is returns. Sold units come from demo simulation or the DN upload sample.
- The audit trigger makes any UPDATE of `core_auditevent` fail — compute timestamps before writing events (seed bug fixed this way).
- Several shipments per PO: `PurchaseOrder.shipment / invoice / sap_delivery / sap_billing` are properties returning the
  latest one (`seq`); `fulfilment.services.delivery_of` returns None while a released PO waits for its next delivery.
  Mark POs paid only through `payments.services.settle_po` (all shipped, every invoice settled).
- Alerts: use `core.services.notify(...)` — it routes by link type/tab to roles (`ALERT_ROLES`) and copies the record's owner.
- The repo sits in an iCloud-synced folder: `git stash` and `sed -i` have produced duplicate or empty files. Edit with
  normal tools; keep a working copy elsewhere if iCloud misbehaves.

## 8. Where things are

```
config/        settings (env-driven), urls, asgi/wsgi, api.py (Django Ninja)
core/          Base model, audit, notes, notifications/alerts, assignments, vendor codes, attachments, holidays, files,
               counters, Action Center (actions.py), reports.py, exports.py, workcal.py, dashboard, search, SSE,
               htmx helpers, middleware, tasks (Procrastinate), templatetags/hub.py, seed_demo command
identity/      User (roles ArrayField, alert_scope), permissions matrix, Entra backend
rules/         engine.py (R1-R12 pure functions), RuleConfig + services
catalog/ orders/ fulfilment/ billing/ payments/ promotions/ debitnotes/ claims/ returns/   business apps
matching/      suggestion features, matchers, AI providers, encrypted key, settings
uploads/       types.py (U1-U10), services.py (parse → map → preview → commit), views
integrations/  connectors.py (adapters), models (Connector, SyncRun), views
templates/     pages/ records/ dialogs/ partials/ registration/
static/        css/hub.css, js/hub.js, js/htmx.min.js, js/alpine.min.js
tests/         conftest (seeds once per session), test_rules, test_flows, test_uploads, test_api, test_access, test_matching,
               test_defects, test_phase_a/b/c, test_smoke, e2e/test_ui
bin/start.sh   container entrypoint · Dockerfile · docker-compose.yml · render.yaml · .env.example
```
