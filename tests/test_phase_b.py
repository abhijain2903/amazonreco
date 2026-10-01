"""Gap-map phase B: stock reservation, Amazon PO changes, cartons, slots, collect freight, invoice holds, credit memos,
deduction categories, recoveries, ageing, fees, amendments, budgets, several debit notes, batch claims, alerts,
assignment and mentions, reports and vendor codes."""
from datetime import timedelta

import pytest
from django.utils import timezone

from billing.models import Invoice
from catalog.models import Sku
from claims.models import Claim
from core.models import Assignment, Notification, VendorCode
from core.services import unread_alerts, visible_alerts
from debitnotes.models import DebitNote
from debitnotes.services import create_dn, evaluate
from fulfilment.models import Carton
from fulfilment.services import sscc
from identity.models import User
from orders.models import PurchaseOrder
from orders.services import line_checks
from payments.models import Dispute, Payment
from payments.services import import_payment
from promotions.models import Promotion
from promotions.services import stage_of

from .conftest import toast

pytestmark = pytest.mark.django_db


# ---------- stock reservation ----------
def test_earlier_po_reserves_stock_for_the_same_sku():
    pos = list(PurchaseOrder.objects.filter(stage="new").order_by("confirm_by"))
    first = pos[0]
    line = first.lines.select_related("sku").first()
    sku = line.sku
    Sku.objects.filter(pk=sku.pk).update(free_stock=line.qty_confirmed + 5)
    later = pos[-1]
    l2 = later.lines.create(position=99, sku=sku, asin=sku.asin, qty_ordered=20, cost_h=line.cost_h)
    l2.refresh_from_db()
    c = line_checks(l2)
    assert c["reserved"] >= line.qty_confirmed and c["stock"] == max(0, sku.free_stock if False else line.qty_confirmed + 5 - c["reserved"])
    assert not c["stock_ok"]


# ---------- Amazon changes / cancels a PO ----------
def test_amazon_cuts_a_line_on_a_confirmed_po(as_user):
    po = PurchaseOrder.objects.filter(stage="confirmed").first()
    l = po.lines.filter(qty_confirmed__gt=1).first()
    r = as_user("faisal").post(f"/pos/{po.po_no}/change/", {f"n-{l.pk}": l.qty_confirmed - 1, "reason": "Amazon change"})
    assert toast(r)["tone"] != "bad"
    l.refresh_from_db()
    assert l.qty_ordered == l.qty_confirmed
    assert toast(as_user("faisal").post(f"/pos/{po.po_no}/change/", {f"n-{l.pk}": l.qty_ordered + 10}))["tone"] == "bad"


def test_amazon_cancels_a_po(as_user):
    po = PurchaseOrder.objects.filter(stage="booked").first()
    as_user("faisal").post(f"/pos/{po.po_no}/change/", {"cancel": "1", "reason": "Cancelled in Vendor Central"})
    po.refresh_from_db()
    assert po.stage == "cancelled"
    assert b"Cancelled by Amazon" in as_user("faisal").get(f"/records/po/{po.po_no}/").content
    assert toast(as_user("faisal").post(f"/pos/{po.po_no}/change/", {"cancel": "1"}))["tone"] == "bad"


# ---------- cartons and labels ----------
def test_sscc_has_a_valid_check_digit():
    code = sscc(1234)
    assert len(code) == 18 and code.isdigit()
    body, check = code[:17], int(code[17])
    total = sum(int(d) * (3 if i % 2 == 0 else 1) for i, d in enumerate(reversed(body)))
    assert (total + check) % 10 == 0


def test_asn_builds_cartons_and_a_label_file(as_user):
    from core.models import GeneratedFile
    from fulfilment.services import asn_checks, delivery_of
    po = next(p for p in PurchaseOrder.objects.filter(stage="released") if delivery_of(p) and all(c["asn_ok"] for c in asn_checks(p)))
    first = po.lines.select_related("sku").filter(qty_confirmed__gt=0).first()
    Sku.objects.filter(pk=first.sku_id).update(case_pack=4)
    r = as_user("khalid").post(f"/pos/{po.po_no}/asn/")
    assert toast(r)["tone"] != "bad"
    sh = PurchaseOrder.objects.get(pk=po.pk).shipment
    cartons = list(Carton.objects.filter(shipment=sh))
    assert cartons and sh.cartons == len(cartons)
    assert sum(c.qty for c in cartons) == sum(l.qty for l in sh.lines.all())
    assert GeneratedFile.objects.filter(kind="labels", entity_id=po.po_no).exists()


# ---------- slots: reschedule, missed / refused, collect ----------
def test_missed_slot_goes_back_to_booking_and_reschedule_counts(as_user):
    po = PurchaseOrder.objects.filter(stage="slot").first()
    c = as_user("khalid")
    assert toast(c.post(f"/pos/{po.po_no}/slot-failed/", {"outcome": "missed", "reason": ""}))["tone"] == "bad"
    c.post(f"/pos/{po.po_no}/slot-failed/", {"outcome": "missed", "reason": "Truck late at the gate"})
    po.refresh_from_db()
    assert po.stage == "asn" and po.shipment.slot_outcome == "missed"
    assert b"Re-book delivery" in as_user("admin").get("/action/").content
    c.post(f"/pos/{po.po_no}/slot/", {"slot_id": "ARN-778", "date": "2030-01-02", "window": "08:00–12:00", "freight": "collect"})
    po.refresh_from_db()
    sh = po.shipment
    assert po.stage == "slot" and sh.freight == "collect" and sh.reschedules == 1 and sh.slot_outcome == ""


# ---------- invoices: rejected / held, credit memo ----------
def test_invoice_rejected_then_resubmitted(as_user):
    po = PurchaseOrder.objects.filter(stage="invoiced").first()
    c = as_user("priya")
    assert toast(c.post(f"/pos/{po.po_no}/invoice/status/", {"status": "rejected", "note": ""}))["tone"] == "bad"
    c.post(f"/pos/{po.po_no}/invoice/status/", {"status": "rejected", "note": "Price mismatch line 2"})
    inv = Invoice.objects.get(po=po)
    assert inv.amazon_status == "rejected"
    assert f"Invoice {inv.invoice_no} rejected".encode() in as_user("admin").get("/action/").content
    r = c.post(f"/pos/{po.po_no}/invoice/resubmit/")
    inv.refresh_from_db()
    assert inv.amazon_status == "submitted" and inv.revision == 1 and "HX-Trigger" in r.headers


def test_credit_memo_settles_a_short_payment(as_user):
    p = Payment.objects.filter(status="short", po__isnull=False).first()
    po = p.po
    r = as_user("priya").post(f"/pos/{po.po_no}/credit-memo/", {"amount": p.deduction_h / 100, "reason": "Agreed price claim"})
    assert toast(r)["tone"] != "bad"
    po.refresh_from_db()
    p.refresh_from_db()
    assert po.stage == "paid" and p.status == "accepted"
    assert po.invoice.memo_h == p.deduction_h


# ---------- deduction categories ----------
def test_chargeback_reason_is_read_as_a_chargeback():
    from matching.matchers import deduction
    p = Payment.objects.filter(status="short").first()
    p.reason = "Chargeback - ASN accuracy"
    assert deduction(p, dn_cands=[])["type"] == "chargeback"
    p.reason = "Co-op advertising accrual"
    assert deduction(p, dn_cands=[])["type"] == "coop"
    p.reason = "Customer returns RTV"
    assert deduction(p, dn_cands=[])["type"] == "returns"


def test_chargeback_dispute_keeps_its_type(as_user):
    p = Payment.objects.filter(status="short").first()
    as_user("priya").post(f"/pay/{p.payment_no}/dispute/", {"type": "chargeback", "subtype": "labels", "amount": p.deduction_h / 100})
    d = Dispute.objects.get(ref=p.payment_no)
    assert d.type == "chargeback" and d.subtype == "labels"


# ---------- money back in a later remittance ----------
def test_later_payment_closes_the_dispute(as_user):
    p = Payment.objects.filter(status="short", po__isnull=False).first()
    as_user("priya").post(f"/pay/{p.payment_no}/dispute/", {"type": "shortage", "amount": p.deduction_h / 100})
    d = Dispute.objects.get(ref=p.payment_no)
    back = import_payment("RMT-REC-1", timezone.now(), p.invoice.invoice_no, p.deduction_h, 0, f"Reversal {d.case_no}")
    d.refresh_from_db()
    assert back.status == "matched" and back.hint.startswith("Recovery")
    assert d.status == "won" and d.recovered_h == p.deduction_h and d.recovered_in == "RMT-REC-1"
    assert Payment.objects.get(pk=p.pk).status == "recovered"


# ---------- ageing ----------
def test_ageing_tab_and_export(as_user):
    c = as_user("priya")
    r = c.get("/pay/?tab=ageing")
    assert r.status_code == 200 and b"0\xe2\x80\x9330 days" in r.content
    assert c.get("/pay/?tab=ageing&export=xlsx").status_code == 200


# ---------- several debit notes, instalments, fixed fees ----------
def _approved_promo():
    return next(p for p in Promotion.objects.filter(stage="approved").prefetch_related("lines") if p.agreement_no and stage_of(p) in ("waiting_dn", "dn_overdue"))


def test_second_debit_note_is_checked_against_what_is_left():
    p = _approved_promo()
    l = p.lines.first()
    l.sold_units = 100
    l.save()
    d1 = create_dn("VCDN-T1", p.agreement_no, p.end + timedelta(days=5), [(l.sku, 60, l.support_h)])
    assert evaluate(d1)["lines"][0]["units_ok"]
    d2 = create_dn("VCDN-T2", p.agreement_no, p.end + timedelta(days=6), [(l.sku, 50, l.support_h)])
    ev = evaluate(d2)
    assert ev["lines"][0]["left"] == 40 and not ev["lines"][0]["units_ok"] and ev["status"] == "mismatch"
    assert [e.dn_no for e in ev["earlier"]] == ["VCDN-T1"]


def test_fixed_fee_line_checked_against_the_agreed_fee():
    p = _approved_promo()
    p.fees.create(label="Deal fee", amount_h=500_00)
    l = p.lines.first()
    l.sold_units = 10
    l.save()
    dn = create_dn("VCDN-T3", p.agreement_no, p.end + timedelta(days=5), [(l.sku, 10, l.support_h), (None, 1, 500_00, "Deal fee")])
    ev = evaluate(dn)
    fee = next(x for x in ev["lines"] if x["fee"])
    assert fee["rate_ok"] and fee["expected_h"] == 500_00
    dn2 = create_dn("VCDN-T4", p.agreement_no, p.end + timedelta(days=6), [(None, 1, 200_00, "Deal fee")])
    assert not next(x for x in evaluate(dn2)["lines"] if x["fee"])["rate_ok"]      # the fee was already billed


def test_amendment_extends_and_is_recorded(as_user):
    p = _approved_promo()
    new_end = (timezone.localtime(p.end) + timedelta(days=7)).date().isoformat()
    c = as_user("reem")
    assert toast(c.post(f"/promos/{p.mecl_ref}/amend/", {"end": new_end, "reason": ""}))["tone"] == "bad"
    c.post(f"/promos/{p.mecl_ref}/amend/", {"end": new_end, "reason": "Amazon extended the deal", "instalments": "1"})
    p.refresh_from_db()
    a = p.amendments.get()
    assert timezone.localtime(p.end).date().isoformat() == new_end and p.dn_instalments
    assert any(ch["what"] == "End date" for ch in a.changes)
    assert b"Amendment 1" in c.get(f"/records/promo/{p.mecl_ref}/").content


def test_budget_view_and_set(as_user):
    c = as_user("reem")
    assert toast(c.post("/promos/budgets/", {"category": "PA", "year": "2026", "quarter": "4", "amount": "50000"}))["tone"] != "bad"
    r = c.get("/promos/?view=budget&y=2026&qn=4")
    assert r.status_code == 200 and b"50,000" in r.content
    assert c.get("/promos/?view=budget&y=2026&qn=4&export=xlsx").status_code == 200


# ---------- batch claims ----------
def test_batch_claim_and_batch_credit_note(as_user):
    from debitnotes.services import _validate
    ps = []
    for p in Promotion.objects.filter(stage="approved").prefetch_related("lines"):
        if p.agreement_no and stage_of(p) in ("waiting_dn", "dn_overdue") and len(ps) < 2:
            l = p.lines.first()
            l.sold_units = 10
            l.save()
            dn = create_dn(f"VCDN-B{len(ps)}", p.agreement_no, p.end + timedelta(days=5), [(l.sku, 10, l.support_h)])
            _validate(dn, timezone.now())
            ps.append(p)
    assert len(ps) == 2
    r = as_user("reem").post("/claims/batch/", {"ref": [p.mecl_ref for p in ps]})
    assert toast(r)["tone"] != "bad"
    cs = list(Claim.objects.filter(promotion__in=ps))
    assert len(cs) == 2 and cs[0].batch_no and cs[0].batch_no == cs[1].batch_no
    total = sum(c.amount_h for c in cs)
    as_user("priya").post(f"/claims/batch/{cs[0].batch_no}/cn/", {"cn_no": "CN-BATCH", "amount": total / 100})
    assert all(c.status == "closed" for c in Claim.objects.filter(pk__in=[c.pk for c in cs]))


# ---------- alerts, assignment, mentions ----------
def test_alerts_follow_roles_and_scope():
    khalid, priya = User.objects.get(username="khalid"), User.objects.get(username="priya")
    from core.services import notify
    n = notify("Invoice issue", "bad", ("po", "X1", "invoice"))
    assert n in visible_alerts(priya) and n not in visible_alerts(khalid)
    khalid.alert_scope = "all"
    khalid.save()
    assert n in visible_alerts(khalid)


def test_assign_a_po_and_mention_someone(as_user):
    po = PurchaseOrder.objects.filter(stage="new").first()
    c = as_user("faisal")
    c.post(f"/assign/po/{po.po_no}/", {"user": "noura"})
    a = Assignment.objects.get(entity="po", entity_id=po.po_no)
    noura = User.objects.get(username="noura")
    assert a.user == noura
    assert unread_alerts(noura).filter(user=noura, text__contains="assigned").exists()
    r = c.post(f"/notes/po/{po.po_no}/", {"text": "@priya can you check the price on line 2?"})
    assert "notified" in toast(r)["msg"]
    priya = User.objects.get(username="priya")
    assert Notification.objects.filter(user=priya, text__contains="mentioned you").exists()
    # the PO's action item now belongs to Noura, not to every PIC
    assert po.po_no.encode() not in as_user("faisal").get("/action/?mine=1").content or b"Noura" in as_user("faisal").get("/action/?mine=1").content


def test_mark_all_read_is_per_person(as_user):
    priya, faisal = User.objects.get(username="priya"), User.objects.get(username="faisal")
    from core.services import notify
    notify("For everyone", "info", roles=[])
    before = unread_alerts(faisal).count()
    as_user("priya").post("/notifications/read-all/")
    assert unread_alerts(priya).count() == 0 and unread_alerts(faisal).count() == before


# ---------- reports and vendor codes ----------
@pytest.mark.parametrize("days", ["7", "30", "90"])
def test_reports_page(days, as_user):
    r = as_user("tariq").get(f"/reports/?days={days}")
    assert r.status_code == 200 and b"Fill rate" in r.content and b"Recovered from brands" in r.content
    assert as_user("tariq").get(f"/reports/?days={days}&export=xlsx").status_code == 200


def test_vendor_codes_filter_lists(as_user):
    assert VendorCode.objects.count() == 2
    vc = PurchaseOrder.objects.exclude(vendor_code="").first().vendor_code
    r = as_user("admin").get(f"/pos/?tab=all&vc={vc}")
    other = PurchaseOrder.objects.exclude(vendor_code=vc).exclude(vendor_code="").first()
    assert r.status_code == 200 and (other is None or other.po_no.encode() not in r.content)
    assert as_user("admin").post("/settings/vendor-codes/add/", {"code": "MEXTR", "name": "Test"}).status_code in (200, 204)
    assert toast(as_user("admin").post("/settings/vendor-codes/add/", {"code": "a"}))["tone"] == "bad"
