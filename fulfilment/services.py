"""SAP deliveries, ASNs, delivery slots and delivery (flow F3)."""
import math
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from core.services import CommandError, audit, check_version, next_number, require, save_file
from orders.services import get_po
from rules import engine
from rules.services import get_cfg

from .models import SapDelivery, Shipment


def make_delivery(po, at, short=0, delivery_no=None, ship_date=None):
    """Record the SAP outbound delivery for a released PO (SAP sync or simulation)."""
    lines = [l for l in po.lines.select_related("sku") if l.qty_confirmed > 0]
    d = SapDelivery.objects.create(po=po, delivery_no=delivery_no or str(next_number("sap_delivery", 8000331100)),
                                   cartons=max(1, math.ceil(sum(l.qty_confirmed for l in lines) / 8)),
                                   ship_date=ship_date or at + timedelta(days=2))
    for i, l in enumerate(lines):
        d.lines.create(sku=l.sku, qty=max(1, l.qty_confirmed - short) if (short and i == 0) else l.qty_confirmed)
    audit("po", po.po_no, f"SAP delivery {d.delivery_no} received ({d.cartons} cartons)", name="SAP sync",
          system=True, action="delivery", at=at)
    return d


def asn_checks(po, cfg=None):
    cfg = cfg or get_cfg()
    d = delivery_of(po)
    if not d:
        return []
    confirmed = {l.sku_id: l.qty_confirmed for l in po.lines.all()}
    out = []
    for dl in d.lines.select_related("sku"):
        asn = dl.asn_qty if dl.asn_qty is not None else dl.qty
        r = engine.asn_line(confirmed.get(dl.sku_id, 0), dl.qty, asn, cfg)
        out.append({"line": dl, "sku": dl.sku, "confirmed": confirmed.get(dl.sku_id, 0), "delivery": dl.qty,
                    "asn": asn, **r})
    return out


def delivery_of(po):
    try:
        return po.sap_delivery
    except SapDelivery.DoesNotExist:
        return None


def shipment_of(po):
    try:
        return po.shipment
    except Shipment.DoesNotExist:
        return None


def slot_at_risk(po, cfg=None):
    sh = shipment_of(po)
    return bool(po.stage == "asn" and sh and engine.slot_at_risk(bool(sh.slot_id), sh.ship_date, timezone.now(), cfg or get_cfg()))


@transaction.atomic
def sync_delivery(user, po_no):
    require(user, "ship")
    po = get_po(po_no, lock=True)
    if po.stage != "released" or delivery_of(po):
        raise CommandError("This order already has a delivery or is not released.")
    if not settings.DEMO_SIMULATIONS:
        raise CommandError("SAP is in file mode. Upload the SAP deliveries file (U5).")
    return make_delivery(po, timezone.now())


@transaction.atomic
def set_asn_qty(user, po_no, sku_code, qty):
    require(user, "ship")
    po = get_po(po_no, lock=True)
    d = delivery_of(po)
    if po.stage != "released" or not d:
        raise CommandError("The ASN can only be edited before it is sent.")
    dl = d.lines.get(sku__sku_code=sku_code)
    dl.asn_qty = max(0, int(qty or 0))
    dl.save()


def _submit_asn(po, at, user=None, name=None):
    d = delivery_of(po)
    sh = Shipment.objects.create(po=po, asn_no=f"ASN{next_number('asn', 7104400)}", sap_delivery_no=d.delivery_no,
                                 cartons=d.cartons, ship_date=d.ship_date, submitted_at=at)
    total = 0
    for dl in d.lines.select_related("sku"):
        q = dl.asn_qty if dl.asn_qty is not None else dl.qty
        sh.lines.create(sku=dl.sku, qty=q)
        total += q
    po.stage = "asn"
    po.bump()
    po.save()
    audit("po", po.po_no, f"ASN {sh.asn_no} sent to Amazon: {total:,} units in {sh.cartons} cartons", user,
          name=name, action="asn", at=at)
    return sh


@transaction.atomic
def submit_asn(user, po_no, version=None):
    require(user, "ship")
    po = get_po(po_no, lock=True)
    check_version(po, version)
    if po.stage != "released" or not delivery_of(po):
        raise CommandError("The ASN needs a released order with a SAP delivery.")
    if any(not c["asn_ok"] for c in asn_checks(po)):
        raise CommandError("ASN quantities must match the SAP delivery (R4).")
    sh = _submit_asn(po, timezone.now(), user)
    rows = [["asn_no", "po_no", "ship_to", "ship_date", "cartons", "asin", "qty"]] + [
        [sh.asn_no, po.po_no, po.fc.code, timezone.localtime(sh.ship_date).date().isoformat(), sh.cartons, l.sku.asin, l.qty]
        for l in sh.lines.select_related("sku")]
    f = save_file("asn", f"ASN_{sh.asn_no}.csv", rows, "po", po.po_no)
    from integrations.connectors import get_adapter
    get_adapter("amazon_vc").push("asn", f)
    return sh, f


def _book_slot(po, at, slot_id, start, window, user=None, name=None):
    sh = shipment_of(po)
    sh.slot_id, sh.slot_start, sh.slot_window = slot_id, start, window
    sh.save()
    po.stage = "slot"
    po.bump()
    po.save()
    audit("po", po.po_no, f"Carrier Central slot {slot_id} booked for {timezone.localtime(start):%d %b, %H:%M}", user,
          name=name, action="slot", at=at)


@transaction.atomic
def book_slot(user, po_no, slot_id, start, window, version=None):
    require(user, "ship")
    po = get_po(po_no, lock=True)
    check_version(po, version)
    if po.stage != "asn":
        raise CommandError("A slot can be booked once the ASN is sent.")
    if not slot_id.strip():
        raise CommandError("Enter the Carrier Central slot ID.")
    _book_slot(po, timezone.now(), slot_id.strip(), start, window, user)
    return po


def _deliver(po, at, user=None, name=None, bill_mismatch=False):
    po.stage, po.delivered_at = "delivered", at
    po.bump()
    po.save()
    audit("po", po.po_no, f"Delivered to {po.fc.code}", user, name=name, action="deliver", at=at)
    from billing.services import make_billing
    make_billing(po, at + timedelta(hours=1), mismatch=bill_mismatch)


@transaction.atomic
def mark_delivered(user, po_no, version=None):
    require(user, "ship")
    po = get_po(po_no, lock=True)
    check_version(po, version)
    if po.stage != "slot":
        raise CommandError("Only shipments with a booked slot can be marked delivered.")
    _deliver(po, timezone.now(), user)
    return po
