from django.shortcuts import render
from django.views.decorators.http import require_POST

from core import htmx
from core.services import timeline
from promotions.models import Promotion
from promotions.services import stage_of
from rules.services import get_cfg

from . import services as svc
from .models import DebitNote

TABS = [("todo", "To validate", "to_validate"), ("mismatch", "Mismatch", "mismatch"), ("unlinked", "Unlinked", "unlinked"),
        ("validated", "Approved", "validated"), ("disputed", "Disputed", "disputed")]
TONE = {"to_validate": ("Matches", "ok"), "mismatch": ("Mismatch", "bad"), "unlinked": ("Unlinked", "warn"),
        "validated": ("Approved", "ok"), "disputed": ("Part disputed", "info")}


def dn_list(request):
    tab = htmx.pick(request, "tab", [k for k, _, _ in TABS], "todo")
    cfg = get_cfg()
    tol = cfg.tol_h()
    E = [(d, svc.evaluate(d, cfg)) for d in DebitNote.objects.prefetch_related("lines__sku")]
    status = dict((k, s) for k, _, s in TABS).get(tab, "to_validate")
    rows = []
    for d, e in E:
        if e["status"] != status:
            continue
        rows.append(dict(d=d, e=e, label=TONE[e["status"]][0], tone=TONE[e["status"]][1],
                         var_bad=e["variance_h"] is not None and abs(e["variance_h"]) > tol))
    rows.sort(key=lambda r: r["d"].dn_date, reverse=True)
    counts = {s: sum(1 for _, e in E if e["status"] == s) for _, _, s in TABS}
    return render(request, "pages/dns.html", dict(tab=tab, rows=rows, tabs=[dict(id=k, label=l, count=counts[s]) for k, l, s in TABS]))


def _similar(a, b):
    a, b = str(a), str(b)
    return 9 if len(a) != len(b) else sum(1 for x, y in zip(a, b) if x != y)


def drawer(request, key):
    dn = svc.get_dn(key)
    ev = svc.evaluate(dn)
    if ev["promo"]:
        from promotions.views import drawer as promo_drawer
        request.GET = request.GET.copy()
        request.GET.setdefault("tab", "dn")
        return promo_drawer(request, ev["promo"].mecl_ref)
    skus = {l["sku"] for l in ev["lines"]}
    cands = []
    for p in Promotion.objects.exclude(agreement_no=None).prefetch_related("lines"):
        if stage_of(p) not in ("waiting_dn", "dn_overdue"):
            continue
        m = skus <= {l.sku_id for l in p.lines.all()}
        cands.append(dict(p=p, d=_similar(p.agreement_no, dn.agreement_no), m=m))
    cands.sort(key=lambda c: (not c["m"], c["d"]))
    return render(request, "records/dn.html", dict(dn=dn, ev=ev, cands=cands, events=timeline("dn", dn.dn_no), url=request.get_full_path()))


@require_POST
def approve(request, dn_no):
    svc.approve(request.user, dn_no, request.POST.get("version"))
    return htmx.done(request, f"DN {dn_no} approved")


def override(request, dn_no):
    dn = svc.get_dn(dn_no)
    if request.method == "POST":
        svc.approve_override(request.user, dn_no, request.POST.get("reason", ""), request.POST.get("version"))
        return htmx.done(request, f"DN {dn_no} approved with override", close_modal=True)
    return render(request, "dialogs/override.html", dict(dn=dn, ev=svc.evaluate(dn)))


@require_POST
def dispute(request, dn_no):
    d = svc.dispute(request.user, dn_no, request.POST.get("version"))
    return htmx.done(request, f"Expected amount approved. Dispute {d.case_no} opened for {d.amount_h / 100:,.0f} SAR", "info")


@require_POST
def link(request, dn_no):
    p = svc.link(request.user, dn_no, request.POST.get("ref", ""), request.POST.get("version"))
    svc.notify_status(svc.get_dn(dn_no))
    return htmx.done(request, f"DN {dn_no} linked to {p.mecl_ref}", open_drawer=f"/records/promo/{p.mecl_ref}/?tab=dn", drawer=False)
