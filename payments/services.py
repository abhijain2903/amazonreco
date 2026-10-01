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


def recovery_for(p, inv=None):
    """Is this payment Amazon paying back a disputed deduction? It is when it names the dispute (our case number or
    Amazon's case ID) or pays an invoice whose deduction is in an open dispute, for no more than the disputed amount."""
    cfg = get_cfg()
    text = norm(f"{p.invoice_ref} {p.reason}")
    open_d = Dispute.objects.filter(recovered_in="", po__isnull=False).exclude(status="lost")
    for d in open_d:
        if norm(d.case_no) in text or (d.amazon_case_id and norm(d.amazon_case_id) in text):
            return d
    if inv:
        refs = Payment.objects.filter(invoice=inv).exclude(pk=p.pk).values_list("payment_no", flat=True)
        d = open_d.filter(ref__in=list(refs)).first()
        if d and p.paid_h <= d.amount_h + cfg.tol_h():
            return d
    return None


def _apply_recovery(p, d, at=None):
    inv = Payment.objects.get(payment_no=d.ref).invoice
    p.invoice, p.po, p.invoice_ref = inv, d.po, inv.invoice_no if inv else p.invoice_ref
    p.status, p.deduction_h, p.hint = "matched", 0, f"Recovery for {d.case_no}"[:40]
    p.save()
    part = p.paid_h < d.amount_h
    d.status, d.recovered_h, d.recovered_in = "won", min(p.paid_h, d.amount_h), p.payment_no
    d.bump()
    d.save()
    Payment.objects.filter(payment_no=d.ref).update(status="recovered")
    po = d.po
    po.stage, po.paid_at = "paid", p.remit_date
    po.bump()
    po.save()
    audit("dispute", d.case_no, f"Amazon paid back {fmt_sar(p.paid_h)} in payment {p.payment_no}" + (f" (part of {fmt_sar(d.amount_h)})" if part else ""),
          name="Auto-match", system=True, action="recovered", at=at)
    audit("po", po.po_no, f"Dispute {d.case_no} recovered: {fmt_sar(p.paid_h)} in payment {p.payment_no}", name="Auto-match",
          system=True, action="payment", at=at)
    return p


def match_payment(p, at=None, cfg=None):
    """Link a payment to its invoice and apply R7. Saves the payment. A payment that pays back a disputed deduction
    is linked to its dispute, which closes as won."""
    cfg = cfg or get_cfg()
    inv = find_invoice(p.invoice_ref)
    d = recovery_for(p, inv)
    if d:
        return _apply_recovery(p, d, at)
    if not inv:
        p.status, p.invoice, p.po = "unmatched", None, None
        p.save()
        return p
    p.invoice, p.po = inv, inv.po
    p.invoice_ref = inv.invoice_no
    already = Payment.objects.filter(invoice=inv, status__in=COUNTED).exclude(pk=p.pk).aggregate(s=Sum("paid_h"))["s"] or 0
    due = inv.net_due_h - already
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


AUTO_MATCH_MIN = 85  # "Run auto-match": apply a single-invoice suggestion at least this good ...
AUTO_MATCH_MARGIN = 10  # ... and clearly better than the next candidate. Everything else stays a suggestion.


@transaction.atomic
def auto_match(user):
    """Match unmatched payments whose best suggestion is confident and unambiguous. An invoice used in this run is
    not offered to the next payment, so one invoice is never matched twice."""
    require(user, "dispute")
    from matching.matchers import payment_invoice
    used, n = set(), 0
    for p in Payment.objects.select_for_update().filter(status="unmatched"):
        c = payment_invoice(p, exclude=used)
        if not c or len(c[0]["targets"]) != 1 or c[0]["score"] < AUTO_MATCH_MIN:
            continue
        if len(c) > 1 and c[0]["score"] - c[1]["score"] < AUTO_MATCH_MARGIN:
            continue
        inv_no = c[0]["targets"][0]
        p.invoice_ref = inv_no
        match_payment(p)
        audit("po", p.po.po_no, f"Payment {p.payment_no} auto-matched to {inv_no} (score {c[0]['score']}: {', '.join(c[0]['reasons'])})",
              user, action="match")
        used.add(inv_no)
        n += 1
    return n


@transaction.atomic
def split_payment(user, payment_no, invoice_nos, version=None, note=""):
    """One remittance line that pays several invoices: allocate it invoice by invoice (each gets what is still due;
    the last gets the remainder) and match each part. The first part keeps the payment number; the others are
    numbered <payment>/2, /3 …"""
    require(user, "dispute")
    from core.services import check_version
    p = Payment.objects.select_for_update().get(payment_no=payment_no)
    check_version(p, version)
    if p.status != "unmatched":
        raise CommandError("This payment is already matched.")
    invs = [find_invoice(n) for n in invoice_nos]
    if len(invs) < 2 or not all(invs) or len({i.pk for i in invs}) != len(invs):
        raise CommandError("Pick two or more different open invoices.")
    remaining, parts = p.paid_h, []
    for k, inv in enumerate(invs):
        already = Payment.objects.filter(invoice=inv, status__in=COUNTED).aggregate(s=Sum("paid_h"))["s"] or 0
        amt = remaining if k == len(invs) - 1 else min(remaining, inv.total_h - already)
        remaining -= amt
        if k == 0:
            part = p
            part.paid_h, part.invoice_ref = amt, inv.invoice_no
        else:
            part = Payment(payment_no=f"{p.payment_no}/{k + 1}", remit_date=p.remit_date, invoice_ref=inv.invoice_no, paid_h=amt,
                           reason=p.reason)
        part.bump()
        match_payment(part)
        parts.append(part)
    for part in parts:
        audit("po", part.po.po_no, f"Payment {p.payment_no} split across {len(parts)} invoices: {fmt_sar(part.paid_h)} "
              f"to {part.invoice_ref}" + (f" ({note})" if note else ""), user, action="match")
    return parts


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
def open_dispute(user, payment_no, dtype, amount_h, note, files=(), subtype=""):
    from .models import CHARGEBACK_TYPES, DISPUTE_TYPES
    require(user, "dispute")
    p = Payment.objects.select_for_update().get(payment_no=payment_no)
    if p.status != "short":
        raise CommandError("Only short payments can be disputed.")
    if dtype not in dict(DISPUTE_TYPES):
        dtype = "other"
    subtype = subtype if dtype == "chargeback" and subtype in dict(CHARGEBACK_TYPES) else ""
    d = Dispute.objects.create(case_no=f"DSP-{next_number('dispute', 41):04d}", type=dtype, subtype=subtype, ref=p.payment_no, po=p.po,
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
    from debitnotes.models import DebitNote
    p = Payment.objects.select_for_update().get(payment_no=payment_no)
    if p.status != "short":
        raise CommandError("Only short payments can be linked to a debit note.")
    dn = DebitNote.objects.filter(dn_no=dn_no, validated=True).first()
    if not dn:
        raise CommandError(f"Debit note {dn_no} is not a validated debit note.")
    if Payment.objects.exclude(pk=p.pk).filter(reason__contains=f"linked to {dn_no}").exists():
        raise CommandError(f"Debit note {dn_no} is already linked to another deduction.")
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
def set_dispute_status(user, case_no, status, recovered_h=None, case_id=None):
    """Open → submitted (with Amazon's case ID) → won (all or part of the amount) or lost."""
    require(user, "dispute")
    d = Dispute.objects.select_for_update().get(case_no=case_no)
    allowed = {"open": ["submitted"], "submitted": ["won", "lost"]}
    if status not in allowed.get(d.status, []):
        raise CommandError("That status change is not allowed.")
    if case_id:
        d.amazon_case_id = case_id.strip()[:40]
    if status == "won":
        recovered_h = d.amount_h if recovered_h in (None, "") else int(recovered_h)
        if not 0 < recovered_h <= d.amount_h:
            raise CommandError(f"The amount recovered must be above 0 and at most {fmt_sar(d.amount_h)}.")
        d.recovered_h = recovered_h
    d.status = status
    d.bump()
    d.save()
    part = status == "won" and d.recovered_h < d.amount_h
    audit("dispute", d.case_no, {"submitted": "Submitted to Amazon" + (f" (case {d.amazon_case_id})" if d.amazon_case_id else ""),
                                 "won": f"Marked won. {fmt_sar(d.recovered_h)} recovered" + (f" of {fmt_sar(d.amount_h)}" if part else ""),
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
