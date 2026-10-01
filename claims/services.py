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


def unclaimed_dns(p):
    """Validated debit notes on the promotion's agreement that have no claim yet (one claim per debit note)."""
    return list(DebitNote.objects.filter(agreement_no=p.agreement_no, validated=True, claims__isnull=True).order_by("dn_date", "created_at"))


def _make_claim(p, at, user=None, name=None, dn=None, batch_no=""):
    dn = dn or unclaimed_dns(p)[0]
    c = Claim.objects.create(claim_no=f"CLM-2026-{next_number('claim', 88):04d}", promotion=p, debit_note=dn,
                             amount_h=dn.approved_h, sent_at=at, batch_no=batch_no)
    p.stage = "claimed"
    p.bump()
    p.save()
    audit("promotion", p.mecl_ref, f"Claim {c.claim_no} for {fmt_sar(c.amount_h)} sent to the product team in sell-out format",
          user, name=name, action="claim", at=at)
    audit("claim", c.claim_no, "Claim sent to product team", user, name=name, action="send", at=at)
    return c


CLAIM_HEAD = ["claim_no", "mecl_ref", "agreement_no", "dn_no", "model", "units", "rate_sar", "amount_sar"]


def claim_rows(c):
    p, dn = c.promotion, c.debit_note
    return [[c.claim_no, p.mecl_ref, p.agreement_no, dn.dn_no, l.sku.model_no if l.sku else l.label, l.units, f"{l.rate_h / 100:.2f}",
             f"{l.units * l.rate_h / 100:.2f}"] for l in dn.lines.select_related("sku")]


@transaction.atomic
def generate_claim(user, ref):
    """One claim per validated, unclaimed debit note on the promotion (instalments get a claim each)."""
    require(user, "promo")
    p = get_promo(ref, lock=True)
    dns = unclaimed_dns(p)
    if stage_of(p) != "dn_validated" or not dns:
        raise CommandError("A claim is created after the debit note is validated.")
    now = timezone.now()
    cs = [_make_claim(p, now, user, dn=dn) for dn in dns]
    rows = [CLAIM_HEAD] + [r for c in cs for r in claim_rows(c)]
    return cs[-1], save_file("claim", f"Claim_{cs[-1].claim_no}.csv", rows, "promotion", p.mecl_ref)


@transaction.atomic
def generate_batch(user, refs):
    """One claim file for several promotions (e.g. a month's promotions for one category / product team)."""
    require(user, "promo")
    ps = [get_promo(r, lock=True) for r in dict.fromkeys(refs)]
    ps = [p for p in ps if stage_of(p) == "dn_validated" and unclaimed_dns(p)]
    if len(ps) < 2:
        raise CommandError("Pick two or more promotions with a validated debit note.")
    batch = f"CLB-2026-{next_number('claim_batch', 10):04d}"
    now = timezone.now()
    cs = [_make_claim(p, now, user, dn=dn, batch_no=batch) for p in ps for dn in unclaimed_dns(p)]
    rows = [CLAIM_HEAD] + [r for c in cs for r in claim_rows(c)]
    total = sum(c.amount_h for c in cs)
    f = save_file("claim", f"Claim_batch_{batch}.csv", rows, "promotion", ps[0].mecl_ref)
    for p in ps[1:]:
        save_file("claim", f"Claim_batch_{batch}.csv", rows, "promotion", p.mecl_ref)
    for c in cs:
        audit("claim", c.claim_no, f"Sent in batch {batch} ({len(cs)} claims, {fmt_sar(total)})", user, action="batch")
    return batch, cs, f


@transaction.atomic
def record_batch_cn(user, batch_no, cn_no, cn_h, cn_date=None):
    """One credit note for a whole batch: split across its open claims in proportion to what is still owed on each
    (the last takes the rounding), then each claim is checked (R11)."""
    require(user, "cn")
    cs = list(Claim.objects.select_for_update().filter(batch_no=batch_no, status__in=["sent", "shortfall"]).select_related("promotion").order_by("claim_no"))
    if not cs:
        raise CommandError(f"Batch {batch_no} has no claims waiting for a credit note.")
    if not (cn_no or "").strip() or not cn_h or cn_h <= 0:
        raise CommandError("Enter the credit note number and amount.")
    cs = [c for c in cs if c.amount_h - (c.cn_h or 0) > 0]
    if not cs:
        raise CommandError(f"Nothing is still owed on batch {batch_no}.")
    owed = [c.amount_h - (c.cn_h or 0) for c in cs]
    total = sum(owed)
    left, at = cn_h, timezone.now()
    for i, c in enumerate(cs):
        part = left if i == len(cs) - 1 else round(cn_h * owed[i] / total)
        left -= part
        _record_cn(c, cn_no.strip(), part, cn_date or at, at, user)
    return cs


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
    others_open = p.claims.exclude(pk=c.pk).filter(status__in=["sent", "shortfall"]).exists()
    what = f"Credit notes now total {fmt_sar(total)}" if several else f"Credit note {cn_no} for {fmt_sar(cn_h)}"
    if engine.cn_check(c.amount_h, total, get_cfg()) == "closed":
        c.status, p.stage = "closed", "claimed" if others_open else "closed"
        text = f"{what}, matching claim {c.claim_no}." + (" Other claims on this promotion are still open" if others_open else " Promotion closed")
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
    if not p.claims.exclude(pk=c.pk).filter(status__in=["sent", "shortfall"]).exists():
        p.stage = "closed"
        p.save()
    audit("promotion", p.mecl_ref, f"Closed with write-off of {fmt_sar(c.gap_h)}", user, action="write_off")
