"""Remittance matching, short payments and disputes (flow F4, rule R7)."""
import re
from datetime import timedelta

from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from billing.models import Invoice
from core.services import CommandError, audit, fmt_sar, next_number, notify, require
from rules import engine
from rules.services import get_cfg

from .models import Dispute, DisputeEvidence, Payment

COUNTED = ["matched", "short", "accepted", "disputed", "recovered"]


def norm(ref):
    return re.sub(r"[^A-Z0-9]", "", str(ref or "").upper())


def find_invoice(ref):
    inv = Invoice.objects.filter(invoice_no=ref).select_related("po").first()
    if inv:
        return inv
    n = norm(ref)
    for i in Invoice.objects.select_related("po").filter(po__stage="invoiced"):
        if norm(i.invoice_no) == n:
            return i
    return None


def match_payment(p, at=None, cfg=None):
    """Link a payment to its invoice and apply R7. Saves the payment."""
    cfg = cfg or get_cfg()
    inv = find_invoice(p.invoice_ref)
    if not inv:
        p.status, p.invoice, p.po = "unmatched", None, None
        p.save()
        return p
    p.invoice, p.po = inv, inv.po
    p.invoice_ref = inv.invoice_no
    already = Payment.objects.filter(invoice=inv, status__in=COUNTED).exclude(pk=p.pk).aggregate(s=Sum("paid_h"))["s"] or 0
    due = inv.total_h - already
    if not p.deduction_h:
        p.deduction_h = max(0, due - p.paid_h)
    if engine.payment_match(p.paid_h, due, cfg) == "matched":
        p.status, p.deduction_h = "matched", 0
        po = inv.po
        po.stage, po.paid_at = "paid", p.remit_date
        po.bump()
        po.save()
        audit("po", po.po_no, f"Payment {p.payment_no} matched: {fmt_sar(p.paid_h)}", name="Auto-match", system=True,
              action="payment", at=at)
    else:
        p.status = "short"
        audit("po", inv.po.po_no, f"Payment {p.payment_no} short by {fmt_sar(due - p.paid_h)}"
              + (f" ({p.reason})" if p.reason else ""), name="Auto-match", system=True, action="payment", at=at)
    p.save()
    return p


def import_payment(payment_no, remit_date, invoice_ref, paid_h, deduction_h=0, reason="", at=None):
    p = Payment.objects.create(payment_no=payment_no, remit_date=remit_date, invoice_ref=invoice_ref, paid_h=paid_h,
                               deduction_h=deduction_h or 0, reason=reason or "")
    match_payment(p, at)
    if p.status == "short":
        notify(f"Short payment on {p.invoice_ref}: {fmt_sar(p.deduction_h)}", "bad", ("po", p.po.po_no, "invoice"))
    return p


@transaction.atomic
def auto_match(user):
    require(user, "dispute")
    open_inv = list(Invoice.objects.filter(po__stage="invoiced").select_related("po"))
    n = 0
    for p in Payment.objects.select_for_update().filter(status="unmatched"):
        g = (next((i for i in open_inv if i.invoice_no == p.hint), None)
             or next((i for i in open_inv if norm(i.invoice_no).endswith(norm(p.invoice_ref)[-5:]) and i.total_h == p.paid_h), None)
             or next((i for i in open_inv if i.total_h == p.paid_h), None))
        if g:
            p.invoice_ref = g.invoice_no
            match_payment(p)
            n += 1
    return n


@transaction.atomic
def manual_match(user, payment_no, invoice_no):
    require(user, "dispute")
    p = Payment.objects.select_for_update().get(payment_no=payment_no)
    if p.status != "unmatched":
        raise CommandError("This payment is already matched.")
    p.invoice_ref = invoice_no
    match_payment(p)
    if p.status == "unmatched":
        raise CommandError(f"Invoice {invoice_no} was not found.")
    audit("po", p.po.po_no, f"Payment {p.payment_no} matched by hand to {invoice_no}", user, action="match")
    return p


@transaction.atomic
def open_dispute(user, payment_no, dtype, amount_h, note, files=()):
    require(user, "dispute")
    p = Payment.objects.select_for_update().get(payment_no=payment_no)
    if p.status != "short":
        raise CommandError("Only short payments can be disputed.")
    d = Dispute.objects.create(case_no=f"DSP-{next_number('dispute', 41):04d}", type=dtype, ref=p.payment_no, po=p.po,
                               amount_h=amount_h or p.deduction_h, due=timezone.now() + timedelta(days=14), note=note)
    for f in files:
        DisputeEvidence.objects.create(dispute=d, filename=f.name, file=f)
    p.status = "disputed"
    p.save()
    audit("dispute", d.case_no, "Dispute opened", user, action="open")
    audit("po", p.po.po_no, f"Dispute {d.case_no} opened for {fmt_sar(d.amount_h)}", user, action="dispute")
    return d


@transaction.atomic
def accept_deduction(user, payment_no, reason):
    require(user, "dispute")
    p = Payment.objects.select_for_update().get(payment_no=payment_no)
    if p.status != "short":
        raise CommandError("Only short payments can be accepted.")
    p.status = "accepted"
    p.save()
    po = p.po
    po.stage, po.paid_at = "paid", timezone.now()
    po.bump()
    po.save()
    audit("po", po.po_no, f"Deduction of {fmt_sar(p.deduction_h)} accepted: {reason}", user, action="accept_deduction",
          reason=reason)
    return p


@transaction.atomic
def link_to_dn(user, payment_no, dn_no):
    require(user, "dispute")
    p = Payment.objects.select_for_update().get(payment_no=payment_no)
    p.status = "accepted"
    p.reason = f"{p.reason} · linked to {dn_no}".strip(" ·")
    p.save()
    po = p.po
    po.stage, po.paid_at = "paid", timezone.now()
    po.bump()
    po.save()
    audit("po", po.po_no, f"Deduction {fmt_sar(p.deduction_h)} linked to debit note {dn_no}", user, action="link_dn")
    return p


@transaction.atomic
def set_dispute_status(user, case_no, status):
    require(user, "dispute")
    d = Dispute.objects.select_for_update().get(case_no=case_no)
    allowed = {"open": ["submitted"], "submitted": ["won", "lost"]}
    if status not in allowed.get(d.status, []):
        raise CommandError("That status change is not allowed.")
    d.status = status
    d.bump()
    d.save()
    audit("dispute", d.case_no, {"submitted": "Submitted to Amazon", "won": f"Marked won. {fmt_sar(d.amount_h)} recovered",
                                 "lost": "Marked lost"}[status], user, action=status)
    p = Payment.objects.filter(payment_no=d.ref).first()
    if p and status in ("won", "lost"):
        p.status = "recovered" if status == "won" else "accepted"
        p.save()
        if p.po:
            po = p.po
            po.stage, po.paid_at = "paid", timezone.now()
            po.bump()
            po.save()
            audit("po", po.po_no, f"Dispute {d.case_no} {status}", user, action="dispute_" + status)
    return d
