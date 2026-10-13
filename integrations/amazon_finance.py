"""Amazon finance through the SP-API: what Amazon paid, what it held back and why.

Finances Invoices API (2026-06-25)
  - ME's invoices (AP_INVENTORY): Amazon's status (in progress / rejected / paid / deducted), how each was settled and
    the reasons for a price or quantity variance -> the hub invoice's Amazon status (on hold / rejected / accepted).
  - Amazon's invoices to ME (OTHER): co-op / promotion funding, price protection, damage allowance, shortage and return
    charges -> AmazonCharge; one with an agreement ID becomes a hub debit note and is checked (R10).
Finance Remittance API (2026-03-17)
  - Each payment and its lines -> hub payments, as the remittance upload (U6) makes them: a line paying an ME invoice is
    matched (R7); an Amazon deduction line is put against the invoice it concerns (via Amazon's own charge references)
    and shows as a short payment with Amazon's reason; anything else waits in "To match".

Both APIs are vendor APIs needing the "Finance and Accounting" role. They are new; if Amazon refuses them for the
account, the rest of the sync carries on and uploads keep working.
"""
import logging
import re
from collections import OrderedDict
from decimal import Decimal, ROUND_HALF_UP

from django.db import transaction
from django.utils import timezone

from core.services import audit, fmt_sar, notify

from .amazon import SYSTEM, parse_dt, vendor_codes

log = logging.getLogger("hub.amazon")

DEFECTS = {
    "DEALBUY": "invoiced at regular cost on a deal PO", "PRODUCT_COST_NOT_ACTIVE": "cost not active at Amazon",
    "PRODUCT_COST_NOT_REGISTERED": "cost not registered at Amazon", "VENDOR_LOWERING_COST_AT_PO_CONFIRMATION":
    "invoiced above the cost confirmed on the PO", "CCOGS_PRICE_PROTECTION_AGREEMENT_PPA": "price protection agreement",
    "FALSE_CCOGS_PPA": "cost adjusted by Amazon in the period", "AMAZON_POC_LOWERING_COST_ON_PO": "Amazon lowered the PO cost",
    "REJECTED_PRICE_INCREASE_ON_PO": "price increase was rejected", "LIST_PRICE_DISCOUNT_ISSUES": "list price / discount code mismatch",
    "ROUNDING_ISSUES": "rounding", "UNRECOGNISED_ASIN": "ASIN not recognised", "OVERBILLING": "billed more than received",
    "DUPLICATE_INVOICE_DEFECT": "duplicate invoice", "DELETED_APPOINTMENT": "appointment deleted", "NO_APPOINTMENT": "no appointment",
    "SHORTAGES_IN_TRANSIT": "shortage in transit", "ASIN_MISMATCH": "ASIN mismatch", "FEED_DEFECTS": "price feed defects",
    "REJECTED_LINE_ITEM_ON_PO": "line was rejected on the PO but shipped", "FAILED_PO_CONFIRMATION": "PO confirmation failed",
    "OTHER": "other"}
PAID_STATES = ("PAID", "PARTIALLY_PAID", "DEDUCTED", "PARTIALLY_DEDUCTED")


def cur_h(c):
    if not c or c.get("currencyAmount") in (None, ""):
        return 0
    return int((Decimal(str(c["currencyAmount"])) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def related(ids, name):
    out = []
    for r in ids or []:
        if r.get("relatedIdentifierName") == name:
            v = r.get("relatedIdentifierValue")
            out += v if isinstance(v, list) else [v]
    return [str(x) for x in out if x]


def _ours(ids):
    codes = vendor_codes()
    vc = related(ids, "VENDOR_CODE")
    return not codes or not vc or any(v.upper() in codes for v in vc)


# ---------- Finances Invoices ----------
def pull_invoices(client, since):
    """Returns (invoice statuses changed, Amazon charges recorded)."""
    changed = charges = 0
    for h in client.finance_invoices(since):
        if not _ours(h.get("relatedIdentifiers")):
            continue
        if h.get("invoiceType") == "AP_INVENTORY":
            changed += _invoice_status(h, client)
        else:
            charges += _charge(h, client)
    return changed, charges


def _variance_text(items):
    from catalog.models import Sku
    asins = {}
    parts = []
    for it in items:
        ctx = it.get("lineItemContext") or {}
        mc = ctx.get("matchingContext") or {}
        reasons = mc.get("varianceReasons") or []
        if not reasons and not mc.get("quantityVariance") and not cur_h(mc.get("priceVariance")):
            continue
        asin = next((i.get("itemIdentifierValue") for i in ctx.get("itemIdentifiers") or [] if i.get("itemIdentifierName") == "ASIN"), "")
        if asin and asin not in asins:
            asins[asin] = Sku.objects.filter(asin=asin.upper()).values_list("model_no", flat=True).first() or asin
        what = []
        if mc.get("quantityVariance"):
            what.append(f"{abs(int(mc['quantityVariance']))} units")
        if cur_h(mc.get("priceVariance")):
            what.append(f"price {fmt_sar(abs(cur_h(mc['priceVariance'])))}")
        why = ", ".join(DEFECTS.get(d, d.lower().replace("_", " ")) for r in reasons for d in (r.get("defectTypes") or [r.get("varianceType", "")]))
        parts.append(f"{asins.get(asin, asin) or 'line ' + str(it.get('lineItemId', ''))}: {' and '.join(what) or 'variance'}" + (f" ({why})" if why else ""))
    return "; ".join(parts)[:300]


def _invoice_status(h, client):
    from billing.models import Invoice
    from payments.models import Payment
    from payments.services import find_invoice
    inv = find_invoice(h.get("invoiceNumber", ""))
    if not inv:
        return 0
    st = h.get("invoiceStatus", "")
    settle = {b.get("breakdownType"): cur_h(b.get("breakdownAmount")) for b in h.get("invoiceSettlementBreakdowns") or []}
    variance = bool(settle.get("QuantityVarianceAmount") or settle.get("PriceVariance"))
    why = inv.amazon_variance
    if st == "REJECTED" or variance:
        _, items = client.finance_invoice(h["invoiceIdentifier"])
        why = _variance_text(items) or why
    new = {"REJECTED": "rejected"}.get(st) or ("on_hold" if st == "IN_PROGRESS" and variance else ("accepted" if st in PAID_STATES else None))
    before = (inv.amazon_status, inv.amazon_finance_status, inv.amazon_variance, inv.amazon_settlement)
    inv.amazon_finance_status, inv.amazon_settlement, inv.amazon_variance = st[:20], settle, (why or "")[:300]
    status_changed = new and new != inv.amazon_status
    if status_changed:
        inv.amazon_status = new
        inv.amazon_note = ("" if new == "accepted" else (why or ("Rejected by Amazon" if new == "rejected" else "Price or quantity variance")))[:200]
    if (inv.amazon_status, inv.amazon_finance_status, inv.amazon_variance, inv.amazon_settlement) == before:
        return 0
    with transaction.atomic():
        inv.bump()
        inv.save()
        if status_changed:
            text = {"accepted": f"Amazon accepted invoice {inv.invoice_no} ({st.lower().replace('_', ' ')})",
                    "on_hold": f"Amazon is holding back part of invoice {inv.invoice_no}: {inv.amazon_note}",
                    "rejected": f"Amazon rejected invoice {inv.invoice_no}: {inv.amazon_note}"}[new]
            audit("po", inv.po.po_no, text, name=SYSTEM, system=True, action="invoice_" + new, reason=inv.amazon_note)
            if new != "accepted":
                notify(text, "bad", ("po", inv.po.po_no, "invoice"))
        if why:     # a short payment on this invoice gets Amazon's reason when it has none yet
            Payment.objects.filter(invoice=inv, status="short", reason="").update(reason=f"Amazon: {why}"[:200])
    return 1


def _charge(h, client):
    from .models import AmazonCharge
    no = h.get("invoiceNumber", "")
    if not no:
        return 0
    old = AmazonCharge.objects.filter(invoice_no=no).first()
    if old and old.status == h.get("invoiceStatus") and old.amount_h == cur_h(h.get("invoiceAmount")):
        return 0
    _, items = client.finance_invoice(h["invoiceIdentifier"])
    ctx = [(it.get("lineItemContext") or {}) for it in items]
    ids = h.get("relatedIdentifiers")
    agreements = related(ids, "AGREEMENT_IDS")
    kind = next((c.get("serviceChargeType") for c in ctx if c.get("serviceChargeType") and c["serviceChargeType"] != "OTHER"), "")
    kind = kind or ("COOP" if agreements else "OTHER")
    c, _ = AmazonCharge.objects.update_or_create(invoice_no=no[:40], defaults=dict(
        identifier=(h.get("invoiceIdentifier") or "")[:80], status=(h.get("invoiceStatus") or "")[:20], kind=kind[:30],
        description=(next((x.get("itemDescription") for x in ctx if x.get("itemDescription")), "") or "")[:200],
        issue_date=parse_dt(h.get("issueDate")), amount_h=cur_h(h.get("invoiceAmount")),
        vendor_code=(related(ids, "VENDOR_CODE") or [""])[0][:12], po_nos=related(ids, "PURCHASE_ORDER_IDS"), agreement_ids=agreements,
        shortage_invoice_ids=related(ids, "SHORTAGE_INVOICE_IDS"), return_ids=related(ids, "RETURN_IDS")))
    if agreements and not c.debit_note_no:
        _debit_note(c, items)
    return 1


def _debit_note(c, items):
    """Amazon's charge for a funding agreement is the debit note ME's process checks against the promotion (R10)."""
    from catalog.models import resolve_sku
    from debitnotes.models import DebitNote
    from debitnotes.services import create_dn, notify_status
    if len(c.invoice_no) > 30 or DebitNote.objects.filter(dn_no=c.invoice_no).exists():
        return
    lines = []
    for it in items:
        ctx = it.get("lineItemContext") or {}
        asin = next((i.get("itemIdentifierValue") for i in ctx.get("itemIdentifiers") or [] if i.get("itemIdentifierName") == "ASIN"), "")
        sku = resolve_sku(asin) if asin else None
        qty = int(it.get("quantity") or 0)
        net = next((cur_h(b.get("breakdownAmount")) for b in it.get("totalAmountBreakdown") or [] if b.get("breakdownType") == "ItemCostWithoutTax"),
                   cur_h(it.get("totalAmount")))
        if sku and qty:
            lines.append((sku, qty, cur_h(it.get("unitCost")) or round(net / qty)))
        else:
            lines.append((None, 1, net, (ctx.get("itemDescription") or c.kind_label)[:40]))
    if not lines:
        return
    with transaction.atomic():
        dn = create_dn(c.invoice_no, c.agreement_ids[0], c.issue_date or timezone.now(), lines, name=SYSTEM, source="Amazon SP-API")
        c.debit_note_no = dn.dn_no
        c.save(update_fields=["debit_note_no", "updated_at"])
        notify_status(dn)
        from core.tasks import run_dn_checks
        transaction.on_commit(lambda: run_dn_checks.defer(dn_no=dn.dn_no))


# ---------- Finance Remittance ----------
def pull_remittances(client, start, end):
    """Load every successful payment not loaded yet. Returns the number of payments loaded."""
    from .models import AmazonRemittance
    n = 0
    for hd in client.remittances(start, end):
        uid = str(hd.get("uniquePaymentId") or "")
        if not uid or AmazonRemittance.objects.filter(unique_payment_id=uid, loaded=True).exists():
            continue
        if not _ours(hd.get("relatedIdentifiers")):
            continue
        rec, _ = AmazonRemittance.objects.update_or_create(unique_payment_id=uid, defaults=dict(
            payment_no=str(hd.get("paymentNumber") or uid)[:40], payment_date=parse_dt(hd.get("paymentDate")),
            amount_h=cur_h(hd.get("paymentAmountInInvoiceCurrency") or hd.get("paymentAmount")), status=(hd.get("remittanceStatus") or "")[:12],
            vendor_code=(related(hd.get("relatedIdentifiers"), "VENDOR_CODE") or [""])[0][:12]))
        if rec.status != "SUCCESSFUL":         # in progress: loaded on a later pass; failed: never paid
            continue
        header, items = client.remittance(uid)
        with transaction.atomic():
            rec.message = load_remittance(rec, items)[:300]
            rec.lines, rec.loaded = len(items), True
            rec.save()
        n += 1
    return n


def load_remittance(rec, items):
    """Turn one payment's lines into hub payments (one per ME invoice it touches, plus anything it could not place)."""
    from payments.models import Payment
    from payments.services import find_invoice, import_payment
    from uploads.types import _suggest_payments
    buckets, loose = OrderedDict(), []
    for it in items:
        num = str(it.get("invoiceNumber") or "").strip()
        net, total = cur_h(it.get("netAmountPaid")), cur_h(it.get("totalAmount"))
        inv = find_invoice(num) if num else None
        if inv:
            b = buckets.setdefault(inv.invoice_no, {"paid": 0, "why": []})
            b["paid"] += net
            disc = next((cur_h(x.get("breakdownAmount")) for x in it.get("netAmountPaidBreakdown") or [] if x.get("breakdownType") == "TermsDiscountTaken"), 0)
            if disc:
                b["why"].append(f"Payment terms discount {fmt_sar(abs(disc))}")
            continue
        if net < 0 or total < 0:
            target, why = _place_deduction(num, it)
            if target:
                b = buckets.setdefault(target.invoice_no, {"paid": 0, "why": []})
                b["paid"] += net
                b["why"].append(why)
                continue
            loose.append((num, net, why))
        else:
            loose.append((num, net, (it.get("description") or "")[:200]))
    made, todo = [], []
    for inv_no, b in buckets.items():
        p = _payment(f"{rec.payment_no}/{inv_no}", rec, inv_no, b["paid"], "; ".join(b["why"]))
        if p:
            made.append(p)
    for num, net, why in loose:
        p = _payment(f"{rec.payment_no}/{num or 'line'}", rec, num or rec.payment_no, net, why)
        if p:
            made.append(p)
    for p in made:
        if rec.vendor_code:
            Payment.objects.filter(pk=p.pk).update(vendor_code=rec.vendor_code)
        if p.status in ("unmatched", "short"):
            todo.append(p)
    _suggest_payments(todo)
    m = sum(p.status == "matched" for p in made)
    s = sum(p.status == "short" for p in made)
    return f"{len(made)} payment line{'s' if len(made) != 1 else ''}: {m} matched, {s} short-paid, {len(made) - m - s} to match"


def _payment(no, rec, ref, paid_h, reason):
    from payments.models import Payment
    from payments.services import import_payment
    no = no[:30]
    if Payment.objects.filter(payment_no=no).exists():
        return None
    return import_payment(no, rec.payment_date or timezone.now(), ref[:40], paid_h, 0, (reason or "")[:200])


def _place_deduction(num, it):
    """The ME invoice an Amazon deduction belongs to, and a reason a person can act on."""
    from orders.models import PurchaseOrder
    from payments.services import find_invoice
    from .models import AmazonCharge
    desc = (it.get("description") or it.get("invoiceType") or "Amazon deduction").strip()
    c = AmazonCharge.objects.filter(invoice_no=num).first()
    why = f"{c.kind_label if c else desc} {num}".strip()
    if c and c.agreement_ids:
        why += f" - agreement {c.agreement_ids[0]}"
    if c and c.description and c.description.lower() not in why.lower():
        why += f": {c.description}"
    for ref in (c.shortage_invoice_ids if c else []):
        inv = find_invoice(ref)
        if inv:
            return inv, why
    for po_no in (c.po_nos if c else []):
        po = PurchaseOrder.objects.filter(po_no=po_no).first()
        inv = po.invoices.order_by("-seq").first() if po else None
        if inv:
            return inv, why
    base = re.sub(r"(SCR|PCR|QCR|SC|PC|DC)$", "", num.upper())       # Amazon's shortage / price claim numbers carry the invoice number
    inv = find_invoice(base) if base and base != num.upper() else None
    return (inv, why) if inv else (None, why)
