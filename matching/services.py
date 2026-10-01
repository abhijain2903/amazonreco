"""Suggestions: compute, store, accept, reject. Accepting applies the match through the owning app's own command
(payments, debit notes), so permissions, rules and the audit trail work exactly as for a manual match."""
from django.db import transaction
from django.utils import timezone

from core.services import CommandError, actor_name, audit, check_version, require

from . import ai, matchers
from .models import KINDS, MatchSettings, MatchSuggestion

PERM = {"pay_inv": "dispute", "pay_dn": "dispute", "dn_promo": "dn", "deduction": "dispute"}
KIND_LABEL = dict(KINDS)
TOP_N = 5
AI_CLOSE_CALL = 10  # ask Claude when the best two are within this many points, or the best is below 80


def pending(kind, source):
    return list(MatchSuggestion.objects.filter(kind=kind, source=source, status="pending"))


def _rejected(kind, source):
    return {tuple(t) for t in MatchSuggestion.objects.filter(kind=kind, source=source, status="rejected").values_list("targets", flat=True)}


def _history(kind, n=8):
    """Recent decisions on this kind of match, for Claude's context: what the team accepted and rejected."""
    return [dict(decision=s.status, record=s.source, candidate=s.label, score=s.score)
            for s in MatchSuggestion.objects.filter(kind=kind, status__in=["accepted", "rejected"]).order_by("-decided_at")[:n]]


def _candidates(kind, obj):
    if kind == "pay_inv":
        return matchers.payment_invoice(obj)
    if kind == "pay_dn":
        return matchers.payment_debit_note(obj)
    if kind == "dn_promo":
        return matchers.debit_note_promotion(obj)
    raise ValueError(kind)


def _record(kind, obj):
    """What Claude is shown about the record being matched."""
    if kind == "pay_inv":
        return dict(payment=obj.payment_no, invoice_reference_from_amazon=obj.invoice_ref, amount_sar=obj.paid_h / 100,
                    remit_date=obj.remit_date.date().isoformat())
    if kind == "pay_dn":
        return dict(payment=obj.payment_no, invoice=obj.invoice_ref, deduction_sar=obj.deduction_h / 100, deduction_reason=obj.reason)
    return dict(debit_note=obj.dn_no, agreement_on_dn=obj.agreement_no, dn_date=obj.dn_date.date().isoformat(),
                lines=[dict(model=l.sku.model_no, units=l.units, rate_sar=l.rate_h / 100) for l in obj.lines.select_related("sku")])


@transaction.atomic
def refresh(kind, obj, use_ai=False):
    """Recompute suggestions for one record. Pairs someone rejected are never proposed again."""
    source = obj.payment_no if kind in ("pay_inv", "pay_dn") else obj.dn_no
    cfg = MatchSettings.get()
    rejected = _rejected(kind, source)
    cands = [c for c in _candidates(kind, obj) if tuple(c["targets"]) not in rejected and c["score"] >= cfg.show_threshold][:TOP_N]
    MatchSuggestion.objects.filter(kind=kind, source=source, status="pending").update(status="superseded")
    rows = [MatchSuggestion.objects.create(kind=kind, source=source, targets=c["targets"], label=c["label"][:200], score=c["score"],
                                           reasons=c["reasons"]) for c in cands]
    if use_ai and rows and ai.available() and (rows[0].score < 80 or (len(rows) > 1 and rows[0].score - rows[1].score < AI_CLOSE_CALL)):
        pick = ai.choose(KIND_LABEL[kind], _record(kind, obj), [dict(label=c["label"], score=c["score"], reasons=c["reasons"]) for c in cands],
                         _history(kind))
        if pick and pick["choice"] >= 0:
            s = rows[pick["choice"]]
            s.method, s.ai_model, s.ai_rationale = "ai", pick["model"], pick["rationale"]
            s.extra = {"ai_confidence": pick["confidence"]}
            s.save()
        elif pick:
            for s in rows:
                s.extra = {"ai_note": pick["rationale"]}
                s.save()
    if kind == "pay_inv" and cfg.auto_apply and rows:
        top = rows[0]
        margin = top.score - rows[1].score if len(rows) > 1 else 100
        if len(top.targets) == 1 and top.score >= cfg.auto_threshold and margin >= AI_CLOSE_CALL:
            _apply(None, top, obj)
            top.status, top.decided_by_name, top.decided_at = "auto", "Auto-match", timezone.now()
            top.save()
    return rows


def refresh_deduction(p, use_ai=False):
    """Type, next step and draft note for a short payment's deduction: rules first, Claude on request."""
    dn = matchers.payment_debit_note(p)
    r = matchers.deduction(p, dn)
    MatchSuggestion.objects.filter(kind="deduction", source=p.payment_no, status="pending").update(status="superseded")
    s = MatchSuggestion.objects.create(kind="deduction", source=p.payment_no, targets=[r["type"]], score=r["confidence"],
                                       label=f"{r['type'].title()} · {ACTION_LABEL[r['action']]}", reasons=r["reasons"],
                                       extra=dict(action=r["action"], note=r["note"], dn=dn[0]["targets"][0] if dn and dn[0]["score"] >= 70 else ""))
    if use_ai and ai.available():
        out = ai.classify_deduction(_record("pay_dn", p), _evidence(p, dn))
        if out:
            s.method, s.ai_model, s.ai_rationale, s.score = "ai", out["model"], out["rationale"], out["confidence"]
            s.targets, s.label = [out["type"]], f"{out['type'].title()} · {ACTION_LABEL[out['action']]}"
            s.extra = dict(s.extra, action=out["action"], note=out["dispute_note"] or s.extra["note"])
            s.save()
    return s


ACTION_LABEL = {"dispute": "dispute it", "accept": "accept it", "link_dn": "link to the debit note", "review": "review by hand"}


def _evidence(p, dn_cands):
    from billing.services import invoice_checks
    po = p.po
    ev = dict(invoice=p.invoice_ref, invoice_total_sar=p.invoice.total_h / 100 if p.invoice else None)
    if po:
        sh = getattr(po, "shipment", None)
        checks = invoice_checks(po)
        ev.update(po=po.po_no, delivered=bool(po.delivered_at), asn=sh.asn_no if sh else None,
                  slot_booked=bool(sh and sh.slot_id), invoice_matches_asn_and_po_price=all(c["qty_ok"] and c["price_ok"] for c in checks),
                  lines=[dict(model=c["sku"].model_no, asn_qty=c["asn_qty"], invoiced_qty=c["bill_qty"], price_sar=c["po_price_h"] / 100) for c in checks])
    ev["validated_debit_notes_that_could_explain_it"] = [dict(label=c["label"], score=c["score"], reasons=c["reasons"]) for c in dn_cands[:3]]
    return ev


def _apply(user, s, obj=None):
    from debitnotes import services as dns
    from payments import services as pay
    if s.kind == "pay_inv":
        if len(s.targets) > 1:
            pay.split_payment(user, s.source, s.targets, note=f"suggestion, score {s.score}")
        elif user is None:
            p = obj
            p.invoice_ref = s.targets[0]
            pay.match_payment(p)
            audit("po", p.po.po_no, f"Payment {p.payment_no} matched to {s.targets[0]} automatically (score {s.score})",
                  name="Auto-match", system=True, action="match")
        else:
            pay.manual_match(user, s.source, s.targets[0])
    elif s.kind == "pay_dn":
        pay.link_to_dn(user, s.source, s.targets[0])
    elif s.kind == "dn_promo":
        dns.link(user, s.source, s.targets[0])
        dns.notify_status(dns.get_dn(s.source))
    else:
        raise CommandError("Deduction suggestions are applied from the dispute dialog.")


@transaction.atomic
def accept(user, sid, version=None):
    s = MatchSuggestion.objects.select_for_update().filter(pk=sid).first()
    if not s:
        raise CommandError("That suggestion no longer exists.")
    require(user, PERM[s.kind])
    check_version(s, version)
    if s.status != "pending":
        raise CommandError("This suggestion was already decided or replaced. It has been refreshed.")
    _apply(user, s)
    s.status, s.decided_by, s.decided_by_name, s.decided_at = "accepted", user, actor_name(user), timezone.now()
    s.bump()
    s.save()
    MatchSuggestion.objects.filter(kind=s.kind, source=s.source, status="pending").update(status="superseded")
    _audit_decision(user, s, "accepted")
    return s


@transaction.atomic
def reject(user, sid, version=None):
    s = MatchSuggestion.objects.select_for_update().filter(pk=sid).first()
    if not s:
        raise CommandError("That suggestion no longer exists.")
    require(user, PERM[s.kind])
    check_version(s, version)
    if s.status != "pending":
        raise CommandError("This suggestion was already decided or replaced. It has been refreshed.")
    s.status, s.decided_by, s.decided_by_name, s.decided_at = "rejected", user, actor_name(user), timezone.now()
    s.bump()
    s.save()
    _audit_decision(user, s, "rejected")
    return s


def _audit_decision(user, s, what):
    entity = "dn" if s.kind == "dn_promo" else "payment"
    by = f"Claude ({s.ai_model})" if s.method == "ai" else "the matching rules"
    audit(entity, s.source, f"Match suggestion {what}: {s.label} (score {s.score}, suggested by {by})", user,
          action=f"suggestion_{what}", after={"targets": s.targets, "reasons": s.reasons, "ai_rationale": s.ai_rationale})


@transaction.atomic
def update_settings(user, auto_apply=None, auto_threshold=None, show_threshold=None, ai_enabled=None):
    require(user, "settings")
    cfg = MatchSettings.get()
    before = dict(auto_apply=cfg.auto_apply, auto_threshold=cfg.auto_threshold, show_threshold=cfg.show_threshold, ai_enabled=cfg.ai_enabled)
    if auto_apply is not None:
        cfg.auto_apply = auto_apply
    if ai_enabled is not None:
        cfg.ai_enabled = ai_enabled
    for f, v in (("auto_threshold", auto_threshold), ("show_threshold", show_threshold)):
        if v is not None:
            v = int(v)
            if not 0 <= v <= 100:
                raise CommandError("Thresholds are between 0 and 100.")
            setattr(cfg, f, v)
    if cfg.auto_threshold < 80:
        raise CommandError("Automatic matching needs a threshold of at least 80.")
    cfg.bump()
    cfg.save()
    audit("settings", "matching", "Matching settings changed", user, action="configure", before=before,
          after=dict(auto_apply=cfg.auto_apply, auto_threshold=cfg.auto_threshold, show_threshold=cfg.show_threshold, ai_enabled=cfg.ai_enabled))
    return cfg
