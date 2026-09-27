"""Browser tests for flows F1-F7 against a running hub with the example data loaded.

    python manage.py seed_demo --reset && python manage.py runserver
    E2E_BASE_URL=http://127.0.0.1:8000 pytest tests/e2e -m e2e

Each test changes data, so re-seed before a new run.
"""
import os
import re

import pytest

BASE = os.environ.get("E2E_BASE_URL")
pytestmark = [pytest.mark.e2e, pytest.mark.skipif(not BASE, reason="set E2E_BASE_URL to run browser tests")]


@pytest.fixture
def page(browser):
    ctx = browser.new_context(base_url=BASE, viewport={"width": 1440, "height": 900})
    p = ctx.new_page()
    errors = []
    p.on("pageerror", lambda e: errors.append(str(e)))
    yield p
    ctx.close()
    assert not errors, errors


def sign_in(page, username):
    page.goto("/login/")
    page.locator(f"button[name=user][value={username}]").click()
    page.wait_for_url(lambda u: "/login/" not in u)


def toast_text(page):
    t = page.locator(".toast").last
    t.wait_for()
    return t.inner_text()


def drawer(page):
    return page.locator("#drawer aside.drawer")


def test_f1_confirm_po(page):
    sign_in(page, "faisal")
    page.goto("/pos/?tab=new")
    page.locator("tr.click").first.click()
    d = drawer(page)
    d.wait_for()
    assert "purchase order" in d.locator(".dh-type").inner_text().lower()
    d.get_by_role("button", name="Accept all green").click()
    toast_text(page)
    # Reject every flagged line so the PO can be confirmed
    for sel in d.locator("select[name^=d-]").all():
        row = sel.locator("xpath=ancestor::tr")
        if "err" in (row.get_attribute("class") or ""):
            sel.select_option("reject")
            page.wait_for_timeout(400)
    d.get_by_role("button", name="Confirm PO").click()
    m = page.locator("#modal .modal")
    m.wait_for()
    assert "PO_ack_" in m.inner_text()


def test_f5_new_promotion_wizard(page):
    sign_in(page, "reem")
    page.goto("/promos/")
    page.get_by_role("button", name="New promotion").first.click()
    m = page.locator("#modal .modal")
    m.wait_for()
    m.locator("#w-name").fill("E2E test promotion")
    m.get_by_role("button", name="Next").click()
    m.get_by_role("button", name="Add model").wait_for()
    m.get_by_role("button", name="Add model").click()
    page.wait_for_selector("#w-sup-0")
    m.get_by_role("button", name="Next").click()
    m.get_by_text("Amazon submission format").wait_for()
    m.get_by_role("button", name="Save as draft").click()
    d = drawer(page)
    d.wait_for()
    assert "Draft" in d.inner_text()


def test_f6_dn_validation_from_promotion(page):
    sign_in(page, "faisal")
    page.goto("/dns/?tab=mismatch")
    page.locator("tr.click").first.click()
    d = drawer(page)
    d.wait_for()
    assert "checks (r10)" in d.inner_text().lower()
    d.get_by_role("button", name="Approve expected, dispute the rest").click()
    assert "Dispute" in toast_text(page)


def test_upload_sample_file(page):
    sign_in(page, "admin")
    page.goto("/uploads/")
    page.locator(".ut").filter(has_text="Stock snapshot").get_by_role("button", name="Upload").click()
    m = page.locator("#modal .modal")
    m.get_by_role("button", name="Use sample file").click()
    m.get_by_text("All required columns matched").wait_for()
    m.get_by_role("button", name="Preview").click()
    m.get_by_role("button", name=re.compile(r"^Import \d+ row")).click()
    m.get_by_text("Import complete").wait_for()


def test_row_buttons_do_not_open_drawer(page):
    sign_in(page, "faisal")
    page.goto("/claims/?tab=toclaim")
    btn = page.get_by_role("button", name="Generate claim").first
    if btn.count():
        btn.click()
        page.locator("#modal .modal").wait_for()
        assert drawer(page).count() == 0


def test_palette_and_role_switch(page):
    sign_in(page, "priya")
    page.keyboard.press("Control+k")
    page.locator("#pal input").wait_for()
    page.keyboard.type("MECL")
    page.wait_for_timeout(500)
    assert page.locator("#pal").inner_text().count("MECL") > 0
    page.keyboard.press("Escape")
