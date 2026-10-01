"""Returns to vendor (RTV): authorise, receive, and check Amazon's deduction against what came back."""
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from catalog.models import FulfilmentCentre, agreed_cost_h
from core.services import CommandError, audit, check_version, fmt_sar, notify, require
from rules.services import get_cfg

from .models import CONDITIONS, RTV_REASONS, ReturnAuth


def get_rtv(no, lock=False):
    qs = ReturnAuth.objects.select_for_update() if lock else ReturnAuth.objects
    try:
        return qs.get(rtv_no=no)
    except ReturnAuth.DoesNotExist:
        raise CommandError(f"Return {no} was not found.")


def create_rtv(user, rtv_no, lines, *, reason="defective", fc_code="", requested_at=None, vendor_code="", note="",
               source="the hub", name=None):
    """lines: [(sku, qty, unit_cost_h or None)]; a missing cost is the agreed cost on the request date."""
    if user is not None:
        require(user, "upload")
    rtv_no = (rtv_no or "").strip()
    if not rtv_no:
        raise CommandError("Enter Amazon's return number.")
    if ReturnAuth.objects.filter(rtv_no=rtv_no).exists():
        raise CommandError(f"Return {rtv_no} is already in the hub.")
    if not lines or any(q <= 0 for _, q, _ in lines):
        raise CommandError("Add at least one model with a quantity above 0.")
    at = requested_at or timezone.now()
    r = ReturnAuth.objects.create(rtv_no=rtv_no, reason=reason if reason in dict(RTV_REASONS) else "other", requested_at=at,
                                  fc=FulfilmentCentre.objects.filter(code=fc_code).first() if fc_code else None,
                                  vendor_code=(vendor_code or "").upper()[:12], note=(note or "")[:200])
    for sku, q, cost in lines:
        r.lines.create(sku=sku, qty=q, unit_cost_h=cost if cost else agreed_cost_h(sku, at))
    audit("rtv", r.rtv_no, f"Return request from Amazon via {source}: {sum(q for _, q, _ in lines):,} units, {fmt_sar(r.amount_h)}",
          user, name=name, system=user is None and name is None, action="import", at=at)
    notify(f"Amazon asks to return {sum(q for _, q, _ in lines):,} units ({r.rtv_no})", "warn", ("rtv", r.rtv_no, ""), roles=["PIC", "Finance"])
    return r


@transaction.atomic
def authorise(user, no, version=None):
    require(user, "dispute")
    r = get_rtv(no, lock=True)
    check_version(r, version)
    if r.status != "requested":
        raise CommandError("Only a requested return can be authorised.")
    r.status, r.authorised_at = "authorised", timezone.now()
    r.bump()
    r.save()
    audit("rtv", r.rtv_no, "Return authorised. Warehouse to expect the goods", user, action="authorise")
    return r


@transaction.atomic
def refuse(user, no, reason, version=None):
    require(user, "dispute")
    r = get_rtv(no, lock=True)
    check_version(r, version)
    if r.status != "requested":
        raise CommandError("Only a requested return can be refused.")
    reason = (reason or "").strip()[:200]
    if not reason:
        raise CommandError("Say why, e.g. not returnable under the agreement.")
    r.status, r.note = "refused", reason
    r.bump()
    r.save()
    audit("rtv", r.rtv_no, f"Return refused: {reason}", user, action="refuse", reason=reason)
    return r


@transaction.atomic
def receive(user, no, counts, version=None):
    """counts: {line id: (qty received, condition)}. Shortages and damage are kept for the deduction check."""
    require(user, "ship")
    r = get_rtv(no, lock=True)
    check_version(r, version)
    if r.status != "authorised":
        raise CommandError("Receive a return once it is authorised.")
    short = 0
    for l in r.lines.select_related("sku"):
        q, cond = counts.get(str(l.pk), (None, None))
        q = l.qty if q in (None, "") else max(0, int(q))
        cond = cond if cond in dict(CONDITIONS) else "good"
        if q == 0:
            cond = "missing"
        l.qty_received, l.condition = q, cond
        l.save()
        short += max(0, l.qty - q)
    r.status, r.received_at = "received", timezone.now()
    r.bump()
    r.save()
    audit("rtv", r.rtv_no, f"Goods received: {fmt_sar(r.received_h)} of {fmt_sar(r.amount_h)}"
          + (f", {short:,} units short" if short else ""), user, action="receive")
    return r


def candidates(r):
    """Short payments whose deduction could be this return, closest amount first."""
    from payments.models import Payment
    ps = list(Payment.objects.filter(status="short").select_related("po"))
    target = r.received_h or r.amount_h
    ps.sort(key=lambda p: (0 if "return" in (p.reason or "").lower() or "rtv" in (p.reason or "").lower() else 1, abs(p.deduction_h - target)))
    return ps


@transaction.atomic
def link_deduction(user, no, payment_no):
    """Match Amazon's deduction to the received return. Up to the value received it is accepted; anything above is
    disputed (units Amazon charged for that never came back)."""
    from payments.models import Payment
    from payments.services import open_dispute, settle_po
    require(user, "dispute")
    r = get_rtv(no, lock=True)
    if r.status != "received":
        raise CommandError("Match the deduction once the goods are received.")
    p = Payment.objects.select_for_update().filter(payment_no=payment_no, status="short").first()
    if not p:
        raise CommandError(f"{payment_no} is not a short payment.")
    tol = get_cfg().tol_h()
    over = p.deduction_h - r.received_h
    r.payment_no = p.payment_no
    if over <= tol:
        p.status = "accepted"
        p.reason = f"{p.reason} · return {r.rtv_no}".strip(" ·")
        p.save()
        if p.po:
            settle_po(p.po)
        r.status = "credited"
        text = f"Deduction {fmt_sar(p.deduction_h)} on {p.payment_no} matches the goods received. Accepted"
    else:
        d = open_dispute(user, p.payment_no, "returns", over,
                         f"Return {r.rtv_no}: Amazon deducted {fmt_sar(p.deduction_h)} but goods worth {fmt_sar(r.received_h)} came back. "
                         f"Please reverse {fmt_sar(over)}.")
        r.status, r.dispute_no = "disputed", d.case_no
        text = f"Deduction {fmt_sar(p.deduction_h)} on {p.payment_no} is {fmt_sar(over)} more than received. Dispute {d.case_no} opened"
    r.bump()
    r.save()
    audit("rtv", r.rtv_no, text, user, action="link_deduction")
    if p.po:
        audit("po", p.po.po_no, f"Deduction on {p.payment_no} linked to return {r.rtv_no}", user, action="rtv")
    return r


def due_for(r):
    from core.workcal import add_working_days
    if r.status == "requested":
        return add_working_days(r.requested_at, 2)
    if r.status == "authorised":
        return (r.authorised_at or r.requested_at) + timedelta(days=14)
    return add_working_days(r.received_at or r.requested_at, 10)
