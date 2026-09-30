"""Debit note validation against promotion agreements (flow F6, rule R10)."""
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from core.services import CommandError, audit, check_version, fmt_sar, next_number, notify, require
from promotions.models import Promotion
from rules import engine
from rules.services import get_cfg

from .models import DebitNote


def evaluate(dn, cfg=None):
    """R10 for one debit note. Returns a dict with status, promo, lines and totals."""
    cfg = cfg or get_cfg()
    promo = Promotion.objects.filter(agreement_no=dn.agreement_no).first() if dn.agreement_no else None
    lines = [{"sku": l.sku_id, "sku_obj": l.sku, "units": l.units, "rate_h": l.rate_h} for l in dn.lines.select_related("sku")]
    if not promo:
        charged = sum(l["units"] * l["rate_h"] for l in lines)
        for l in lines:
            l["charged_h"] = l["units"] * l["rate_h"]
        return {"status": "validated" if dn.validated else "unlinked", "promo": None, "lines": lines,
                "charged_h": charged, "expected_h": None, "variance_h": None, "date_ok": True}
    pls = {l.sku_id: {"support_h": l.support_h, "sold": l.sold_units} for l in promo.lines.all()}
    r = engine.dn_check([{k: v for k, v in l.items() if k != "sku_obj"} for l in lines], pls, dn.dn_date, promo.end, cfg)
    skus = {l["sku"]: l["sku_obj"] for l in lines}
    for l in r["lines"]:
        l["sku_obj"] = skus[l["sku"]]
    if dn.validated:
        status = "disputed" if dn.disputed_h > 0 else "validated"
    else:
        status = "to_validate" if r["ok"] else "mismatch"
    return {"status": status, "promo": promo, **r}


def get_dn(dn_no, lock=False):
    qs = DebitNote.objects.select_for_update() if lock else DebitNote.objects
    try:
        return qs.get(dn_no=dn_no)
    except DebitNote.DoesNotExist:
        raise CommandError(f"Debit note {dn_no} was not found.")


def create_dn(dn_no, agreement, dn_date, lines, user=None, name=None, source="file upload", at=None):
    """lines: [(sku, units, rate_h)]"""
    dn = DebitNote.objects.create(dn_no=dn_no, agreement_no=str(agreement), dn_date=dn_date)
    for sku, u, r in lines:
        dn.lines.create(sku=sku, units=u, rate_h=r)
    audit("dn", dn_no, f"Debit note received from Amazon for agreement {agreement} ({source})", user, name=name,
          system=user is None, action="import", at=at)
    return dn


def _validate(dn, at, user=None, name=None, mode="approve", reason=""):
    ev = evaluate(dn)
    p = ev["promo"]
    dn.validated, dn.validated_at = True, at
    if mode == "dispute":
        dn.disputed_h, dn.approved_h = ev["variance_h"], ev["expected_h"]
    else:
        dn.approved_h = ev["charged_h"]
        dn.override_reason = reason if mode == "override" else ""
    dn.bump()
    dn.save()
    p.stage = "dn_validated"
    p.bump()
    p.save()
    text = {"dispute": f"DN {dn.dn_no} validated. {fmt_sar(ev['expected_h'])} approved, {fmt_sar(dn.disputed_h)} disputed with Amazon",
            "override": f"DN {dn.dn_no} approved with override ({reason})",
            "approve": f"DN {dn.dn_no} validated: {fmt_sar(dn.approved_h)} matches the agreement"}[mode]
    audit("promotion", p.mecl_ref, text, user, name=name, action="dn_" + mode, reason=reason, at=at)
    audit("dn", dn.dn_no, "Validated and approved" if mode != "dispute" else f"Validated with {fmt_sar(dn.disputed_h)} disputed",
          user, name=name, action="validate", at=at)
    return ev


@transaction.atomic
def approve(user, dn_no, version=None):
    require(user, "dn")
    dn = get_dn(dn_no, lock=True)
    check_version(dn, version)
    ev = evaluate(dn)
    if dn.validated or ev["status"] != "to_validate":
        raise CommandError("This debit note does not match the agreement. Dispute it or approve with an override.")
    return _validate(dn, timezone.now(), user)


@transaction.atomic
def approve_override(user, dn_no, reason, version=None):
    require(user, "override")
    if not (reason or "").strip():
        raise CommandError("Enter a reason for the override.")
    dn = get_dn(dn_no, lock=True)
    check_version(dn, version)
    if dn.validated or evaluate(dn)["status"] != "mismatch":
        raise CommandError("Only mismatched, unvalidated debit notes need an override.")
    return _validate(dn, timezone.now(), user, mode="override", reason=reason.strip())


@transaction.atomic
def dispute(user, dn_no, version=None):
    require(user, "dn")
    dn = get_dn(dn_no, lock=True)
    check_version(dn, version)
    if dn.validated or evaluate(dn)["status"] != "mismatch":
        raise CommandError("Only mismatched debit notes can be disputed.")
    ev = _validate(dn, timezone.now(), user, mode="dispute")
    from payments.models import Dispute
    d = Dispute.objects.create(case_no=f"DSP-{next_number('dispute', 41):04d}", type="promo", ref=dn.dn_no,
                               promotion=ev["promo"], amount_h=dn.disputed_h, due=timezone.now() + timedelta(days=14),
                               note="DN charges more units than the Amazon sales report shows for the promo dates.")
    audit("dispute", d.case_no, "Dispute opened for the DN variance", user, action="open")
    return d


@transaction.atomic
def link(user, dn_no, ref, version=None):
    require(user, "dn")
    dn = get_dn(dn_no, lock=True)
    check_version(dn, version)
    p = Promotion.objects.filter(mecl_ref=ref).first()
    if not p or not p.agreement_no:
        raise CommandError("Pick a promotion that has an agreement #.")
    old = dn.agreement_no
    dn.agreement_no = p.agreement_no
    dn.bump()
    dn.save()
    audit("dn", dn.dn_no, f"Linked to {p.mecl_ref} (agreement # corrected from {old})", user, action="link")
    audit("promotion", p.mecl_ref, f"Debit note {dn.dn_no} linked (agreement # on DN was {old})", user, action="link_dn")
    return p


def notify_status(dn):
    ev = evaluate(dn)
    if ev["status"] == "mismatch":
        notify(f"DN {dn.dn_no} is {fmt_sar(ev['variance_h'])} above the agreement", "bad", ("promo", ev["promo"].mecl_ref, "dn"))
    elif ev["status"] == "unlinked":
        notify(f"DN {dn.dn_no} has an unknown agreement #", "warn", ("dn", dn.dn_no, ""))
    return ev
