"""Gap-map phase C: backorders, several shipments per PO with an invoice each, and returns to vendor (RTV)."""
import pytest
from django.utils import timezone

from billing.models import Invoice
from fulfilment.services import delivery_of, open_qty, shipped_qty
from orders.models import PurchaseOrder
from payments.models import Dispute, Payment
from payments.services import import_payment
from returns.models import ReturnAuth

from .conftest import toast

pytestmark = pytest.mark.django_db


def run_cycle(c, po):
    """ASN → slot → delivered → invoice for the PO's current delivery, through the views."""
    assert toast(c.post(f"/pos/{po.po_no}/asn/"))["tone"] != "bad"
    c.post(f"/pos/{po.po_no}/slot/", {"slot_id": f"CC-{po.shipments.count()}-T", "date": "2030-01-01", "window": "08:00–12:00"})
    assert toast(c.post(f"/pos/{po.po_no}/deliver/"))["tone"] != "bad"
    po.refresh_from_db()
    if po.stage == "delivered":
        c.post(f"/pos/{po.po_no}/fix-billing/")
    r = c.post(f"/pos/{po.po_no}/invoice/")
    assert toast(r)["tone"] != "bad", toast(r)
    po.refresh_from_db()
    return po


# ---------- backorders ----------
def test_backorder_line_on_confirmation(as_user):
    po = PurchaseOrder.objects.filter(stage="new").first()
    l = po.lines.select_related("sku").first()
    c = as_user("faisal")
    c.post(f"/pos/{po.po_no}/lines/", {f"d-{l.pk}": "backorder"}, HTTP_HX_TRIGGER_NAME=f"d-{l.pk}")
    l.refresh_from_db()
    assert l.decision == "backorder" and l.qty_confirmed + l.qty_backorder == l.qty_ordered and l.backorder_eta
    c.post(f"/pos/{po.po_no}/lines/", {f"q-{l.pk}": "1", f"e-{l.pk}": "2030-02-01"})
    l.refresh_from_db()
    assert l.qty_confirmed == 1 and l.qty_backorder == l.qty_ordered - 1 and l.backorder_eta.isoformat() == "2030-02-01"


def test_backorder_ships_second_shipment_with_its_own_invoice_then_pays(as_user):
    po = PurchaseOrder.objects.get(stage="backorder")
    left = open_qty(po)
    assert left > 0 and po.invoices.count() == 1
    logi, fin = as_user("khalid"), as_user("priya")
    assert b"still to ship" in logi.get(f"/records/po/{po.po_no}/?tab=shipment").content
    r = logi.post(f"/pos/{po.po_no}/backorder/ship/")
    assert toast(r)["tone"] != "bad"
    po.refresh_from_db()
    d = delivery_of(po)
    assert po.stage == "released" and d and d.seq == 2 and sum(l.qty for l in d.lines.all()) == left
    po = run_cycle(as_user("faisal"), po)
    assert po.stage == "invoiced" and po.invoices.count() == 2 and open_qty(po) == 0
    assert sorted(po.invoices.values_list("seq", flat=True)) == [1, 2]
    assert sum(shipped_qty(po).values()) == sum(l.committed for l in po.lines.all())
    # Pay invoice 2 first: the PO is not paid while invoice 1 is open
    i1, i2 = po.invoices.order_by("seq")
    import_payment("RMT-C2", timezone.now(), i2.invoice_no, i2.total_h)
    po.refresh_from_db()
    assert po.stage == "invoiced"
    import_payment("RMT-C1", timezone.now(), i1.invoice_no, i1.total_h)
    po.refresh_from_db()
    assert po.stage == "paid"
    assert b"Invoices on this PO" in fin.get(f"/records/po/{po.po_no}/?tab=invoice").content


def test_close_backorder_ends_on_what_shipped(as_user):
    po = PurchaseOrder.objects.get(stage="backorder")
    c = as_user("faisal")
    assert toast(c.post(f"/pos/{po.po_no}/backorder/close/", {"reason": ""}))["tone"] == "bad"
    c.post(f"/pos/{po.po_no}/backorder/close/", {"reason": "Amazon cancelled the rest"})
    po.refresh_from_db()
    assert po.stage == "invoiced" and open_qty(po) == 0


def test_amazon_cuts_the_backorder(as_user):
    po = PurchaseOrder.objects.get(stage="backorder")
    l = next(l for l in po.lines.all() if l.qty_backorder)
    shipped = shipped_qty(po)[l.sku_id]
    c = as_user("faisal")
    assert toast(c.post(f"/pos/{po.po_no}/change/", {f"n-{l.pk}": shipped - 1}))["tone"] == "bad"     # below what shipped
    c.post(f"/pos/{po.po_no}/change/", {f"n-{l.pk}": shipped})
    po.refresh_from_db()
    l.refresh_from_db()
    assert l.qty_backorder == 0 and po.stage == "invoiced"


def test_short_delivery_leaves_the_rest_open(as_user):
    po = next(p for p in PurchaseOrder.objects.filter(stage="released") if delivery_of(p)
              and sum(l.qty for l in delivery_of(p).lines.all()) < sum(l.qty_confirmed for l in p.lines.all()))
    po = run_cycle(as_user("faisal"), po)
    assert po.stage == "backorder" and open_qty(po) > 0


# ---------- returns (RTV) ----------
def test_return_authorise_receive_and_dispute_the_overcharge(as_user):
    r = ReturnAuth.objects.get(status="requested")
    fin, logi = as_user("priya"), as_user("khalid")
    assert toast(logi.post(f"/returns/{r.rtv_no}/authorise/"))["tone"] == "bad"        # logistics cannot authorise
    fin.post(f"/returns/{r.rtv_no}/authorise/")
    r.refresh_from_db()
    assert r.status == "authorised"
    l1, l2 = r.lines.all()
    logi.post(f"/returns/{r.rtv_no}/receive/", {f"r-{l1.pk}": l1.qty - 1, f"c-{l1.pk}": "good", f"r-{l2.pk}": l2.qty, f"c-{l2.pk}": "damaged"})
    r.refresh_from_db()
    assert r.status == "received" and r.received_h == r.amount_h - l1.unit_cost_h
    # Amazon deducts the full requested value
    po = PurchaseOrder.objects.filter(stage="invoiced").exclude(payments__isnull=False).first()
    inv = po.invoice
    p = import_payment("RMT-RTV-T", timezone.now(), inv.invoice_no, inv.total_h - r.amount_h, r.amount_h, "Vendor returns")
    assert p.status == "short"
    fin.post(f"/returns/{r.rtv_no}/link/", {"payment_no": p.payment_no})
    r.refresh_from_db()
    d = Dispute.objects.get(case_no=r.dispute_no)
    assert r.status == "disputed" and d.type == "returns" and d.amount_h == l1.unit_cost_h


def test_seeded_received_return_matches_its_deduction(as_user):
    r = ReturnAuth.objects.get(status="received")
    assert b"Match Amazon" in as_user("priya").get(f"/records/rtv/{r.rtv_no}/").content
    p = Payment.objects.get(status="short", reason__icontains=r.rtv_no)
    as_user("priya").post(f"/returns/{r.rtv_no}/link/", {"payment_no": p.payment_no})
    r.refresh_from_db()
    assert r.status == "disputed"            # one unit never arrived, so Amazon over-deducted


def test_refuse_and_new_return(as_user):
    r = ReturnAuth.objects.get(status="requested")
    fin = as_user("priya")
    assert toast(fin.post(f"/returns/{r.rtv_no}/refuse/", {"reason": ""}))["tone"] == "bad"
    fin.post(f"/returns/{r.rtv_no}/refuse/", {"reason": "Not returnable under the agreement"})
    r.refresh_from_db()
    assert r.status == "refused"
    from catalog.models import Sku
    s = Sku.objects.first()
    res = fin.post("/returns/new/", {"rtv_no": "RTV-T1", "reason": "recall", "sku-0": s.sku_code, "qty-0": "3"})
    assert toast(res)["tone"] != "bad"
    n = ReturnAuth.objects.get(rtv_no="RTV-T1")
    assert n.amount_h == 3 * n.lines.get().unit_cost_h and n.reason == "recall"


def test_returns_page_and_action_items(as_user):
    c = as_user("admin")
    for tab in ("todo", "transit", "received", "closed", "all"):
        assert c.get(f"/returns/?tab={tab}").status_code == 200
    assert c.get("/returns/?export=xlsx").status_code == 200
    assert b"Authorise or refuse return" in c.get("/action/").content


def test_u10_upload_creates_returns(as_user):
    from uploads.models import UploadBatch
    c = as_user("admin")
    r = c.post("/uploads/new/", {"type": "U10", "sample": "1"})
    assert r.status_code == 200
    b = UploadBatch.objects.latest("created_at")
    c.post(f"/uploads/{b.pk}/preview/")
    c.post(f"/uploads/{b.pk}/commit/")
    new = ReturnAuth.objects.exclude(rtv_no__in=["RTV-118842", "RTV-118517", "RTV-117903"]).get()
    assert new.status == "requested" and new.lines.count() == 3 and new.reason == "defective"
