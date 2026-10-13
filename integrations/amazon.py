"""Amazon Vendor Central through the Selling Partner API: what the hub pulls and what it sends.

In:  POs (new, changed, cancelled) — getPurchaseOrders; what Amazon has received per line — getPurchaseOrdersStatus.
Out: PO acknowledgements, ASNs (shipment confirmations), invoices and credit notes; each answered by a transaction ID
     whose result (accepted / refused with errors) is polled with getTransaction.

Payloads are built from the hub's records at send time, so a document always reflects what the hub holds. Sending
happens in a background job after the business change is committed: Amazon being slow or down never blocks or rolls
back a confirmation, an ASN or an invoice; a failed send is retried and shows on the record and the connector.
"""
import logging
from datetime import datetime, timedelta, timezone as dt_tz
from decimal import Decimal, ROUND_HALF_UP

from django.db import transaction
from django.utils import timezone

from core.services import CommandError, audit, notify, vat_h

from .spapi import MAX_WINDOW_DAYS, Client, SpApiError

log = logging.getLogger("hub.amazon")

SYSTEM = "Amazon (API)"
CURRENCY = "SAR"
VAT_RATE = "15"


# ---------- settings ----------
def connector():
    from .connectors import get_adapter
    return get_adapter("amazon_vc").c


def api_settings():
    return connector().settings.get("api", {})


def is_live():
    return connector().mode == "api"


def vendor_codes():
    raw = api_settings().get("vendor_codes", "")
    return [v.strip().upper() for v in raw.replace(";", ",").split(",") if v.strip()]


# ---------- small converters ----------
def money(h):
    return {"currencyCode": CURRENCY, "amount": f"{Decimal(h) / 100:.2f}"}


def to_h(m):
    """Amazon Money -> halalas."""
    if not m or m.get("amount") in (None, ""):
        return 0
    return int((Decimal(str(m["amount"])) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def eaches(q):
    """Amazon ItemQuantity -> units."""
    q = q or {}
    n = int(q.get("amount") or 0)
    return n * int(q.get("unitSize") or 1) if q.get("unitOfMeasure") == "Cases" else n


def parse_dt(v):
    if not v:
        return None
    d = datetime.fromisoformat(str(v).strip().replace("Z", "+00:00"))
    return d if d.tzinfo else d.replace(tzinfo=dt_tz.utc)


def window(v):
    """Amazon's '2026-10-08T00:00:00Z--2026-10-15T00:00:00Z' -> (start, end)."""
    if not v or "--" not in str(v):
        return None, None
    a, b = str(v).split("--", 1)
    return parse_dt(a), parse_dt(b)


def iso(d):
    return timezone.localtime(d, dt_tz.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if d else None


def qty(units, line=None):
    """Hub units -> Amazon ItemQuantity, in cases when Amazon ordered in cases and the units are whole cases."""
    if line is not None and line.amazon_uom == "Cases" and line.unit_size > 1 and units % line.unit_size == 0:
        return {"amount": units // line.unit_size, "unitOfMeasure": "Cases", "unitSize": line.unit_size}
    return {"amount": units, "unitOfMeasure": "Eaches"}


# ---------- PO in ----------
def import_order(order):
    """One Amazon PO into the hub. Returns (outcome, text): created | changed | unchanged | skipped | error."""
    from catalog.models import FulfilmentCentre, resolve_sku
    from orders.models import PurchaseOrder
    from orders.services import CHANGEABLE, default_confirm_by

    no = order.get("purchaseOrderNumber")
    det = order.get("orderDetails") or {}
    vc = ((det.get("sellingParty") or {}).get("partyId") or "").upper()[:12]
    codes = vendor_codes()
    if codes and vc and vc not in codes:
        return "skipped", f"{no}: vendor code {vc} is not one of ME's configured codes"
    items = det.get("items") or []
    old = PurchaseOrder.objects.filter(po_no=no).first()
    if old:
        return _apply_changes(old, order, items, CHANGEABLE)
    if order.get("purchaseOrderState") != "New":
        return "skipped", f"{no}: already {order.get('purchaseOrderState', '').lower()} in Vendor Central before the hub; load open POs with the go-live upload"
    fc_code = (det.get("shipToParty") or {}).get("partyId", "")
    fc = FulfilmentCentre.objects.filter(code=fc_code).first()
    problems = [] if fc else [f"FC {fc_code} is not in the FC master"]
    lines = []
    for it in items:
        sku = resolve_sku(it.get("amazonProductIdentifier")) or resolve_sku(it.get("vendorProductIdentifier"))
        if not sku:
            problems.append(f"ASIN {it.get('amazonProductIdentifier')} ({it.get('vendorProductIdentifier') or 'no vendor SKU'}) is not in the SKU master")
            continue
        lines.append((sku, it))
    if problems:
        # Master data is never created from a transaction: an admin adds the FC / SKU, the next sync imports the PO.
        return "error", f"{no}: " + "; ".join(problems)
    od = parse_dt(det.get("purchaseOrderDate")) or timezone.now()
    ws, we = window(det.get("shipWindow"))
    from orders.services import create_po
    with transaction.atomic():
        po = create_po(no, fc, od, default_confirm_by(od), [(s, eaches(it.get("orderedQuantity")), to_h(it.get("netCost"))) for s, it in lines],
                       window_start=ws, window_end=we, name=SYSTEM, source="Amazon SP-API")
        po.vendor_code, po.source, po.po_type = vc, "api", det.get("purchaseOrderType", "")[:24]
        po.amazon_changed_at = parse_dt(det.get("purchaseOrderChangedDate"))
        po.save(update_fields=["vendor_code", "source", "po_type", "amazon_changed_at", "updated_at"])
        for l, (_, it) in zip(po.lines.order_by("position"), lines):
            q = it.get("orderedQuantity") or {}
            l.item_seq, l.vendor_sku = str(it.get("itemSequenceNumber") or "")[:10], (it.get("vendorProductIdentifier") or "")[:40]
            l.backorder_allowed = bool(it.get("isBackOrderAllowed", True))
            l.amazon_uom, l.unit_size = ("Cases" if q.get("unitOfMeasure") == "Cases" else "Eaches"), int(q.get("unitSize") or 1)
            l.save()
    from orders.services import notify_issues
    notify_issues(po)
    from core.tasks import run_po_checks
    transaction.on_commit(lambda: run_po_checks.defer(po_no=no))
    return "created", f"{no}: {len(lines)} lines"


def _apply_changes(po, order, items, changeable):
    """Amazon changed or cancelled a PO the hub already has. ME must ship the changed PO, not the original."""
    from orders.services import amazon_change
    det = order.get("orderDetails") or {}
    changed_at = parse_dt(det.get("purchaseOrderChangedDate"))
    if not changed_at or (po.amazon_changed_at and changed_at <= po.amazon_changed_at):
        return "unchanged", po.po_no
    by_seq = {l.item_seq: l for l in po.lines.select_related("sku") if l.item_seq}
    by_sku = {l.sku.asin: l for l in po.lines.select_related("sku")}
    want, up = {}, []
    for it in items:
        l = by_seq.get(str(it.get("itemSequenceNumber"))) or by_sku.get((it.get("amazonProductIdentifier") or "").upper())
        if not l:
            continue
        q = eaches(it.get("orderedQuantity"))
        if q < l.qty_ordered:
            want[str(l.pk)] = q
        elif q > l.qty_ordered:
            up.append(f"{l.sku.model_no} {l.qty_ordered} → {q}")
    _, we = window(det.get("shipWindow"))
    cancel = bool(items) and all(eaches(it.get("orderedQuantity")) == 0 for it in items)
    moved = we and po.window_end and abs((we - po.window_end).total_seconds()) > 60
    text = "unchanged"
    if (want or cancel or moved) and po.stage not in changeable:
        notify(f"Amazon changed PO {po.po_no} after the ASN was sent: raise it with Amazon as a shortage", "bad", ("po", po.po_no, "lines"))
        text = "past the ASN, flagged"
    elif want or cancel or moved:
        try:
            amazon_change(None, po.po_no, want, cancel=cancel, window_end=we if moved else None, source="Amazon SP-API", name=SYSTEM,
                          reason="Changed by Amazon in Vendor Central")
            text = "cancelled" if cancel else f"{len(want)} line{'s' if len(want) != 1 else ''} cut" + (", ship window moved" if moved else "")
        except CommandError as e:
            text = f"not applied: {e}"
    if up:
        notify(f"Amazon raised quantities on PO {po.po_no} ({', '.join(up)}). The hub does not raise a confirmed PO; check with the buyer",
               "warn", ("po", po.po_no, "lines"))
    po.refresh_from_db()
    po.amazon_changed_at = changed_at
    po.save(update_fields=["amazon_changed_at", "updated_at"])
    return ("changed" if text != "unchanged" else "unchanged"), f"{po.po_no}: {text}"


def pull_orders(client, start, end=None):
    """POs created or changed between start and end, in 7-day windows. Returns counts and problem lines."""
    end = end or timezone.now()
    out = {"created": 0, "changed": 0, "unchanged": 0, "skipped": 0, "error": 0, "problems": []}
    seen = set()
    codes = vendor_codes() or [None]
    t = start
    while t < end:
        u = min(end, t + timedelta(days=MAX_WINDOW_DAYS))
        for vc in codes:
            for params in ({"createdAfter": t, "createdBefore": u}, {"changedAfter": t, "changedBefore": u, "isPOChanged": "true"}):
                for order in client.purchase_orders(orderingVendorCode=vc, **params):
                    key = (order.get("purchaseOrderNumber"), (order.get("orderDetails") or {}).get("purchaseOrderChangedDate"))
                    if key in seen:
                        continue
                    seen.add(key)
                    outcome, text = import_order(order)
                    out[outcome] += 1
                    if outcome in ("error", "skipped"):
                        out["problems"].append(text)
        t = u
    return out


def pull_receipts(client, start, end=None):
    """What Amazon has received per PO line. Flags a line that Amazon closed with fewer units than were shipped."""
    from fulfilment.services import shipped_qty
    from orders.models import PurchaseOrder
    end = end or timezone.now()
    n = 0
    t = start
    while t < end:
        u = min(end, t + timedelta(days=MAX_WINDOW_DAYS))
        for st in client.purchase_order_statuses(updatedAfter=t, updatedBefore=u):
            po = PurchaseOrder.objects.filter(po_no=st.get("purchaseOrderNumber")).first()
            if not po:
                continue
            lines = {l.item_seq: l for l in po.lines.select_related("sku") if l.item_seq}
            done = shipped_qty(po)
            short = []
            for it in st.get("itemStatus") or []:
                l = lines.get(str(it.get("itemSequenceNumber")))
                rs = it.get("receivingStatus") or {}
                if not l or not rs.get("receivedQuantity"):
                    continue
                got = eaches(rs.get("receivedQuantity"))
                if got != l.qty_received:
                    l.qty_received, l.received_at = got, parse_dt(rs.get("lastReceiveDate"))
                    l.save(update_fields=["qty_received", "received_at", "updated_at"])
                    n += 1
                sent = done.get(l.sku_id, 0)
                if rs.get("receiveStatus") == "RECEIVED" or st.get("purchaseOrderStatus") == "CLOSED":
                    if got < sent:
                        short.append(f"{l.sku.model_no} {got} of {sent}")
            if short:
                text = f"Amazon received less than was shipped on {po.po_no}: {', '.join(short)}. Expect a shortage deduction; the POD is your evidence"
                from core.models import AuditEvent
                if not AuditEvent.objects.filter(entity="po", entity_id=po.po_no, action="amazon_received", text=text).exists():
                    notify(text, "warn", ("po", po.po_no, "shipment"))
                    audit("po", po.po_no, text, name=SYSTEM, system=True, action="amazon_received")
        t = u
    return n


# ---------- documents out ----------
def _party(pid, vat=False):
    p = {"partyId": pid}
    v = api_settings().get("vat_number", "")
    if vat and v:
        p["taxRegistrationDetails"] = [{"taxRegistrationType": "VAT", "taxRegistrationNumber": v}]
    return p


def acknowledgement(po):
    """submitAcknowledgement for a confirmed (or fully rejected) PO, from the hub's line decisions."""
    items = []
    for l in po.lines.select_related("sku").order_by("position"):
        acks = []
        if l.qty_confirmed:
            acks.append({"acknowledgementCode": "Accepted", "acknowledgedQuantity": qty(l.qty_confirmed, l),
                         **({"scheduledShipDate": iso(po.window_start)} if po.window_start else {})})
        if l.qty_backorder:
            eta = datetime.combine(l.backorder_eta, datetime.min.time(), dt_tz.utc) if l.backorder_eta else None
            acks.append({"acknowledgementCode": "Backordered", "acknowledgedQuantity": qty(l.qty_backorder, l),
                         **({"scheduledShipDate": iso(eta)} if eta else {})})
        rejected = l.qty_ordered - l.qty_confirmed - l.qty_backorder
        if rejected > 0:
            acks.append({"acknowledgementCode": "Rejected", "acknowledgedQuantity": qty(rejected, l), "rejectionReason": l.amazon_reject_code})
        if not acks:            # Amazon cut the line to 0 after it was sent: acknowledge nothing on it
            continue
        items.append({"itemSequenceNumber": l.item_seq or str(l.position + 1), "amazonProductIdentifier": l.asin,
                      "vendorProductIdentifier": l.vendor_sku or l.sku.model_no, "orderedQuantity": qty(l.qty_ordered, l),
                      "netCost": money(l.cost_h), "itemAcknowledgements": acks})
    return {"purchaseOrderNumber": po.po_no, "sellingParty": _party(po.vendor_code), "acknowledgementDate": iso(po.confirmed_at or timezone.now()),
            "items": items}


def shipment_confirmation(sh):
    """SubmitShipmentConfirmations: the ASN with one entry per SKU and one carton per SSCC label."""
    po = sh.po
    seq_of, shipped = {}, []
    lines = {l.sku_id: l for l in po.lines.select_related("sku")}
    for i, sl in enumerate(sh.lines.select_related("sku").order_by("created_at"), 1):
        if not sl.qty:
            continue
        l = lines.get(sl.sku_id)
        seq_of[sl.sku_id] = str(i)
        shipped.append({"itemSequenceNumber": str(i), "amazonProductIdentifier": sl.sku.asin,
                        "vendorProductIdentifier": (l.vendor_sku if l and l.vendor_sku else sl.sku.model_no),
                        "shippedQuantity": {"amount": sl.qty, "unitOfMeasure": "Eaches"},
                        "itemDetails": {"purchaseOrderNumber": po.po_no}})
    cartons = [{"cartonIdentifiers": [{"containerIdentificationType": "SSCC", "containerIdentificationNumber": c.sscc}],
                "cartonSequenceNumber": str(c.seq),
                "items": [{"itemReference": seq_of.get(c.sku_id, "1"), "shippedQuantity": {"amount": c.qty, "unitOfMeasure": "Eaches"}}]}
               for c in sh.carton_list.select_related("sku")]
    cfg = api_settings()
    body = {"shipmentIdentifier": sh.asn_no, "shipmentConfirmationType": "Original",
            "shipmentType": cfg.get("shipment_type") or "LessThanTruckLoad", "shipmentStructure": "LooseStandardCase",
            "shipmentConfirmationDate": iso(sh.submitted_at), "shippedDate": iso(sh.ship_date),
            "sellingParty": _party(po.vendor_code), "shipFromParty": _party(cfg.get("ship_from_party_id") or po.vendor_code),
            "shipToParty": _party(po.fc.code), "shipmentMeasurements": {"cartonCount": len(cartons)},
            "shippedItems": shipped, "cartons": cartons}
    if sh.freight == "collect" and sh.slot_id:
        body["amazonReferenceNumber"] = sh.slot_id
    return body


def invoice(inv):
    """submitInvoices, invoiceType Invoice: lines at the PO cost, VAT 15%."""
    po = inv.po
    lines = {l.sku_id: l for l in po.lines.all()}
    items = []
    for i, il in enumerate(inv.lines.select_related("sku").order_by("created_at"), 1):
        l = lines.get(il.sku_id)
        items.append({"itemSequenceNumber": str(i), "amazonProductIdentifier": il.sku.asin,
                      "vendorProductIdentifier": (l.vendor_sku if l and l.vendor_sku else il.sku.model_no),
                      "invoicedQuantity": {"amount": il.qty, "unitOfMeasure": "Eaches"}, "netCost": money(il.price_h),
                      "purchaseOrderNumber": po.po_no,
                      "taxDetails": [{"taxType": "VAT", "taxRate": VAT_RATE, "taxAmount": money(vat_h(il.net_h)), "taxableAmount": money(il.net_h)}]})
    return {"invoiceType": "Invoice", "id": inv.invoice_no, "date": iso(inv.invoice_date),
            "remitToParty": _party(po.vendor_code, vat=True), "shipToParty": _party(po.fc.code),
            "invoiceTotal": money(inv.total_h), "invoiceBaseAmount": money(inv.net_h),
            "taxDetails": [{"taxType": "VAT", "taxRate": VAT_RATE, "taxAmount": money(inv.vat_h), "taxableAmount": money(inv.net_h)}],
            "items": items}


def credit_note(memo):
    """submitInvoices, invoiceType CreditNote, against one invoice. The memo amount includes VAT."""
    inv = memo.invoice
    net = int((Decimal(memo.amount_h) / Decimal("1.15")).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    return {"invoiceType": "CreditNote", "id": memo.memo_no, "date": iso(memo.created_at), "referenceNumber": inv.invoice_no,
            "remitToParty": _party(inv.po.vendor_code, vat=True), "invoiceTotal": money(memo.amount_h),
            "taxDetails": [{"taxType": "VAT", "taxRate": VAT_RATE, "taxAmount": money(memo.amount_h - net), "taxableAmount": money(net)}],
            "additionalDetails": [{"type": "OCR", "detail": memo.reason[:100]}] if memo.reason else [],
            "creditNoteDetails": {"referenceInvoiceNumber": inv.invoice_no, "originalInvoiceDate": iso(inv.invoice_date)}}


def build(kind, ref):
    """(po_no, payload) for a document the hub holds."""
    from billing.models import CreditMemo, Invoice
    from fulfilment.models import Shipment
    from orders.models import PurchaseOrder
    if kind == "po_ack":
        po = PurchaseOrder.objects.select_related("fc").get(po_no=ref)
        return po.po_no, acknowledgement(po)
    if kind == "asn":
        sh = Shipment.objects.select_related("po__fc").get(asn_no=ref)
        return sh.po.po_no, shipment_confirmation(sh)
    if kind == "invoice":
        inv = Invoice.objects.select_related("po__fc").get(invoice_no=ref)
        return inv.po.po_no, invoice(inv)
    if kind == "credit_note":
        m = CreditMemo.objects.select_related("invoice__po").get(memo_no=ref)
        return m.invoice.po.po_no, credit_note(m)
    raise ValueError(kind)


LABEL = {"po_ack": "Acknowledgement", "asn": "ASN", "invoice": "Invoice", "credit_note": "Credit note"}
TAB = {"po_ack": "lines", "asn": "shipment", "invoice": "invoice", "credit_note": "invoice"}


def send(kind, ref, client=None):
    """Build and submit one document; record the transaction. Never raises for an Amazon error: it is recorded."""
    from .models import AmazonTransaction
    po_no, body = build(kind, ref)
    t = AmazonTransaction.objects.create(kind=kind, po_no=po_no, ref=ref, payload=body, status="queued")
    return submit(t, client)


def submit(t, client=None):
    from .models import SyncRun
    client = client or Client()
    t.attempts += 1
    try:
        submit_fn = {"po_ack": lambda b: client.submit_acknowledgements([b]),
                     "asn": lambda b: client.submit_shipment_confirmations([b]),
                     "invoice": lambda b: client.submit_invoices([b]), "credit_note": lambda b: client.submit_invoices([b])}[t.kind]
        t.transaction_id = submit_fn(t.payload)
        t.status, t.errors, t.sent_at = "processing", [], timezone.now()
        t.save()
        SyncRun.objects.create(connector=connector(), kind=t.kind, message=f"{LABEL[t.kind]} {t.ref} sent to Amazon (transaction {t.transaction_id})",
                               records_out=1)
        audit("po", t.po_no, f"{LABEL[t.kind]} {t.ref} sent to Amazon by API", name=SYSTEM, system=True, action="amazon_sent")
    except SpApiError as e:
        t.status, t.errors = "error", (e.errors or [{"code": str(e.status or ""), "message": str(e)}])
        t.save()
        SyncRun.objects.create(connector=connector(), kind=t.kind, ok=False, message=f"{LABEL[t.kind]} {t.ref} not sent: {e}"[:300])
        notify(f"{LABEL[t.kind]} {t.ref} could not be sent to Amazon: {t.error_text}", "bad", ("po", t.po_no, TAB[t.kind]),
               roles=["Admin"] + _owners(t.kind))
    return t


def _owners(kind):
    return {"po_ack": ["PIC"], "asn": ["Logistics"], "invoice": ["PIC", "Finance"], "credit_note": ["Finance"]}[kind]


def check(t, client=None):
    """Poll Amazon's answer for one transaction."""
    from billing.models import Invoice
    client = client or Client()
    res = client.transaction(t.transaction_id)
    st = (res.get("status") or "").lower()
    t.checked_at = timezone.now()
    if st == "success":
        t.status = "success"
        audit("po", t.po_no, f"Amazon accepted {LABEL[t.kind].lower()} {t.ref}", name=SYSTEM, system=True, action="amazon_accepted")
    elif st == "failure":
        t.status, t.errors = "failure", res.get("errors") or []
        text = f"Amazon refused {LABEL[t.kind].lower()} {t.ref}: {t.error_text or 'no reason given'}"
        audit("po", t.po_no, text, name=SYSTEM, system=True, action="amazon_refused", reason=t.error_text)
        notify(text, "bad", ("po", t.po_no, TAB[t.kind]), roles=_owners(t.kind))
        if t.kind == "invoice":
            Invoice.objects.filter(invoice_no=t.ref).update(amazon_status="rejected", amazon_note=(t.error_text or "Refused by Amazon")[:200])
    t.save()
    return t


@transaction.atomic
def resend(user, transaction_pk):
    """Send a document again after fixing what Amazon refused (or after a connection error)."""
    from core.services import require
    from .models import AmazonTransaction
    require(user, "settings")
    old = AmazonTransaction.objects.select_for_update().filter(pk=transaction_pk).first()
    if not old or old.status not in ("failure", "error"):
        raise CommandError("Only a document Amazon refused, or one that could not be sent, is sent again.")
    if not is_live():
        raise CommandError("The Amazon connector is not in API mode.")
    po_no, body = build(old.kind, old.ref)
    if old.kind == "asn":
        body["shipmentConfirmationType"] = "Replace" if old.status == "failure" else "Original"
    t = AmazonTransaction.objects.create(kind=old.kind, po_no=po_no, ref=old.ref, payload=body, status="queued")
    audit("po", po_no, f"{LABEL[old.kind]} {old.ref} queued to send to Amazon again", user, action="amazon_resend")
    from core.tasks import amazon_submit
    transaction.on_commit(lambda: amazon_submit.defer(transaction_pk=str(t.pk)))
    return t


# ---------- one sync pass ----------
def sync(client=None, since=None):
    """Pull new / changed POs and receipts since the last pass (with 10 minutes' overlap), then check open
    transactions. The first pass looks back 7 days; a longer backfill is `manage.py amazon_sync --days N`."""
    from .models import AmazonTransaction, SyncRun
    client = client or Client()
    c = connector()
    now = timezone.now()
    cursor = parse_dt(c.settings.get("cursor")) if c.settings.get("cursor") else None
    start = since or ((cursor - timedelta(minutes=10)) if cursor else now - timedelta(days=MAX_WINDOW_DAYS))
    try:
        res = pull_orders(client, start, now)
        received = pull_receipts(client, start, now)
    except SpApiError as e:
        run = SyncRun.objects.create(connector=c, kind="sync", ok=False, message=f"Sync failed: {e}"[:300])
        return run
    checked = 0
    for t in AmazonTransaction.objects.filter(status="processing", sent_at__lt=now - timedelta(seconds=30)):
        try:
            check(t, client)
            checked += 1
        except SpApiError as e:
            log.warning("transaction %s: %s", t.transaction_id, e)
    c.settings = {**c.settings, "cursor": now.isoformat()}
    c.last_sync = now
    c.records_today += res["created"] + res["changed"]
    c.save(update_fields=["settings", "last_sync", "records_today", "updated_at"])
    msg = (f"Sync: {res['created']} new PO{'s' if res['created'] != 1 else ''}, {res['changed']} changed, "
           f"{received} receipt update{'s' if received != 1 else ''}, {checked} transaction{'s' if checked != 1 else ''} checked")
    if res["error"]:
        msg += f"; {res['error']} PO{'s' if res['error'] != 1 else ''} not imported ({res['problems'][0]})"
        notify(f"{res['error']} Amazon PO{'s' if res['error'] != 1 else ''} could not be imported: {res['problems'][0]}", "bad",
               ("conn", "amazon_vc", ""), roles=["Admin"])
    return SyncRun.objects.create(connector=c, kind="sync", ok=not res["error"], message=msg[:300], records_in=res["created"] + res["changed"])


def test_connection(client=None):
    client = client or Client()
    if not client.configured():
        return False, "Amazon credentials are missing on the server: " + ", ".join(client.missing())
    try:
        client.token()
        now = timezone.now()
        n = sum(1 for _ in client.purchase_orders(createdAfter=now - timedelta(days=1), createdBefore=now, includeDetails="false", limit=10))
    except SpApiError as e:
        return False, f"Amazon refused the connection: {e}"
    return True, f"Connected to Amazon SP-API{' (sandbox)' if 'sandbox' in client.endpoint else ''}: {n} PO{'s' if n != 1 else ''} in the last 24 hours."
