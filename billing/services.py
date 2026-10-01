"""SAP billing documents and invoices to Amazon (flow F3, rule R6)."""
from django.db import transaction
from django.utils import timezone

from core.services import CommandError, audit, check_version, next_number, require, save_file, vat_h
from fulfilment.services import shipment_of
from orders.services import get_po
from rules import engine
from rules.services import get_cfg

from .models import Invoice, SapBilling


def billing_of(po):
    return po.sap_billing


def invoice_of(po):
    return po.invoice


def make_billing(po, at, mismatch=False, billing_no=None):
    """SAP billing document for a delivered shipment (SAP sync or simulation). One per shipment."""
    sh = shipment_of(po)
    cost = {l.sku_id: l.cost_h for l in po.lines.all()}
    b = SapBilling.objects.create(po=po, seq=sh.seq, shipment=sh, billing_no=billing_no or str(next_number("sap_billing", 9000100000)),
                                  received_at=at)
    for i, l in enumerate(sh.lines.all()):
        b.lines.create(sku_id=l.sku_id, qty=l.qty + (2 if mismatch and i == 0 else 0), price_h=cost[l.sku_id])
    audit("po", po.po_no, f"SAP billing document {b.billing_no} received", name="SAP sync", system=True,
          action="billing", at=at)
    return b


def invoice_checks(po, cfg=None):
    cfg = cfg or get_cfg()
    b, sh = billing_of(po), shipment_of(po)
    if not b or not sh or b.seq != sh.seq:
        return []
    cost = {l.sku_id: l.cost_h for l in po.lines.all()}
    bl = {x.sku_id: x for x in b.lines.all()}
    out = []
    for l in sh.lines.select_related("sku"):
        x = bl.get(l.sku_id)
        bq, bp = (x.qty, x.price_h) if x else (0, cost[l.sku_id])
        out.append({"sku": l.sku, "asn_qty": l.qty, "bill_qty": bq, "po_price_h": cost[l.sku_id], "bill_price_h": bp,
                    "net_h": bq * bp, **engine.invoice_line(l.qty, bq, cost[l.sku_id], bp, cfg)})
    return out


def invoice_blocked(po, cfg=None):
    return any(not c["qty_ok"] or not c["price_ok"] for c in invoice_checks(po, cfg))


@transaction.atomic
def fix_billing(user, po_no):
    """Simulates SAP re-issuing the billing document to match the ASN."""
    require(user, "invoice")
    po = get_po(po_no, lock=True)
    b, sh = billing_of(po), shipment_of(po)
    if po.stage != "delivered" or not b:
        raise CommandError("There is no billing document to correct.")
    cost = {l.sku_id: l.cost_h for l in po.lines.all()}
    b.lines.all().delete()
    for l in sh.lines.all():
        b.lines.create(sku_id=l.sku_id, qty=l.qty, price_h=cost[l.sku_id])
    audit("po", po.po_no, f"SAP billing {b.billing_no} corrected to ASN quantities", user, action="fix_billing")


def _invoice(po, at, user=None, name=None):
    sh, b = shipment_of(po), billing_of(po)
    cost = {l.sku_id: l.cost_h for l in po.lines.all()}
    lines = [(l.sku_id, l.qty, cost[l.sku_id]) for l in sh.lines.all()]
    net = sum(q * p for _, q, p in lines)
    vat = vat_h(net)
    inv = Invoice.objects.create(po=po, seq=sh.seq, shipment=sh, invoice_no=f"MEI-2026-{next_number('invoice', 4310):05d}",
                                 invoice_date=at, net_h=net, vat_h=vat, total_h=net + vat, sap_billing_no=b.billing_no)
    for sku_id, q, p in lines:
        inv.lines.create(sku_id=sku_id, qty=q, price_h=p, net_h=q * p)
    from fulfilment.services import open_qty
    left = open_qty(po)
    po.stage = "backorder" if left else "invoiced"
    po.bump()
    po.save()
    audit("po", po.po_no, f"Invoice {inv.invoice_no}" + (f" (shipment {sh.seq})" if sh.seq > 1 or left else "")
          + f" submitted to Amazon for SAR {round(inv.total_h / 100):,}" + (f". {left:,} units still to ship" if left else ""), user,
          name=name, action="invoice", at=at)
    if po.stage == "invoiced":
        from payments.services import settle_po
        settle_po(po, at)          # earlier invoices may already be paid
    return inv


@transaction.atomic
def submit_invoice(user, po_no, version=None):
    require(user, "invoice")
    po = get_po(po_no, lock=True)
    check_version(po, version)
    if po.stage != "delivered":
        raise CommandError("Only delivered orders can be invoiced.")
    if invoice_blocked(po):
        raise CommandError("Invoice is blocked. Fix the billing first (R6).")
    inv = _invoice(po, timezone.now(), user)
    rows = [["invoice_no", "po_no", "invoice_date", "asin", "qty", "unit_price_sar", "net_sar"]] + [
        [inv.invoice_no, po.po_no, timezone.localtime(inv.invoice_date).date().isoformat(), l.sku.asin, l.qty,
         f"{l.price_h / 100:.2f}", f"{l.net_h / 100:.2f}"] for l in inv.lines.select_related("sku")] + [
        ["", "", "", "", "", "VAT 15%", f"{inv.vat_h / 100:.2f}"], ["", "", "", "", "", "Total", f"{inv.total_h / 100:.2f}"]]
    f = save_file("invoice", f"Invoice_{inv.invoice_no}.csv", rows, "po", po.po_no)
    from integrations.connectors import get_adapter
    get_adapter("amazon_vc").push("invoice", f)
    return inv, f


STATUS_TEXT = {"accepted": "Amazon accepted invoice {inv}", "on_hold": "Amazon put invoice {inv} on hold",
               "rejected": "Amazon rejected invoice {inv}"}


def pick_invoice(po, invoice_no=None):
    """The invoice a command is about: the one named, or the PO's latest (one invoice per shipment)."""
    if invoice_no:
        inv = po.invoices.filter(invoice_no=invoice_no).first()
        if not inv:
            raise CommandError(f"Invoice {invoice_no} is not on PO {po.po_no}.")
        return inv
    return invoice_of(po)


@transaction.atomic
def set_invoice_status(user, po_no, status, note="", invoice_no=None):
    """Amazon's answer to the invoice: accepted, on hold (price / quantity mismatch) or rejected."""
    require(user, "invoice")
    po = get_po(po_no, lock=True)
    inv = pick_invoice(po, invoice_no)
    if not inv:
        raise CommandError("This PO has no invoice yet.")
    if status not in STATUS_TEXT:
        raise CommandError("Pick accepted, on hold or rejected.")
    note = (note or "").strip()[:200]
    if status in ("on_hold", "rejected") and not note:
        raise CommandError("Give Amazon's reason, e.g. price mismatch on line 2.")
    inv.amazon_status, inv.amazon_note = status, note if status != "accepted" else ""
    inv.bump()
    inv.save()
    audit("po", po.po_no, STATUS_TEXT[status].format(inv=inv.invoice_no) + (f": {note}" if note else ""), user,
          action="invoice_" + status, reason=note)
    if status != "accepted":
        from core.services import notify
        notify(f"Invoice {inv.invoice_no} {'on hold' if status == 'on_hold' else 'rejected'}: {note}", "bad", ("po", po.po_no, "invoice"))
    return inv


@transaction.atomic
def resubmit_invoice(user, po_no, note="", invoice_no=None):
    """Send the corrected invoice again (after a rejection or to clear a hold)."""
    require(user, "invoice")
    po = get_po(po_no, lock=True)
    inv = pick_invoice(po, invoice_no)
    if not inv or inv.amazon_status not in ("rejected", "on_hold"):
        raise CommandError("Only a rejected or held invoice is sent again.")
    inv.revision += 1
    inv.amazon_status, inv.amazon_note = "submitted", ""
    inv.bump()
    inv.save()
    rows = [["invoice_no", "revision", "po_no", "invoice_date", "asin", "qty", "unit_price_sar", "net_sar"]] + [
        [inv.invoice_no, inv.revision, po.po_no, timezone.localtime(inv.invoice_date).date().isoformat(), l.sku.asin, l.qty,
         f"{l.price_h / 100:.2f}", f"{l.net_h / 100:.2f}"] for l in inv.lines.select_related("sku")]
    f = save_file("invoice", f"Invoice_{inv.invoice_no}_r{inv.revision}.csv", rows, "po", po.po_no)
    audit("po", po.po_no, f"Invoice {inv.invoice_no} corrected and sent again (revision {inv.revision})" + (f": {note}" if note else ""),
          user, action="invoice_resubmit", reason=note or "")
    from integrations.connectors import get_adapter
    get_adapter("amazon_vc").push("invoice", f)
    return inv, f


@transaction.atomic
def issue_credit_memo(user, po_no, amount_h, reason, memo_no="", invoice_no=None):
    """A credit memo against the invoice. If Amazon has already short-paid by about this much, the short payment is
    settled by the memo and the PO closes."""
    from django.db.models import Sum

    from core.services import fmt_sar
    from payments.models import Payment
    from payments.services import COUNTED
    from .models import CreditMemo
    require(user, "invoice")
    po = get_po(po_no, lock=True)
    inv = pick_invoice(po, invoice_no)
    if not inv:
        raise CommandError("This PO has no invoice yet.")
    reason = (reason or "").strip()[:200]
    if not reason:
        raise CommandError("Give the reason for the credit memo.")
    if not amount_h or amount_h <= 0 or amount_h > inv.net_due_h:
        raise CommandError(f"The memo must be above 0 and at most {fmt_sar(inv.net_due_h)}.")
    memo_no = (memo_no or "").strip()[:30] or f"MCM-2026-{next_number('credit_memo', 120):05d}"
    if CreditMemo.objects.filter(memo_no=memo_no).exists():
        raise CommandError(f"Credit memo {memo_no} already exists.")
    m = CreditMemo.objects.create(invoice=inv, memo_no=memo_no, amount_h=amount_h, reason=reason, by_name=user.name)
    audit("po", po.po_no, f"Credit memo {memo_no} for {fmt_sar(amount_h)} against {inv.invoice_no}: {reason}", user, action="credit_memo", reason=reason)
    rows = [["credit_memo_no", "invoice_no", "po_no", "amount_sar", "reason"], [memo_no, inv.invoice_no, po.po_no, f"{amount_h / 100:.2f}", reason]]
    save_file("credit_memo", f"Credit_memo_{memo_no}.csv", rows, "po", po.po_no)
    paid = Payment.objects.filter(invoice=inv, status__in=COUNTED).aggregate(s=Sum("paid_h"))["s"] or 0
    if engine.payment_match(paid, inv.net_due_h, get_cfg()) == "matched" and paid:
        for p in Payment.objects.filter(invoice=inv, status="short"):
            p.status = "accepted"
            p.reason = f"{p.reason} · settled by credit memo {memo_no}".strip(" ·")
            p.save()
        from payments.services import settle_po
        if settle_po(po, timezone.now()):
            audit("po", po.po_no, f"Paid in full after credit memo {memo_no}", user, action="paid")
    return m
