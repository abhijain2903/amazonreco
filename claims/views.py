from datetime import datetime

from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.http import require_POST

from core import htmx
from core.services import next_number, peek_number, to_h
from debitnotes.models import DebitNote
from promotions.models import Promotion
from promotions.services import stage_of
from rules.services import get_cfg

from . import services as svc
from .models import Claim

TABS = [("toclaim", "To claim"), ("sent", "Waiting for CN"), ("shortfall", "Shortfall"), ("closed", "Closed")]


def claim_list(request):
    tab = htmx.pick(request, "tab", [k for k, _ in TABS], "toclaim")
    to_claim = []
    for p in Promotion.objects.filter(stage="dn_validated"):
        if stage_of(p) == "dn_validated":
            p.dn = DebitNote.objects.filter(agreement_no=p.agreement_no, validated=True).first()
            to_claim.append(p)
    C = list(Claim.objects.select_related("promotion"))
    sent = [c for c in C if c.status == "sent"]
    sh = [c for c in C if c.status == "shortfall"]
    cl = [c for c in C if c.status in ("closed", "written_off")]
    tol = get_cfg().tol_h()
    for c in C:
        c.gap_bad = c.gap_h is not None and c.gap_h > tol
    sar = lambda h: f"{round(h / 100):,}"
    kpis = [dict(l="To claim", v=len(to_claim), s="Validated debit notes", url="?tab=toclaim"),
            dict(l="Waiting for CN", v=sar(sum(c.amount_h for c in sent)), s=f"SAR · {len(sent)} claims", url="?tab=sent"),
            dict(l="CN shortfall", v=sar(sum(c.gap_h for c in sh)), s=f"SAR · {len(sh)} claims", url="?tab=shortfall", alert=bool(sh)),
            dict(l="Recovered, all time", v=sar(sum(c.cn_h or 0 for c in cl)), s="SAR from credit notes", url="?tab=closed")]
    lists = {"sent": sent, "shortfall": sh, "closed": cl}
    rows = sorted(lists.get(tab, []), key=lambda c: c.sent_at, reverse=True)
    from core.exports import sar, wants_export, xlsx
    if wants_export(request):
        if tab == "toclaim":
            return xlsx("Claims_to_claim", ["MECL ref", "Promotion", "Category", "DN", "Claim SAR"],
                        [[p.mecl_ref, p.name, p.category, p.dn.dn_no if p.dn else "", sar(p.dn.approved_h) if p.dn else None] for p in to_claim])
        return xlsx(f"Claims_{tab}", ["Claim", "MECL ref", "Category", "Sent", "Claim SAR", "Credit note", "CN SAR", "Gap SAR", "Status"],
                    [[c.claim_no, c.promotion.mecl_ref, c.promotion.category, c.sent_at, sar(c.amount_h), c.cn_no, sar(c.cn_h), sar(c.gap_h),
                      c.get_status_display()] for c in rows])
    counts = {"toclaim": len(to_claim), "sent": len(sent), "shortfall": len(sh), "closed": len(cl)}
    return render(request, "pages/claims.html", dict(tab=tab, kpis=kpis, to_claim=to_claim, rows=rows,
                  tabs=[dict(id=k, label=l, count=counts[k]) for k, l in TABS]))


def cn(request, claim_no):
    c = svc.get_claim(claim_no)
    if request.method == "POST":
        d = request.POST.get("date")
        when = timezone.make_aware(datetime.strptime(d, "%Y-%m-%d").replace(hour=10)) if d else None
        amount = request.POST.get("amount")
        cn_no = request.POST.get("cn_no", "").strip()
        c = svc.record_cn(request.user, claim_no, cn_no, to_h(amount) if amount not in (None, "") else None, when)
        if cn_no == f"CN-{peek_number('credit_note', 552010)}":
            next_number("credit_note", 552010)  # the suggested number was used; move the counter on
        if c.status == "closed":
            return htmx.done(request, f"Credit note matches. {c.promotion.mecl_ref} closed", close_modal=True)
        return htmx.done(request, f"Credit note is {c.gap_h / 100:,.0f} SAR short. Flagged for follow-up", "bad", close_modal=True)
    left = c.amount_h - (c.cn_h or 0)
    return render(request, "dialogs/cn.html", dict(c=c, left_h=max(left, 0), next_cn=f"CN-{peek_number('credit_note', 552010)}", today=timezone.localdate().isoformat(),
                                                    tol=get_cfg().tol_h()))


@require_POST
def chase(request, claim_no):
    svc.chase(request.user, claim_no)
    return htmx.done(request, "Chase logged on the timeline")


@require_POST
def write_off(request, claim_no):
    svc.write_off(request.user, claim_no)
    return htmx.done(request, "Closed with write-off", "info")
