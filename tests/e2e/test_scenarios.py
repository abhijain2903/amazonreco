"""Browser walk-throughs of the real-world cases (phases A–C), clicking the screens as each team would.

    python manage.py seed_demo --reset && python manage.py runserver
    E2E_BASE_URL=http://127.0.0.1:8000 pytest tests/e2e -m e2e

Each test changes data, so re-seed before a new run. Record numbers come from the deterministic seed.
"""
import os

import pytest

from .test_ui import drawer, page, sign_in, toast_text  # noqa: F401  (page is a fixture)

BASE = os.environ.get("E2E_BASE_URL")
pytestmark = [pytest.mark.e2e, pytest.mark.skipif(not BASE, reason="set E2E_BASE_URL to run browser tests")]


def expect_toast(page, text):
    page.locator(".toast", has_text=text).first.wait_for()


def open_record(page, url):
    page.evaluate(f"htmx.ajax('GET', '{url}', {{target: '#drawer'}})")
    d = drawer(page)
    d.wait_for()
    page.wait_for_timeout(300)
    return d


def test_backorder_a_line_and_confirm(page):
    sign_in(page, "faisal")
    page.goto("/pos/?tab=new")
    page.locator("tr.click").last.click()
    d = drawer(page)
    d.wait_for()
    page.wait_for_timeout(800)                       # let htmx wire up the drawer's form
    d.locator("select[name^=d-]").first.select_option("backorder")
    q = drawer(page).locator("input[name^=q-]:not([readonly])").first      # the drawer re-renders with the qty editable
    q.wait_for()
    q.fill("1")
    q.dispatch_event("change")
    page.wait_for_timeout(800)
    for sel in d.locator("select[name^=d-]").all():
        if "err" in (sel.locator("xpath=ancestor::tr").get_attribute("class") or ""):
            sel.select_option("reject")
            page.wait_for_timeout(400)
    d.get_by_role("button", name="Confirm PO").click()
    m = page.locator("#modal .modal")
    m.wait_for()
    assert "PO_ack_" in m.inner_text()


def test_ship_the_backorder_from_shipments(page):
    sign_in(page, "khalid")
    page.goto("/ship/?tab=backorder")
    assert page.locator("tr.click").count() >= 1
    page.get_by_role("button", name="Ship backorder").first.click()
    assert "next shipment" in toast_text(page)
    page.goto("/ship/?tab=asn")
    assert page.get_by_text("SA7N97YY").count() >= 1


def test_amazon_cancels_a_po(page):
    sign_in(page, "faisal")
    page.goto("/pos/?tab=book")
    page.locator("tr.click").first.click()
    d = drawer(page)
    d.wait_for()
    d.get_by_role("button", name="Amazon change").click()
    m = page.locator("#modal .modal")
    m.wait_for()
    m.locator("input[name=cancel]").check()
    m.locator("input[name=reason]").fill("Cancelled in Vendor Central")
    m.get_by_role("button", name="Apply change").click()
    assert "cancelled" in toast_text(page).lower()


def test_missed_appointment_then_rebook(page):
    sign_in(page, "khalid")
    page.goto("/ship/?tab=transit")
    po = page.locator("tr.click b.mono").first.inner_text()
    d = open_record(page, f"/records/po/{po}/?tab=shipment")
    d.get_by_role("button", name="Missed / refused").click()
    m = page.locator("#modal .modal")
    m.wait_for()
    m.locator("input[name=reason]").fill("Truck late at the gate")
    m.get_by_role("button", name="Release the slot").click()
    toast_text(page)
    page.goto("/action/?mine=0")
    assert page.get_by_text(f"Re-book delivery for {po}").count() == 1


def test_invoice_hold_and_credit_memo(page):
    sign_in(page, "priya")
    page.goto("/pay/?tab=short")
    page.locator("tr.click").first.click()
    d = drawer(page)
    d.wait_for()
    d.get_by_text("Amazon rejected or held this invoice?").click()
    d.locator("input[name=note]").fill("Price mismatch on line 2")
    d.get_by_role("button", name="Record").click()
    toast_text(page)
    page.wait_for_timeout(600)
    assert "on hold" in drawer(page).inner_text().lower()
    drawer(page).get_by_text("Issue a credit memo against this invoice").click()
    drawer(page).locator("input[name=amount]").fill("100")
    drawer(page).locator("input[name=reason]").fill("Agreed price claim")
    drawer(page).get_by_role("button", name="Record memo").click()
    expect_toast(page, "Credit memo")


def test_chargeback_dispute(page):
    sign_in(page, "priya")
    page.goto("/pay/?tab=short")
    page.get_by_role("button", name="Dispute").first.click()
    m = page.locator("#modal .modal")
    m.wait_for()
    m.locator("select[name=type]").select_option("chargeback")
    m.locator("select[name=subtype]").select_option("labels")
    m.get_by_role("button", name="Open dispute").click()
    assert "DSP-" in toast_text(page)
    page.goto("/pay/?tab=disputes")
    assert page.get_by_text("Chargeback").count() >= 1


def test_return_authorise_receive_and_match(page):
    sign_in(page, "priya")
    d = open_record(page, "/records/rtv/RTV-118842/")
    d.get_by_role("button", name="Authorise return").click()
    toast_text(page)
    page.context.clear_cookies()
    sign_in(page, "khalid")
    d = open_record(page, "/records/rtv/RTV-118842/")
    d.locator("input[name^=r-]").first.fill("5")
    d.get_by_role("button", name="Record goods received").click()
    assert "received" in toast_text(page).lower()
    page.context.clear_cookies()
    sign_in(page, "priya")
    d = open_record(page, "/records/rtv/RTV-117903/")
    d.get_by_role("button", name="Match").first.click()
    assert "Dispute" in toast_text(page)


def test_amend_a_live_promotion(page):
    sign_in(page, "reem")
    page.goto("/promos/")
    d = open_record(page, "/records/promo/MECL-PR-2026-0162/")
    d.get_by_role("button", name="Amend").click()
    m = page.locator("#modal .modal")
    m.wait_for()
    m.locator("input[name=reason]").fill("Amazon extended the deal")
    m.locator("input[name=fee_label]").fill("Deal fee")
    m.locator("input[name=fee_amount]").fill("1500")
    m.get_by_role("button", name="Record amendment").click()
    assert "Amendment 1" in toast_text(page)


def test_assign_and_mention(page):
    sign_in(page, "faisal")
    page.goto("/pos/?tab=new")
    page.locator("tr.click").first.click()
    d = drawer(page)
    d.wait_for()
    page.wait_for_timeout(800)
    d.locator("form.owner select").select_option("noura")
    expect_toast(page, "Assigned to Noura")
    page.wait_for_timeout(800)
    drawer(page).get_by_role("tab", name="Notes").click()
    page.wait_for_timeout(600)
    drawer(page).locator("#note-in").fill("@priya please check the price")
    drawer(page).get_by_role("button", name="Add note").click()
    expect_toast(page, "Priya Nair notified")


def test_reports_tabs_and_export(page):
    sign_in(page, "tariq")
    for tab in ("summary", "orders", "shipping", "cash", "promos", "speed"):
        page.goto(f"/reports/?tab={tab}&days=90")
        page.locator(".kpis").wait_for()
    with page.expect_download() as dl:
        page.get_by_role("link", name="Export").click()
    assert dl.value.suggested_filename.startswith("Management_report_")


def test_documents_and_budget(page):
    sign_in(page, "admin")
    page.goto("/ship/?tab=submitted")
    po = page.locator("tr.click td.mono").first.inner_text()
    d = open_record(page, f"/records/po/{po}/?tab=docs")
    assert "Invoice_" in d.inner_text()
    page.goto("/promos/?view=budget")
    assert "Digital imaging" in page.inner_text("#view")
