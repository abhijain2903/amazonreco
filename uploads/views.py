"""Upload wizard: type -> file -> map columns -> preview -> import (flow U1-U9)."""
import csv
import io
import json

from django.http import HttpResponse
from django.shortcuts import get_object_or_404, render
from django.views.decorators.http import require_POST

from core.nav import TITLES, section_for
from core.services import CommandError, require

from . import services as svc
from . import types
from .models import UploadBatch

STEPS = ["Type", "File", "Map columns", "Preview", "Done"]


def _types():
    out = []
    for tid, t in types.TYPES.items():
        last = UploadBatch.objects.filter(upload_type=tid, status="committed").first()
        out.append(dict(id=tid, name=t["name"], src=t["src"], required=sum(1 for c in t["cols"] if c[1]), last=last,
                        perm=t.get("perm", "upload")))
    return out


# Go-live: the order to load opening data in, so every later file finds what it refers to.
CUTOVER = [
    ("U1", "SKU master & ASIN map", "Every active model with its ASIN, EAN and case pack. POs, prices and promotions all look SKUs up here."),
    ("U2", "Agreed price list", "Current agreed cost per SKU with its valid-from date. Older prices only if open POs were ordered at them."),
    (None, "Settings", "Users and roles, the Saudi public holidays for the year (Settings → Calendar) and the rule tolerances."),
    ("U3", "Stock snapshot", "Free stock from SAP on the cut-over morning."),
    ("U4", "Open Amazon POs", "Only POs not yet invoiced. Older, paid POs stay in SAP / Vendor Central."),
    ("U5", "SAP deliveries", "Deliveries already picked for the open POs, so their ASNs can be built here."),
    ("U6", "Open remittances", "Payments from the cut-over date onwards, plus any short-payments still being worked."),
    ("U7", "Live and upcoming promotions", "Promotions still running, or ended but not yet claimed."),
    ("U8", "Open debit notes", "Debit notes not yet validated or claimed."),
    ("U9", "Credit notes", "Credit notes for claims already sent, so their shortfalls show correctly."),
]


def upload_page(request):
    hist = UploadBatch.objects.filter(status="committed")[:100]
    for b in hist:
        b.type_name = types.TYPES[b.upload_type]["name"]
    utypes = _types()
    done = {u["id"] for u in utypes if u["last"]}
    cutover = [dict(n=i, tid=t, title=title, text=text, done=t in done) for i, (t, title, text) in enumerate(CUTOVER, 1)]
    return render(request, "pages/uploads.html", dict(utypes=utypes, hist=hist, cutover=cutover,
                                                      cut_done=sum(1 for c in cutover if c["done"]), cut_total=sum(1 for c in cutover if c["tid"])))


def _wiz(request, step, tid=None, batch=None, **extra):
    t = types.TYPES.get(tid) if tid else None
    ctx = dict(step=step, steps=STEPS, tid=tid, t=t, b=batch, utypes=_types() if step == 1 else None, **extra)
    if t:
        ctx["cols"] = [dict(f=f, req=req, syn=syn) for f, req, syn in t["cols"]]
    if step == 3 and batch:
        first = batch.raw_rows[0] if batch.raw_rows else []
        ctx["maprows"] = [dict(f=f, req=req, m=batch.mapping.get(f, {"idx": -1, "how": "missing"}),
                               sample=(first[batch.mapping[f]["idx"]] if 0 <= batch.mapping.get(f, {}).get("idx", -1) < len(first) else None))
                          for f, req, _ in t["cols"]]
        ctx["missing"] = svc.missing_required(batch)
        ctx["dup"] = svc.duplicate_of(batch)
    if step == 4 and batch:
        rows = list(batch.rows.all())
        key = [c[0] for c in t["cols"] if c[1]][:4]
        ctx.update(key=key, prows=[dict(r=r, vals=[r.data.get(k, "") for k in key]) for r in rows[:60]], n=len(rows),
                   ok=sum(1 for r in rows if not r.errors), err=sum(1 for r in rows if r.errors),
                   warn=sum(1 for r in rows if r.warnings and not r.errors))
    if step == 5 and batch:
        ctx["go"] = t["go"]
        ctx["go_name"] = TITLES.get(section_for(t["go"].split("?")[0]), "the list")
    return render(request, "dialogs/upload.html", ctx)


def new(request):
    """GET: pick a type or show the file step. POST: receive the file (or a sample) and go to column mapping."""
    tid = request.GET.get("type") or request.POST.get("type")
    if request.method == "GET":
        return _wiz(request, 2 if tid in types.TYPES else 1, tid if tid in types.TYPES else None)
    if tid not in types.TYPES:
        require(request.user, "upload")
        raise CommandError("Choose what you are uploading.")
    svc.require_type(request.user, tid)  # before a sample file is built
    try:
        if request.POST.get("sample"):
            name, data = svc.sample(tid)
        else:
            f = request.FILES.get("file")
            if not f:
                return _wiz(request, 2, tid, err="Choose a file first.")
            name, data = f.name, f.read()
        b = svc.create_batch(request.user, tid, name, data)
    except CommandError as e:
        return _wiz(request, 2, tid, err=str(e))
    return _wiz(request, 3, tid, b)


def step(request, pk):
    """Re-open a batch at its current step (used by Back)."""
    b = get_object_or_404(UploadBatch, pk=pk)
    want = int(request.GET.get("step", 3))
    return _wiz(request, min(want, 3) if b.status != "committed" else 5, b.upload_type, b)


@require_POST
def mapping(request, pk):
    b = get_object_or_404(UploadBatch, pk=pk)
    require(request.user, "upload")
    for f, _, _ in types.TYPES[b.upload_type]["cols"]:
        v = request.POST.get(f"map-{f}")
        if v is not None and int(v) != b.mapping.get(f, {}).get("idx", -1):
            svc.set_mapping(b, f, int(v))
    if b.status == "preview":
        b.status = "mapping"
        b.save(update_fields=["status"])
    return _wiz(request, 3, b.upload_type, b)


@require_POST
def preview(request, pk):
    b = get_object_or_404(UploadBatch, pk=pk)
    try:
        svc.build_preview(request.user, b)
    except CommandError as e:
        return _wiz(request, 3, b.upload_type, b, err=str(e))
    return _wiz(request, 4, b.upload_type, b)


@require_POST
def commit(request, pk):
    b = svc.commit(request.user, pk)
    resp = _wiz(request, 5, b.upload_type, b)
    resp["HX-Trigger"] = json.dumps({"toast": {"msg": f"{b.filename} imported", "tone": "ok"}, "refresh": True})
    return resp


def template(request, tid):
    if tid not in types.TYPES:
        raise CommandError("Unknown upload type.")
    t = types.TYPES[tid]
    _, rows = types.sample_rows(tid, dry=True)
    buf = io.StringIO()
    csv.writer(buf, lineterminator="\n").writerows(rows)
    cols = [dict(f=f, req=req, syn=", ".join(syn) or "—",
                 kind="Number" if f in types.NUM_FIELDS else "Date (YYYY-MM-DD)" if f in types.DATE_FIELDS else "Text") for f, req, syn in t["cols"]]
    return render(request, "dialogs/template.html", dict(tid=tid, t=t, cols=cols, csv=buf.getvalue(), back=request.GET.get("back")))


def sample_file(request, tid):
    if tid not in types.TYPES:
        raise CommandError("Unknown upload type.")
    _, rows = types.sample_rows(tid, dry=True)
    buf = io.StringIO()
    csv.writer(buf).writerows(rows)
    name = f"template_{tid}.csv"
    resp = HttpResponse(buf.getvalue(), content_type="text/csv")
    resp["Content-Disposition"] = f'attachment; filename="{name}"'
    return resp
