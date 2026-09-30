"""Regressions for the defects logged in ME_Vendor_Hub_Test_Report.xlsx (Defects tab), one test per DEF id."""
import io
import json

import pytest
from django.test import Client

from catalog.models import FulfilmentCentre
from core.models import AuditEvent
from debitnotes.models import DebitNote
from debitnotes.services import evaluate
from identity.models import User
from orders.models import PoLine, PurchaseOrder
from orders.services import line_checks, po_issues
from payments.models import Dispute
from uploads.models import UploadBatch
from uploads.types import _flagged_skus, sample_rows

from .conftest import toast

pytestmark = pytest.mark.django_db


def page_client(username):
    """A browser-style client (full page requests, no HX-Request header)."""
    c = Client()
    c.force_login(User.objects.get(username=username))
    return c


# DEF-01 / DEF-14: an unknown or missing tab falls back to the default tab instead of raising
@pytest.mark.parametrize("url", ["/ship/?tab=invoice", "/ship/?tab=bogus", "/pos/?tab=bogus", "/pos/?view=bogus", "/dns/?tab=bogus",
                                 "/pay/?tab=bogus", "/claims/?tab=bogus", "/promos/?tab=bogus&view=bogus", "/action/?tab=bogus"])
def test_unknown_tab_falls_back(url, as_user):
    assert as_user("admin").get(url).status_code == 200


def test_claims_unknown_tab_renders_default_tab(as_user):
    r = as_user("reem").get("/claims/?tab=bogus")
    assert b'class="tab on" href="?tab=toclaim"' in r.content and b"Closed claims appear here" not in r.content


# DEF-02: the footer reflects the PO's checks on every tab, not only on Lines
@pytest.mark.parametrize("tab", ["lines", "checks", "timeline", "notes", "shipment"])
def test_po_footer_matches_checks_on_every_tab(tab, as_user):
    po = next(p for p in PurchaseOrder.objects.filter(stage="new") if po_issues(p))
    r = as_user("faisal").get(f"/records/po/{po.po_no}/?tab={tab}")
    assert b"Decide on the flagged lines" in r.content and b"Every line passed" not in r.content


# DEF-03 / DEF-04: Settings and Integrations are Admin-only on read, not only on write
@pytest.mark.parametrize("url", ["/settings/", "/settings/?tab=users", "/integrations/"])
def test_admin_pages_refused_for_other_roles(url, as_user):
    t = toast(as_user("faisal").get(url))                                   # HTMX: red toast, nothing rendered
    assert t["tone"] == "bad" and t["msg"] == "This needs the Admin role."
    r = page_client("omar").get(url, HTTP_REFERER=f"http://testserver{url}")
    assert r.status_code == 302 and r.url == "/"                          # full page: back out, never to itself
    assert as_user("admin").get(url).status_code == 200


def test_connector_drawer_refused_for_other_roles(as_user):
    from integrations.connectors import ensure_connectors
    from integrations.models import Connector
    ensure_connectors()
    key = Connector.objects.first().key
    assert toast(as_user("faisal").get(f"/records/conn/{key}/"))["tone"] == "bad"
    assert toast(as_user("faisal").post(f"/integrations/{key}/test/"))["tone"] == "bad"


# DEF-05 (and the admin-console part of DEF-09): the sidebar only offers what the role may use
def test_sidebar_follows_permissions(as_user):
    nav = lambda u: as_user(u).get("/nav/?path=/").content
    pic, credit, admin = nav("faisal"), nav("omar"), nav("admin")
    assert b'href="/uploads/"' in pic and b'href="/settings/"' not in pic and b'href="/integrations/"' not in pic
    assert b'href="/admin/"' not in pic
    assert b'href="/uploads/"' not in credit                              # Credit control cannot upload
    assert all(u in admin for u in (b'href="/uploads/"', b'href="/settings/"', b'href="/integrations/"', b'href="/admin/"'))


def test_search_does_not_offer_admin_pages(as_user):
    r = as_user("faisal").get("/search/")
    assert b'href="/settings/"' not in r.content and b"Settings" not in r.content


# DEF-07: the import event records the agreement # exactly as Amazon sent it
def test_unlinked_dn_import_event_keeps_received_agreement():
    dn = next(d for d in DebitNote.objects.all() if evaluate(d)["status"] == "unlinked")
    ev = AuditEvent.objects.get(entity="dn", entity_id=dn.dn_no, action="import")
    assert f"agreement {dn.agreement_no}" in ev.text


# DEF-08: a record URL opened directly lands on its list page with the drawer opening on top
def test_record_link_opens_full_page():
    po = PurchaseOrder.objects.first()
    r = page_client("faisal").get(f"/records/po/{po.po_no}/?tab=lines")
    assert r.status_code == 302 and r.url == f"/pos/?tab=all&open=/records/po/{po.po_no}/%3Ftab%3Dlines"
    assert page_client("faisal").get(r.url).status_code == 200


# DEF-13: the slot dialog is gated like the other actions, and the server refuses the booking
def test_slot_booking_gated_for_finance(as_user):
    po = PurchaseOrder.objects.filter(stage="asn").first()
    fin = as_user("priya")
    r = fin.get(f"/pos/{po.po_no}/slot/")
    assert b"Needs the" in r.content and b"disabled" in r.content
    r = fin.post(f"/pos/{po.po_no}/slot/", {"slot_id": "CC1", "date": "2030-01-01", "window": "08:00–12:00", "version": po.version})
    assert toast(r)["tone"] == "bad"
    assert PurchaseOrder.objects.get(pk=po.pk).stage == "asn"


# DEF-15: the dispute header's opened date matches its "Dispute opened" event
def test_dispute_opened_date_matches_timeline():
    for d in Dispute.objects.all():
        ev = AuditEvent.objects.filter(entity="dispute", entity_id=d.case_no, text__startswith="Dispute opened").first()
        if ev:
            assert d.created_at.date() == ev.at.date(), d.case_no


# DEF-16: Content-Security-Policy and Permissions-Policy on app responses
def test_security_headers(as_user):
    r = as_user("faisal").get("/")
    assert "script-src 'self'" in r["Content-Security-Policy"] and "frame-ancestors 'none'" in r["Content-Security-Policy"]
    assert "camera=()" in r["Permissions-Policy"]
    assert "Content-Security-Policy" not in as_user("faisal").get("/api/v1/docs")   # Swagger UI loads from a CDN


# DEF-17: optimistic locking on line decisions, debit notes, promotions and connectors
def test_po_line_autosave_keeps_its_version_and_refuses_stale(as_user):
    po = PurchaseOrder.objects.filter(stage="new").first()
    line = po.lines.first()
    pic = as_user("faisal")
    r = pic.post(f"/pos/{po.po_no}/lines/", {f"d-{line.pk}": "reject", "version": po.version}, HTTP_HX_TRIGGER_NAME=f"d-{line.pk}")
    new_v = json.loads(r["HX-Trigger"])["version"]["v"]
    assert new_v == po.version + 1
    r = pic.post(f"/pos/{po.po_no}/lines/", {f"d-{line.pk}": "accept", "version": new_v})          # same user, next edit: fine
    assert "toast" not in json.loads(r["HX-Trigger"])
    r = pic.post(f"/pos/{po.po_no}/lines/", {f"d-{line.pk}": "reject", "version": po.version})     # someone's older view
    assert toast(r)["tone"] == "bad" and "changed this record" in toast(r)["msg"]


def test_dn_action_refuses_stale_version(as_user):
    dn = next(d for d in DebitNote.objects.filter(validated=False) if evaluate(d)["status"] == "to_validate")
    r = as_user("faisal").post(f"/dns/{dn.dn_no}/approve/", {"version": dn.version - 1})
    assert toast(r)["tone"] == "bad" and not DebitNote.objects.get(pk=dn.pk).validated


def test_promotion_action_refuses_stale_version(as_user):
    from promotions.models import Promotion
    p = Promotion.objects.filter(stage="submitted").first()
    r = as_user("reem").post(f"/promos/{p.mecl_ref}/reject/", {"version": p.version - 1})
    assert toast(r)["tone"] == "bad" and Promotion.objects.get(pk=p.pk).stage == "submitted"


# DEF-18: the sign-in page sends a signed-in user into the app
def test_login_redirects_when_signed_in():
    r = page_client("reem").get("/login/")
    assert r.status_code == 302 and r.url == "/promos/"


# DEF-19: an unknown FC code is an error, never new master data; admins add FCs explicitly
def test_unknown_fc_is_rejected_on_po_import(as_user):
    po_line = PoLine.objects.select_related("sku").first()
    csv = ("po_no,fc_code,order_date,ship_window_end,asin,qty_ordered,unit_cost_sar\n"
           f"ZZTEST01,ZZZ-FC9,2026-09-01,2026-09-20,{po_line.sku.asin},5,{po_line.cost_h / 100:.2f}\n")
    f = io.BytesIO(csv.encode())
    f.name = "po.csv"
    c = as_user("faisal")
    c.post("/uploads/new/", {"type": "U4", "file": f})
    b = UploadBatch.objects.latest("created_at")
    c.post(f"/uploads/{b.pk}/preview/")
    row = b.rows.get()
    assert any("ZZZ-FC9" in e for e in row.errors)
    assert not FulfilmentCentre.objects.filter(code="ZZZ-FC9").exists()


def test_admin_adds_fc(as_user):
    assert toast(as_user("faisal").post("/settings/fcs/add/", {"code": "ZZZ-FC9", "name": "Test"}))["tone"] == "bad"
    r = as_user("admin").post("/settings/fcs/add/", {"code": "zzz-fc9", "name": "Test FC", "city": "Riyadh"})
    assert toast(r)["tone"] == "ok" and FulfilmentCentre.objects.filter(code="ZZZ-FC9").exists()
    assert toast(as_user("admin").post("/settings/fcs/add/", {"code": "ZZZ-FC9", "name": "Again"}))["tone"] == "bad"


# DEF-20: generated sample files never touch the price or stock of a flagged line on a PO awaiting confirmation
@pytest.mark.parametrize("tid", ["U2", "U3", "U4"])
def test_sample_files_leave_demo_examples_alone(tid):
    from catalog.models import Sku
    flagged = {s.sku_code for s in Sku.objects.filter(pk__in=_flagged_skus())} | {s.asin for s in Sku.objects.filter(pk__in=_flagged_skus())}
    assert flagged, "the example data has flagged lines"
    before = [(l.pk, line_checks(l)["tone"]) for l in PoLine.objects.filter(po__stage="new").select_related("sku")]
    _, rows = sample_rows(tid)
    assert not {str(c) for r in rows[1:] for c in r} & flagged
    assert [(l.pk, line_checks(l)["tone"]) for l in PoLine.objects.filter(po__stage="new").select_related("sku")] == before


# DEF-21: the import summary agrees in number
def test_po_import_summary_grammar(as_user):
    c = as_user("admin")
    c.post("/uploads/new/", {"type": "U4", "sample": "1"})
    b = UploadBatch.objects.latest("created_at")
    c.post(f"/uploads/{b.pk}/preview/")
    c.post(f"/uploads/{b.pk}/commit/")
    b.refresh_from_db()
    line = next(s for s in b.summary if "attention" in s)
    n = int(line.split()[0])
    assert line.startswith(f"{n} PO needs") if n == 1 else line.startswith(f"{n} POs need")
