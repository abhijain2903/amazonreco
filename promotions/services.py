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
    """Committed support: per-unit support on expected units, plus fixed fees."""
    return sum(l.support_h * l.expected_units for l in p.lines.all()) + sum(f.amount_h for f in p.fees.all())


def get_promo(ref, lock=False):
    qs = Promotion.objects.select_for_update() if lock else Promotion.objects
    try:
        return qs.get(mecl_ref=ref)
    except Promotion.DoesNotExist:
        raise CommandError(f"Promotion {ref} was not found.")


def new_ref():
    return f"MECL-PR-2026-{next_number('promotion', 137):04d}"


def create_promotion(user, name, category, start, end, owner, lines, *, source="the hub", at=None, name_override=None,
                     promo_type="price_discount", vendor_code=""):
    """lines: [(sku, support_h, expected_units)]."""
    if not name.strip():
        raise CommandError("Give the promotion a name.")
    if end < start:
        raise CommandError("End date must be on or after the start date.")
    if not lines:
        raise CommandError("Add at least one model.")
    if any(s <= 0 or u <= 0 for _, s, u in lines):
        raise CommandError("Every model needs support per unit and expected units above 0.")
    p = Promotion.objects.create(mecl_ref=new_ref(), name=name.strip(), category=category, promo_type=promo_type or "price_discount", vendor_code=vendor_code or "", start=start, end=end,
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
    require(user, "promo")
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


# ---------- fixed fees, amendments, budgets ----------
@transaction.atomic
def add_fee(user, ref, label, amount_h, version=None):
    """A fixed fee on a promotion not yet approved. After approval, fees change through an amendment."""
    from core.services import fmt_sar
    require(user, "promo")
    p = get_promo(ref, lock=True)
    check_version(p, version)
    if p.stage not in ("draft", "submitted"):
        raise CommandError("After approval, add fees with an amendment.")
    label = (label or "").strip()[:80]
    if not label or not amount_h or amount_h <= 0:
        raise CommandError("Give the fee a name (e.g. Lightning Deal fee) and an amount above 0.")
    p.fees.create(label=label, amount_h=amount_h)
    p.bump()
    p.save()
    audit("promotion", p.mecl_ref, f"Fixed fee added: {label}, {fmt_sar(amount_h)}", user, action="fee")
    return p


@transaction.atomic
def set_instalments(user, ref, on):
    require(user, "promo")
    p = get_promo(ref, lock=True)
    if p.stage not in ("draft", "submitted"):
        raise CommandError("After approval, change billing with an amendment.")
    p.dn_instalments = bool(on)
    p.bump()
    p.save()
    audit("promotion", p.mecl_ref, "Amazon bills this promotion in instalments" if on else "Amazon bills this promotion with one debit note", user, action="billing")
    return p


AMENDABLE = ["approved"]          # stored stage: approved, live, waiting for / received a debit note not yet validated


@transaction.atomic
def amend_promotion(user, ref, reason, *, end=None, support=None, add=None, fee=None, instalments=None, version=None):
    """Change an approved promotion: extend or shorten it, change support per unit, add a model or a fixed fee,
    or mark it as billed in instalments. Every change is recorded; the debit-note check uses the amended terms."""
    from core.services import fmt_sar
    from .models import PromoAmendment
    require(user, "promo")
    p = get_promo(ref, lock=True)
    check_version(p, version)
    if p.stage not in AMENDABLE:
        raise CommandError("Only approved promotions are amended (before their debit note is validated).")
    reason = (reason or "").strip()[:200]
    if not reason:
        raise CommandError("Give the reason for the amendment, e.g. Amazon extended the deal by a week.")
    ch = []
    if end and timezone.localtime(end).date() != timezone.localtime(p.end).date():
        if end < p.start:
            raise CommandError("The end date must be on or after the start date.")
        ch.append(dict(what="End date", old=f"{timezone.localtime(p.end):%d %b %Y}", new=f"{timezone.localtime(end):%d %b %Y}"))
        p.end, p.dn_due = end, end + timedelta(days=30)
    for l in p.lines.select_related("sku"):
        new = (support or {}).get(str(l.pk))
        if new is not None and new != l.support_h:
            if new <= 0:
                raise CommandError(f"{l.sku.model_no}: support must be above 0.")
            ch.append(dict(what=f"Support {l.sku.model_no}", old=fmt_sar(l.support_h), new=fmt_sar(new)))
            l.support_h = new
            l.save()
    if add:
        sku, s, u = add
        if p.lines.filter(sku=sku).exists():
            raise CommandError(f"{sku.model_no} is already on the promotion.")
        if s <= 0 or u <= 0:
            raise CommandError("A new model needs support per unit and expected units above 0.")
        p.lines.create(sku=sku, support_h=s, expected_units=u)
        ch.append(dict(what="Model added", old="", new=f"{sku.model_no} at {fmt_sar(s)} × {u:,}"))
    if fee:
        label, amt = fee
        if label and amt and amt > 0:
            p.fees.create(label=label[:80], amount_h=amt)
            ch.append(dict(what="Fixed fee added", old="", new=f"{label}: {fmt_sar(amt)}"))
    if instalments is not None and instalments != p.dn_instalments:
        ch.append(dict(what="Billing", old="One debit note" if not instalments else "Instalments", new="Instalments" if instalments else "One debit note"))
        p.dn_instalments = instalments
    if not ch:
        raise CommandError("Nothing changed.")
    no = p.amendments.count() + 1
    PromoAmendment.objects.create(promotion=p, no=no, reason=reason, changes=ch, by_name=user.name)
    p.bump()
    p.save()
    audit("promotion", p.mecl_ref, f"Amendment {no}: " + "; ".join(f"{c['what']} {c['old']} → {c['new']}".replace("  ", " ") for c in ch)
          + f". Reason: {reason}", user, action="amend", reason=reason)
    return p, no


@transaction.atomic
def set_budget(user, category, year, quarter, amount_h):
    from catalog.models import CATEGORY_NAMES
    from core.services import fmt_sar
    from .models import Budget
    require(user, "promo")
    if category not in CATEGORY_NAMES or int(quarter) not in (1, 2, 3, 4) or not 2000 < int(year) < 2100:
        raise CommandError("Pick a category, year and quarter.")
    if amount_h is None or amount_h < 0:
        raise CommandError("Enter the budget in SAR.")
    b, _ = Budget.objects.update_or_create(category=category, year=int(year), quarter=int(quarter), defaults={"amount_h": amount_h})
    audit("settings", "budgets", f"Budget {category} Q{quarter} {year} set to {fmt_sar(amount_h)}", user, action="configure")
    return b


def quarter_of(d):
    d = timezone.localtime(d) if hasattr(d, "tzinfo") else d
    return d.year, (d.month - 1) // 3 + 1


def budget_report(year, quarter):
    """Per category for one quarter (by promotion start): budget, committed support (approved onwards), billed by
    Amazon (validated debit notes) and recovered from brands (credit notes)."""
    from catalog.models import CATEGORY_NAMES
    from claims.models import Claim
    from debitnotes.models import DebitNote
    from .models import Budget
    budgets = {b.category: b.amount_h for b in Budget.objects.filter(year=year, quarter=quarter)}
    ps = [p for p in Promotion.objects.exclude(stage__in=["draft", "rejected"]).prefetch_related("lines", "fees") if quarter_of(p.start) == (year, quarter)]
    out = []
    for cat, name in CATEGORY_NAMES.items():
        mine = [p for p in ps if p.category == cat]
        agr = [p.agreement_no for p in mine if p.agreement_no]
        committed = sum(support_h(p) for p in mine)
        billed = sum(d.approved_h for d in DebitNote.objects.filter(agreement_no__in=agr, validated=True))
        recovered = sum(c.cn_h or 0 for c in Claim.objects.filter(promotion__in=mine))
        budget = budgets.get(cat)
        out.append(dict(cat=cat, name=name, n=len(mine), budget=budget, committed=committed, billed=billed, recovered=recovered,
                        left=None if budget is None else budget - committed, over=budget is not None and committed > budget,
                        pct=None if not budget else min(100, round(committed * 100 / budget))))
    return out
