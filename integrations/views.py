import re

from django.shortcuts import get_object_or_404, render
from django.views.decorators.http import require_POST

from core import htmx
from core.services import CommandError, audit, require

from .connectors import FIELDS, SECRET_WORDS, ensure_connectors, get_adapter
from .models import MODES, Connector, SyncRun

MODE_LABELS = dict(MODES)
TONE = {"file": "info", "manual": "", "off": ""}
SCHEDULES = ["Every 15 minutes", "Hourly", "Daily at 06:00"]


def slug(label):
    return re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")


def page(request):
    ensure_connectors()
    conns = list(Connector.objects.all())
    for c in conns:
        c.tone = TONE.get(c.mode, "warn")
        c.mode_label = MODE_LABELS.get(c.mode, c.mode)
    return render(request, "pages/integrations.html", dict(conns=conns))


def drawer(request, key):
    c = get_object_or_404(Connector, key=key)
    cfg = FIELDS.get(key, {"modes": [c.mode], "data": []})
    mode = request.GET.get("mode") or c.mode
    if mode not in cfg["modes"]:
        mode = cfg["modes"][0]
    saved = c.settings.get(mode, {})
    fields = [dict(label=l, name=slug(l), secret=any(w in l.lower() for w in SECRET_WORDS), value=saved.get(slug(l), ""))
              for l in cfg.get(mode, [])]
    live = mode not in ("off", "file", "manual")
    return render(request, "records/conn.html", dict(
        c=c, mode=mode, modes=[(m, MODE_LABELS.get(m, m)) for m in cfg["modes"]], fields=fields, live=live, data=cfg["data"],
        schedules=SCHEDULES, schedule=c.settings.get("schedule", SCHEDULES[0]), tone=TONE.get(c.mode, "warn"),
        runs=SyncRun.objects.filter(connector=c)[:25], url=f"/records/conn/{key}/?mode={mode}"))


@require_POST
def save(request, key):
    require(request.user, "settings")
    c = get_object_or_404(Connector, key=key)
    cfg = FIELDS.get(key, {"modes": [c.mode]})
    mode = request.POST.get("mode", c.mode)
    if mode not in cfg["modes"]:
        raise CommandError("That mode is not available for this connector.")
    vals = {}
    for l in cfg.get(mode, []):
        if any(w in l.lower() for w in SECRET_WORDS):
            continue  # secrets live in the server's secret store / environment, never in the database
        vals[slug(l)] = request.POST.get(slug(l), "").strip()
    before = c.mode
    c.settings = {**c.settings, mode: vals, "schedule": request.POST.get("schedule", c.settings.get("schedule", SCHEDULES[0]))}
    c.mode = mode
    c.save()
    audit("connector", c.key, f"{c.name} saved: mode {MODE_LABELS.get(before, before)} → {MODE_LABELS.get(mode, mode)}", request.user,
          action="configure", before={"mode": before}, after={"mode": mode, **vals})
    SyncRun.objects.create(connector=c, kind="config", message=f"Settings saved by {request.user.name} (mode: {MODE_LABELS.get(mode, mode)})")
    return htmx.done(request, f"{c.name} saved")


@require_POST
def test(request, key):
    c = get_object_or_404(Connector, key=key)
    ok, msg = get_adapter(key).test_connection()
    SyncRun.objects.create(connector=c, kind="test", ok=ok, message=msg)
    return htmx.done(request, msg, "ok" if ok else "bad", refresh=False)


@require_POST
def sync(request, key):
    require(request.user, "settings")
    run = get_adapter(key).sync()
    return htmx.done(request, run.message, "info")
