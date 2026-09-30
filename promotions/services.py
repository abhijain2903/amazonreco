"""Promotion tracker (flow F5, rules R8-R9)."""
import random
import re
from datetime import timedelta

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from core.services import CommandError, audit, check_version, next_number, require, save_file
from rules import engine
from rules.services import get_cfg

from .models import Promotion


def stage_of(p, cfg=None, now=None):
    """Derived stage: approved promos become live / waiting for DN / DN overdue / DN received over time."""
    if p.stage != "approved":
        return p.stage
    now = now or timezone.now()
    if now < p.start:
        return "approved"
    if now < p.end + timedelta(days=1):
        return "live"
    from debitnotes.models import DebitNote
    if DebitNote.objects.filter(agreement_no=p.agreement_no).exists():
        return "dn_received"
    return "dn_overdue" if engine.dn_overdue(p.dn_due, now, cfg or get_cfg()) else "waiting_dn"


def support_h(p):
    return sum(l.support_h * l.expected_units for l in p.lines.all())


def get_promo(ref, lock=False):
    qs = Promotion.objects.select_for_update() if lock else Promotion.objects
    try:
        return qs.get(mecl_ref=ref)
    except Promotion.DoesNotExist:
        raise CommandError(f"Promotion {ref} was not found.")


def new_ref():
    return f"MECL-PR-2026-{next_number('promotion', 137):04d}"


def create_promotion(user, name, category, start, end, owner, lines, *, source="the hub", at=None, name_override=None):
    """lines: [(sku, support_h, expected_units)]."""
    if not name.strip():
        raise CommandError("Give the promotion a name.")
    if end < start:
        raise CommandError("End date must be on or after the start date.")
    if not lines:
        raise CommandError("Add at least one model.")
    if any(s <= 0 or u <= 0 for _, s, u in lines):
        raise CommandError("Every model needs support per unit and expected units above 0.")
    p = Promotion.objects.create(mecl_ref=new_ref(), name=name.strip(), category=category, start=start, end=end,
                                 dn_due=end + timedelta(days=30), owner_name=owner, stage="draft")
    for sku, s, u in lines:
        p.lines.create(sku=sku, support_h=s, expected_units=u)
    audit("promotion", p.mecl_ref, f"Promotion received from product team via {source}", user, name=name_override,
          action="create", at=at)
    return p


def _submit(p, at, user=None, name=None):
    p.stage = "submitted"
    p.bump()
    p.save()
    audit("promotion", p.mecl_ref, "Support per unit checked and promotion submitted to Amazon", user, name=name,
          action="submit", at=at)


@transaction.atomic
def submit_promotion(user, ref, version=None):
    require(user, "promo")
    p = get_promo(ref, lock=True)
    check_version(p, version)
    if p.stage != "draft":
        raise CommandError("Only drafts can be submitted.")
    _submit(p, timezone.now(), user)
    rows = [["mecl_ref", "asin", "model", "start_date", "end_date", "funding_per_unit_sar", "estimated_units"]] + [
        [p.mecl_ref, l.sku.asin, l.sku.model_no, timezone.localtime(p.start).date().isoformat(),
         timezone.localtime(p.end).date().isoformat(), f"{l.support_h / 100:.2f}", l.expected_units]
        for l in p.lines.select_related("sku")]
    return p, save_file("promotion", f"Promo_{p.mecl_ref}.csv", rows, "promotion", p.mecl_ref)


def _approve(p, agreement, at, user=None, name=None):
    p.agreement_no, p.stage = agreement, "approved"
    p.bump()
    p.save()
    audit("promotion", p.mecl_ref, f"Amazon approved. Agreement # {agreement} logged against {p.mecl_ref}", user,
          name=name, action="approve", at=at)


@transaction.atomic
def record_approval(user, ref, agreement, version=None):
    require(user, "promo")
    p = get_promo(ref, lock=True)
    check_version(p, version)
    agreement = (agreement or "").strip()
    if p.stage != "submitted":
        raise CommandError("Only submitted promotions can be approved.")
    if not re.fullmatch(r"\d{6,12}", agreement):
        raise CommandError("Enter the Amazon agreement # (6 to 12 digits).")
    dup = Promotion.objects.filter(agreement_no=agreement).exclude(pk=p.pk).first()
    if dup and get_cfg().on("R8"):
        raise CommandError(f"R8: agreement {agreement} is already linked to {dup.mecl_ref}.")
    try:
        with transaction.atomic():
            _approve(p, agreement, timezone.now(), user)
    except IntegrityError:
        raise CommandError(f"R8: agreement {agreement} is already in use.")
    return p


@transaction.atomic
def reject_promotion(user, ref, version=None):
    require(user, "promo")
    p = get_promo(ref, lock=True)
    check_version(p, version)
    if p.stage != "submitted":
        raise CommandError("Only submitted promotions can be rejected.")
    p.stage = "rejected"
    p.bump()
    p.save()
    audit("promotion", ref, "Marked as rejected by Amazon", user, action="reject")
    return p


@transaction.atomic
def pull_sold_units(user, ref):
    """Simulated pull from the Amazon sales report."""
    p = get_promo(ref, lock=True)
    if not settings.DEMO_SIMULATIONS:
        raise CommandError("The Amazon sales report connector is not live yet.")
    rnd = random.Random(p.mecl_ref)
    for l in p.lines.filter(sold_units__isnull=True):
        l.sold_units = round(l.expected_units * (0.85 + rnd.random() * 0.3))
        l.save()
    audit("promotion", ref, "Sold units pulled from the Amazon sales report (simulated)", user, action="sold_units")


@transaction.atomic
def chase_amazon(user, ref):
    require(user, "promo")
    audit("promotion", ref, "Chased Amazon vendor manager for the missing debit note", user, action="chase")
