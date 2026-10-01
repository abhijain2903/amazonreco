"""Sell-out claims and credit notes (flow F7, rule R11)."""
from django.db import transaction
from django.utils import timezone

from core.services import CommandError, audit, fmt_sar, next_number, notify, require, save_file
from debitnotes.models import DebitNote
from promotions.services import get_promo, stage_of
from rules import engine
from rules.services import get_cfg

from .models import Claim


def get_claim(no, lock=False):
    qs = Claim.objects.select_for_update() if lock else Claim.objects
    try:
        return qs.select_related("promotion", "debit_note").get(claim_no=no)
    except Claim.DoesNotExist:
        raise CommandError(f"Claim {no} was not found.")


def _make_claim(p, at, user=None, name=None):
    dn = DebitNote.objects.filter(agreement_no=p.agreement_no, validated=True).first()
    c = Claim.objects.create(claim_no=f"CLM-2026-{next_number('claim', 88):04d}", promotion=p, debit_note=dn,
                             amount_h=dn.approved_h, sent_at=at)
    p.stage = "claimed"
    p.bump()
    p.save()
    audit("promotion", p.mecl_ref, f"Claim {c.claim_no} for {fmt_sar(c.amount_h)} sent to the product team in sell-out format",
          user, name=name, action="claim", at=at)
    audit("claim", c.claim_no, "Claim sent to product team", user, name=name, action="send", at=at)
    return c


@transaction.atomic
def generate_claim(user, ref):
    require(user, "promo")
    p = get_promo(ref, lock=True)
    if stage_of(p) != "dn_validated":
        raise CommandError("A claim is created after the debit note is validated.")
    c = _make_claim(p, timezone.now(), user)
    dn = c.debit_note
    rows = [["claim_no", "mecl_ref", "agreement_no", "dn_no", "model", "units", "rate_sar", "amount_sar"]] + [
        [c.claim_no, p.mecl_ref, p.agreement_no, dn.dn_no, l.sku.model_no, l.units, f"{l.rate_h / 100:.2f}",
         f"{l.units * l.rate_h / 100:.2f}"] for l in dn.lines.select_related("sku")]
    return c, save_file("claim", f"Claim_{c.claim_no}.csv", rows, "promotion", p.mecl_ref)


def _record_cn(c, cn_no, cn_h, cn_date, at, user=None, name=None):
    """Adds a credit note; the claim compares the total of all its credit notes with the claim (R11)."""
    from .models import CreditNote
    if c.credit_notes.filter(cn_no=cn_no).exists():
        raise CommandError(f"Credit note {cn_no} is already recorded on {c.claim_no}.")
    CreditNote.objects.create(claim=c, cn_no=cn_no, amount_h=cn_h, cn_date=cn_date)
    total = sum(c.credit_notes.values_list("amount_h", flat=True))
    several = c.credit_notes.count() > 1
    c.cn_no, c.cn_h, c.cn_date = cn_no, total, cn_date
    p = c.promotion
    what = f"Credit notes now total {fmt_sar(total)}" if several else f"Credit note {cn_no} for {fmt_sar(cn_h)}"
    if engine.cn_check(c.amount_h, total, get_cfg()) == "closed":
        c.status, p.stage = "closed", "closed"
        text = f"{what}, matching the claim. Promotion closed"
    else:
        c.status, p.stage = "shortfall", "cn_shortfall"
        text = (f"{what}, {fmt_sar(c.amount_h - total)} short of the claim" if total < c.amount_h
                else f"{what}, {fmt_sar(total - c.amount_h)} more than the claim")
    c.bump()
    c.save()
    p.bump()
    p.save()
    audit("promotion", p.mecl_ref, text, user, name=name, action="cn", at=at)
    audit("claim", c.claim_no, f"Credit note {cn_no} recorded: {fmt_sar(cn_h)}", user, name=name, action="cn", at=at)
    return c


@transaction.atomic
def record_cn(user, claim_no, cn_no, cn_h, cn_date=None):
    require(user, "cn")
    c = get_claim(claim_no, lock=True)
    if c.status not in ("sent", "shortfall"):
        raise CommandError("This claim is closed. No more credit notes can be added.")
    if not (cn_no or "").strip() or cn_h is None or cn_h < 0:
        raise CommandError("Enter the credit note number and amount.")
    _record_cn(c, cn_no.strip(), cn_h, cn_date or timezone.now(), timezone.now(), user)
    if c.status == "shortfall":
        notify(f"Credit note short on {c.claim_no}: {fmt_sar(c.gap_h)}", "bad", ("promo", c.promotion.mecl_ref, "claim"))
    return c


@transaction.atomic
def chase(user, claim_no):
    require(user, "cn")
    c = get_claim(claim_no)
    audit("claim", c.claim_no, "Chased the product team for the missing credit", user, action="chase")
    audit("promotion", c.promotion.mecl_ref, f"Chased product team for {fmt_sar(c.amount_h - (c.cn_h or 0))}", user,
          action="chase")


@transaction.atomic
def write_off(user, claim_no):
    require(user, "cn")
    c = get_claim(claim_no, lock=True)
    if c.status != "shortfall":
        raise CommandError("Only claims with a shortfall can be written off.")
    c.status = "written_off"
    c.save()
    p = c.promotion
    p.stage = "closed"
    p.save()
    audit("promotion", p.mecl_ref, f"Closed with write-off of {fmt_sar(c.gap_h)}", user, action="write_off")
