"""Run the expert demo end to end on freshly seeded data and screenshot each scene.

    python manage.py seed_demo --reset && python manage.py runserver 127.0.0.1:8010
    python docs/demo/expert_run.py [out_dir]

Every step is done through the screens as the named person would; the run also proves the script works.
"""
import os
import sys

from playwright.sync_api import sync_playwright

BASE = os.environ.get("DEMO_BASE_URL", "http://127.0.0.1:8010")
OUT = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(os.path.abspath(__file__)), "out", "shots")
os.makedirs(OUT, exist_ok=True)
POD = os.path.join(OUT, "POD_9TVM98RZ.pdf")
open(POD, "wb").write(b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF\n")
errs, done = [], []


def ctx(b, user, width=1440):
    c = b.new_context(base_url=BASE, viewport={"width": width, "height": 900}, device_scale_factor=2)
    pg = c.new_page()
    pg.on("response", lambda r: r.status >= 500 and errs.append(f"{user} {r.status} {r.url}"))
    pg.on("pageerror", lambda e: errs.append(f"{user} js {e}"))
    pg.goto("/login/")
    pg.locator(f"button[name=user][value={user}]").click()
    pg.wait_for_url(lambda u: "/login/" not in u)
    pg.wait_for_timeout(400)
    PAGES.append(pg)
    return pg


def shot(pg, name, wait=700, keep_toast=False):
    pg.wait_for_timeout(wait)
    if not keep_toast:
        pg.evaluate("document.querySelectorAll('.toast').forEach(t => t.remove())")
    pg.screenshot(path=f"{OUT}/{name}.png")
    done.append(name)


def drawer(pg, url, wait=900):
    pg.evaluate(f"htmx.ajax('GET', '{url}', {{source: '#drawer', target: '#drawer'}})")
    pg.locator(f'#drawer [data-url^="{url.split("?")[0]}"] aside.drawer').wait_for()
    pg.wait_for_timeout(wait)
    return pg.locator("#drawer aside.drawer")


def modal(pg, url, wait=900):
    pg.evaluate(f"htmx.ajax('GET', '{url}', {{target: '#modal'}})")
    pg.locator("#modal .modal").wait_for()
    pg.wait_for_timeout(wait)
    return pg.locator("#modal .modal")


def toast(pg, text):
    pg.locator(".toast", has_text=text).first.wait_for(timeout=8000)


def close_modal(pg):
    pg.evaluate("document.getElementById('modal').innerHTML=''")
    pg.wait_for_timeout(300)


def scroll_lines(pg):
    pg.evaluate("document.querySelector('#drawer .db').scrollLeft = 10000")
    pg.wait_for_timeout(200)


PAGES = []


def step(name, fn):
    import traceback
    try:
        fn()
    except Exception as e:  # keep going, report at the end; save what each open page showed
        line = next((fr.lineno for fr in reversed(traceback.extract_tb(e.__traceback__)) if fr.filename == __file__), "?")
        errs.append(f"SCENE {name} (line {line}): {str(e).splitlines()[0][:200]}")
        for i, pg in enumerate(PAGES):
            try:
                pg.screenshot(path=f"{OUT}/FAIL_{name}_{i}.png")
            except Exception:
                pass


with sync_playwright() as p:
    b = p.chromium.launch()

    # ---- Part 1: the morning ----
    t = ctx(b, "tariq")
    step("01", lambda: (t.goto("/"), shot(t, "01_dashboard")))
    tw = ctx(b, "tariq", width=1680)
    step("01t", lambda: (tw.goto("/?view=po"), shot(tw, "01a_po_tracker"),
                         tw.goto("/?view=po&status=all&q=5DRWD3VG"), shot(tw, "01b_split_portions"),
                         tw.goto("/?view=sellout"), shot(tw, "01c_sellout_tracker"),
                         tw.goto("/?view=claims"), shot(tw, "01d_claim_tracker")))
    f = ctx(b, "faisal")
    step("02", lambda: (f.goto("/action/"), shot(f, "02_action")))

    # ---- Part 2: confirm POs ----
    def s03():
        f.goto("/pos/?tab=new")
        d = drawer(f, "/records/po/KU4YDNY8/?tab=lines")
        shot(f, "03a_overdue_po")
        d.get_by_role("button", name="Accept all green").click(); toast(f, "green line")
        f.wait_for_timeout(900)
        f.locator("#drawer").get_by_role("button", name="Confirm PO").click()
        f.locator("#modal .modal").wait_for(); shot(f, "03b_ack_file")
        close_modal(f)
    step("03", s03)

    def s04():
        drawer(f, "/records/po/8TY68ZN6/?tab=checks"); shot(f, "04a_price_checks")
        drawer(f, "/records/po/8TY68ZN6/?tab=lines"); scroll_lines(f); shot(f, "04b_price_line")
    step("04", s04)

    def s05():
        d = drawer(f, "/records/po/CMAMACG8/?tab=lines")
        d.locator("tr.warnrow select[name^=d-]").first.select_option("backorder")
        f.locator("#drawer input[name^=q-]:not([readonly])").first.wait_for()
        f.wait_for_timeout(600); scroll_lines(f); shot(f, "05a_backorder_line")
        f.locator("#drawer").get_by_role("button", name="Confirm PO").click()
        f.locator("#modal .modal").wait_for(); shot(f, "05b_backorder_ack")
        close_modal(f)
    step("05", s05)

    a = ctx(b, "admin")

    def s06():
        a.goto("/settings/?tab=skus")
        a.get_by_text("Add or edit one SKU").click()
        fm = a.locator("form[hx-post='/settings/skus/save/']")
        for k, v in dict(sku_code="ME10113", model_no="DI-VC455S", asin="B09FX6Z97F", ean="6283725587758", case_pack="6").items():
            fm.locator(f"[name={k}]").fill(v)
        fm.locator("[name=category]").select_option("DI")
        shot(a, "06a_sku_edit", 300)
        fm.get_by_role("button").last.click(); toast(a, "SKU ME10113")
        drawer(f, "/records/po/Z5L5YAEW/?tab=lines"); shot(f, "06b_case_pack")
    step("06", s06)

    def s07():
        m = modal(f, "/pos/5SJQWU42/change/")
        m.locator("input[name^=n-]").first.fill("30")
        m.locator("input[name=reason]").fill("Amazon PO change notice, 2 Oct")
        shot(f, "07_amazon_change", 300)
        m.get_by_role("button", name="Apply change").click(); toast(f, "Amazon's change applied")
    step("07", s07)

    # ---- Part 3: book, credit, ship ----
    o = ctx(b, "omar")

    def s08():
        o.goto("/pos/?tab=release")
        d = drawer(o, "/records/po/CXTQTJSP/")
        d.locator("input[name=reason]").fill("Over credit limit, overdue invoices")
        d.get_by_role("button", name="Hold").click(); toast(o, "credit hold")
        drawer(o, "/records/po/CXTQTJSP/"); shot(o, "08_credit_hold")
    step("08", s08)

    k = ctx(b, "khalid")

    def s09():
        k.goto("/ship/?tab=asn")
        drawer(k, "/records/po/66TSGPW2/?tab=shipment"); shot(k, "09a_asn_short")
        k.locator("#drawer").get_by_role("button", name="Submit ASN").click()
        k.locator("#modal .modal").wait_for(); close_modal(k)
        d = drawer(k, "/records/po/66TSGPW2/?tab=shipment")
        d.locator("details.cartons summary").click(); shot(k, "09b_cartons")
    step("09", s09)

    def s10():
        k.goto("/ship/?tab=slot")
        m = modal(k, "/pos/P79ELFHD/slot/")
        m.locator("select[name=freight]").select_option("collect")
        m.locator("input[name=slot_id]").fill("ARN-58830192")
        shot(k, "10_slot_collect", 300)
        m.get_by_role("button", name="Save slot").click(); toast(k, "Pickup")
    step("10", s10)

    def s11():
        m = modal(k, "/pos/J3FPZQDS/slot-failed/")
        m.locator("select[name=outcome]").select_option("refused")
        m.locator("input[name=reason]").fill("Carton labels did not match the ASN")
        shot(k, "11a_refused", 300)
        m.get_by_role("button", name="Release the slot").click(); toast(k, "appointment released")
        k.goto("/action/?mine=1"); shot(k, "11b_rebook")
    step("11", s11)

    def s12():
        drawer(k, "/records/po/9TVM98RZ/?tab=shipment"); shot(k, "12a_pod_prompt")
        d = drawer(k, "/records/po/9TVM98RZ/?tab=docs")
        d.locator("input[type=file]").set_input_files(POD)
        d.locator("select[name=kind]").select_option("pod")
        d.get_by_role("button", name="Attach", exact=True).click(); toast(k, "attached")
        drawer(k, "/records/po/9TVM98RZ/?tab=docs"); shot(k, "12b_documents")
    step("12", s12)

    def s13():
        f.goto("/ship/?tab=invoice")
        drawer(f, "/records/po/4YQ4SZVM/?tab=invoice"); shot(f, "13_invoice_blocked")
        f.locator("#drawer").get_by_role("button", name="Correct SAP billing").click(); toast(f, "Billing corrected")
        f.wait_for_timeout(900)
        f.locator("#drawer").get_by_role("button", name="Submit invoice").click()
        f.locator("#modal .modal").wait_for(); close_modal(f)
    step("13", s13)

    def s14():
        k.goto("/ship/?tab=backorder"); shot(k, "14a_backorders")
        drawer(k, "/records/po/SA7N97YY/?tab=shipment"); shot(k, "14b_backorder_po")
        k.locator("#drawer").get_by_role("button", name="Ship the backorder").click(); toast(k, "next shipment")
        drawer(k, "/records/po/SA7N97YY/?tab=shipment"); shot(k, "14c_second_delivery")
    step("14", s14)

    # ---- Part 4: cash ----
    pr = ctx(b, "priya")

    def s15():
        pr.goto("/pay/?tab=match"); shot(pr, "15a_to_match")
        drawer(pr, "/records/payment/RMT-9102218/"); shot(pr, "15b_combined_payment")
    step("15", s15)

    def s16():
        pr.goto("/pay/?tab=short"); shot(pr, "16a_short_paid")
        drawer(pr, "/records/po/CYG9KQKN/?tab=invoice"); shot(pr, "16b_link_dn_suggestion")
    step("16", s16)

    def s17():
        m = modal(pr, "/pay/RMT-9102215/dispute/")
        m.locator("details.help summary").click()
        shot(pr, "17a_dispute_dialog", 300)
        m.get_by_role("button", name="Open dispute").click(); toast(pr, "Dispute DSP-")
        d = drawer(pr, "/records/dispute/DSP-0042/")
        d.locator("input[name=recovered]").fill("6000")
        shot(pr, "17b_partial_win", 300)
        d.get_by_role("button", name="Mark won").click(); toast(pr, "won")
    step("17", s17)

    def s18():
        d = drawer(pr, "/records/po/EQ777JHQ/?tab=invoice")
        for s in d.locator("details.inline-form summary").all():
            s.click()
        d.locator("input[name=note]").fill("Quantity on line 3 does not match Amazon's receipt")
        d.get_by_role("button", name="Record", exact=True).click(); toast(pr, "on hold")
        d = drawer(pr, "/records/po/EQ777JHQ/?tab=invoice")
        for s in d.locator("details.inline-form summary").all():
            s.click()
        shot(pr, "18_invoice_hold_memo")
    step("18", s18)

    step("19", lambda: (pr.goto("/pay/?tab=ageing"), shot(pr, "19_ageing")))

    def s20():
        pr.goto("/returns/?tab=all"); shot(pr, "20a_returns")
        drawer(pr, "/records/rtv/RTV-118842/").get_by_role("button", name="Authorise return").click(); toast(pr, "authorised")
        d = drawer(k, "/records/rtv/RTV-118517/")
        d.locator("input[name^=r-]").first.fill("2")
        d.locator("select[name^=c-]").first.select_option("damaged")
        shot(k, "20b_receive", 300)
        d.get_by_role("button", name="Record goods received").click(); toast(k, "received")
        drawer(pr, "/records/rtv/RTV-117903/"); shot(pr, "20c_match_deduction")
        pr.locator("#drawer").get_by_role("button", name="Match").first.click(); toast(pr, "Dispute")
        drawer(pr, "/records/rtv/RTV-117903/"); shot(pr, "20d_return_disputed")
    step("20", s20)

    # ---- Part 5: promotions ----
    r = ctx(b, "reem")

    def s21():
        r.goto("/promos/")
        m = modal(r, "/promos/new/")
        m.locator("#w-name").fill("Prime Day deals · Party speaker")
        m.locator("#w-ptype").select_option("prime_day")
        m.locator("#w-cat").select_option("PA")
        shot(r, "21_wizard", 300)
        close_modal(r)
    step("21", s21)

    def s22():
        d = drawer(r, "/records/promo/MECL-PR-2026-0166/?tab=models")
        r.evaluate("document.querySelectorAll('.toast').forEach(t => t.remove())")
        d.locator("#agr-in").fill("71040765")
        r.locator("#drawer").get_by_role("button", name="Record approval").click(); toast(r, "R8")
        shot(r, "22_r8_duplicate", 200, keep_toast=True)
    step("22", s22)

    def s23():
        m = modal(r, "/promos/MECL-PR-2026-0162/amend/")
        m.locator("input[name=reason]").fill("Amazon extended the deal by 7 days")
        m.locator("input[name=fee_label]").fill("Payday deal fee")
        m.locator("input[name=fee_amount]").fill("2500")
        m.locator("input[name=instalments]").check()
        shot(r, "23_amend", 300)
        m.get_by_role("button", name="Record amendment").click(); toast(r, "Amendment 1")
    step("23", s23)

    def s24():
        drawer(f, "/records/promo/MECL-PR-2026-0152/?tab=dn"); shot(f, "24_dn_600")
        drawer(f, "/records/dn/VCDN-4046836/"); shot(f, "25_dn_unlinked")
    step("24", s24)

    def s26():
        r.goto("/claims/?tab=toclaim")
        for cb in r.locator("input[name=ref]").all():
            cb.check()
        shot(r, "26a_batch_select", 300)
        r.get_by_role("button", name="Claim selected together").click()
        r.locator("#modal .modal").wait_for(); close_modal(r)
        r.goto("/claims/?tab=sent"); shot(r, "26b_batch_sent")
    step("26", s26)

    step("27", lambda: (drawer(r, "/records/promo/MECL-PR-2026-0146/?tab=claim"), shot(r, "27_cn_shortfall")))
    step("28", lambda: (r.goto("/promos/?view=budget"), shot(r, "28_budget")))

    # ---- Part 6: control ----
    step("29", lambda: (t.goto("/reports/?tab=summary&days=90"), shot(t, "29a_reports_summary"),
                        t.goto("/reports/?tab=cash&days=90"), shot(t, "29b_reports_cash"),
                        t.goto("/reports/?tab=speed&days=90"), shot(t, "29c_reports_speed")))

    def s30():
        a.goto("/settings/?tab=rules"); shot(a, "30a_rules")
        a.goto("/settings/?tab=calendar"); shot(a, "30b_calendar")
        a.goto("/uploads/")
        a.locator("details.cutover summary").click(); shot(a, "30c_golive")
    step("30", s30)

    def s31():
        drawer(f, "/records/po/RA5ZCYUG/?tab=notes")
        f.locator("#drawer form.owner select").select_option("noura"); toast(f, "Assigned")
        f.wait_for_timeout(900)
        d = drawer(f, "/records/po/RA5ZCYUG/?tab=notes")
        d.locator("#note-in").fill("@priya Amazon's cost on DI-CC521 is below our price list. Can you confirm the new agreed cost?")
        d.get_by_role("button", name="Add note").click(); toast(f, "notified")
        pr.goto("/action/")
        pr.locator("button[aria-label=Notifications]").click(); shot(pr, "31_mention_alert", 1000)
    step("31", s31)

    b.close()

print("shots:", len(done), "→", OUT)
print("\n".join(errs) if errs else "no errors")
