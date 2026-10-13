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
