from datetime import datetime

from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.http import require_POST

from catalog.models import FulfilmentCentre, Sku
from core import htmx
from core.models import Note
from core.services import CommandError, timeline, to_h

from . import services as svc
from .models import CONDITIONS, RTV_REASONS, ReturnAuth

TABS = [("todo", "To authorise", ["requested"]), ("transit", "On its way", ["authorised"]), ("received", "To match", ["received"]),
        ("closed", "Closed", ["credited", "disputed", "refused"]), ("all", "All", None)]


def rtv_list(request):
    tab = htmx.pick(request, "tab", [k for k, _, _ in TABS], "todo")
    allr = list(ReturnAuth.objects.select_related("fc").prefetch_related("lines__sku"))
    st = dict((k, s) for k, _, s in TABS)[tab]
    rows = [r for r in allr if st is None or r.status in st]
    counts = {k: sum(1 for r in allr if s is None or r.status in s) for k, _, s in TABS}
    sar = lambda h: f"{round(h / 100):,}"
    open_r = [r for r in allr if r.status in ("requested", "authorised", "received")]
    kpis = [dict(l="To authorise", v=counts["todo"], s="Amazon return requests", url="?tab=todo", alert=counts["todo"] > 0),
            dict(l="On its way back", v=counts["transit"], s="Authorised, not received", url="?tab=transit"),
            dict(l="Deductions to match", v=counts["received"], s="Received returns", url="?tab=received"),
            dict(l="Open returns value", v=sar(sum(r.amount_h for r in open_r)), s="SAR at cost", url="?tab=all")]
    from core.exports import sar as s2, wants_export, xlsx
    if wants_export(request):
        return xlsx(f"Returns_{tab}", ["Return", "Vendor code", "FC", "Reason", "Requested", "Units", "Value SAR", "Received SAR", "Status", "Payment", "Dispute"],
                    [[r.rtv_no, r.vendor_code, r.fc.code if r.fc else "", r.get_reason_display(), r.requested_at, sum(l.qty for l in r.lines.all()),
                      s2(r.amount_h), s2(r.received_h) if r.received_at else None, r.get_status_display(), r.payment_no, r.dispute_no] for r in rows])
    return render(request, "pages/returns.html", dict(rows=rows, tab=tab, kpis=kpis, tabs=[dict(id=k, label=l, count=counts[k]) for k, l, _ in TABS]))


def drawer(request, key):
    r = svc.get_rtv(key)
    tab = htmx.pick(request, "tab", ["lines", "timeline", "notes"], "lines")
    base = f"/records/rtv/{r.rtv_no}/"
    from core.views import owner_ctx
    ctx = dict(r=r, tab=tab, base=base, conditions=CONDITIONS, url=f"{base}?tab={tab}", lines=list(r.lines.select_related("sku")),
               dtabs=[dict(id="lines", label="Models"), dict(id="timeline", label="Timeline"),
                      dict(id="notes", label="Notes", n=Note.objects.filter(entity="rtv", entity_id=r.rtv_no).count())],
               **owner_ctx("rtv", r.rtv_no))
    if r.status == "received":
        ctx["cands"] = svc.candidates(r)[:6]
    if tab == "timeline":
        ctx["events"] = timeline("rtv", r.rtv_no)
    if tab == "notes":
        ctx.update(notes=Note.objects.filter(entity="rtv", entity_id=r.rtv_no), entity="rtv", key=r.rtv_no)
    return render(request, "records/rtv.html", ctx)


def new(request):
    if request.method == "POST":
        from core.services import require
        require(request.user, "upload")
        P = request.POST
        lines = []
        for i in range(5):
            code = P.get(f"sku-{i}", "").strip()
            if not code:
                continue
            s = Sku.objects.filter(sku_code=code).first()
            if not s:
                raise CommandError(f"SKU {code} is not in the master.")
            lines.append((s, int(float(P.get(f"qty-{i}") or 0)), to_h(P.get(f"cost-{i}")) if P.get(f"cost-{i}") else None))
        d = P.get("date")
        at = timezone.make_aware(datetime.strptime(d, "%Y-%m-%d").replace(hour=9)) if d else None
        r = svc.create_rtv(request.user, P.get("rtv_no", ""), lines, reason=P.get("reason", "other"), fc_code=P.get("fc", ""),
                           requested_at=at, vendor_code=P.get("vendor_code", ""), note=P.get("note", ""))
        return htmx.done(request, f"Return {r.rtv_no} added", close_modal=True, open_drawer=f"/records/rtv/{r.rtv_no}/", drawer=False)
    from core.models import VendorCode
    return render(request, "dialogs/rtv_new.html", dict(reasons=RTV_REASONS, fcs=FulfilmentCentre.objects.all(), skus=Sku.objects.filter(active=True),
                                                         vcodes=VendorCode.objects.all(), today=timezone.localdate().isoformat(), rows=range(5)))


@require_POST
def authorise(request, no):
    svc.authorise(request.user, no, request.POST.get("version"))
    return htmx.done(request, f"Return {no} authorised")


@require_POST
def refuse(request, no):
    svc.refuse(request.user, no, request.POST.get("reason", ""), request.POST.get("version"))
    return htmx.done(request, f"Return {no} refused", "info")


@require_POST
def receive(request, no):
    P = request.POST
    counts = {k[2:]: (v, P.get(f"c-{k[2:]}")) for k, v in P.items() if k.startswith("r-")}
    r = svc.receive(request.user, no, counts, P.get("version"))
    return htmx.done(request, f"Return {no} received: SAR {r.received_h / 100:,.2f}")


@require_POST
def link(request, no):
    r = svc.link_deduction(request.user, no, request.POST.get("payment_no", ""))
    if r.status == "disputed":
        return htmx.done(request, f"Deduction is more than the goods received. Dispute {r.dispute_no} opened", "bad")
    return htmx.done(request, "Deduction matches the return. Accepted")
