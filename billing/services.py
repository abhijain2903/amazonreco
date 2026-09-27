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
    try:
        return po.sap_billing
    except SapBilling.DoesNotExist:
        return None


def invoice_of(po):
    try:
        return po.invoice
    except Invoice.DoesNotExist:
        return None


def make_billing(po, at, mismatch=False, billing_no=None):
    """SAP billing document for a delivered order (SAP sync or simulation)."""
    sh = shipment_of(po)
    cost = {l.sku_id: l.cost_h for l in po.lines.all()}
    b = SapBilling.objects.create(po=po, billing_no=billing_no or str(next_number("sap_billing", 9000100000)), received_at=at)
    for i, l in enumerate(sh.lines.all()):
        b.lines.create(sku_id=l.sku_id, qty=l.qty + (2 if mismatch and i == 0 else 0), price_h=cost[l.sku_id])
    audit("po", po.po_no, f"SAP billing document {b.billing_no} received", name="SAP sync", system=True,
          action="billing", at=at)
    return b


def invoice_checks(po, cfg=None):
    cfg = cfg or get_cfg()
    b, sh = billing_of(po), shipment_of(po)
    if not b or not sh:
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
    inv = Invoice.objects.create(po=po, invoice_no=f"MEI-2026-{next_number('invoice', 4310):05d}", invoice_date=at,
                                 net_h=net, vat_h=vat, total_h=net + vat, sap_billing_no=b.billing_no)
    for sku_id, q, p in lines:
        inv.lines.create(sku_id=sku_id, qty=q, price_h=p, net_h=q * p)
    po.stage = "invoiced"
    po.bump()
    po.save()
    audit("po", po.po_no, f"Invoice {inv.invoice_no} submitted to Amazon for SAR {round(inv.total_h / 100):,}", user,
          name=name, action="invoice", at=at)
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
