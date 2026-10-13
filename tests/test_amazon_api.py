"""Amazon Selling Partner API (vendor retail procurement): POs in, acknowledgement / ASN / invoice / credit note out,
transaction results and receipts back. A fake Amazon answers every call; nothing leaves the machine."""
import json
import urllib.parse
from datetime import timedelta

import pytest
from django.utils import timezone

from billing.models import CreditMemo, Invoice
from catalog.models import FulfilmentCentre, Sku
from core.models import AuditEvent, Notification
from fulfilment.models import Shipment
from identity.models import User
from integrations import amazon
from integrations.models import AmazonTransaction, Connector
from integrations.spapi import Client, SpApiError
from orders.models import PurchaseOrder

pytestmark = pytest.mark.django_db


class FakeAmazon:
    """Answers SP-API calls like Amazon does. orders / statuses: what the GET endpoints return; sent: what the hub POSTed."""

    def __init__(self):
        self.orders, self.statuses, self.sent, self.calls = [], [], [], []
        self.tx_status = {}          # transactionId -> "Success" | "Failure" | "Processing"
        self.tx_errors = {}
        self.fail_next = []          # status codes to answer before the real answer (429 throttling …)
        self.tokens = 0
        self.reports, self.report_docs, self.report_status = {}, {}, {}   # reportId -> type; type -> document; type -> status
        self.shipments, self.labels, self.catalog = {}, {}, {}           # by vendorShipmentIdentifier / ASIN
        self.fin_invoices, self.fin_items, self.remits, self.remit_items = [], {}, [], {}   # finance APIs
        self.refuse_finance = False

    def download(self, url, timeout=120):
        import gzip
        return gzip.compress(json.dumps(self.report_docs[self.reports[url.rsplit("/", 1)[1]]]).encode())

    def __call__(self, method, url, headers, body=None, timeout=30):
        u = urllib.parse.urlparse(url)
        q = dict(urllib.parse.parse_qsl(u.query))
        self.calls.append((method, u.path, q))
        if "auth/o2/token" in url:
            self.tokens += 1
            return 200, {}, json.dumps({"access_token": f"Atza|t{self.tokens}", "expires_in": 3600})
        assert headers["x-amz-access-token"].startswith("Atza|")
        if self.fail_next:
            return self.fail_next.pop(0), {}, json.dumps({"errors": [{"code": "QuotaExceeded", "message": "slow down"}]})
        if u.path.startswith(self.refuse_finance if isinstance(self.refuse_finance, str) else "/finances/") and self.refuse_finance:
            return 403, {}, json.dumps({"errors": [{"code": "Unauthorized", "message": "Access to requested resource is denied."}]})
        if u.path == "/finances/invoices/2026-06-25/invoices":
            return 200, {}, json.dumps({"invoices": self.fin_invoices, "numOfRecords": len(self.fin_invoices)})
        if u.path.startswith("/finances/invoices/2026-06-25/invoices/"):
            ident = urllib.parse.unquote(u.path.rsplit("/", 1)[1])
            head = next(h for h in self.fin_invoices if h["invoiceIdentifier"] == ident)
            return 200, {}, json.dumps({"invoiceHeader": head, "invoiceItems": self.fin_items.get(ident, [])})
        if u.path == "/finances/remittances/2026-03-17/remittances":
            return 200, {}, json.dumps({"remittances": self.remits, "numOfRecords": len(self.remits)})
        if u.path.startswith("/finances/remittances/2026-03-17/remittances/"):
            uid = u.path.rsplit("/", 1)[1]
            items = self.remit_items[uid]
            head = next(h for h in self.remits if h["uniquePaymentId"] == uid)
            if "nextTokenForLineItems" in q:                                   # second page
                return 200, {}, json.dumps({"remittanceHeader": head, "remittanceItems": items[2:]})
            return 200, {}, json.dumps({"remittanceHeader": head, "remittanceItems": items[:2], **({"nextTokenForLineItems": "p2"} if len(items) > 2 else {})})
        if u.path == "/vendor/orders/v1/purchaseOrders":
            want_changed = q.get("isPOChanged") == "true"
            orders = [o for o in self.orders if bool(o.get("_changed")) == want_changed]
            if "nextToken" in q:
                return 200, {}, json.dumps({"payload": {"orders": [_clean(o) for o in orders[1:]]}})
            page = {"orders": [_clean(o) for o in orders[:1]]}
            if len(orders) > 1:
                page["pagination"] = {"nextToken": "p2"}
            return 200, {}, json.dumps({"payload": page})
        if u.path == "/vendor/orders/v1/purchaseOrdersStatus":
            return 200, {}, json.dumps({"payload": {"ordersStatus": self.statuses}})
        if u.path == "/reports/2021-06-30/reports" and method == "POST":
            b = json.loads(body)
            rid = f"R{len(self.reports) + 1}"
            self.reports[rid] = b["reportType"]
            self.sent.append((u.path, b))
            return 202, {}, json.dumps({"reportId": rid})
        if u.path.startswith("/reports/2021-06-30/reports/"):
            rid = u.path.rsplit("/", 1)[1]
            st = self.report_status.get(self.reports[rid], "DONE")
            return 200, {}, json.dumps({"reportId": rid, "processingStatus": st, **({"reportDocumentId": rid} if st == "DONE" else {})})
        if u.path.startswith("/reports/2021-06-30/documents/"):
            rid = u.path.rsplit("/", 1)[1]
            return 200, {}, json.dumps({"reportDocumentId": rid, "url": f"https://s3.example/{rid}", "compressionAlgorithm": "GZIP"})
        if u.path == "/vendor/shipping/v1/shipments" and method == "GET":
            return 200, {}, json.dumps({"payload": {"shipments": self.shipments.get(q.get("vendorShipmentIdentifier"), [])}})
        if u.path == "/vendor/shipping/v1/transportLabels":
            return 200, {}, json.dumps({"payload": {"transportLabels": self.labels.get(q.get("vendorShipmentIdentifier"), [])}})
        if u.path.startswith("/catalog/2022-04-01/items/"):
            asin = u.path.rsplit("/", 1)[1]
            if asin not in self.catalog:
                return 404, {}, json.dumps({"errors": [{"code": "NOT_FOUND", "message": f"Requested item '{asin}' not found"}]})
            return 200, {}, json.dumps(self.catalog[asin])
        if method == "POST":
            tid = f"tx-{len(self.sent) + 1}"
            self.sent.append((u.path, json.loads(body)))
            self.tx_status.setdefault(tid, "Success")
            return 202, {}, json.dumps({"payload": {"transactionId": tid}})
        if u.path.startswith("/vendor/transactions/v1/transactions/"):
            tid = u.path.rsplit("/", 1)[1]
            return 200, {}, json.dumps({"payload": {"transactionStatus": {"transactionId": tid, "status": self.tx_status[tid],
                                                                          "errors": self.tx_errors.get(tid, [])}}})
        return 404, {}, json.dumps({"errors": [{"code": "NotFound", "message": u.path}]})


def _clean(o):
    return {k: v for k, v in o.items() if not k.startswith("_")}


@pytest.fixture
def fake(settings, monkeypatch):
    settings.SPAPI_CLIENT_ID, settings.SPAPI_CLIENT_SECRET, settings.SPAPI_REFRESH_TOKEN = "amzn1.application-oa2-client.x", "s", "Atzr|r"
    f = FakeAmazon()
    monkeypatch.setattr("integrations.spapi.urllib_transport", f)
    monkeypatch.setattr("integrations.spapi.urllib_download", f.download)
    from integrations.connectors import ensure_connectors
    ensure_connectors()
    c = Connector.objects.get(key="amazon_vc")
    c.mode = "api"
    c.settings = {"api": {"vendor_codes": "", "ship_from_party_id": "MEWH1", "vat_number": "300000000000003"}}
    c.save()
    return f


def order(no, sku, fc, qty=12, cost="150.00", state="New", changed=None, cases=None, backorder=True, vc="MEAUD", extra_items=()):
    now = timezone.now()
    q = {"amount": qty // cases, "unitOfMeasure": "Cases", "unitSize": cases} if cases else {"amount": qty, "unitOfMeasure": "Eaches"}
    items = [{"itemSequenceNumber": "1", "amazonProductIdentifier": sku.asin, "vendorProductIdentifier": sku.model_no,
              "orderedQuantity": q, "isBackOrderAllowed": backorder, "netCost": {"currencyCode": "SAR", "amount": cost}}, *extra_items]
    o = {"purchaseOrderNumber": no, "purchaseOrderState": state, "orderDetails": {
        "purchaseOrderDate": (now - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ"), "purchaseOrderType": "RegularOrder",
        "purchaseOrderStateChangedDate": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "sellingParty": {"partyId": vc}, "shipToParty": {"partyId": fc.code},
        "shipWindow": f"{(now + timedelta(days=2)):%Y-%m-%dT00:00:00Z}--{(now + timedelta(days=9)):%Y-%m-%dT00:00:00Z}",
        "items": items}}
    if changed:
        o["orderDetails"]["purchaseOrderChangedDate"] = changed.strftime("%Y-%m-%dT%H:%M:%SZ")
        o["_changed"] = True
    return o


def a_sku():
    return Sku.objects.filter(active=True, case_pack=1).order_by("sku_code").first()


def test_new_po_comes_in_with_amazon_line_details(fake):
    s, fc = a_sku(), FulfilmentCentre.objects.first()
    fake.orders = [order("AMZ0001A", s, fc, qty=24, cases=6, backorder=False, cost=f"{s.cost_h / 100:.2f}")]
    run = amazon.sync()
    assert run.ok and "1 new PO" in run.message
    po = PurchaseOrder.objects.get(po_no="AMZ0001A")
    l = po.lines.get()
    assert (po.source, po.vendor_code, po.po_type, po.fc) == ("api", "MEAUD", "RegularOrder", fc)
    assert (l.qty_ordered, l.amazon_uom, l.unit_size, l.item_seq, l.backorder_allowed) == (24, "Cases", 6, "1", False)
    assert l.cost_h == s.cost_h and po.window_end > po.window_start
    assert AuditEvent.objects.filter(entity="po", entity_id="AMZ0001A", action="import", text__contains="Amazon SP-API").exists()
    assert amazon.sync().message.startswith("Sync: 0 new POs")          # the next pass does not import it twice


def test_unknown_sku_or_fc_is_reported_not_created(fake):
    s, fc = a_sku(), FulfilmentCentre.objects.first()
    ghost = Sku(asin="B0NOTOURS1", model_no="GHOST-1")
    bad_fc = FulfilmentCentre(code="XXX9")
    fake.orders = [order("AMZ0002A", ghost, fc), order("AMZ0003A", s, bad_fc)]
    n = Sku.objects.count(), FulfilmentCentre.objects.count()
    run = amazon.sync()
    assert not run.ok and "2 POs not imported" in run.message
    assert (Sku.objects.count(), FulfilmentCentre.objects.count()) == n          # master data never comes from a PO
    assert not PurchaseOrder.objects.filter(po_no__in=["AMZ0002A", "AMZ0003A"]).exists()
    assert Notification.objects.filter(text__contains="could not be imported").exists()


def test_only_configured_vendor_codes_and_new_pos_are_imported(fake):
    s, fc = a_sku(), FulfilmentCentre.objects.first()
    c = Connector.objects.get(key="amazon_vc")
    c.settings = {**c.settings, "api": {**c.settings["api"], "vendor_codes": "MEAUD, MEVIS"}}
    c.save()
    fake.orders = [order("AMZ0004A", s, fc, vc="OTHER"), order("AMZ0005A", s, fc, state="Acknowledged"), order("AMZ0006A", s, fc, vc="MEVIS")]
    amazon.sync()
    assert list(PurchaseOrder.objects.filter(po_no__startswith="AMZ000").values_list("po_no", flat=True)) == ["AMZ0006A"]


def test_amazon_change_cuts_the_line_then_cancels_the_po(fake):
    s, fc = a_sku(), FulfilmentCentre.objects.first()
    fake.orders = [order("AMZ0007A", s, fc, qty=20)]
    amazon.sync(since=timezone.now() - timedelta(days=1))
    t1 = timezone.now() + timedelta(minutes=1)
    fake.orders = [order("AMZ0007A", s, fc, qty=12, changed=t1)]
    amazon.sync(since=timezone.now() - timedelta(days=1))
    po = PurchaseOrder.objects.get(po_no="AMZ0007A")
    assert po.lines.get().qty_ordered == 12 and po.amazon_changed_at == t1.replace(microsecond=0)
    assert AuditEvent.objects.filter(entity_id="AMZ0007A", action="amazon_change", actor_name=amazon.SYSTEM, system=True).exists()
    amazon.sync(since=timezone.now() - timedelta(days=1))                     # the same change again: nothing happens
    assert AuditEvent.objects.filter(entity_id="AMZ0007A", action="amazon_change").count() == 1
    fake.orders = [order("AMZ0007A", s, fc, qty=0, changed=t1 + timedelta(minutes=5))]
    amazon.sync(since=timezone.now() - timedelta(days=1))
    assert PurchaseOrder.objects.get(po_no="AMZ0007A").stage == "cancelled"


def test_acknowledgement_carries_each_decision_in_amazons_terms(fake):
    from orders.services import save_lines
    s = a_sku()
    others = list(Sku.objects.filter(active=True, case_pack=1).exclude(pk=s.pk).order_by("sku_code")[:3])
    fc = FulfilmentCentre.objects.first()
    items = [{"itemSequenceNumber": str(i + 2), "amazonProductIdentifier": o.asin, "vendorProductIdentifier": o.model_no,
              "orderedQuantity": {"amount": 10, "unitOfMeasure": "Eaches"}, "isBackOrderAllowed": True,
              "netCost": {"currencyCode": "SAR", "amount": f"{o.cost_h / 100:.2f}"}} for i, o in enumerate(others)]
    fake.orders = [order("AMZ0008A", s, fc, qty=12, cases=6, cost=f"{s.cost_h / 100:.2f}", extra_items=items)]
    amazon.sync()
    po = PurchaseOrder.objects.get(po_no="AMZ0008A")
    l1, l2, l3, l4 = po.lines.order_by("position")
    faisal = User.objects.get(username="faisal")
    # As on screen: the decision first, then the quantities, reason and date it opens up
    save_lines(faisal, po.po_no, {str(l.pk): (d, None, None) for l, d in ((l1, "accept"), (l2, "partial"), (l3, "backorder"), (l4, "reject"))})
    save_lines(faisal, po.po_no, {str(l2.pk): (None, 7, "Limited stock"), str(l3.pk): (None, 4, "Backordered: stock expected", "2030-01-15"),
                                  str(l4.pk): (None, None, "Discontinued")})
    _, body = amazon.build("po_ack", po.po_no)
    by = {i["itemSequenceNumber"]: i for i in body["items"]}
    assert body["sellingParty"] == {"partyId": "MEAUD"} and body["purchaseOrderNumber"] == "AMZ0008A"
    a1 = by["1"]["itemAcknowledgements"]
    assert a1 == [{"acknowledgementCode": "Accepted", "acknowledgedQuantity": {"amount": 2, "unitOfMeasure": "Cases", "unitSize": 6},
                   "scheduledShipDate": a1[0]["scheduledShipDate"]}]
    assert [(a["acknowledgementCode"], a["acknowledgedQuantity"]["amount"], a.get("rejectionReason")) for a in by["2"]["itemAcknowledgements"]] == [
        ("Accepted", 7, None), ("Rejected", 3, "TemporarilyUnavailable")]
    bo = by["3"]["itemAcknowledgements"]
    assert [(a["acknowledgementCode"], a["acknowledgedQuantity"]["amount"]) for a in bo] == [("Accepted", 4), ("Backordered", 6)]
    assert bo[1]["scheduledShipDate"] == "2030-01-15T00:00:00Z"
    assert by["4"]["itemAcknowledgements"] == [{"acknowledgementCode": "Rejected", "acknowledgedQuantity": {"amount": 10, "unitOfMeasure": "Eaches"},
                                                "rejectionReason": "ObsoleteProduct"}]
    assert by["2"]["netCost"] == {"currencyCode": "SAR", "amount": f"{others[0].cost_h / 100:.2f}"}


def test_confirming_in_api_mode_sends_the_acknowledgement_and_reads_amazons_answer(fake, monkeypatch, django_capture_on_commit_callbacks):
    from core import tasks
    from orders.services import confirm_po
    monkeypatch.setattr(tasks.amazon_send, "defer", lambda **kw: tasks.amazon_send(**kw))
    s, fc = a_sku(), FulfilmentCentre.objects.first()
    fake.orders = [order("AMZ0009A", s, fc, qty=5, cost=f"{s.cost_h / 100:.2f}")]
    amazon.sync()
    with django_capture_on_commit_callbacks(execute=True):
        confirm_po(User.objects.get(username="faisal"), "AMZ0009A")
    path, body = fake.sent[-1]
    assert path == "/vendor/orders/v1/acknowledgements" and body["acknowledgements"][0]["purchaseOrderNumber"] == "AMZ0009A"
    t = AmazonTransaction.objects.get(ref="AMZ0009A")
    assert (t.kind, t.status, t.transaction_id) == ("po_ack", "processing", "tx-1")
    AmazonTransaction.objects.filter(pk=t.pk).update(sent_at=timezone.now() - timedelta(minutes=1))
    amazon.sync()
    t.refresh_from_db()
    assert t.status == "success"
    assert AuditEvent.objects.filter(entity_id="AMZ0009A", action="amazon_accepted").exists()


def test_file_mode_sends_nothing(fake, django_capture_on_commit_callbacks, monkeypatch):
    from core import tasks
    from orders.services import confirm_po
    Connector.objects.filter(key="amazon_vc").update(mode="file")
    monkeypatch.setattr(tasks.amazon_send, "defer", lambda **kw: pytest.fail("no API send in file mode"))
    po = PurchaseOrder.objects.filter(stage="new").first()
    with django_capture_on_commit_callbacks(execute=True):
        confirm_po(User.objects.get(username="faisal"), po.po_no)
    assert not fake.sent and not AmazonTransaction.objects.exists()


def test_asn_lists_every_carton_by_its_sscc(fake):
    sh = Shipment.objects.filter(carton_list__isnull=False).distinct().first()
    _, body = amazon.build("asn", sh.asn_no)
    assert body["shipmentIdentifier"] == sh.asn_no and body["shipFromParty"] == {"partyId": "MEWH1"}
    assert body["shipToParty"] == {"partyId": sh.po.fc.code}
    ssccs = [c["cartonIdentifiers"][0]["containerIdentificationNumber"] for c in body["cartons"]]
    assert ssccs == list(sh.carton_list.values_list("sscc", flat=True))
    assert sum(i["shippedQuantity"]["amount"] for i in body["shippedItems"]) == sum(sh.lines.values_list("qty", flat=True))
    refs = {i["itemSequenceNumber"] for i in body["shippedItems"]}
    assert all(c["items"][0]["itemReference"] in refs for c in body["cartons"])
    assert sum(c["items"][0]["shippedQuantity"]["amount"] for c in body["cartons"]) == sum(sh.lines.values_list("qty", flat=True))


def test_invoice_and_credit_note_carry_vat_and_references(fake):
    inv = Invoice.objects.filter(lines__isnull=False).distinct().first()
    _, body = amazon.build("invoice", inv.invoice_no)
    assert body["invoiceType"] == "Invoice" and body["id"] == inv.invoice_no
    assert body["invoiceTotal"]["amount"] == f"{inv.total_h / 100:.2f}"
    assert body["taxDetails"][0] == {"taxType": "VAT", "taxRate": "15", "taxAmount": {"currencyCode": "SAR", "amount": f"{inv.vat_h / 100:.2f}"},
                                     "taxableAmount": {"currencyCode": "SAR", "amount": f"{inv.net_h / 100:.2f}"}}
    assert body["remitToParty"]["taxRegistrationDetails"] == [{"taxRegistrationType": "VAT", "taxRegistrationNumber": "300000000000003"}]
    assert sum(int(i["invoicedQuantity"]["amount"]) for i in body["items"]) == sum(inv.lines.values_list("qty", flat=True))
    m = CreditMemo.objects.create(invoice=inv, memo_no="MCM-TEST-1", amount_h=11500, reason="Price correction", by_name="t")
    _, cn = amazon.build("credit_note", m.memo_no)
    assert cn["invoiceType"] == "CreditNote" and cn["creditNoteDetails"]["referenceInvoiceNumber"] == inv.invoice_no
    assert (cn["invoiceTotal"]["amount"], cn["taxDetails"][0]["taxableAmount"]["amount"], cn["taxDetails"][0]["taxAmount"]["amount"]) == (
        "115.00", "100.00", "15.00")


def test_a_refused_invoice_is_flagged_and_can_be_sent_again(fake, as_user, monkeypatch, django_capture_on_commit_callbacks):
    from core import tasks
    inv = Invoice.objects.filter(amazon_status="submitted").first()
    fake.tx_status["tx-1"], fake.tx_errors["tx-1"] = "Failure", [{"code": "INVALID_TAX", "message": "VAT amount does not match"}]
    t = amazon.send("invoice", inv.invoice_no)
    AmazonTransaction.objects.filter(pk=t.pk).update(sent_at=timezone.now() - timedelta(minutes=1))
    amazon.sync()
    t.refresh_from_db()
    inv.refresh_from_db()
    assert t.status == "failure" and inv.amazon_status == "rejected" and "VAT amount" in inv.amazon_note
    assert Notification.objects.filter(text__contains=f"Amazon refused invoice {inv.invoice_no}").exists()
    r = as_user("priya").post(f"/integrations/amazon/tx/{t.pk}/resend/")
    assert json.loads(r.headers["HX-Trigger"])["toast"]["msg"].startswith("This needs the")
    monkeypatch.setattr(tasks.amazon_submit, "defer", lambda **kw: tasks.amazon_submit(**kw))
    with django_capture_on_commit_callbacks(execute=True):
        r = as_user("admin").post(f"/integrations/amazon/tx/{t.pk}/resend/")
    assert r.status_code == 204
    again = AmazonTransaction.objects.filter(ref=inv.invoice_no).exclude(pk=t.pk).get()
    assert again.status == "processing" and fake.sent[-1][0] == "/vendor/payments/v1/invoices"


def test_amazon_down_does_not_undo_the_asn_and_is_recorded(fake, monkeypatch):
    fake.fail_next = [500] * 5
    monkeypatch.setattr("integrations.spapi.time.sleep", lambda s: None)
    sh = Shipment.objects.first()
    t = amazon.send("asn", sh.asn_no)
    assert t.status == "error" and Shipment.objects.filter(pk=sh.pk).exists()
    assert Notification.objects.filter(text__contains=f"ASN {sh.asn_no} could not be sent").exists()


def test_client_logs_in_once_retries_throttling_and_follows_pages(fake, settings):
    s, fc = a_sku(), FulfilmentCentre.objects.first()
    fake.orders = [order("AMZ0010A", s, fc), order("AMZ0011A", s, fc)]
    fake.fail_next = [429]
    waits = []
    c = Client(sleep=waits.append)
    now = timezone.now()
    got = [o["purchaseOrderNumber"] for o in c.purchase_orders(createdAfter=now - timedelta(days=1), createdBefore=now)]
    assert got == ["AMZ0010A", "AMZ0011A"] and waits == [1] and fake.tokens == 1
    assert any(q.get("nextToken") == "p2" for _, _, q in fake.calls)
    settings.SPAPI_REFRESH_TOKEN = ""
    with pytest.raises(SpApiError, match="HUB_SPAPI_REFRESH_TOKEN"):
        Client().token()


def test_receipts_show_on_the_line_and_a_short_receipt_warns(fake):
    from fulfilment.services import shipped_qty
    po = next(p for p in PurchaseOrder.objects.filter(stage__in=["delivered", "invoiced"]) if shipped_qty(p))
    l = po.lines.select_related("sku").first()
    l.item_seq = "1"
    l.save()
    sent = shipped_qty(po)[l.sku_id]
    fake.statuses = [{"purchaseOrderNumber": po.po_no, "purchaseOrderStatus": "CLOSED", "itemStatus": [
        {"itemSequenceNumber": "1", "receivingStatus": {"receiveStatus": "RECEIVED", "receivedQuantity": {"amount": sent - 2, "unitOfMeasure": "Eaches"},
                                                        "lastReceiveDate": "2026-10-01T09:00:00Z"}}]}]
    amazon.sync()
    l.refresh_from_db()
    assert l.qty_received == sent - 2 and l.received_at.year == 2026
    assert Notification.objects.filter(text__contains=f"Amazon received less than was shipped on {po.po_no}").count() == 1
    amazon.sync()                                          # no second alert for the same receipt
    assert Notification.objects.filter(text__contains=f"Amazon received less than was shipped on {po.po_no}").count() == 1


def test_connection_test_and_connector_panel(fake, as_user, settings):
    ok, msg = amazon.test_connection()
    assert ok and msg.startswith("Connected to Amazon SP-API")
    r = as_user("admin").get("/records/conn/amazon_vc/?mode=api")
    html = r.content.decode()
    assert "Amazon Selling Partner API" in html and "HUB_SPAPI_REFRESH_TOKEN" in html and "Placeholder" not in html
    settings.SPAPI_CLIENT_SECRET = ""
    ok, msg = amazon.test_connection()
    assert not ok and "HUB_SPAPI_CLIENT_SECRET" in msg


# ---------- Reports API ----------
def _report_docs(skus, promo, coupon_promo):
    a, b = skus[0].asin, skus[1].asin
    y = (timezone.localdate() - timedelta(days=1)).isoformat()
    m = timezone.localdate().replace(day=1)
    return {
        "GET_VENDOR_SALES_REPORT": {"reportSpecification": {}, "salesAggregate": [], "salesByAsin": [
            {"asin": a, "startDate": y, "endDate": y, "shippedUnits": 12, "customerReturns": 2, "shippedCogs": {}, "shippedRevenue": {}},
            {"asin": "B0UNKNOWN1", "startDate": y, "endDate": y, "shippedUnits": 5, "shippedCogs": {}, "shippedRevenue": {}}]},
        "GET_VENDOR_INVENTORY_REPORT": {"reportSpecification": {}, "inventoryAggregate": [], "inventoryByAsin": [
            {"asin": a, "startDate": y, "endDate": y, "sellableOnHandInventoryUnits": 37}]},
        "GET_VENDOR_FORECASTING_REPORT": {"reportSpecification": {}, "forecastByAsin": [
            {"forecastGenerationDate": "2026-01-01", "asin": a, "startDate": m.isoformat(), "meanForecastUnits": 99},          # an older run
            {"forecastGenerationDate": "2026-02-01", "asin": a, "startDate": m.isoformat(), "meanForecastUnits": 10.4},
            {"forecastGenerationDate": "2026-02-01", "asin": a, "startDate": (m + timedelta(days=7)).isoformat(), "meanForecastUnits": 20.2}]},
        "GET_VENDOR_NET_PURE_PRODUCT_MARGIN_REPORT": {"reportSpecification": {}, "netPureProductMarginAggregate": [],
                                                     "netPureProductMarginByAsin": [{"asin": b, "startDate": "2026-09-01", "netPureProductMargin": 0.1234}]},
        "GET_PROMOTION_PERFORMANCE_REPORT": {"reportSpecification": {}, "promotions": [{
            "promotionId": "PRM-1", "promotionName": "Deal", "vendorCode": "MEAUD", "fundingAgreementId": promo.agreement_no,
            "unitsSold": 41, "amountSpent": 2050.5, "revenue": 9000, "startDateTime": "2026-01-01T00:00:00Z",
            "endDateTime": "2026-01-05T00:00:00Z", "status": "ENDED", "includedProducts": [
                {"asin": l.sku.asin, "productUnitsSold": 40 + i, "productAmountSpent": 1000} for i, l in enumerate(promo.lines.select_related("sku"))]}]},
        "GET_COUPON_PERFORMANCE_REPORT": {"reportSpecification": {}, "campaigns": [{
            "campaignId": "C1", "campaignName": "Coupons", "vendorCode": "MEVIS", "coupons": [{
                "couponId": "CPN-1", "name": "10% off", "startDateTime": coupon_promo.start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "endDateTime": coupon_promo.end.strftime("%Y-%m-%dT%H:%M:%SZ"), "redemptions": 17, "budgetSpent": 340,
                "asins": [{"asin": coupon_promo.lines.select_related("sku").first().sku.asin}]}]}]},
    }


def test_daily_reports_fill_sellout_stock_forecast_margin_and_promotion_units(fake):
    from catalog.models import AmazonStock, Forecast, ProductMargin, SellOut
    from core import trackers
    from integrations.amazon_reports import request_all
    from integrations.models import AmazonPromoPerformance, AmazonReport
    from promotions.models import Promotion
    skus = list(Sku.objects.filter(active=True).order_by("sku_code")[:2])
    ended = Promotion.objects.filter(agreement_no__isnull=False, end__lt=timezone.now()).order_by("mecl_ref")
    promo, cp = ended[0], ended[1]
    promo.lines.update(sold_units=None)
    cp.promo_type = "coupon"
    cp.save()
    cp.lines.update(sold_units=None)
    fake.report_docs = _report_docs(skus, promo, cp)
    made = request_all()
    assert sorted(r.report_type for r in made) == sorted(fake.report_docs) and all(r.report_id for r in made)
    sales_req = next(b for p, b in fake.sent if b.get("reportType") == "GET_VENDOR_SALES_REPORT")
    assert sales_req["marketplaceIds"] == ["A17E79C6D8DWNP"] and sales_req["reportOptions"]["reportPeriod"] == "DAY"
    run = amazon.sync()
    assert "6 reports loaded" in run.message, run.message
    y = timezone.localdate() - timedelta(days=1)
    assert SellOut.objects.get(sku=skus[0], day=y).units == 10                       # net of returns
    assert AmazonStock.objects.get(sku=skus[0], as_of=y).units == 37
    assert Forecast.objects.get(sku=skus[0], month=timezone.localdate().replace(day=1)).sellout_units == 31   # latest run only
    assert str(ProductMargin.objects.get(sku=skus[1]).pct) == "12.34"
    r = AmazonReport.objects.get(report_type="GET_VENDOR_SALES_REPORT")
    assert r.status == "loaded" and "B0UNKNOWN1" in r.message
    perf = AmazonPromoPerformance.objects.get(kind="promotion")
    assert perf.promotion == promo and perf.spent_h == 205050
    assert [l.sold_units for l in promo.lines.order_by("created_at")] == [40 + i for i in range(promo.lines.count())]
    assert AmazonPromoPerformance.objects.get(kind="coupon").promotion == cp and cp.lines.order_by("created_at").first().sold_units == 17
    cols, rows, _ = trackers.sellout_tracker()
    assert cols[-1] == ("Amazon margin", "pct")
    assert next(x for x in rows if x["cells"][0] == skus[1].model_no)["cells"][-1] == ProductMargin.objects.get(sku=skus[1]).pct
    assert not any(t == "GET_VENDOR_NET_PURE_PRODUCT_MARGIN_REPORT" for t, *_ in __import__("integrations.amazon_reports", fromlist=["wanted"]).wanted())


def test_a_report_without_data_or_a_failed_report_is_recorded(fake):
    from integrations.amazon_reports import request_all
    from integrations.models import AmazonReport
    fake.report_status = {"GET_VENDOR_SALES_REPORT": "CANCELLED", "GET_VENDOR_INVENTORY_REPORT": "FATAL",
                          "GET_VENDOR_FORECASTING_REPORT": "IN_PROGRESS"}
    fake.report_docs = {k: {} for k in ("GET_PROMOTION_PERFORMANCE_REPORT", "GET_COUPON_PERFORMANCE_REPORT", "GET_VENDOR_NET_PURE_PRODUCT_MARGIN_REPORT")}
    request_all()
    amazon.sync()
    st = dict(AmazonReport.objects.values_list("report_type", "status"))
    assert (st["GET_VENDOR_SALES_REPORT"], st["GET_VENDOR_INVENTORY_REPORT"], st["GET_VENDOR_FORECASTING_REPORT"]) == ("no_data", "failed", "processing")
    assert Notification.objects.filter(text__contains="inventory report failed").exists()


# ---------- Vendor Shipments: collect pickup, tracking, labels ----------
def test_asking_amazon_to_collect_books_the_slot_when_amazon_schedules_it(fake, as_user, settings, tmp_path, monkeypatch,
                                                                          django_capture_on_commit_callbacks):
    import base64

    from core import tasks
    from core.models import Attachment
    settings.MEDIA_ROOT = tmp_path
    monkeypatch.setattr(tasks.amazon_send, "defer", lambda **kw: tasks.amazon_send(**kw))
    sh = Shipment.objects.filter(po__stage="asn", carton_list__isnull=False).distinct().first()
    po = sh.po
    with django_capture_on_commit_callbacks(execute=True):
        r = as_user("khalid").post(f"/pos/{po.po_no}/pickup/", {"ready": "2026-10-20T09:00", "pallets": "2", "weight_kg": "350.5",
                                                                 "version": po.version})
    assert r.status_code == 204, r.content
    path, body = fake.sent[-1]
    req = body["shipments"][0]
    assert path == "/vendor/shipping/v1/shipments" and req["shipmentFreightTerm"] == "Collect" and req["vendorShipmentIdentifier"] == sh.asn_no
    assert req["collectFreightPickupDetails"]["requestedPickUp"] == "2026-10-20T06:00:00Z"                 # 09:00 Riyadh
    assert req["shipmentMeasurements"] == {"totalCartonCount": sh.carton_list.count(), "totalPalletStackable": 2,
                                           "shipmentWeight": {"unitOfMeasure": "Kg", "value": "350.50"}}
    assert [c["containerIdentifiers"][0]["containerIdentificationNumber"] for c in req["containers"]] == list(sh.carton_list.values_list("sscc", flat=True))
    fake.shipments[sh.asn_no] = [{"vendorShipmentIdentifier": sh.asn_no, "currentShipmentStatus": "CarrierAssigned", "buyerReferenceNumber": "ARN-77001",
                                  "collectFreightPickupDetails": {"scheduledPickUp": "2026-10-21T07:00:00Z"},
                                  "transportationDetails": {"carrierDetails": {"name": "Aramex Freight", "shipmentReferenceNumber": "PRO-55"}}}]
    fake.labels[sh.asn_no] = [{"labelData": [{"labelSequenceNumber": 1, "labelFormat": "PDF", "label": base64.b64encode(b"%PDF-1.4 fake").decode()}]}]
    amazon.sync()
    sh.refresh_from_db()
    po.refresh_from_db()
    assert (po.stage, sh.freight, sh.slot_id, sh.carrier, sh.tracking_no, sh.amazon_status) == (
        "slot", "collect", "ARN-77001", "Aramex Freight", "PRO-55", "CarrierAssigned")
    assert sh.slot_start.isoformat().startswith("2026-10-21T07:00") and sh.labels_fetched
    att = Attachment.objects.get(entity_id=po.po_no, filename__startswith="Amazon_label_")
    assert att.file.read() == b"%PDF-1.4 fake"
    assert Notification.objects.filter(text__contains=f"Amazon will collect PO {po.po_no}").exists()
    html = as_user("khalid").get(f"/records/po/{po.po_no}/?tab=shipment").content.decode()
    assert "Aramex Freight" in html and "PRO-55" in html


def test_pickup_needs_the_api_and_an_asn(fake, as_user):
    sh = Shipment.objects.filter(po__stage="asn").first()
    Connector.objects.filter(key="amazon_vc").update(mode="file")
    r = as_user("khalid").post(f"/pos/{sh.po.po_no}/pickup/", {"ready": "2026-10-20T09:00"})
    assert "not connected by API" in json.loads(r.headers["HX-Trigger"])["toast"]["msg"]
    Connector.objects.filter(key="amazon_vc").update(mode="api")
    other = PurchaseOrder.objects.filter(stage="delivered").first()
    r = as_user("khalid").post(f"/pos/{other.po_no}/pickup/", {"ready": "2026-10-20T09:00"})
    assert "once the ASN is sent" in json.loads(r.headers["HX-Trigger"])["toast"]["msg"]


# ---------- Catalog Items ----------
def test_catalog_lookup_shows_amazons_details_and_differences(fake, as_user):
    s = a_sku()
    fake.catalog[s.asin] = {"asin": s.asin, "summaries": [{"marketplaceId": "A17E79C6D8DWNP", "itemName": "Vlog camera 4K", "brand": "Kodak",
                                                          "modelNumber": "OTHER-MODEL", "packageQuantity": 1}],
                            "identifiers": [{"marketplaceId": "A17E79C6D8DWNP", "identifiers": [{"identifierType": "EAN", "identifier": "6281234567890"}]}],
                            "vendorDetails": [{"marketplaceId": "A17E79C6D8DWNP", "productGroup": "Camera"}]}
    html = as_user("admin").get(f"/integrations/amazon/catalog/?sku={s.sku_code}").content.decode()
    assert "Vlog camera 4K" in html and "Kodak" in html and f"Model: hub {s.model_no}, Amazon OTHER-MODEL" in html
    assert "Not found in Amazon.sa" in as_user("faisal").get("/integrations/amazon/catalog/?asin=B0NOPE0000").content.decode()
    assert "Check at Amazon" in as_user("admin").get(f"/records/sku/{s.sku_code}/").content.decode()
    Connector.objects.filter(key="amazon_vc").update(mode="file")
    assert "not connected by API" in as_user("admin").get(f"/integrations/amazon/catalog/?sku={s.sku_code}").content.decode()


# ---------- Finance: invoice status, Amazon's charges, remittances ----------
def sar(h):
    return {"currencyCode": "SAR", "currencyAmount": round(h / 100, 2)}


def test_amazon_holds_back_part_of_an_invoice_then_pays_it(fake, as_user):
    inv = Invoice.objects.filter(amazon_status="submitted").select_related("po").first()
    l = inv.lines.select_related("sku").first()
    ident = f"{inv.invoice_no}~PAYEE1"
    head = {"invoiceNumber": inv.invoice_no, "invoiceIdentifier": ident, "invoiceType": "AP_INVENTORY", "invoiceStatus": "IN_PROGRESS",
            "invoiceAmount": sar(inv.total_h), "invoiceSettlementBreakdowns": [{"breakdownType": "QuantityVarianceAmount", "breakdownAmount": sar(46000)}],
            "relatedIdentifiers": [{"relatedIdentifierName": "VENDOR_CODE", "relatedIdentifierValue": ["MEAUD"]}]}
    fake.fin_invoices = [head]
    fake.fin_items[ident] = [{"lineItemId": "1", "totalAmount": sar(100), "lineItemContext": {
        "lineItemType": "GOODS", "itemIdentifiers": [{"itemIdentifierName": "ASIN", "itemIdentifierValue": l.sku.asin}],
        "matchingContext": {"matchedQuantity": 8, "matchedAmount": sar(100), "quantityVariance": -2,
                            "varianceReasons": [{"varianceType": "PQV", "defectTypes": ["SHORTAGES_IN_TRANSIT"]}]}}}]
    run = amazon.sync()
    assert "1 invoice status update" in run.message, run.message
    inv.refresh_from_db()
    assert (inv.amazon_status, inv.amazon_finance_status) == ("on_hold", "IN_PROGRESS")
    assert f"{l.sku.model_no}: 2 units (shortage in transit)" in inv.amazon_note and inv.amazon_settlement == {"QuantityVarianceAmount": 46000}
    assert Notification.objects.filter(text__contains=f"Amazon is holding back part of invoice {inv.invoice_no}").exists()
    html = as_user("priya").get(f"/records/po/{inv.po.po_no}/?tab=invoice").content.decode()
    assert "Held back: quantity" in html and "SAR 460" in html and "shortage in transit" in html
    head.update(invoiceStatus="PAID", invoiceSettlementBreakdowns=[{"breakdownType": "AmountPaid", "breakdownAmount": sar(inv.total_h)}])
    amazon.sync()
    inv.refresh_from_db()
    assert (inv.amazon_status, inv.amazon_finance_status, inv.amazon_note, inv.amazon_variance) == ("accepted", "PAID", "", "")


def test_an_amazon_charge_for_an_agreement_becomes_a_checked_debit_note(fake):
    from debitnotes.models import DebitNote
    from integrations.models import AmazonCharge
    from promotions.models import Promotion
    from debitnotes.services import evaluate
    promo = Promotion.objects.filter(agreement_no__isnull=False).exclude(agreement_no__in=DebitNote.objects.values("agreement_no")).first()
    l = promo.lines.select_related("sku").first()
    fake.fin_invoices = [{"invoiceNumber": "VCDN-7000001", "invoiceIdentifier": "VCDN-7000001", "invoiceType": "OTHER", "invoiceStatus": "DEDUCTED",
                          "issueDate": "2026-10-01T00:00:00Z", "invoiceAmount": sar(l.support_h * 30),
                          "relatedIdentifiers": [{"relatedIdentifierName": "AGREEMENT_IDS", "relatedIdentifierValue": [promo.agreement_no]}]}]
    fake.fin_items["VCDN-7000001"] = [{"lineItemId": "1", "quantity": 30, "unitCost": sar(l.support_h), "totalAmount": sar(l.support_h * 30),
                                       "lineItemContext": {"lineItemType": "SERVICES", "itemDescription": "Promotion funding",
                                                           "itemIdentifiers": [{"itemIdentifierName": "ASIN", "itemIdentifierValue": l.sku.asin}]}}]
    run = amazon.sync()
    assert "1 Amazon charge" in run.message
    c = AmazonCharge.objects.get(invoice_no="VCDN-7000001")
    dn = DebitNote.objects.get(dn_no="VCDN-7000001")
    assert (c.kind, c.debit_note_no, dn.agreement_no) == ("COOP", "VCDN-7000001", promo.agreement_no)
    assert [(x.sku, x.units, x.rate_h) for x in dn.lines.all()] == [(l.sku, 30, l.support_h)]
    assert evaluate(dn)["status"] != "unlinked"
    amazon.sync()                                                     # unchanged charge: not fetched or created again
    assert DebitNote.objects.filter(dn_no="VCDN-7000001").count() == 1


def test_a_remittance_pays_invoices_and_puts_each_deduction_on_its_invoice(fake):
    from integrations.models import AmazonCharge, AmazonRemittance
    from payments.models import Payment
    a, b, c = list(Invoice.objects.filter(po__stage="invoiced", payments__isnull=True).select_related("po").order_by("invoice_no")[:3])
    AmazonCharge.objects.create(invoice_no="PPA-55001", kind="PRICE_PROTECTION", po_nos=[c.po.po_no], amount_h=50000)
    uid = "756473657"
    fake.remits = [{"uniquePaymentId": uid, "paymentNumber": "355522363", "paymentDate": "2026-10-12T00:00:00Z", "remittanceStatus": "SUCCESSFUL",
                    "paymentAmount": sar(1), "relatedIdentifiers": [{"relatedIdentifierName": "VENDOR_CODE", "relatedIdentifierValue": ["MEAUD"]}]},
                   {"uniquePaymentId": "999", "paymentNumber": "1", "paymentDate": "2026-10-12T00:00:00Z", "remittanceStatus": "IN_PROGRESS", "paymentAmount": sar(1)}]
    line = lambda no, h, desc="": {"invoiceNumber": no, "totalAmount": sar(h), "netAmountPaid": sar(h), "description": desc, "invoiceType": "Standard"}
    fake.remit_items[uid] = [line(a.invoice_no, a.total_h), line(b.invoice_no, b.total_h), line(b.invoice_no + "SCR", -230000, "Shortage claim"),
                             line(c.invoice_no, c.total_h), line("PPA-55001", -50000, "Price protection"), line("XYZ-0001", 120000, "Payment")]
    run = amazon.sync()
    assert "1 remittance," in run.message, run.message
    pa, pb, pc = (Payment.objects.get(payment_no=f"355522363/{i.invoice_no}") for i in (a, b, c))
    assert pa.status == "matched" and pa.invoice == a
    assert (pb.status, pb.deduction_h, pb.paid_h) == ("short", 230000, b.total_h - 230000) and f"{b.invoice_no}SCR" in pb.reason
    assert (pc.status, pc.deduction_h) == ("short", 50000) and pc.reason.startswith("Price protection PPA-55001")
    assert Payment.objects.get(payment_no="355522363/XYZ-0001").status == "unmatched"
    assert Payment.objects.get(payment_no="355522363/XYZ-0001").vendor_code == "MEAUD"
    assert AmazonRemittance.objects.get(unique_payment_id=uid).loaded and not AmazonRemittance.objects.get(unique_payment_id="999").loaded
    n = Payment.objects.count()
    amazon.sync()                                                     # a loaded payment is never loaded twice
    assert Payment.objects.count() == n


def test_without_the_finance_role_everything_else_still_syncs(fake):
    s, fc = a_sku(), FulfilmentCentre.objects.first()
    fake.refuse_finance = True
    fake.orders = [order("AMZ0099A", s, fc)]
    run = amazon.sync()
    assert run.ok and "1 new PO" in run.message and "invoice status not synced (403: needs the Finance and Accounting role)" in run.message
    assert "remittances not synced (403" in run.message
    assert Notification.objects.filter(text__startswith="Amazon invoice status could not be read").count() == 1
    amazon.sync()
    assert Notification.objects.filter(text__startswith="Amazon invoice status could not be read").count() == 1   # once a day


# ---------- review fixes ----------
def _remit(fake, uid, pno, items, status="SUCCESSFUL"):
    fake.remits.append({"uniquePaymentId": uid, "paymentNumber": pno, "paymentDate": "2026-10-12T00:00:00Z", "remittanceStatus": status,
                        "paymentAmount": sar(1)})
    fake.remit_items[uid] = items


def _line(no, h, desc=""):
    return {"invoiceNumber": no, "totalAmount": sar(h), "netAmountPaid": sar(h), "description": desc, "invoiceType": "Standard"}


def test_every_payment_line_gets_its_own_payment_even_with_long_numbers(fake):
    from payments.models import Payment
    invs = list(Invoice.objects.filter(po__stage="invoiced", payments__isnull=True).order_by("invoice_no")[:2])
    _remit(fake, "U1", "PAYMENT-NUMBER-0000012345678", [_line(i.invoice_no, i.total_h) for i in invs] + [_line("", 5000, "a"), _line("", 7000, "b")])
    amazon.sync()
    got = Payment.objects.filter(remit_date__date="2026-10-12").exclude(payment_no__startswith="RMT")
    assert got.count() == 4 and all(len(p.payment_no) <= 30 for p in got)
    assert {p.invoice for p in got if p.invoice} == set(invs) and all(p.status == "matched" for p in got if p.invoice)


def test_a_payment_already_uploaded_is_not_loaded_again(fake):
    from integrations.models import AmazonRemittance
    from payments.models import Payment
    p = Payment.objects.filter(status="matched").first()
    _remit(fake, "U2", p.payment_no, [_line(p.invoice_ref, p.paid_h)])
    n = Payment.objects.count()
    amazon.sync()
    assert Payment.objects.count() == n
    assert AmazonRemittance.objects.get(unique_payment_id="U2").message.startswith("Already loaded by remittance upload")


def test_a_deduction_on_its_own_records_only_the_deduction(fake):
    from integrations.models import AmazonCharge
    from payments.models import Payment
    inv = Invoice.objects.filter(po__stage="invoiced", payments__isnull=True).select_related("po").order_by("invoice_no").first()
    AmazonCharge.objects.create(invoice_no="PPA-1", kind="PRICE_PROTECTION", po_nos=[inv.po.po_no], amount_h=50000)
    _remit(fake, "U3", "P3", [_line("PPA-1", -50000)])
    amazon.sync()
    d = Payment.objects.get(payment_no="P3/" + inv.invoice_no)
    assert (d.status, d.paid_h, d.deduction_h) == ("short", 0, 50000)
    _remit(fake, "U4", "P4", [_line(inv.invoice_no, inv.total_h)])          # the invoice itself is paid later
    amazon.sync()
    assert Payment.objects.get(payment_no="P4/" + inv.invoice_no).status == "matched"
    d.refresh_from_db()
    assert (d.status, d.deduction_h) == ("short", 50000)                      # the deduction still stands


def test_the_sync_does_not_undo_what_a_person_did_after_amazons_last_news(fake, as_user):
    inv = Invoice.objects.filter(amazon_status="submitted").select_related("po").first()
    ident = f"{inv.invoice_no}~P"
    head = {"invoiceNumber": inv.invoice_no, "invoiceIdentifier": ident, "invoiceType": "AP_INVENTORY", "invoiceStatus": "IN_PROGRESS",
            "invoiceAmount": sar(inv.total_h), "invoiceSettlementBreakdowns": [{"breakdownType": "PriceVariance", "breakdownAmount": sar(1000)}]}
    fake.fin_invoices, fake.fin_items[ident] = [head], []
    amazon.sync()
    inv.refresh_from_db()
    assert inv.amazon_status == "on_hold"
    as_user("priya").post(f"/pos/{inv.po.po_no}/invoice/resubmit/", {"invoice_no": inv.invoice_no})
    inv.refresh_from_db()
    assert (inv.amazon_status, inv.revision) == ("submitted", 1)
    amazon.sync()                                     # Amazon says the same as before: the resubmission stands
    inv.refresh_from_db()
    assert (inv.amazon_status, inv.revision) == ("submitted", 1)


def test_finance_catches_up_after_a_refusal_and_remittances_do_not_depend_on_invoices(fake):
    from payments.models import Payment
    inv = Invoice.objects.filter(po__stage="invoiced", payments__isnull=True).order_by("invoice_no").first()
    fake.refuse_finance = "/finances/invoices"
    _remit(fake, "U5", "P5", [_line(inv.invoice_no, inv.total_h)])
    first = timezone.now()
    run = amazon.sync()
    assert "invoice status not synced" in run.message and Payment.objects.filter(payment_no="P5/" + inv.invoice_no).exists()
    fake.refuse_finance = False
    fake.calls.clear()
    amazon.sync()
    since = next(q["invoicesModifiedAfter"] for m, path, q in fake.calls if path == "/finances/invoices/2026-06-25/invoices")
    assert parse_iso(since) < first - timedelta(days=29)                     # not just since the last (failed) pass


def parse_iso(v):
    from datetime import datetime
    return datetime.fromisoformat(v.replace("Z", "+00:00"))


def test_one_bad_record_does_not_stop_the_others(fake):
    a, b = list(Invoice.objects.filter(amazon_status="submitted").order_by("invoice_no")[:2])
    bad = {"invoiceNumber": a.invoice_no, "invoiceType": "AP_INVENTORY", "invoiceStatus": "REJECTED", "invoiceAmount": sar(1)}   # no identifier
    good = {"invoiceNumber": b.invoice_no, "invoiceIdentifier": "B~1", "invoiceType": "AP_INVENTORY", "invoiceStatus": "PAID", "invoiceAmount": sar(1)}
    fake.fin_invoices = [bad, good]
    run = amazon.sync()
    b.refresh_from_db()
    assert run.ok and b.amazon_status == "accepted"
    assert Notification.objects.filter(text__startswith=f"Amazon invoice {a.invoice_no} could not be loaded").exists()
