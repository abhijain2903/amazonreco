"""Regressions found in the review of phases A–C: instalment payments, cancelling part-shipped POs, fully
backordered POs, stock reservation after booking, R10 with fee lines / repeated models / disputed earlier DNs,
batch credit notes, per-invoice commands, ageing, recovery matching and over-received returns."""
from datetime import timedelta

import pytest
from django.utils import timezone

from catalog.models import Sku
from debitnotes.services import _validate, create_dn, evaluate
from fulfilment.services import open_qty, shipped_qty, to_ship
from orders.models import PurchaseOrder
from orders.services import _release, line_checks
from payments.models import Dispute, Payment
from payments.services import import_payment
from promotions.models import Promotion
from promotions.services import stage_of
from rules import engine
from rules.services import get_cfg

from .conftest import toast

pytestmark = pytest.mark.django_db


def test_invoice_paid_in_two_instalments_closes_the_po():
    po = PurchaseOrder.objects.filter(stage="invoiced").exclude(payments__isnull=False).first()
    inv = po.invoice
    half = inv.total_h // 2
    p1 = import_payment("RMT-I1", timezone.now(), inv.invoice_no, half)
    assert p1.status == "short"
    p2 = import_payment("RMT-I2", timezone.now(), inv.invoice_no, inv.total_h - half)
    p1.refresh_from_db()
    po.refresh_from_db()
    assert p2.status == "matched" and p1.status == "matched" and po.stage == "paid"


def test_cancel_after_part_shipped_keeps_what_was_delivered(as_user):
    po = PurchaseOrder.objects.get(stage="backorder")
    shipped = sum(shipped_qty(po).values())
    as_user("faisal").post(f"/pos/{po.po_no}/change/", {"cancel": "1", "reason": "Amazon cancelled the backorder"})
    po.refresh_from_db()
    assert po.stage == "invoiced" and open_qty(po) == 0
    assert sum(l.qty_ordered for l in po.lines.all()) == shipped


def test_fully_backordered_po_goes_straight_to_backorder():
    po = PurchaseOrder.objects.filter(stage="booked").first()
    for l in po.lines.all():
        l.decision, l.qty_backorder, l.qty_confirmed = "backorder", l.committed, 0
        l.save()
    assert not to_ship(po)
    _release(po, timezone.now(), with_delivery=True)
    po.refresh_from_db()
    assert po.stage == "backorder" and not po.sap_deliveries.exists()


def test_stock_stays_reserved_after_booking():
    a = PurchaseOrder.objects.filter(stage="new").order_by("confirm_by").first()
    la = a.lines.select_related("sku").first()
    sku = la.sku
    later = PurchaseOrder.objects.filter(stage="new").order_by("confirm_by").last()
    lb = later.lines.create(position=99, sku=sku, asin=sku.asin, qty_ordered=5, cost_h=la.cost_h)
    Sku.objects.filter(pk=sku.pk).update(stock_as_of=timezone.now() - timedelta(days=1))
    PurchaseOrder.objects.filter(pk=a.pk).update(stage="booked", confirmed_at=timezone.now())
    lb.refresh_from_db()
    assert line_checks(lb)["reserved"] >= la.qty_confirmed


def test_r10_two_fee_lines_and_a_model_on_two_lines():
    now = timezone.now()
    r = engine.dn_check([{"sku": None, "units": 1, "rate_h": 5000_00}, {"sku": None, "units": 1, "rate_h": 3000_00}],
                        {}, now, now - timedelta(days=1), get_cfg(), fees_h=8000_00)
    assert r["ok"] and r["variance_h"] == 0
    r = engine.dn_check([{"sku": 1, "units": 60, "rate_h": 10_00}, {"sku": 1, "units": 60, "rate_h": 10_00}],
                        {1: {"support_h": 10_00, "sold": 100}}, now, now - timedelta(days=1), get_cfg())
    assert not r["ok"] and r["variance_h"] == 20 * 10_00      # 120 billed against 100 sold


def test_disputed_excess_of_an_earlier_dn_is_not_counted_as_billed():
    p = next(p for p in Promotion.objects.filter(stage="approved").prefetch_related("lines") if p.agreement_no and stage_of(p) in ("waiting_dn", "dn_overdue"))
    l = p.lines.first()
    l.sold_units = 100
    l.save()
    p.dn_instalments = True
    p.save()
    d1 = create_dn("VCDN-R1", p.agreement_no, p.end + timedelta(days=2), [(l.sku, 120, l.support_h)])
    _validate(d1, timezone.now(), mode="dispute")                # 100 approved, 20 disputed
    d2 = create_dn("VCDN-R2", p.agreement_no, p.end + timedelta(days=3), [(l.sku, 0, l.support_h)])
    assert evaluate(d2)["lines"][0]["billed_before"] == 100


def test_batch_credit_note_with_nothing_owed_is_refused():
    from claims.models import Claim
    from claims.services import record_batch_cn
    from identity.models import User
    c = Claim.objects.filter(status="sent").first()
    Claim.objects.filter(pk=c.pk).update(batch_no="CLB-T", cn_h=c.amount_h, status="shortfall")
    from core.services import CommandError
    with pytest.raises(CommandError):
        record_batch_cn(User.objects.get(username="priya"), "CLB-T", "CN-X", 100_00)


def test_hold_is_recorded_on_the_invoice_named(as_user):
    po = PurchaseOrder.objects.filter(stage="invoiced").first()
    inv = po.invoice
    r = as_user("priya").post(f"/pos/{po.po_no}/invoice/status/", {"status": "on_hold", "note": "Qty", "invoice_no": inv.invoice_no})
    assert toast(r)["tone"] != "bad"
    inv.refresh_from_db()
    assert inv.amazon_status == "on_hold"
    assert toast(as_user("priya").post(f"/pos/{po.po_no}/invoice/status/", {"status": "on_hold", "note": "x", "invoice_no": "NOPE"}))["tone"] == "bad"


def test_ageing_leaves_out_settled_invoices():
    from payments.views import ageing
    rows, _, _ = ageing()
    nos = {r["inv"].invoice_no for r in rows}
    accepted = Payment.objects.filter(status="accepted", invoice__isnull=False, po__stage="invoiced")
    assert not any(p.invoice.invoice_no in nos for p in accepted if all(q.status in ("matched", "accepted", "recovered") for q in p.invoice.payments.all()))


def test_a_short_case_id_does_not_capture_payments():
    d = Dispute.objects.filter(status="submitted").first()
    Dispute.objects.filter(pk=d.pk).update(amazon_case_id="-")
    po = PurchaseOrder.objects.filter(stage="invoiced").exclude(payments__isnull=False).first()
    p = import_payment("RMT-CID", timezone.now(), po.invoice.invoice_no, po.invoice.total_h, 0, "Payment")
    d.refresh_from_db()
    assert p.status == "matched" and d.recovered_in == ""


def test_return_received_is_capped_at_what_was_requested(as_user):
    from returns.models import ReturnAuth
    r = ReturnAuth.objects.get(status="authorised")
    l = r.lines.first()
    as_user("khalid").post(f"/returns/{r.rtv_no}/receive/", {f"r-{l.pk}": l.qty + 50})
    l.refresh_from_db()
    assert l.qty_received == l.qty
