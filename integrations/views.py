import re

from django.shortcuts import get_object_or_404, render
from django.views.decorators.http import require_POST

from core import htmx
from core.services import CommandError, audit, check_version, require

from .connectors import FIELDS, HELP, SECRET_WORDS, SERVER_FIELDS, ensure_connectors, get_adapter
from .models import MODES, AmazonTransaction, Connector, SyncRun

MODE_LABELS = dict(MODES)
SCHEDULES = ["Every 15 minutes", "Hourly", "Daily at 06:00"]
BUILT = {("amazon_vc", "api")}          # live modes with a working connector; the others are placeholders


def slug(label):
    return re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")


def _server_kept(key, label):
    return (key, label) in SERVER_FIELDS or any(w in label.lower() for w in SECRET_WORDS)


def page(request):
    require(request.user, "settings")
    ensure_connectors()
    conns = list(Connector.objects.all())
    for c in conns:
        c.mode_label = MODE_LABELS.get(c.mode, c.mode)
    return render(request, "pages/integrations.html", dict(conns=conns))


def drawer(request, key):
    require(request.user, "settings")
    c = get_object_or_404(Connector, key=key)
    cfg = FIELDS.get(key, {"modes": [c.mode], "data": []})
    mode = request.GET.get("mode") or c.mode
    if mode not in cfg["modes"]:
        mode = cfg["modes"][0]
    saved = c.settings.get(mode, {})
    from django.conf import settings
    fields = [dict(label=l, name=slug(l), secret=_server_kept(key, l), value=saved.get(slug(l), ""), help=HELP.get((key, l), ""),
                   env=SERVER_FIELDS.get((key, l), ""),
                   env_set=bool(getattr(settings, SERVER_FIELDS.get((key, l), "").removeprefix("HUB_"), "")))   # HUB_X -> settings.X
              for l in cfg.get(mode, [])]
    live = mode not in ("off", "file", "manual")
    return render(request, "records/conn.html", dict(
        c=c, mode=mode, modes=[(m, MODE_LABELS.get(m, m)) for m in cfg["modes"]], fields=fields, live=live, data=cfg.get(f"data_{mode}", cfg["data"]),
        schedules=SCHEDULES, schedule=c.settings.get("schedule", SCHEDULES[0]), tone=c.tone,
        runs=SyncRun.objects.filter(connector=c)[:25], url=f"/records/conn/{key}/?mode={mode}",
        built=(key, mode) in BUILT, txs=AmazonTransaction.objects.all()[:30] if key == "amazon_vc" and mode == "api" else []))


@require_POST
def save(request, key):
    require(request.user, "settings")
    c = get_object_or_404(Connector, key=key)
    check_version(c, request.POST.get("version"))
    cfg = FIELDS.get(key, {"modes": [c.mode]})
    mode = request.POST.get("mode", c.mode)
    if mode not in cfg["modes"]:
        raise CommandError("That mode is not available for this connector.")
    vals = {}
    for l in cfg.get(mode, []):
        if _server_kept(key, l):
            continue  # secrets live in the server's secret store / environment, never in the database
        vals[slug(l)] = request.POST.get(slug(l), "").strip()
    before = c.mode
    c.settings = {**c.settings, mode: vals, "schedule": request.POST.get("schedule", c.settings.get("schedule", SCHEDULES[0]))}
    c.mode = mode
    c.bump()
    c.save()
    audit("connector", c.key, f"{c.name} saved: mode {MODE_LABELS.get(before, before)} → {MODE_LABELS.get(mode, mode)}", request.user,
          action="configure", before={"mode": before}, after={"mode": mode, **vals})
    SyncRun.objects.create(connector=c, kind="config", message=f"Settings saved by {request.user.name} (mode: {MODE_LABELS.get(mode, mode)})")
    return htmx.done(request, f"{c.name} saved")


@require_POST
def test(request, key):
    require(request.user, "settings")
    c = get_object_or_404(Connector, key=key)
    ok, msg = get_adapter(key).test_connection()
    SyncRun.objects.create(connector=c, kind="test", ok=ok, message=msg)
    return htmx.done(request, msg, "ok" if ok else "bad", refresh=False)


@require_POST
def sync(request, key):
    require(request.user, "settings")
    run = get_adapter(key).sync()
    return htmx.done(request, run.message, "info")


@require_POST
def amazon_resend(request, pk):
    from .amazon import resend
    t = resend(request.user, pk)
    return htmx.done(request, f"{t.get_kind_display()} {t.ref} queued to send to Amazon again")


def amazon_catalog(request):
    """What Amazon's catalog says about an ASIN (SKU master: check a SKU, or look up an ASIN before adding it)."""
    from catalog.models import Sku

    from .amazon import catalog_lookup, is_live
    from .spapi import Client, SpApiError
    asin = (request.GET.get("asin") or "").strip().upper()
    sku = Sku.objects.filter(sku_code=request.GET.get("sku", "")).first()
    asin = asin or (sku.asin if sku else "")
    ctx = dict(asin=asin, sku=sku)
    if not asin:
        ctx["error"] = "Type the ASIN first."
    elif not is_live() or not Client.configured():
        ctx["error"] = "Amazon is not connected by API, so the catalog cannot be checked from here."
    else:
        try:
            ctx["item"] = catalog_lookup(asin, sku)
        except SpApiError as e:
            ctx["error"] = "Not found in Amazon.sa's catalog." if e.status == 404 else f"Amazon refused the lookup: {e}"
    return render(request, "partials/amazon_catalog.html", ctx)
