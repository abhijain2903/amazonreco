"""End-to-end flows F1-F7 through the real views (HTMX requests), on the example data."""
import re

import pytest
from django.db import connection

from claims.models import Claim
from core.models import AuditEvent, GeneratedFile
from debitnotes.models import DebitNote
from debitnotes.services import evaluate
from orders.models import PurchaseOrder
from orders.services import line_checks, po_issues
from payments.models import Dispute, Payment
from promotions.models import Promotion
from promotions.services import stage_of

from .conftest import toast

pytestmark = pytest.mark.django_db


def refresh(po):
    return PurchaseOrder.objects.get(pk=po.pk)


# ---------- F1: confirm a PO ----------
def test_f1_confirm_po_with_flagged_lines(as_user):
    po = next(p for p in PurchaseOrder.objects.filter(stage="new") if po_issues(p))
    pic = as_user("faisal")
    r = pic.get(f"/records/po/{po.po_no}/?tab=lines")
    assert r.status_code == 200 and re.search(rb"lines? needs? a decision", r.content)
    # Accept a red (price) line without a reason: blocked
    red = next((l for l in po.lines.select_related("sku") if not line_checks(l)["price_ok"]), None)
    if red:
        pic.post(f"/pos/{po.po_no}/lines/", {f"d-{red.pk}": "accept"}, HTTP_HX_TRIGGER_NAME=f"d-{red.pk}")
        pic.post(f"/pos/{po.po_no}/lines/", {f"r-{red.pk}": ""})
        r = pic.post(f"/pos/{po.po_no}/confirm/")
        assert toast(r).get("tone") == "bad" and "override reason" in toast(r)["msg"]
        pic.post(f"/pos/{po.po_no}/lines/", {f"r-{red.pk}": "Override: new price agreed with buyer"})
    r = pic.post(f"/pos/{po.po_no}/confirm/")
    assert r.status_code == 200 and b"PO_ack_" in r.content          # file dialog
    assert refresh(po).stage == "confirmed"
    assert GeneratedFile.objects.filter(kind="po_ack", entity_id=po.po_no).exists()
    assert AuditEvent.objects.filter(entity="po", entity_id=po.po_no, action="confirm").exists()


def test_permissions_block_wrong_role(as_user):
    po = PurchaseOrder.objects.filter(stage="new").first()
    r = as_user("khalid").post(f"/pos/{po.po_no}/confirm/")         # Logistics cannot confirm
    assert toast(r)["tone"] == "bad"
    assert refresh(po).stage == "new"


def test_stale_version_is_rejected(as_user):
    po = PurchaseOrder.objects.filter(stage="confirmed").first()
    r = as_user("noura").post(f"/pos/{po.po_no}/book/", {"version": po.version - 1 if po.version > 1 else 999})
    assert toast(r)["tone"] == "bad"
    assert refresh(po).stage == "confirmed"


# ---------- F2-F4: book, release, ASN, slot, deliver, invoice ----------
def test_f2_to_f4_order_to_invoice(as_user):
    po = PurchaseOrder.objects.filter(stage="confirmed").first()
    assert as_user("noura").post(f"/pos/{po.po_no}/book/").status_code == 204
    po = refresh(po)
    assert po.stage == "booked" and po.sap_order_no
    assert as_user("omar").post(f"/pos/{po.po_no}/release/").status_code == 204
    assert refresh(po).stage == "released"
    k = as_user("khalid")
    r = k.post(f"/pos/{po.po_no}/asn/")
    assert r.status_code == 200 and b"ASN_" in r.content
    assert refresh(po).stage == "asn"
    r = k.post(f"/pos/{po.po_no}/slot/", {"slot_id": "CC-TEST-1", "date": "2026-10-02", "window": "08:00–12:00"})
    assert r.status_code == 204 and refresh(po).stage == "slot"
    assert k.post(f"/pos/{po.po_no}/deliver/").status_code == 204
    assert refresh(po).stage == "delivered"
    r = as_user("priya").post(f"/pos/{po.po_no}/invoice/")
    assert r.status_code == 200 and b"INV" in r.content.upper()
    assert refresh(po).stage == "invoiced"


def test_r4_blocks_asn_that_differs_from_delivery(as_user):
    from fulfilment.services import delivery_of
    po = next(p for p in PurchaseOrder.objects.filter(stage="released") if delivery_of(p))
    line = delivery_of(po).lines.select_related("sku").first()
    k = as_user("khalid")
    r = k.post(f"/pos/{po.po_no}/asn/", {f"a-{line.sku.sku_code}": line.qty + 5})
    assert toast(r)["tone"] == "bad" and "R4" in toast(r)["msg"]
    assert refresh(po).stage == "released"


# ---------- payments ----------
def test_short_payment_dispute(as_user):
    p = Payment.objects.filter(status="short").first()
    r = as_user("priya").post(f"/pay/{p.payment_no}/dispute/", {"type": "shortage", "amount": p.deduction_h / 100, "note": "POD attached"})
    assert r.status_code == 204
    assert Dispute.objects.filter(ref=p.payment_no).exists()
    p.refresh_from_db()
    assert p.status == "disputed"


# ---------- F5: promotion wizard -> submit -> approval (R8) ----------
def test_f5_promotion_wizard_and_r8(as_user):
    from catalog.models import Sku
    c = as_user("faisal")
    c.get("/promos/new/")
    c.post("/promos/new/", {"act": "next", "name": "", "cat": "PA", "start": "2026-11-01", "end": "2026-11-07", "owner": "Reem"})
    r = c.post("/promos/new/", {"act": "next", "name": "Test week", "cat": "PA", "start": "2026-11-01", "end": "2026-11-07", "owner": "Reem"})
    assert b"Add model" in r.content
    sku = Sku.objects.filter(category="PA").first()
    c.post("/promos/new/", {"act": "add", "sku": sku.sku_code})
    c.post("/promos/new/", {"act": "next", "support-0": "25", "expected-0": "100"})
    r = c.post("/promos/new/", {"act": "submit"})
    assert r.status_code == 200 and b"Promo_MECL" in r.content
    p = Promotion.objects.get(name="Test week")
    assert p.stage == "submitted" and p.lines.get().support_h == 25_00
    taken = Promotion.objects.exclude(agreement_no=None).first().agreement_no
    r = c.post(f"/promos/{p.mecl_ref}/approve/", {"agreement": taken})
    assert toast(r)["tone"] == "bad"                                  # R8: agreement already used
    r = c.post(f"/promos/{p.mecl_ref}/approve/", {"agreement": "71999001"})
    assert r.status_code == 204
    p.refresh_from_db()
    assert p.agreement_no == "71999001" and stage_of(p) == "approved"


# ---------- F6: debit notes ----------
def test_f6_mismatched_dn_dispute(as_user):
    dn = next(d for d in DebitNote.objects.all() if evaluate(d)["status"] == "mismatch")
    ev = evaluate(dn)
    r = as_user("faisal").post(f"/dns/{dn.dn_no}/dispute/")
    assert r.status_code == 204
    dn.refresh_from_db()
    assert dn.validated and dn.disputed_h == ev["variance_h"] and dn.approved_h == ev["expected_h"]
    assert Dispute.objects.filter(ref=dn.dn_no, type="promo").exists()


def test_f6_override_needs_reason_and_role(as_user):
    dn = next(d for d in DebitNote.objects.all() if evaluate(d)["status"] == "mismatch")
    assert toast(as_user("faisal").post(f"/dns/{dn.dn_no}/override/", {"reason": ""}))["tone"] == "bad"
    assert toast(as_user("reem").post(f"/dns/{dn.dn_no}/override/", {"reason": "ok"}))["tone"] == "bad"   # Product cannot override
    assert as_user("tariq").post(f"/dns/{dn.dn_no}/override/", {"reason": "Buyer extended promo"}).status_code == 204


def test_f6_unlinked_dn_link(as_user):
    dn = next(d for d in DebitNote.objects.all() if evaluate(d)["status"] == "unlinked")
    r = as_user("faisal").get(f"/records/dn/{dn.dn_no}/")
    assert b"is not in the tracker" in r.content
    target = next(p for p in Promotion.objects.exclude(agreement_no=None) if stage_of(p) in ("waiting_dn", "dn_overdue"))
    r = as_user("faisal").post(f"/dns/{dn.dn_no}/link/", {"ref": target.mecl_ref})
    assert r.status_code == 204
    dn.refresh_from_db()
    assert dn.agreement_no == target.agreement_no


# ---------- F7: claim and credit note ----------
def test_f7_claim_then_short_cn_then_write_off(as_user):
    p = next(p for p in Promotion.objects.filter(stage="dn_validated"))
    r = as_user("faisal").post(f"/promos/{p.mecl_ref}/claim/")
    assert r.status_code == 200 and b"Claim_" in r.content
    c = Claim.objects.get(promotion=p)
    fin = as_user("priya")
    r = fin.post(f"/claims/{c.claim_no}/cn/", {"cn_no": "CN-T1", "amount": (c.amount_h - 50_00) / 100, "date": "2026-09-20"})
    assert toast(r)["tone"] == "bad"
    c.refresh_from_db()
    assert c.status == "shortfall" and c.gap_h == 50_00
    assert fin.post(f"/claims/{c.claim_no}/write-off/").status_code == 204
    p.refresh_from_db()
    assert p.stage == "closed"


def test_f7_matching_cn_closes(as_user):
    c = Claim.objects.filter(status="sent").first()
    r = as_user("priya").post(f"/claims/{c.claim_no}/cn/", {"cn_no": "CN-T2", "amount": c.amount_h / 100})
    c.refresh_from_db()
    assert c.status == "closed" and c.promotion.stage == "closed"


# ---------- platform ----------
def test_audit_is_append_only():
    e = AuditEvent.objects.first()
    with pytest.raises(Exception):
        with connection.cursor() as cur:
            cur.execute("UPDATE core_auditevent SET text = 'x' WHERE id = %s", [e.id])


def test_rule_change_is_audited(as_user):
    r = as_user("admin").post("/settings/rules/R1/", {"field": "pct", "value": "2"})
    assert r.status_code == 204
    assert AuditEvent.objects.filter(entity="rules", entity_id="R1", action="update_rule").exists()
    assert toast(as_user("faisal").post("/settings/rules/R1/", {"field": "pct", "value": "5"}))["tone"] == "bad"


def test_action_center_per_role(as_user):
    from core.actions import action_items
    from identity.models import User
    mine = action_items(User.objects.get(username="faisal"), mine=True)
    assert mine and all("PIC" in i["roles"] for i in mine)
