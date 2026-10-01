"""Candidate finders: for one record, which records could it match, how well (0-100) and why.

Each matcher returns candidates sorted best first: {"targets": [...], "label": str, "score": int, "reasons": [str]}.
They only read data; applying a match goes through the owning app's services (see matching/services.py).
"""
import re

from django.db.models import Sum

from rules.services import get_cfg

from . import features as f

AMBIGUOUS = "another candidate scores almost the same"


def _days(a, b):
    return (a - b).total_seconds() / 86400


def _open_invoices():
    """Invoices Amazon still owes (PO invoiced, not paid), with what is still due on each."""
    from billing.models import Invoice
    from payments.models import Payment
    from payments.services import COUNTED
    paid = dict(Payment.objects.filter(status__in=COUNTED, invoice__isnull=False).values("invoice").annotate(s=Sum("paid_h"))
                .values_list("invoice", "s"))
    out = []
    for inv in Invoice.objects.filter(po__stage="invoiced").select_related("po"):
        due = inv.total_h - (paid.get(inv.pk) or 0)
        if due > 0:
            inv.due_h = due
            out.append(inv)
    return out


# ---------- remittance line → invoice(s) ----------
def payment_invoice(p, exclude=()):
    """A payment Amazon sent against an invoice reference we could not find exactly. Single invoices are scored on
    reference similarity (50%), amount (35%) and timing (15%); combinations of invoices that add up to the payment
    are proposed for one remittance line covering several invoices."""
    tol = get_cfg().tol_h()
    invs = [i for i in _open_invoices() if i.invoice_no not in exclude]
    cands = []
    for inv in invs:
        rs, rr = f.ref_similarity(p.invoice_ref, inv.invoice_no)
        am, ar = f.amount_score(p.paid_h, inv.due_h, tol)
        days = _days(p.remit_date, inv.invoice_date)
        ds = f.window_score(days, 0, 90)
        score = round(f.weighted([(rs, 50), (am, 35), (ds, 15)]) * (0.6 + 0.4 * am))  # amount far off: at most 60% of the score
        if hint := (p.hint and p.hint == inv.invoice_no):
            score = max(score, 90)
        reasons = [r for r in (rr, ar) if r] + ([f"paid {round(days)} days after the invoice"] if days >= 0 else ["paid before the invoice date"])
        if hint:
            reasons.insert(0, "invoice number given in the remittance advice")
        cands.append(dict(targets=[inv.invoice_no], label=f"{inv.invoice_no} · PO {inv.po.po_no} · SAR {inv.due_h / 100:,.2f}",
                          score=score, reasons=reasons))
    # One remittance line paying several invoices: only when no single invoice explains the amount.
    if not any(c["score"] >= 70 and "amount matches" in c["reasons"] for c in cands):
        near = [i for i in invs if -7 <= _days(p.remit_date, i.invoice_date) <= 120]
        by_no = {i.invoice_no: i for i in near}
        for combo in f.subset_sum(p.paid_h, [(i.invoice_no, i.due_h) for i in near], tol):
            parts = [by_no[n] for n in combo]
            ref = max(f.ref_similarity(p.invoice_ref, i.invoice_no)[0] for i in parts)
            score = f.weighted([(1.0, 55), (max(ref, 0.5), 25), (1.0 if len({i.po.fc_id for i in parts}) == 1 else 0.7, 20)])
            cands.append(dict(targets=combo, label=f"{len(combo)} invoices: " + " + ".join(combo),
                              score=min(score, 89), reasons=[f"the {len(combo)} invoices add up to the amount paid",
                                                             "one remittance line covering several invoices"]))
    return _rank(cands)


# ---------- short payment → debit note ----------
def payment_debit_note(p):
    """A deduction on a payment that may be Amazon collecting a promotion debit note. Scored on the amount (60%),
    references in the deduction text (30%) and timing (10%)."""
    from debitnotes.models import DebitNote
    from payments.models import Payment
    if not p.deduction_h:
        return []
    tol = get_cfg().tol_h()
    linked = " ".join(Payment.objects.exclude(pk=p.pk).filter(reason__contains="linked to ").values_list("reason", flat=True))
    text_nums = set(re.findall(r"\d{6,}", p.reason or ""))
    promo_words = bool(re.search(r"promo|co-?op|allowance|marketing|agreement|debit note|vcdn", p.reason or "", re.I))
    cands = []
    for dn in DebitNote.objects.filter(validated=True).prefetch_related("lines"):
        if dn.dn_no in linked:
            continue
        charged = sum(l.charged_h for l in dn.lines.all())
        amt = max(f.amount_score(p.deduction_h, charged, tol)[0], f.amount_score(p.deduction_h, dn.approved_h, tol)[0])
        ref = 1.0 if (text_nums & {f.digits(dn.dn_no), dn.agreement_no}) else (0.5 if promo_words else 0.0)
        days = _days(p.remit_date, dn.dn_date)
        score = f.weighted([(amt, 60), (ref, 30), (f.window_score(days, 0, 60), 10)])
        reasons = (["deduction equals the debit note amount"] if amt == 1 else []) + \
                  (["deduction text quotes this DN or its agreement #"] if ref == 1 else ["deduction text mentions a promotion"] if ref else []) + \
                  ([f"deducted {round(days)} days after the DN"] if days >= 0 else [])
        cands.append(dict(targets=[dn.dn_no], label=f"{dn.dn_no} · agreement {dn.agreement_no} · SAR {charged / 100:,.2f}",
                          score=score, reasons=reasons))
    return _rank(cands)


# ---------- debit note → promotion ----------
def debit_note_promotion(dn):
    """A debit note whose agreement # is not in the tracker. Scored on agreement # similarity (45%), models on the
    DN versus the promotion (30%), rate per unit versus agreed support (15%) and timing after the promotion (10%)."""
    from promotions.models import Promotion
    from promotions.services import stage_of
    lines = [l for l in dn.lines.all() if l.sku_id]      # fixed-fee lines carry no model
    skus = {l.sku_id for l in lines}
    cands = []
    for pr in Promotion.objects.exclude(agreement_no=None).prefetch_related("lines"):
        st = stage_of(pr)
        if st not in ("waiting_dn", "dn_overdue", "live"):
            continue
        rs, rr = f.ref_similarity(dn.agreement_no, pr.agreement_no)
        support = {l.sku_id: l.support_h for l in pr.lines.all()}
        sk = f.jaccard(skus, support)
        rates = [l.rate_h == support.get(l.sku_id) for l in lines if l.sku_id in support]
        rt = sum(rates) / len(lines) if lines else 0
        days = _days(dn.dn_date, pr.end)
        score = round(f.weighted([(rs, 45), (sk, 30), (rt, 15), (f.window_score(days, 0, 45), 10)]) * (0.6 + 0.4 * sk))
        rr = rr.replace("reference", "agreement #")
        reasons = [r for r in (rr,) if r] + \
                  ([("the model is" if len(skus) == 1 else f"all {len(skus)} models are") + " on the promotion"] if sk == 1 else [f"{round(sk * 100)}% of models in common"] if sk else ["no models in common"]) + \
                  (["rates equal the agreed support per unit"] if rates and all(rates) else []) + \
                  ([f"raised {round(days)} days after the promotion ended"] if days >= 0 else ["raised before the promotion ended"])
        cands.append(dict(targets=[pr.mecl_ref], label=f"{pr.mecl_ref} · agreement {pr.agreement_no} · {pr.name}", score=score, reasons=reasons))
    return _rank(cands)


# ---------- deduction reason → dispute type and next step ----------
KEYWORDS = [("chargeback", r"chargeback|charge-back|non-?compliance|asn accuracy|asn on-?time|label|prep|overweight|oversize|appointment"),
            ("returns", r"\breturn|rtv|customer return"),
            ("promo", r"promo|allowance|agreement|debit note|vcdn"),
            ("coop", r"co-?op|advertis|marketing|sponsored|accrual"),
            ("damage", r"damage|defect|broken|crushed"),
            ("price", r"price|cost|pricing|rate"),
            ("shortage", r"short|received less|missing|not received|quantity|qty")]
NOTES = {
    "shortage": "Full quantity per ASN {asn} was delivered to {fc} in the booked slot. Please reverse the shortage deduction of SAR {amt}. Proof of delivery attached.",
    "price": "Invoice {inv} was billed at the agreed cost for every line (checked against the price list, R1/R6). Please reverse the price deduction of SAR {amt}.",
    "promo": "This deduction relates to a promotion debit note. Please confirm the agreement # so it can be matched; SAR {amt} is already covered by the validated debit note.",
    "damage": "Goods left our warehouse undamaged per the carrier receipt for ASN {asn}. Please share the damage report or reverse the deduction of SAR {amt}.",
    "chargeback": "ASN {asn} was submitted on time with carton labels matching the shipment, and the delivery kept its booked appointment. Please reverse the chargeback of SAR {amt}.",
    "returns": "Please share the return authorisation and receipt for the units deducted (SAR {amt}) on invoice {inv}; we will match them against the stock received back.",
    "coop": "Please confirm the co-op / advertising agreement behind the deduction of SAR {amt}; it does not match an agreed accrual on our side.",
    "other": "Please share the basis for the deduction of SAR {amt} on invoice {inv} so we can review it.",
}


def deduction(p, dn_cands=None):
    """Rules-based reading of Amazon's deduction reason: type, recommended action and a draft dispute note."""
    text = p.reason or ""
    kind = next((k for k, rx in KEYWORDS if re.search(rx, text, re.I)), "other")
    dn_cands = payment_debit_note(p) if dn_cands is None else dn_cands
    sh = getattr(p.po, "shipment", None) if p.po else None
    ctx = dict(asn=sh.asn_no if sh else "—", fc=p.po.fc.code if p.po else "the FC", amt=f"{p.deduction_h / 100:,.2f}", inv=p.invoice_ref)
    if kind == "promo" and dn_cands and dn_cands[0]["score"] >= 70:
        action, why = "link_dn", f"matches debit note {dn_cands[0]['targets'][0]}"
    elif kind in ("shortage", "price", "damage"):
        action, why = "dispute", "ME's records (ASN, delivery, agreed price) support the full invoice"
    elif kind == "chargeback":
        action, why = "dispute", "check the chargeback type against the ASN time, labels and appointment before disputing"
    elif kind in ("returns", "coop"):
        action, why = "review", "check it against the return authorisations / co-op agreement; accept if they match"
    elif kind == "promo":
        action, why = "dispute", "promotion deduction without a matching validated debit note"
    else:
        action, why = "review", "the reason does not say what was deducted"
    conf = 80 if kind != "other" else 40
    return dict(type=kind, action=action, confidence=conf, reasons=[f'reason text reads as "{kind}"' if kind != "other" else "reason text is unclear", why],
                note=NOTES[kind].format(**ctx))


def _rank(cands):
    cands.sort(key=lambda c: (-c["score"], len(c["targets"])))
    if len(cands) > 1 and cands[0]["score"] - cands[1]["score"] < 5:
        cands[0]["reasons"].append(AMBIGUOUS)
    return cands


# ---------- "did you mean" for references on imports ----------
def closest(value, options, min_score=0.8):
    """Best option for a mistyped reference (SKU, claim #), or None."""
    best = max(((f.ref_similarity(value, o)[0], o) for o in options if o), default=(0, None))
    return best[1] if best[0] >= min_score else None


def closest_sku(value):
    from catalog.models import Sku
    opts = {}
    for code, asin, model in Sku.objects.values_list("sku_code", "asin", "model_no"):
        for o in (code, asin, model):
            opts[o] = model
    hit = closest(value, list(opts))
    return f"{opts[hit]} ({hit})" if hit and opts[hit] != hit else hit


def closest_claim(value):
    from claims.models import Claim
    return closest(value, list(Claim.objects.filter(status="sent").values_list("claim_no", flat=True)))
