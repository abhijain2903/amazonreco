from datetime import timedelta

from django.db.models import Sum
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from billing.models import Invoice
from core import htmx
from core.services import timeline, to_h
from debitnotes.models import DebitNote

from . import services as svc
from .models import DISPUTE_TYPES, Dispute, Payment

TABS = [("short", "Short-paid"), ("match", "To match"), ("matched", "Matched"), ("disputes", "Disputes")]


def pay_list(request):
    tab = htmx.pick(request, "tab", [k for k, _ in TABS], "short")
    now = timezone.now()
    P = Payment.objects.select_related("invoice", "po")
    short = list(P.filter(status="short"))
    unm = list(P.filter(status="unmatched"))
    matched = list(P.filter(status__in=["matched", "accepted", "recovered"]).order_by("-remit_date"))
    disputes = list(Dispute.objects.select_related("po", "promotion").order_by("-created_at"))
    paid30 = [p for p in P.filter(status__in=["matched", "recovered"], remit_date__gt=now - timedelta(days=30))]
    open_d = [d for d in disputes if d.status in ("open", "submitted")]
    if tab == "short":
        from matching.views import deduction_for
        for p in short:
            p.ded = deduction_for(p)
    kpis = [dict(l="Paid, last 30 days", v=f"{round(sum(p.paid_h for p in paid30) / 100):,}", s=f"SAR · {len(paid30)} payments", url="?tab=matched"),
            dict(l="Short-paid", v=f"{round(sum(p.deduction_h for p in short) / 100):,}", s=f"SAR · {len(short)} to resolve", url="?tab=short", alert=bool(short)),
            dict(l="To match", v=len(unm), s="Payments without an invoice", url="?tab=match"),
            dict(l="Open disputes", v=f"{round(sum(d.amount_h for d in open_d) / 100):,}", s=f"SAR · {len(open_d)} case{'' if len(open_d) == 1 else 's'}", url="?tab=disputes")]
    counts = {"short": len(short), "match": len(unm), "matched": len(matched), "disputes": len(disputes)}
    return render(request, "pages/pay.html", dict(tab=tab, kpis=kpis, short=short, unm=unm, matched=matched, disputes=disputes,
                  tabs=[dict(id=k, label=l, count=counts[k]) for k, l in TABS]))


@require_POST
def automatch(request):
    n = svc.auto_match(request.user)
    return htmx.done(request, f"{n} payment{'s' if n != 1 else ''} matched" if n else "No confident matches. Match the rest by hand", "ok" if n else "info")


@require_POST
def match(request, pay_no):
    p = svc.manual_match(request.user, pay_no, request.POST.get("invoice_no", ""))
    return htmx.done(request, f"{p.payment_no} matched to {p.invoice_ref}" + (" · short-paid" if p.status == "short" else ""),
                     "info" if p.status == "short" else "ok", open_drawer=f"/records/po/{p.po.po_no}/?tab=invoice")


def dispute(request, pay_no):
    p = get_object_or_404(Payment, payment_no=pay_no)
    if request.method == "POST":
        d = svc.open_dispute(request.user, pay_no, request.POST.get("type", "other"), to_h(request.POST.get("amount") or 0),
                             request.POST.get("note", ""), request.FILES.getlist("evidence"))
        return htmx.done(request, f"Dispute {d.case_no} opened", close_modal=True)
    from matching.views import deduction_for
    s = deduction_for(p)
    guess = s.targets[0] if s.targets and s.targets[0] in dict(DISPUTE_TYPES) else "other"
    return render(request, "dialogs/dispute.html", dict(p=p, types=DISPUTE_TYPES, guess=guess, s=s))


def accept(request, pay_no):
    p = get_object_or_404(Payment, payment_no=pay_no)
    if request.method == "POST":
        svc.accept_deduction(request.user, pay_no, request.POST.get("reason", ""))
        return htmx.done(request, "Deduction accepted. Invoice closed", close_modal=True)
    return render(request, "dialogs/accept.html", dict(p=p))


def link_dn(request, pay_no):
    p = get_object_or_404(Payment, payment_no=pay_no)
    if request.method == "POST":
        svc.link_to_dn(request.user, pay_no, request.POST.get("dn_no"))
        return htmx.done(request, "Deduction linked to debit note", close_modal=True)
    return render(request, "dialogs/link_dn.html", dict(p=p, dns=DebitNote.objects.filter(validated=True).order_by("-dn_date")[:50]))


@require_POST
def dispute_status(request, case_no, status):
    d = svc.set_dispute_status(request.user, case_no, status)
    return htmx.done(request, f"Dispute {d.case_no}: {d.get_status_display().lower()}")


def payment_drawer(request, key):
    p = get_object_or_404(Payment, payment_no=key)
    if p.status != "unmatched" and p.po:
        from orders.views import drawer
        request.GET = request.GET.copy()
        request.GET.setdefault("tab", "invoice")
        return drawer(request, p.po.po_no)
    from matching.ai import available
    from matching.views import suggestions_for
    open_inv = list(Invoice.objects.filter(po__stage="invoiced").select_related("po"))
    return render(request, "records/payment.html", dict(p=p, invoices=open_inv, url=request.get_full_path(),
                  sugs=suggestions_for("pay_inv", p), kind="pay_inv", source=p.payment_no, perm_name="dispute", ai_on=available()))


def dispute_drawer(request, key):
    d = get_object_or_404(Dispute, case_no=key)
    return render(request, "records/dispute.html", dict(d=d, events=timeline("dispute", d.case_no), url=request.get_full_path()))
