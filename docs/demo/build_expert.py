"""ME Vendor Hub — expert demo script (A4 landscape PDF).

    python docs/demo/expert_run.py          # fresh data first: seed_demo --reset; screenshots into docs/demo/out/shots
    python docs/demo/build_expert.py        # -> ME_Vendor_Hub_Expert_Demo_Script.pdf in the project folder

Scene numbers, cross-references ([[key]] in any text), the agenda and the cast table are computed, so scenes can be
added or moved freely."""
import html
import os
import re
import subprocess

from PIL import Image
from playwright.sync_api import sync_playwright

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
SHOTS, JPG = os.path.join(HERE, "out", "shots"), os.path.join(HERE, "out", "jpg")
OUT = os.path.join(ROOT, "ME_Vendor_Hub_Expert_Demo_Script.pdf")
BUILD = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True, text=True).stdout.strip()
os.makedirs(JPG, exist_ok=True)


def jpg(name):
    src, dst = os.path.join(SHOTS, name + ".png"), os.path.join(JPG, name + ".jpg")
    im = Image.open(src).convert("RGB")
    im.thumbnail((2000, 2000))
    im.save(dst, "JPEG", quality=84, optimize=True)
    return "file://" + dst


P = []   # pages: dict(part, n, title, who, where, shot, real, do, hub, qa)


def scene(part, title, who, where, shot, real, do, hub, qa=(), key=None):
    P.append(dict(part=part, n=len(P) + 1, title=title, who=who, where=where, shot=shot, real=real, do=do, hub=hub, qa=qa, key=key))


A, B, C, D, E, F = ("1 · Start of the day", "2 · Confirming Amazon's POs", "3 · Book, credit, ship, invoice",
                    "4 · Cash, deductions and returns", "5 · Promotions to credit note", "6 · Control and management")

# ---------------- Part 1 ----------------
scene(A, "Where the business stands this morning", "Tariq · Manager", "Dashboard", "01_dashboard",
      "A manager wants one view of open orders, cash at risk and promotions, without asking four teams.",
      ["Sign in as <b>Tariq (Manager)</b>.", "Walk the KPI row, then the two pipelines.", "Point at the red numbers: DN variance and CN shortfall."],
      ["Everything is live state: open POs and value in flow, POs waiting for confirmation, unpaid invoices, open disputes, promotions.",
       "Sell-in pipeline (PO → paid) and sell-out pipeline (promotion → credit note); red segments are money at risk."],
      [("Is this a report someone prepares?", "No. It is computed from the records each time the page opens; nobody maintains it."),
       ("Where is the trend over time?", "In Reports ([[reports]]). The dashboard is deliberately 'now'.")])

scene(A, "ME's PO tracker, kept by the hub", "Tariq · Manager", "Dashboard → PO tracker", "01a_po_tracker",
      "The team keeps a PO tracker in Excel today and re-types every SAP and Amazon number into it.",
      ["Click the <b>PO tracker</b> tab on the dashboard.", "Walk ME's own columns: PO, Article, ASIN, CAT, WH, Qty, Cost, Delivery value, "
       "Max hand-off date, RFPO, Sales order, ASN, DEL ID, Del date, Invoice no, Inv submission.", "Click <b>Export</b>: the same sheet, same column order."],
      ["Built from the hub's records — nobody types it. One row per PO line and booking portion; quantities not yet shipped get their own shaded row.",
       "Red: hand-off date passed and not delivered. Amber: delivered but not invoiced after 2 working days.",
       "Filters: open / invoiced / all, warehouse, category, vendor code, PO search; Stage and Vendor code are extra columns after ME's."],
      [("Where do RFPO and the sales order come from?", "RFPO is typed at booking (editable under References on the PO); the sales order comes from SAP for each portion."),
       ("Is the export ours or yours?", "Yours: the Excel has exactly the tracker's columns and order, so existing formulas and pivots keep working.")])

scene(A, "One PO line booked in two portions", "Tariq · Manager", "PO tracker → search 5DRWD3VG", "01b_split_portions",
      "ME's example: Amazon ordered 438 units on one line; it was booked in two portions, 386 and 52, each with its own paperwork.",
      ["Search <b>5DRWD3VG</b>, status All.", "Show the two rows: 386 and 52 units, sales orders 3009477 and 3009779, two ASNs, two delivery IDs, two invoices."],
      ["Each portion has its own SAP sales order, delivery, ASN, delivery date and invoice; the total still shows 438.",
       "A short SAP delivery or a backorder produces the second portion the same way ([[ship2]])."],
      [("Can a portion's sales order differ from the PO's?", "Yes — the first portion uses the PO's SAP order; later portions carry their own (from SAP, or the U5 delivery upload).")])

scene(A, "Sell-out tracker: stock, sell-out and forecast", "Tariq · Manager", "Dashboard → Sell-out tracker", "01c_sellout_tracker",
      "Today someone downloads Vendor Central's sales and inventory reports every week and pastes them into a sheet.",
      ["Click <b>Sell-out tracker</b>: Model, ASIN, Cat, Status, RRP, PO cost, SOH, sell-out per month (MTD), this month's sell-in and sell-out forecast.",
       "Point at the amber rows: less than two weeks of cover."],
      ["Sell-out (net of returns) and stock on hand come from Vendor Central's sales and inventory reports — upload U11; the forecast from Amazon's forecasting report — U12.",
       "Headers follow ME's style (“SOH 3rd Oct”, “MTD Jan, 2026”); Sell-in so far and Weeks of cover are the hub's extra columns.",
       "The same sell-out figures feed the debit-note check: units sold during a promotion ([[dn600]])."],
      [("Does Amazon's forecast include sell-in?", "When the file has it, it is used; otherwise the hub estimates it (sell-out forecast − stock + 4 weeks' cover) and marks it as an estimate."),
       ("Can this come automatically?", "Yes — the Reports API (sales and inventory, Brand Analytics role) replaces the upload once connected.")])

scene(A, "Claim tracker: from agreement to credit note", "Tariq · Manager", "Dashboard → Claim tracker", "01d_claim_tracker",
      "The claim tracker is how ME sees which promotions are still unclaimed and for how long.",
      ["Click <b>Claim tracker</b>: Cat, VC, Promotion title, both MECL references, AMZ agreement ID, status, start / end, days pending, "
       "submitted date and value with and without VAT, Amazon's claim value.", "Filter <b>Pending</b>: red rows are more than 30 days after the promotion ended."],
      ["Days pending count from the promotion end until the claim is sent, then show Done; VAT is 15%.",
       "AMZ claim value is what Amazon charged on its debit notes; Credited and Gap (extra columns) show what the brand paid back."],
      [("Where does the MECL reference come from?", "From Salesforce (PRO-…) and the brand reference — typed on the promotion or loaded with U7.")])

scene(A, "Each person's queue: only what needs a human", "Faisal · Amazon account (PIC)", "Action Center", "02_action",
      "Today the PIC starts the day in Vendor Central and email. The hub gives one list, most urgent first.",
      ["Switch to <b>Faisal</b> (top-right user menu).", "Show the tabs: All · Due today · Mismatches · Waiting on others.",
       "Toggle <b>My role / Everyone</b>."],
      ["Each item carries the amount, the deadline and one button for the next step.",
       "Deadlines count Saudi working time: Sunday–Thursday, 08:00–17:00, public holidays skipped (Settings → Calendar).",
       "An item assigned to a person shows their name and leaves other people's lists."],
      [("What drops off the list automatically?", "Anything whose checks passed: a PO with all lines green moves on; a payment that matches its invoice closes itself."),
       ("Can work be given to one person?", "Yes — any PO, promotion, dispute, debit note or return has an owner picker ([[owners]]).")])

# ---------------- Part 2 ----------------
scene(B, "An overdue PO with nothing wrong in it", "Faisal · PIC", "Purchase Orders → KU4YDNY8", "03a_overdue_po",
      "Amazon expects the acknowledgement within 24 hours. Clean POs still miss it because they sit in a queue behind problem POs.",
      ["Open <b>KU4YDNY8</b> (DMM-FC1) from To confirm — it is past its confirm-by time.",
       "Click <b>Accept all green</b>, then <b>Confirm PO</b>."],
      ["R1 price and R2 stock ran on every line when the PO arrived; all four are green.",
       "R3 flags the confirm-by deadline (overdue here) in the list, the drawer and the Action Center."],
      [("Does the hub send the acknowledgement to Amazon?", "Today it produces the file (next page). With the Vendor Orders API or EDI 855 connected, the same click sends it."),
       ("What if two people work the same PO?", "Every change carries the record's version; the second person is told it changed and sees the fresh state.")])

scene(B, "The acknowledgement, ready for Amazon", "Faisal · PIC", "Confirm PO → file", "03b_ack_file",
      "The PIC re-types decisions into Vendor Central today.",
      ["Show the file: one row per line with the decision and reason.", "Click <b>Done</b>."],
      ["The file is also kept on the PO's Documents tab — it does not disappear when the pop-up closes.",
       "The decision is recorded on the PO's timeline with who and when."],
      [("Can we see past acknowledgements?", "Documents tab on every PO lists every file the hub produced plus anything attached.")])

scene(B, "Amazon's cost differs from the agreed price", "Faisal · PIC", "PO 8TY68ZN6 → Checks", "04a_price_checks",
      "Amazon's PO cost lags price changes. Confirming at the wrong cost leads to a price deduction later.",
      ["Open <b>8TY68ZN6</b> → <b>Checks</b> tab.", "Point at PA-EX237G: agreed 150.00, PO 142.50, gap −7.50."],
      ["R1 compares against the agreed price <b>valid on the PO's order date</b> — the price list keeps its history (valid from / to).",
       "Tolerance is set in Settings → Rules (R1 and the global R12)."],
      [("We changed the price on the 1st; the PO is from the 29th. Which applies?", "The one valid on the 29th. A price set with a future valid-from does not affect older POs."),
       ("Can someone just accept it?", "Only with an override reason (next page); the reason goes into the audit trail.")])

scene(B, "Deciding the red line", "Faisal · PIC", "PO 8TY68ZN6 → Lines", "04b_price_line",
      "The real choice: reject the line, or accept because the buyer agreed the new cost.",
      ["On the red line choose <b>Reject</b> (reason: cost differs) <i>or</i> keep Accept and pick an override reason.",
       "Show that Confirm PO is refused while an accepted red line has no reason."],
      ["Decisions: Accept · Partial (rest dropped) · <b>Backorder rest</b> · Reject — each with its own reason list.",
       "The drawer re-renders when a decision changes, so the quantity / date fields that apply become editable."],
      [("Who may override a failed check?", "PIC, Finance and Manager — set in the permission matrix; always with a reason.")])

scene(B, "Short stock: confirm now, ship the rest later", "Faisal · PIC", "PO CMAMACG8 → Lines", "05a_backorder_line",
      "Your feedback #1: when stock is short, vendors acknowledge the shortfall as backordered with a date and ship it later on the same PO.",
      ["Open <b>CMAMACG8</b>. PA-TW483W: ordered 84, available 76 (amber).",
       "Set the decision to <b>Backorder rest</b>: 76 now, <b>+8 later</b>; set the expected date.", "Click <b>Confirm PO</b>."],
      ["R2 uses <b>available</b> stock: free stock minus what POs due earlier have already been promised (“held for earlier POs”).",
       "Backordered units stay open on the PO; they become shipment 2 when stock arrives ([[ship2]])."],
      [("Two POs today want the same 76 units — who gets them?", "The PO with the earlier confirm-by. The later one sees only what is left, so the stock is never promised twice."),
       ("Does Amazon accept backorders?", "Yes — the acknowledgement supports backordered quantities with an expected date (next page).")])

scene(B, "What Amazon receives for a backorder", "Faisal · PIC", "Confirm PO → file", "05b_backorder_ack",
      "Amazon needs to know what is coming now and what later.",
      ["Show the row for PA-TW483W: 84 ordered, 76 confirmed, 8 backordered, expected date, status Backordered."],
      ["Other lines show Accepted. The PO's value counts confirmed plus backordered units."],
      [("And if the stock never comes?", "Close the backorder with a reason (Amazon cancelled, supplier failed); the PO closes on what shipped.")])

scene(B, "Case packs and master data in the hub", "System admin", "Settings → SKU master", "06a_sku_edit",
      "Amazon orders 8 units of a model packed in cartons of 6. Warehouses ship whole cases.",
      ["Sign in as <b>admin</b> → Settings → SKU master → <b>Add or edit one SKU</b>.",
       "Edit DI-VC455S (ME10113): case pack <b>6</b>. Save."],
      ["Single SKU and dated price edits in Settings, with history; uploads U1 / U2 for bulk.",
       "Master data is never created by a transaction import — an unknown SKU on a PO is an error, not a new product."],
      [("Who can change master data?", "Admin only (permission matrix); every change is in the audit trail with before / after.")])

scene(B, "The case-pack hint on the PO", "Faisal · PIC", "PO Z5L5YAEW → Lines", "06b_case_pack",
      "",
      ["Open <b>Z5L5YAEW</b>: DI-VC455S now says “Not whole cases of 6: 6 would be whole cases”.",
       "Use Partial with reason Case-pack rounding if ME rounds down."],
      ["The hint is advisory; the decision stays with the PIC."],
      [("Can it round automatically?", "It can, but most vendors want a person to decide case rounding per account — confirm ME's rule.")])

scene(B, "Amazon changes or cancels a PO", "Faisal · PIC", "PO 5SJQWU42 → Amazon change", "07_amazon_change",
      "Amazon cuts quantities, moves the ship window or cancels after the PO was sent.",
      ["Open confirmed PO <b>5SJQWU42</b> → <b>Amazon change</b>.",
       "Cut DI-AC681B to 30, add the reference, <b>Apply change</b>. (Or tick “cancelled the whole PO”.)"],
      ["Quantities can only go down; the backorder is cut first, then what ships now; a draft ASN is capped.",
       "If part of the PO already shipped, a cancel closes it on what shipped — delivered goods stay invoiced.",
       "Re-uploading the PO file (U4) applies the same changes."],
      [("Can Amazon increase a line?", "Not on the same PO — extra units come as a new PO. The hub refuses an increase."),
       ("After the ASN?", "Changes stop at the ASN; after that it is a shortage conversation with Amazon.")])

# ---------------- Part 3 ----------------
scene(C, "Credit control holds a booked order", "Omar · Credit control", "PO CXTQTJSP", "08_credit_hold",
      "Credit control blocks orders for limits or overdue balances; today that lives in email.",
      ["Switch to <b>Omar</b> → To release → <b>CXTQTJSP</b>.", "Type the reason, click <b>Hold</b>.",
       "Show the hint “On credit hold: …” and the button <b>Lift hold &amp; release</b>."],
      ["The hold appears in the Action Center with its reason; release lifts it and records both steps.",
       "Release creates the SAP delivery (simulated in the demo; U5 upload or the SAP connector in production)."],
      [("Partial release?", "Not built: an order is held or released in full. Tell us if ME releases part of an order.")])

scene(C, "SAP delivered less than was confirmed", "Khalid · Logistics", "Shipments → 66TSGPW2", "09a_asn_short", key="short", real=
      "Stock arrives in batches: SAP picks 32 of the 34 confirmed units.", do=
      ["Switch to <b>Khalid</b> → Shipments → ASN to create → <b>66TSGPW2</b>.",
       "Show the amber row PA-NB228B: confirmed 34, SAP delivery 32, ASN 32.", "Click <b>Submit ASN</b>."],
      hub=["R4: the ASN must equal what SAP delivered (blocking); short of confirmed is a warning.",
       "The 2 units not shipped stay open on the PO and come as the next shipment."],
      qa=[("Why not send 34 on the ASN?", "Amazon receives against the ASN; announcing units that are not on the truck creates a shortage and an ASN-accuracy chargeback.")])

scene(C, "Cartons and SSCC labels", "Khalid · Logistics", "PO 66TSGPW2 → Shipment", "09b_cartons",
      "Amazon receives by carton label; labels that don't match the ASN are a classic chargeback.",
      ["Open the <b>carton labels</b> list under the ASN."],
      ["One SSCC per carton (case packs respected), and a label file in Documents for the label printer.",
       "SSCC = GS1 prefix + serial + check digit; ME's real GS1 prefix goes in the server settings."],
      [("We use a 3PL for labels.", "Then the label file is what you send them — or we switch label generation off. Confirm who prints.")])

scene(C, "Freight: Amazon collects (routing request)", "Khalid · Logistics", "Slot to book → P79ELFHD", "10_slot_collect",
      "Prepaid: ME books a Carrier Central appointment. Collect: ME submits a routing request and Amazon picks up.",
      ["Open <b>Book delivery slot</b> for <b>P79ELFHD</b>.", "Choose <b>Collect</b>, enter Amazon's reference ARN-58830192 and the pickup date."],
      ["R5 warns when a truck ships soon without a slot (three POs are red in Slot to book).",
       "Both paths end in the same tracked delivery."],
      [("Can the hub book Carrier Central?", "No — Amazon offers no public API for appointments; the slot is booked in the portal and recorded here.")])

scene(C, "Amazon refused the delivery", "Khalid · Logistics", "PO J3FPZQDS → Missed / refused", "11a_refused",
      "Trucks miss appointments and Amazon turns deliveries away — both can bring chargebacks.",
      ["On booked PO <b>J3FPZQDS</b> click <b>Missed / refused</b>.", "Choose Refused, reason “Carton labels did not match the ASN”."],
      ["The slot is released, the shipment goes back to booking, and the reason is kept for any chargeback dispute.",
       "Reschedules keep a count and a reason."],
      [("Where does that reason help later?", "If Amazon charges a chargeback, the dispute has the appointment history at hand; the AI reads it too.")])

scene(C, "Re-booking lands in the Action Center", "Khalid · Logistics", "Action Center", "11b_rebook",
      "",
      ["Open the Action Center: “Re-book delivery for J3FPZQDS: last slot refused”."],
      ["Routed to Logistics; Managers see it under Everyone."], [])

scene(C, "Proof of delivery, before anyone asks for it", "Khalid · Logistics", "PO 9TVM98RZ → Shipment", "12a_pod_prompt", key="pod", real=
      "Shortage deductions arrive weeks later; by then the signed delivery note is hard to find.", do=
      ["Open delivered PO <b>9TVM98RZ</b>: the banner asks for the POD.", "Click <b>Attach POD</b>."],
      hub=["The POD is attached to the PO and is ready for any dispute."])

scene(C, "Every file on the record", "Khalid · Logistics", "PO 9TVM98RZ → Documents", "12b_documents",
      "Your feedback #2: downloads disappeared when the pop-up closed.",
      ["Attach a file with type <b>Proof of delivery</b>.", "Show the list: POD, carton labels, ASN, acknowledgement — View / Download."],
      ["Generated files and attachments together; dispute evidence also appears here."],
      [("Size and types?", "PDF, images, Excel, up to 25 MB per file.")])

scene(C, "Invoice blocked: SAP billed more than was shipped", "Faisal · PIC", "Shipments → Invoice to submit → 4YQ4SZVM", "13_invoice_blocked",
      "SAP billing that differs from the ASN is short-paid by Amazon every time.",
      ["Open <b>4YQ4SZVM</b> → Invoice tab: red line, billed qty ≠ ASN qty.",
       "Demo: <b>Correct SAP billing</b> (simulated), then <b>Submit invoice</b>."],
      ["R6 blocks an invoice whose quantity or price differs from the ASN / PO.",
       "SAP stays the tax invoice of record (ZATCA); the hub checks before anything goes to Amazon."],
      [("Do you issue the e-invoice?", "No. SAP issues it with its ZATCA integration; the hub only checks and submits to Amazon.")])

scene(C, "Backorders waiting for stock", "Khalid · Logistics", "Shipments → Backorders", "14a_backorders",
      "Backorders are where vendors lose track — the PO looks done but units are still owed.",
      ["Open the <b>Backorders</b> tab: SA7N97YY, 42 units, expected date."],
      ["A short SAP delivery ([[short]]) also lands here once its first shipment is invoiced.",
       "An overdue expected date turns the Action Center item amber."], [])

scene(C, "One PO, several shipments and invoices", "Khalid · Logistics", "PO SA7N97YY → Shipment", "14b_backorder_po",
      "Shipment 1 was delivered and invoiced; 42 units are still to come.",
      ["Open <b>SA7N97YY</b>: “42 units still to ship”, footer <b>Ship the backorder</b> / <b>Close backorder</b>.",
       "Click <b>Ship the backorder</b>."],
      ["Each shipment has its own SAP delivery, ASN, slot, delivery date and invoice.",
       "The PO is marked paid only when nothing is left to ship and every invoice is settled."],
      [("Payment for invoice 1 arrives while shipment 2 is on its way?", "It matches invoice 1; the PO stays open until invoice 2 is paid too.")])

scene(C, "Shipment 2 starts", "Khalid · Logistics", "PO SA7N97YY → Shipment", "14c_second_delivery", key="ship2", real=
      "",
      do=["Show SAP delivery 2 for the 42 units, with its own sales order, ready for its ASN — the PO tracker now shows it as a second portion."],
      hub=["In file mode the second delivery comes by U5 upload; the PO waits in Released until it does."])

# ---------------- Part 4 ----------------
scene(D, "Payments Amazon could not tie to an invoice", "Priya · Finance", "Payments → To match", "15a_to_match",
      "Remittances reformat invoice numbers and pay several invoices in one line.",
      ["Switch to <b>Priya</b> → Payments → <b>To match</b>.",
       "Three cases: MEI/2026/04331 (formatting), MULTIPLE INVOICES, MEI-26-04315 (trimmed year)."],
      ["Run auto-match applies only confident, unambiguous suggestions (score 85+ and clearly ahead).",
       "Everything else is a suggestion with reasons; a person accepts."], [])

scene(D, "One remittance line paying two invoices", "Priya · Finance", "RMT-9102218", "15b_combined_payment",
      "SAR 713,960 with the reference “MULTIPLE INVOICES”.",
      ["Open <b>RMT-9102218</b>: suggested MEI-2026-04318 + MEI-2026-04316, score 82, “add up to the amount paid”.",
       "<b>Accept</b> splits the payment invoice by invoice; each part is then checked (R7)."],
      ["Matching uses rules first (reference similarity, amount, dates, open invoices); AI only where rules are unsure."],
      [("Will it ever match without us?", "Only with auto-match on and above the threshold an admin sets; every match is on the timeline."),
       ("Two instalments for one invoice?", "The first looks short; when the second covers the rest, both become matched and the PO closes.")])

scene(D, "Short-paid, with the right next step", "Priya · Finance", "Payments → Short-paid", "16a_short_paid",
      "Your feedback #3: a short-paid PO used to ask for the remittance again.",
      ["Open <b>Short-paid</b>: three deductions with Amazon's reason and the suggested type."],
      ["Each row: Dispute · Link to debit note · Accept."], [])

scene(D, "A promotion deduction that matches a debit note", "Priya · Finance", "PO CYG9KQKN → Invoice & payment", "16b_link_dn_suggestion",
      "Amazon deducts co-op / promotion support from payments; Finance must tie it to the validated debit note.",
      ["Open <b>CYG9KQKN</b>: “Short-paid by SAR 18,200 (Promotional allowance — agreement 71077964)”.",
       "Suggested: <b>Promo · link to the debit note</b> VCDN-2228996. Click <b>Link to debit note</b>."],
      ["A debit note can be linked once; the PO closes as paid with the deduction explained."],
      [("And if there is no matching debit note?", "The suggestion becomes Dispute with a drafted note; the AI reads the reason with ME's records.")])

scene(D, "Opening a dispute — the right type and evidence", "Priya · Finance", "Short-paid RMT-9102215 → Dispute", "17a_dispute_dialog",
      "Shortage of SAR 2,300 on GCGH8LCW. Amazon accepts disputes only with the evidence it expects for the type.",
      ["Open <b>Dispute</b> for RMT-9102215.", "Show the types: shortage, price, promo, damage, <b>chargeback</b> (with sub-type), <b>returns</b>, <b>co-op</b>.",
       "Open “What evidence Amazon expects”. Attach the POD; <b>Open dispute</b>."],
      ["The note is drafted from ME's records (ASN, delivery, POD); edit before sending.",
       "Chargeback sub-types: ASN accuracy, ASN on-time, labels, PO on-time, prep, overweight, missed appointment."],
      [("Is the dispute sent to Amazon?", "Disputes are raised in Vendor Central; the hub keeps the case, Amazon's case ID, evidence and follow-up date.")])

scene(D, "Amazon pays back only part", "Priya · Finance", "Dispute DSP-0042", "17b_partial_win",
      "A price dispute of SAR 10,596 is with Amazon; Amazon agrees to SAR 6,000.",
      ["Open <b>DSP-0042</b> (With Amazon).", "Enter <b>6000</b> and click <b>Mark won</b> → “Part won”."],
      ["The unrecovered SAR 4,596 shows in Reports as leakage.",
       "When the money comes in a later remittance naming the case, the hub links it and closes the dispute itself."],
      [("Follow-ups?", "Each dispute has a follow-up date; overdue ones appear in the Action Center.")])

scene(D, "Invoice on hold at Amazon, and a credit memo", "Priya · Finance", "PO EQ777JHQ → Invoice & payment", "18_invoice_hold_memo",
      "Amazon holds or rejects invoices for price / quantity mismatches; corrections go as credit memos.",
      ["Open <b>EQ777JHQ</b> → “Amazon rejected or held this invoice?” → On hold with Amazon's reason → <b>Record</b>.",
       "Show the banner with <b>Send corrected invoice</b> / <b>Hold released</b>, and the credit memo form."],
      ["A credit memo reduces what Amazon owes; a short payment of that size then closes.",
       "With several invoices on a PO, each action names the invoice."],
      [("Is the memo the tax credit note?", "No — SAP issues the tax credit note; the hub records its number and amount.")])

scene(D, "Ageing: what Amazon still owes", "Priya · Finance", "Payments → Ageing", "19_ageing",
      "Finance chases invoices past payment terms.",
      ["Open <b>Ageing</b>: 0–30 / 31–60 / 61–90 / 90+ days, owed per invoice, past-terms flag. <b>Export</b>."],
      ["Owed = invoice − credit memos − payments; settled invoices (accepted, recovered) are left out.",
       "Payment terms are a setting (60 days assumed)."], [])

scene(D, "Returns (RTV): stock coming back", "Priya · Finance", "Returns (RTV)", "20a_returns",
      "Amazon returns stock and deducts it from a later payment — often for more than actually came back.",
      ["Open <b>Returns (RTV)</b>: three requests at different stages.",
       "Open <b>RTV-118842</b> → <b>Authorise return</b> (or refuse with a reason)."],
      ["Requests come from Vendor Central by upload (U10) or are typed in; cost defaults to the agreed price on the request date."], [])

scene(D, "Counting what arrives", "Khalid · Logistics", "RTV-118517", "20b_receive",
      "The warehouse counts the return; shortages and damage matter for the deduction.",
      ["Switch to <b>Khalid</b>, open <b>RTV-118517</b>.", "Received 2 of 3, condition Damaged → <b>Record goods received</b>."],
      ["Received quantities can't exceed what Amazon asked to return."], [])

scene(D, "Matching Amazon's deduction to the return", "Priya · Finance", "RTV-117903", "20c_match_deduction",
      "7 units requested (SAR 6,720); 6 arrived (SAR 5,510). Amazon deducted SAR 6,720.",
      ["Open <b>RTV-117903</b>: requested vs received value.", "Click <b>Match</b> on RMT-9102221 (“Vendor returns — RTV-117903”)."],
      ["Up to the value received is accepted; anything above is disputed automatically."], [])

scene(D, "The over-deduction becomes a dispute", "Priya · Finance", "RTV-117903", "20d_return_disputed",
      "",
      ["Show: Disputed, dispute <b>DSP-0048</b> for SAR 1,210 — the unit that never came back."],
      ["The dispute carries the RTV number and the received counts as evidence."],
      [("And if returns come without paperwork?", "Then the deduction is the first sign; create the return from it and count what arrives. Confirm ME's reality.")])

# ---------------- Part 5 ----------------
scene(E, "A new promotion request", "Reem · Product team", "Promotions → New promotion", "21_wizard",
      "Promotions arrive from the product team in Excel; types and funding differ.",
      ["Switch to <b>Reem</b> → <b>New promotion</b>.", "Type: <b>Prime Day / event</b>; category, dates; next: models and support per unit; review."],
      ["Types: price discount, deal, coupon, Prime Day / event, price protection.",
       "Fixed fees (deal fee, co-op) can be added on top of per-unit support; bulk upload U7 for many at once."], [])

scene(E, "The agreement number can only be used once", "Reem · Product team", "MECL-PR-2026-0166 → Models", "22_r8_duplicate",
      "A copied agreement number makes a debit note land on the wrong promotion.",
      ["Open submitted <b>MECL-PR-2026-0166</b>; enter <b>71040765</b> → Record approval.",
       "Refused: R8 — already linked to MECL-PR-2026-0164. Enter the right number."],
      ["R8 keeps one agreement per promotion; the debit note check depends on it."], [])

scene(E, "Amazon extends the deal and adds a fee", "Reem · Product team", "MECL-PR-2026-0162 → Amend", "23_amend",
      "Live promotions get extended, models added, support changed — after approval.",
      ["Open live <b>MECL-PR-2026-0162</b> → <b>Amend</b>.",
       "New end date, fixed fee “Payday deal fee” SAR 2,500, tick <b>instalments</b>, reason → <b>Record amendment</b>."],
      ["Amendments are numbered with what changed; the debit-note check uses the amended terms.",
       "Instalments: Amazon may bill in several debit notes; each is checked against what is left."], [])

scene(E, "The SAR 600 in ME's own example", "Faisal · PIC", "MECL-PR-2026-0152 → Debit note check", "24_dn_600", key="dn600", real=
      "Amazon bills 192 units at SAR 50; the sales report shows 180 sold.", do=
      ["Open <b>MECL-PR-2026-0152</b> → Debit note check: expected SAR 15,500, charged SAR 16,100, <b>+600</b>.",
       "Click <b>Approve expected, dispute the rest</b> (or approve with override + reason)."],
      hub=["R10: units × rate per model against sold units, fixed fees, and the date after the promotion.",
       "Earlier debit notes on the same agreement are taken off what this one may bill."],
      qa=[("Where do sold units come from?", "Amazon's vendor sales report: the same sell-out data as the Sell-out tracker (upload U11 today, Reports API later); "
                                                   "“Pull sold units” on the promotion adds them up over the promotion dates.")])

scene(E, "A debit note with the wrong agreement number", "Faisal · PIC", "Debit Notes → VCDN-4046836", "25_dn_unlinked",
      "Amazon's agreement number on the debit note has two digits swapped.",
      ["Open <b>VCDN-4046836</b> (agreement 71060476): suggested MECL-PR-2026-0155 (71060467) with reasons.",
       "Accept to link; R10 runs."],
      ["Scored on agreement similarity, models, rate per unit and timing; the AI is asked only when the rules are unsure."], [])

scene(E, "One claim for several promotions", "Reem · Product team", "Claims → To claim", "26a_batch_select",
      "Brands want one claim per month, not one per promotion.",
      ["Tick <b>MECL-PR-2026-0150</b> and <b>-0151</b> → <b>Claim selected together</b>."],
      ["One claim per debit note, one combined file for the batch."], [])

scene(E, "Credit notes against the batch", "Reem · Product team", "Claims → Waiting for CN", "26b_batch_sent",
      "",
      ["Show the claims with batch CLB-2026-0011 and the <b>CN for batch</b> button."],
      ["One credit note is split across the batch's open claims in proportion to what each is owed; each is checked (R11)."], [])

scene(E, "The brand paid less than claimed", "Reem · Product team", "MECL-PR-2026-0146 → Claim", "27_cn_shortfall",
      "Claimed SAR 64,105; credited SAR 52,566 — SAR 11,539 short.",
      ["Open <b>MECL-PR-2026-0146</b>: R11 shortfall.", "Options: <b>Add another credit note</b> · Chase · <b>Close with write-off</b>."],
      ["Several credit notes per claim; it closes when the total matches. Write-offs show as leakage in Reports."], [])

scene(E, "Budget against committed, billed and recovered", "Reem · Product team", "Promotions → Budget", "28_budget",
      "Category budgets for promotion support.",
      ["Open <b>Budget</b>: Digital imaging is over budget this quarter.", "Set a budget from the form below the table."],
      ["Committed = support × expected units + fixed fees, from approval; billed = validated debit notes; recovered = credit notes."], [])

# ---------------- Part 6 ----------------
scene(F, "Reports: where money leaked", "Tariq · Manager", "Reports → Summary (90 days)", "29a_reports_summary", key="reports", real=
      "Management wants the trend and the leakage, not today's queue.", do=
      ["Switch to <b>Tariq</b> → Reports → 90 days.", "Walk the KPIs (vs previous period), the 12-week trends, then “Where money leaked”."],
      hub=["Leaked: deductions accepted, disputes lost or part-won, write-offs, DN overrides. Protected: recovered, DN excess disputed, credit notes."])

scene(F, "Deductions and disputes by type", "Tariq · Manager", "Reports → Cash & deductions", "29b_reports_cash",
      "",
      ["Show deductions by type (accepted / disputed / open), dispute win rates and days to close, ageing, days to pay by vendor code."],
      ["Export gives every tab as sheets in one workbook."], [])

scene(F, "How long each hand-off takes", "Tariq · Manager", "Reports → Team speed", "29c_reports_speed",
      "",
      ["Show median working hours per step vs the previous period: confirm, book, release, invoice; days to dispute, validate, credit."],
      ["Working hours count Sunday–Thursday 08:00–17:00, holidays excluded."], [])

scene(F, "Rules the business controls", "System admin", "Settings → Rules & tolerances", "30a_rules",
      "",
      ["Show R1–R12: switch on/off, tolerances, alert hours."],
      ["A change re-checks open records in the background and is logged."], [])

scene(F, "Saudi working calendar", "System admin", "Settings → Calendar", "30b_calendar",
      "",
      ["Show the holidays; add this year's Eid dates."],
      ["Every internal deadline uses it; Amazon's own clock deadlines (24-hour acknowledgement) stay on the clock."], [])

scene(F, "Go-live: loading opening data", "System admin", "Uploads → Go-live guide", "30c_golive",
      "Open POs, unpaid invoices and running promotions exist on day one.",
      ["Open the <b>Go-live guide</b>: the order to load, and what is already loaded."],
      ["Every source works by upload (U1–U10) until connectors are live; nothing is saved before preview."], [])

scene(F, "Owners and @mentions", "Faisal · PIC → Priya · Finance", "PO RA5ZCYUG → Notes; bell", "31_mention_alert", key="owners", real=
      "Work belongs to roles, but a record often needs one owner and a question to one colleague.", do=
      ["On <b>RA5ZCYUG</b> assign to Noura; add a note with <b>@priya</b>.", "Switch to Priya: the bell shows the mention."],
      hub=["Alerts go to the roles and people whose work it is; each person chooses “my work” or “everything”."])


# ---------------- HTML ----------------
CSS = """
@page { size: A4 landscape; margin: 0; }
* { box-sizing: border-box; }
body { margin: 0; font-family: 'Inter', -apple-system, 'Helvetica Neue', Arial, sans-serif; color: #1b2a2a; font-size: 10.5pt; }
.page { width: 297mm; height: 210mm; padding: 13mm 14mm 11mm; position: relative; page-break-after: always; overflow: hidden; }
.page:last-child { page-break-after: auto; }
.foot { position: absolute; bottom: 6mm; left: 14mm; right: 14mm; display: flex; justify-content: space-between; font-size: 8pt; color: #7a8a8a; }
h1 { font-size: 30pt; margin: 0 0 4mm; color: #0f5e57; letter-spacing: -0.5px; }
h2 { font-size: 17pt; margin: 0 0 3mm; color: #0f5e57; }
h3 { font-size: 11pt; margin: 4mm 0 1.5mm; color: #0f5e57; text-transform: uppercase; letter-spacing: .6px; }
.kicker { font-size: 9pt; font-weight: 700; color: #0f766e; text-transform: uppercase; letter-spacing: 1px; }
.cover { background: linear-gradient(135deg, #0f5e57 0%, #127a70 60%, #1d9486 100%); color: #fff; display: flex; flex-direction: column; justify-content: center; padding: 25mm; }
.cover h1 { color: #fff; font-size: 40pt; }
.cover p { font-size: 14pt; max-width: 190mm; line-height: 1.45; color: #e3f4f1; }
.cover .meta { margin-top: 14mm; font-size: 11pt; color: #cfe9e5; }
.badge { display: inline-block; background: rgba(255,255,255,.16); border-radius: 4px; padding: 2px 8px; margin-right: 6px; }
.grid { display: grid; grid-template-columns: 186mm 1fr; gap: 7mm; height: 166mm; }
.shot { border: 1px solid #d5e0de; border-radius: 6px; overflow: hidden; box-shadow: 0 2px 6px rgba(0,0,0,.08); align-self: start; }
.shot img { display: block; width: 100%; }
.side { font-size: 9.6pt; line-height: 1.42; overflow: hidden; }
.head { display: flex; align-items: baseline; gap: 4mm; margin-bottom: 4mm; }
.num { background: #0f766e; color: #fff; font-weight: 700; border-radius: 50%; width: 9mm; height: 9mm; display: inline-flex; align-items: center; justify-content: center; font-size: 11pt; flex: none; }
.head h2 { margin: 0; }
.where { font-size: 8.8pt; color: #4b5d5d; margin: -2mm 0 3mm 13mm; }
.where b { color: #1b2a2a; }
ol, ul { margin: 0; padding-left: 5mm; }
li { margin-bottom: 1.2mm; }
.say { background: #eef7f5; border-left: 3px solid #0f766e; padding: 2.5mm 3.5mm; border-radius: 0 4px 4px 0; font-style: italic; color: #173a37; }
.point li { color: #33494a; }
table.t { border-collapse: collapse; width: 100%; font-size: 9.6pt; }
table.t th, table.t td { border-bottom: 1px solid #dde6e4; padding: 2mm 2.5mm; text-align: left; vertical-align: top; }
table.t th { background: #eef4f3; color: #0f5e57; font-size: 8.8pt; text-transform: uppercase; letter-spacing: .4px; }
.cols { display: grid; grid-template-columns: 1fr 1fr; gap: 9mm; }
.box { border: 1px solid #d5e0de; border-radius: 6px; padding: 4mm 5mm; }
.warn { background: #fff7e6; border: 1px solid #f3d9a4; border-radius: 6px; padding: 3mm 4mm; font-size: 9.6pt; }
code { background: #eef2f1; padding: 1px 4px; border-radius: 3px; font-size: 9pt; }
p { margin: 0 0 2.5mm; line-height: 1.45; }
"""
CSS += """
.grid { grid-template-columns: 178mm 1fr; }
.side { font-size: 9.1pt; line-height: 1.38; }
.real { background:#fff7e6; border-left:3px solid #c98a12; padding:2mm 3mm; border-radius:0 4px 4px 0; margin-bottom:2mm; }
.qa p { margin:0 0 1.6mm; } .qa b { color:#0f5e57; }
.mini { font-size:8.6pt; color:#4b5d5d; }
table.t td, table.t th { padding: 1.6mm 2.2mm; }
"""


def span(ns):
    """[1,2,3,5,7,8] -> '1–3, 5, 7–8'"""
    out, i = [], 0
    while i < len(ns):
        j = i
        while j + 1 < len(ns) and ns[j + 1] == ns[j] + 1:
            j += 1
        out.append(str(ns[i]) if i == j else f"{ns[i]}–{ns[j]}")
        i = j + 1
    return ", ".join(out)


AGENDA = [(A, "dashboard, ME's three trackers, Action Center", 8), (B, "overdue, price, short stock, case pack, Amazon change", 14),
          (C, "hold, short delivery, labels, freight, refused, POD, R6, backorders", 16),
          (D, "matching, DN link, disputes, partial win, hold, memo, ageing, RTV", 16),
          (E, "types, R8, amendments, R10 SAR 600, batch claims, R11, budget", 14), (F, "reports, rules, calendar, go-live, owners and mentions", 8)]


def agenda():
    rows = "".join(f"<tr><td><b>{html.escape(p)}</b> — {html.escape(what)}</td><td>{span([s['n'] for s in P if s['part'] == p])}</td><td>{m}</td></tr>"
                   for p, what, m in AGENDA)
    return (f'<table class="t"><tr><th>Part</th><th>Scenes</th><th>Min</th></tr>{rows}'
            '<tr><td><b>Close</b> — what is live vs file today, open questions for ME</td><td>—</td><td>3</td></tr></table>')


CAST = [("Faisal Al-Harbi", "Amazon account (PIC)", "Faisal"), ("Omar Siddiqui", "Credit control", "Omar"), ("Khalid Mansour", "Logistics", "Khalid"),
        ("Priya Nair", "Finance", "Priya"), ("Reem Al-Dosari", "Product team", "Reem"), ("Tariq Hassan", "Manager", "Tariq"), ("System admin", "Admin", "admin")]


def cast():
    rows = "".join(f"<tr><td>{n}</td><td>{r}</td><td>{span([s['n'] for s in P if s['who'].startswith(k) or (k == 'admin' and 'admin' in s['who'])])}</td></tr>"
                   for n, r, k in CAST)
    return f'<table class="t"><tr><th>Person</th><th>Role</th><th>Scenes</th></tr>{rows}</table>'


KEYS = {s["key"]: s["n"] for s in P if s["key"]}


def refs(text):
    return re.sub(r"\[\[(\w+)\]\]", lambda m: f"scene {KEYS[m.group(1)]}", text)


def foot(n):
    return f'<div class="foot"><span>ME Vendor Hub · Expert demo script · Iksula</span><span>{n}</span></div>'


pages = [f"""<div class="page cover">
<div class="kicker" style="color:#bfe7e1">Iksula · Modern Electronics (KSA)</div>
<h1>ME Vendor Hub</h1>
<p><b>Expert demo script</b> — for an audience that runs the Amazon vendor process every day. One working day, end to end:
the clean cases in seconds, then the exceptions that cost money — short stock, price gaps, Amazon changes, short deliveries,
refused appointments, backorders, deductions, returns, debit-note overcharges and credit-note shortfalls.</p>
<div class="meta"><span class="badge">https://amazonhub.iksulalive.com</span><span class="badge">≈ 80 minutes</span><span class="badge">{len(P)} scenes</span><span class="badge">build {BUILD}</span></div>
</div>"""]

pages.append(f"""<div class="page"><div class="kicker">Before you start</div><h2>Setting up — and how to run it</h2>
<div class="cols"><div>
<h3>30 minutes before</h3>
<ol><li><b>Reset the demo data</b> (SSM session on the server): <code>sudo mehub-manage seed_demo --reset</code>. Every record in this script
exists after a reset; the steps change data, so reset again before a second run.</li>
<li>Check Settings → Matching shows the AI connected (OpenAI); re-enter the key if not.</li>
<li>Chrome, full screen, 100% zoom; one window shared. Have a small PDF on the desktop to attach as a POD ([[pod]]).</li>
<li>The tracker tabs on the dashboard show today's data; sell-out and forecast numbers come from the demo uploads (U11, U12).</li>
<li>Deadlines (“due in 5 h”, “overdue”) follow the clock; record numbers and amounts are fixed.</li></ol>
<h3>How to switch people</h3>
<p>Top-right user menu → pick the person. Each person has one role and sees that team's work — switch often; it is the strongest part of the demo.</p>
<h3>With an expert audience</h3>
<ul><li>Lead with the exception, not the happy path: say what goes wrong in real life, then show the hub catching it.</li>
<li>Let them pick the next exception — every scene stands on its own.</li>
<li>Be exact about what is file / manual today versus a live connector (last page).</li></ul>
</div><div>
<h3>Agenda (≈ 80 min)</h3>
{agenda()}
<h3>Three moments to land</h3>
<ul><li><b>Backorder</b> on CMAMACG8 → shipment 2 on SA7N97YY (your feedback #1).</li>
<li><b>SAR 600</b> DN overcharge on MECL-PR-2026-0152 — the example in ME's own process document.</li>
<li><b>Returns:</b> Amazon deducted SAR 6,720 for goods worth SAR 5,510 that came back — the SAR 1,210 dispute opens itself.</li></ul>
</div></div>{foot(2)}</div>""")

pages.append(f"""<div class="page"><div class="kicker">Cast and records</div><h2>Who does what, and the records used</h2>
<div class="cols"><div>
{cast()}
<h3>The checks (R1–R12)</h3>
<p class="mini">R1 price vs agreed (on the PO date) · R2 stock (after earlier POs) · R3 confirm deadline · R4 ASN = SAP delivery · R5 slot before dispatch ·
R6 invoice = ASN qty and PO price · R7 payment = invoice · R8 one agreement per promotion · R9 DN due · R10 DN vs sold units, rates, fees ·
R11 credit notes = claim · R12 global tolerance (SAR 1).</p>
</div><div>
<table class="t"><tr><th>Record</th><th>Shows</th></tr>
<tr><td>5DRWD3VG</td><td>438 units booked in two portions (PO tracker)</td></tr>
<tr><td>KU4YDNY8</td><td>Overdue clean PO (R3)</td></tr>
<tr><td>8TY68ZN6</td><td>Price below agreed (R1)</td></tr>
<tr><td>CMAMACG8</td><td>Short stock → backorder</td></tr>
<tr><td>Z5L5YAEW / 5SJQWU42</td><td>Case pack / Amazon change</td></tr>
<tr><td>CXTQTJSP</td><td>Credit hold</td></tr>
<tr><td>66TSGPW2 · P79ELFHD · J3FPZQDS</td><td>Short delivery + SSCC · collect · refused</td></tr>
<tr><td>9TVM98RZ · 4YQ4SZVM · SA7N97YY</td><td>POD · R6 block · shipment 2</td></tr>
<tr><td>RMT-9102218 · CYG9KQKN · RMT-9102215</td><td>Two invoices · DN link · dispute</td></tr>
<tr><td>DSP-0042 · EQ777JHQ</td><td>Part win · invoice hold + memo</td></tr>
<tr><td>RTV-118842 / -118517 / -117903</td><td>Authorise / receive / match</td></tr>
<tr><td>MECL-PR-2026-0166 / -0162 / -0152</td><td>R8 · amendment · R10 SAR 600</td></tr>
<tr><td>VCDN-4046836 · -0150/-0151 · -0146</td><td>Unlinked DN · batch claim · R11 shortfall</td></tr></table>
</div></div>{foot(3)}</div>""")

pg = len(pages)
for s in P:
    pg += 1
    do = "".join(f"<li>{x}</li>" for x in s["do"])
    hub = "".join(f"<li>{x}</li>" for x in s["hub"])
    qa = "".join(f"<p><b>“{html.escape(q)}”</b><br>{html.escape(a)}</p>" for q, a in s["qa"])
    real = f'<div class="real">{s["real"]}</div>' if s["real"] else ""
    pages.append(f"""<div class="page">
<div class="kicker">{html.escape(s['part'])}</div>
<div class="head"><span class="num">{s['n']}</span><h2>{html.escape(s['title'])}</h2></div>
<div class="where">Who: <b>{html.escape(s['who'])}</b> &nbsp;·&nbsp; Where: <b>{html.escape(s['where'])}</b></div>
<div class="grid"><div class="shot"><img src="{jpg(s['shot'])}"></div>
<div class="side">{real}
<h3 style="margin-top:0">Do</h3><ol>{do}</ol>
<h3>What the hub does</h3><ul class="point">{hub}</ul>
{f'<h3>Expert asks</h3><div class="qa">{qa}</div>' if qa else ''}
</div></div>{foot(pg)}</div>""")

pg += 1
pages.append(f"""<div class="page"><div class="kicker">Close</div><h2>What is live today, what is file-based, and what ME must confirm</h2>
<div class="cols"><div>
<h3>Connections (say it plainly)</h3>
<table class="t"><tr><th>Step</th><th>Today</th><th>Later</th></tr>
<tr><td>POs, acknowledgement, ASN, invoices</td><td>Upload / generated file</td><td>Vendor Orders, Shipments, Invoices APIs or EDI 850/855/856/810</td></tr>
<tr><td>Invoice status at Amazon</td><td>Recorded by hand</td><td>Invoices API (vendors)</td></tr>
<tr><td>Remittance and deductions</td><td>Upload U6</td><td>Finance Remittance API — confirm it is enabled for .sa</td></tr>
<tr><td>Returns</td><td>Upload U10 / typed</td><td>Vendor returns data</td></tr>
<tr><td>Sell-out, stock on hand, forecast</td><td>Upload U11 / U12</td><td>Reports API: sales, inventory, forecasting (Brand Analytics)</td></tr>
<tr><td>Carrier Central slots, promotions in VC</td><td>Manual</td><td>Stay manual — no Amazon API</td></tr>
<tr><td>SAP order, delivery, billing</td><td>Simulated / upload</td><td>SAP OData (S/4) or IDoc / BAPI (ECC)</td></tr>
<tr><td>Email alerts</td><td>In-app</td><td>Microsoft 365</td></tr></table>
<p class="mini">Not in scope today: Arabic interface; partial credit release.</p>
</div><div>
<h3>Questions to put to ME (write the answers down)</h3>
<ol><li>Does Amazon cancel backorders after a time? How long?</li>
<li>Freight terms: prepaid (ME books Carrier Central) or collect?</li>
<li>Who prints carton labels — ME's warehouse or a 3PL? ME's GS1 prefix?</li>
<li>Which chargebacks does ME actually receive, and how often?</li>
<li>Do returns arrive with paperwork, or only as a deduction?</li>
<li>Does Amazon bill long promotions in several debit notes?</li>
<li>Do brands pay claims in one credit note or several? Monthly batches?</li>
<li>Payment terms; budgets per category or per brand; real FC and vendor codes.</li>
<li>Trackers: what RFPO and DEL ID are in ME's systems; whether INVOICE NO is SAP's billing number; ME's lifecycle statuses.</li></ol>
<h3>Proposed next step</h3>
<div class="say" style="font-style:normal">Run one real week of ME's POs, remittances and debit notes through the hub with each team (UAT),
then switch on the first live connection.</div>
</div></div>{foot(pg)}</div>""")

doc = refs(f"<html><head><meta charset='utf-8'><style>{CSS}</style></head><body>{''.join(pages)}</body></html>")
hp = os.path.join(HERE, "out", "expert.html")
open(hp, "w").write(doc)
with sync_playwright() as pw:
    b = pw.chromium.launch()
    p = b.new_page()
    p.goto("file://" + hp)
    p.wait_for_timeout(1000)
    p.pdf(path=OUT, format="A4", landscape=True, print_background=True, margin=dict(top="0", bottom="0", left="0", right="0"))
    b.close()
print(OUT, os.path.getsize(OUT) // 1024, "KB,", len(pages), "pages,", len(P), "scenes")
