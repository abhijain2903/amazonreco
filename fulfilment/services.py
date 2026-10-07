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


def shipped_qty(po):
    """Units already sent on ASNs, per SKU."""
    from django.db.models import Sum
    from .models import ShipmentLine
    return dict(ShipmentLine.objects.filter(shipment__po=po).values_list("sku_id").annotate(n=Sum("qty")))


def to_ship(po):
    """What the next delivery should carry, per PO line: the first one ships what was confirmed now; later ones ship
    what is still open (backorders and anything a short delivery left behind)."""
    done = shipped_qty(po)
    first = not po.shipments.exists()
    out = []
    for l in po.lines.select_related("sku"):
        q = l.qty_confirmed if first else max(0, l.committed - done.get(l.sku_id, 0))
        if q > 0:
            out.append((l, q))
    return out


def open_qty(po):
    """Units still to ship on the PO (backorders and short deliveries)."""
    done = shipped_qty(po)
    return sum(max(0, l.committed - done.get(l.sku_id, 0)) for l in po.lines.all())


def make_delivery(po, at, short=0, delivery_no=None, ship_date=None, sales_order=""):
    """Record the SAP outbound delivery for a released PO (SAP sync or simulation). A PO can have several:
    stock arriving in batches, or a backorder shipped later."""
    lines = to_ship(po)
    seq = po.sap_deliveries.count() + 1
    # Each booking portion has its own sales order: the first is the PO's; later ones are booked separately
    if not sales_order:
        sales_order = po.sap_order_no if seq == 1 else (str(next_number("sap_order", 4500018420)) if settings.DEMO_SIMULATIONS else "")
    d = SapDelivery.objects.create(po=po, seq=seq, sales_order=sales_order, delivery_no=delivery_no or str(next_number("sap_delivery", 8000331100)),
                                   cartons=max(1, math.ceil(sum(q for _, q in lines) / 8)),
                                   ship_date=ship_date or at + timedelta(days=2))
    for i, (l, q) in enumerate(lines):
        d.lines.create(sku=l.sku, qty=max(1, q - short) if (short and i == 0) else q)
    audit("po", po.po_no, f"SAP delivery {d.delivery_no}{f' (delivery {seq})' if seq > 1 else ''} received ({d.cartons} cartons)",
          name="SAP sync", system=True, action="delivery", at=at)
    return d


def asn_checks(po, cfg=None):
    cfg = cfg or get_cfg()
    d = delivery_of(po)
    if not d:
        return []
    confirmed = {l.sku_id: q for l, q in to_ship(po)}      # what this shipment should carry
    out = []
    for dl in d.lines.select_related("sku"):
        asn = dl.asn_qty if dl.asn_qty is not None else dl.qty
        r = engine.asn_line(confirmed.get(dl.sku_id, 0), dl.qty, asn, cfg)
        out.append({"line": dl, "sku": dl.sku, "confirmed": confirmed.get(dl.sku_id, 0), "delivery": dl.qty,
                    "asn": asn, **r})
    return out


def delivery_of(po):
    """The SAP delivery being worked on: the latest one, unless it has already gone out on an ASN and the PO is
    waiting for the next delivery (backorder)."""
    d = po.sap_delivery
    if d and po.stage == "released" and po.shipments.filter(seq=d.seq).exists():
        return None
    return d


def shipment_of(po):
    return po.shipment


def slot_at_risk(po, cfg=None):
    sh = shipment_of(po)
    return bool(po.stage == "asn" and sh and engine.slot_at_risk(bool(sh.slot_id), sh.ship_date, timezone.now(), cfg or get_cfg()))


@transaction.atomic
def sync_delivery(user, po_no):
    require(user, "ship")
    po = get_po(po_no, lock=True)
    if po.stage != "released" or delivery_of(po):
        raise CommandError("This order already has a delivery or is not released.")
    if not to_ship(po):
        raise CommandError("Nothing left to ship on this PO.")
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


def sscc(serial):
    """SSCC-18: extension digit, GS1 company prefix, serial reference, mod-10 check digit."""
    body = ("0" + settings.HUB_GS1_PREFIX + str(serial).zfill(16 - len(settings.HUB_GS1_PREFIX)))[:17]
    total = sum(int(d) * (3 if i % 2 == 0 else 1) for i, d in enumerate(reversed(body)))
    return body + str((10 - total % 10) % 10)


def build_cartons(sh):
    """Carton plan for the ASN. SKUs with a case pack go in whole cases (plus one part case); the rest are spread over
    the remaining cartons SAP reported. One SSCC label per carton."""
    from .models import Carton
    plan, loose = [], []
    for l in sh.lines.select_related("sku").order_by("created_at"):
        cp = l.sku.case_pack or 1
        if l.qty <= 0:
            continue
        if cp > 1:
            full, rest = divmod(l.qty, cp)
            plan += [(l.sku, cp)] * full + ([(l.sku, rest)] if rest else [])
        else:
            loose.append(l)
    free = max(1, sh.cartons - len(plan)) if loose else 0
    for l in loose:
        n = max(1, min(l.qty, round(free * l.qty / sum(x.qty for x in loose)) or 1))
        base, extra = divmod(l.qty, n)
        plan += [(l.sku, base + (1 if i < extra else 0)) for i in range(n)]
    Carton.objects.filter(shipment=sh).delete()
    for i, (sku, q) in enumerate(plan, 1):
        Carton.objects.create(shipment=sh, seq=i, sscc=sscc(next_number("sscc", 1000)), sku=sku, qty=q)
    sh.cartons = len(plan)
    sh.save(update_fields=["cartons", "updated_at"])
    return plan


def _submit_asn(po, at, user=None, name=None):
    d = delivery_of(po)
    sh = Shipment.objects.create(po=po, seq=d.seq, sales_order=d.sales_order, asn_no=f"ASN{next_number('asn', 7104400)}", sap_delivery_no=d.delivery_no,
                                 cartons=d.cartons, ship_date=d.ship_date, submitted_at=at)
    total = 0
    for dl in d.lines.select_related("sku"):
        q = dl.asn_qty if dl.asn_qty is not None else dl.qty
        sh.lines.create(sku=dl.sku, qty=q)
        total += q
    build_cartons(sh)
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
    ck = asn_checks(po)
    if any(not c["asn_ok"] for c in ck):
        raise CommandError("ASN quantities must match the SAP delivery (R4).")
    if not sum(c["asn"] for c in ck):
        raise CommandError("This ASN has no units. Check the SAP delivery, or backorder the PO.")
    sh = _submit_asn(po, timezone.now(), user)
    rows = [["asn_no", "po_no", "ship_to", "ship_date", "cartons", "asin", "qty"]] + [
        [sh.asn_no, po.po_no, po.fc.code, timezone.localtime(sh.ship_date).date().isoformat(), sh.cartons, l.sku.asin, l.qty]
        for l in sh.lines.select_related("sku")]
    f = save_file("asn", f"ASN_{sh.asn_no}.csv", rows, "po", po.po_no)
    labels = [["carton", "of", "sscc", "asn_no", "po_no", "ship_to", "asin", "model_no", "qty"]] + [
        [c.seq, sh.cartons, c.sscc, sh.asn_no, po.po_no, po.fc.code, c.sku.asin, c.sku.model_no, c.qty]
        for c in sh.carton_list.select_related("sku")]
    save_file("labels", f"Carton_labels_{sh.asn_no}.csv", labels, "po", po.po_no)
    from integrations.connectors import get_adapter
    get_adapter("amazon_vc").push("asn", f)
    return sh, f


def _book_slot(po, at, slot_id, start, window, user=None, name=None, freight="prepaid", reason=""):
    sh = shipment_of(po)
    again = po.stage == "slot" or bool(sh.slot_outcome)
    old = sh.slot_id
    sh.slot_id, sh.slot_start, sh.slot_window, sh.freight = slot_id, start, window, freight
    if again:
        sh.reschedules += 1
    sh.slot_outcome, sh.slot_note = "", ""
    sh.save()
    po.stage = "slot"
    po.bump()
    po.save()
    when = f"{timezone.localtime(start):%d %b, %H:%M}"
    if freight == "collect":
        text = f"Amazon pickup {slot_id} (routing request) scheduled for {when}"
    else:
        text = f"Carrier Central slot {slot_id} booked for {when}"
    if again:
        text = ("Rescheduled: " + text + (f" (was {old})" if old and old != slot_id else "")
                + (f". Reason: {reason}" if reason else ""))
    audit("po", po.po_no, text, user, name=name, action="reschedule" if again else "slot", at=at, reason=reason)


@transaction.atomic
def book_slot(user, po_no, slot_id, start, window, version=None, freight="prepaid", reason=""):
    """Prepaid: ME books a Carrier Central appointment. Collect: ME submits a routing request and Amazon schedules a
    pickup (the reference goes in slot_id). Also used to reschedule a booked slot."""
    require(user, "ship")
    po = get_po(po_no, lock=True)
    check_version(po, version)
    if po.stage not in ("asn", "slot"):
        raise CommandError("A slot can be booked once the ASN is sent, and changed until delivery.")
    if not slot_id.strip():
        raise CommandError("Enter the Amazon reference (ARN / pickup ID)." if freight == "collect" else "Enter the Carrier Central slot ID.")
    if freight not in ("prepaid", "collect"):
        freight = "prepaid"
    _book_slot(po, timezone.now(), slot_id.strip(), start, window, user, freight=freight, reason=(reason or "").strip()[:200])
    return po


@transaction.atomic
def slot_failed(user, po_no, outcome, reason, version=None):
    """The truck missed the appointment, or Amazon refused the delivery. The slot is released; book a new one."""
    require(user, "ship")
    po = get_po(po_no, lock=True)
    check_version(po, version)
    if po.stage != "slot":
        raise CommandError("Only a booked slot can be missed or refused.")
    if outcome not in ("missed", "refused"):
        raise CommandError("Pick missed or refused.")
    reason = (reason or "").strip()[:200]
    if not reason:
        raise CommandError("Say what happened, e.g. truck late at the gate / Amazon refused: labels unreadable.")
    sh = shipment_of(po)
    old = sh.slot_id
    sh.slot_outcome, sh.slot_note = outcome, reason
    sh.slot_id, sh.slot_start, sh.slot_window = "", None, ""
    sh.save()
    po.stage = "asn"
    po.bump()
    po.save()
    audit("po", po.po_no, f"Appointment {old} {'missed' if outcome == 'missed' else 'refused by Amazon'}: {reason}. Book a new slot",
          user, action=outcome, reason=reason)
    from core.services import notify
    notify(f"{po.po_no}: delivery {'missed' if outcome == 'missed' else 'refused'}. Re-book the slot", "bad", ("po", po.po_no, "shipment"))
    return po


def _deliver(po, at, user=None, name=None, bill_mismatch=False):
    sh = shipment_of(po)
    sh.delivered_at = at
    sh.save(update_fields=["delivered_at", "updated_at"])
    po.stage, po.delivered_at = "delivered", at
    po.bump()
    po.save()
    audit("po", po.po_no, f"Delivered to {po.fc.code}" + (f" (shipment {sh.seq}, ASN {sh.asn_no})" if sh.seq > 1 else ""), user,
          name=name, action="deliver", at=at)
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
