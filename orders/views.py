from datetime import datetime, timedelta

from django.db.models import Prefetch, Q
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.http import require_POST

from billing import services as billing
from billing.services import billing_of, invoice_checks, invoice_of
from catalog.models import FulfilmentCentre
from core import htmx
from core.models import Note
from core.services import CommandError, timeline
from fulfilment import services as ful
from fulfilment.services import asn_checks, delivery_of, shipment_of, slot_at_risk
from payments.models import Payment
from rules import engine
from rules.services import get_cfg

from . import services as svc
from .models import STAGE_LABELS, STAGES, PoLine, PurchaseOrder
from .services import REASONS, get_po, line_checks, po_issues, po_units, po_value_h

TABS = [("new", "To confirm", ["new"]), ("book", "To book", ["confirmed"]), ("release", "To release", ["booked"]),
        ("ship", "In fulfilment", ["released", "asn", "slot", "delivered", "backorder"]), ("done", "Invoiced & paid", ["invoiced", "paid", "rejected", "cancelled"]),
        ("all", "All", None)]
OWNER = {"new": "PIC", "confirmed": "Planning", "booked": "Credit", "released": "PIC", "asn": "Logistics", "slot": "Logistics",
         "delivered": "PIC", "invoiced": "Finance", "backorder": "Logistics", "paid": "Finance"}


def pos_list(request):
    tab = htmx.pick(request, "tab", [k for k, _, _ in TABS], "new")
    view = htmx.pick(request, "view", ["table", "board"], "table")
    q = request.GET.get("q", "").strip()
    fc = request.GET.get("fc", "")
    vc = request.GET.get("vc", "")
    cfg = get_cfg()
    now = timezone.now()
    qs = PurchaseOrder.objects.select_related("fc").prefetch_related(Prefetch("lines", queryset=PoLine.objects.select_related("sku")))
    if fc:
        qs = qs.filter(fc__code=fc)
    if vc:
        qs = qs.filter(vendor_code=vc)
    if q:
        qs = qs.filter(Q(po_no__icontains=q) | Q(sap_order_no__icontains=q) | Q(lines__sku__model_no__icontains=q) | Q(lines__asin__icontains=q)).distinct()
    rows = []
    for p in qs:
        ls = list(p.lines.all())
        rows.append(dict(po=p, n_lines=len(ls), units=po_units(p, ls), value=po_value_h(p, ls),
                         issues=po_issues(p, cfg, ls) if p.stage == "new" else 0,
                         last=p.updated_at, state=engine.confirm_state(p.confirm_by, now, cfg) if p.stage == "new" else ""))
    counts = {k: sum(1 for r in rows if stages is None or r["po"].stage in stages) for k, _, stages in TABS}
    all_rows = rows
    if view == "table":
        st = dict((k, s) for k, _, s in TABS).get(tab)
        if st:
            rows = [r for r in rows if r["po"].stage in st]
    rows.sort(key=lambda r: (0, r["po"].confirm_by.timestamp()) if r["po"].stage == "new" else (1, -r["po"].order_date.timestamp()))
    month = [r for r in all_rows if r["po"].order_date > now - timedelta(days=30)]
    new = [r for r in all_rows if r["po"].stage == "new"]
    kpis = [
        dict(l="To confirm", v=len(new), s=f"{sum(1 for r in new if r['po'].confirm_by < now)} overdue", url="?tab=new", alert=any(r["po"].confirm_by < now for r in new)),
        dict(l="Lines needing a decision", v=sum(r["issues"] for r in new), s="Price or stock mismatch", url="?tab=new"),
        dict(l="To book / release", v=counts["book"] + counts["release"], s="Planning and credit control", url="?tab=book"),
        dict(l="Ordered, last 30 days", v=f"{round(sum(r['value'] for r in month) / 100):,}", s=f"SAR · {len(month)} POs", url="?tab=all"),
    ]
    from core.exports import sar, wants_export, xlsx
    if wants_export(request):
        return xlsx(f"POs_{tab}", ["PO", "Vendor code", "FC", "Ordered", "Confirm by", "Lines", "Units", "Value SAR", "Lines to decide", "Stage"],
                    [[r["po"].po_no, r["po"].vendor_code, r["po"].fc.code, r["po"].order_date, r["po"].confirm_by, r["n_lines"], r["units"], sar(r["value"]),
                      r["issues"], r["po"].stage_label] for r in rows])
    board = []
    if view == "board":
        for s in STAGES:
            items = [r for r in rows if r["po"].stage == s]
            board.append(dict(stage=s, label=STAGE_LABELS[s], items=items[:8], more=max(0, len(items) - 8), owner=OWNER[s]))
    from core.models import VendorCode
    return render(request, "pages/pos.html", dict(rows=rows, tab=tab, view=view, q=q, fc=fc, vc=vc, vcodes=VendorCode.objects.all(), kpis=kpis, board=board,
                  fcs=FulfilmentCentre.objects.all(), tabs=[dict(id=k, label=l, count=counts[k]) for k, l, _ in TABS], r3=cfg.p("R3", "hrs")))


def _steps(po, cfg, lines):
    i = po.stage_index
    is_new = po.stage == "new"
    pr = all(line_checks(l, cfg)["price_ok"] for l in lines)
    sk = all(line_checks(l, cfg)["stock_ok"] for l in lines)
    s = lambda done: "done" if done else ""
    arr = [dict(l="Order confirmation", st=s(i >= 1 or po.stage in ("rejected", "cancelled"))),
           dict(l="Price check", st=("done" if pr else "fail") if is_new else "done"),
           dict(l="Stock check", st=("done" if sk else "fail") if is_new else "done"),
           dict(l="Salesforce entry", st=s(bool(po.sf_order_id) or i >= 2)), dict(l="SAP booking", st=s(bool(po.sap_order_no))),
           dict(l="Credit release", st=s(i >= 3)), dict(l="ASN", st=s(i >= 4)), dict(l="Carrier Central slot", st=s(i >= 5)),
           dict(l="Dispatch & deliver", st=s(i >= 6)), dict(l="Invoice upload", st=s(i >= 7))]
    cur = next((k for k, x in enumerate(arr) if x["st"] == ""), None)
    if cur is not None and po.stage not in ("rejected", "cancelled"):
        arr[cur]["st"] = "cur"
    return arr


def drawer(request, po_no):
    po = get_po(po_no)
    tab = htmx.pick(request, "tab", ["lines", "shipment", "invoice", "checks", "timeline", "notes", "docs"], "lines")
    cfg = get_cfg()
    now = timezone.now()
    lines = list(po.lines.select_related("sku"))
    pays = list(Payment.objects.filter(po=po))
    # What the payment side is waiting on, for the footer on every tab
    short_pay = next((p for p in pays if p.status == "short"), None)
    disputed = next((p for p in pays if p.status == "disputed"), None)
    d, sh, b, inv = delivery_of(po), shipment_of(po), billing_of(po), invoice_of(po)
    base = f"/records/po/{po.po_no}/"
    from core.models import Attachment
    from payments.models import Dispute
    needs_pod = po.delivered_at is not None and not Attachment.objects.filter(entity="po", entity_id=po.po_no, kind="pod").exists()
    from core.views import owner_ctx
    ctx = dict(po=po, tab=tab, needs_pod=needs_pod, changeable=svc.CHANGEABLE, **owner_ctx("po", po.po_no), base=base, url=f"{base}?tab={tab}", lines=lines, pays=pays, short_pay=short_pay,
               open_dispute=Dispute.objects.filter(ref=disputed.payment_no).first() if disputed else None, d=d, sh=sh, b=b, inv=inv, now=now,
               value=po_value_h(po, lines), units=po_units(po, lines), state=engine.confirm_state(po.confirm_by, now, cfg),
               steps=_steps(po, cfg, lines), reasons=REASONS,
               chain=[dict(l="Amazon PO", v=po.po_no), dict(l="SAP order", v=po.sap_order_no), dict(l="Salesforce", v=po.sf_order_id),
                      dict(l="ASN", v=sh and sh.asn_no, tab="shipment"), dict(l="Invoice", v=inv and inv.invoice_no, tab="invoice"),
                      dict(l="Payment", v=pays and pays[0].payment_no, tab="invoice")],
               dtabs=[dict(id="lines", label="Lines", n=len(lines)), dict(id="shipment", label="Shipment"), dict(id="invoice", label="Invoice & payment"),
                      dict(id="checks", label="Checks"), dict(id="timeline", label="Timeline"),
                      dict(id="notes", label="Notes", n=Note.objects.filter(entity="po", entity_id=po.po_no).count()),
                      dict(id="docs", label="Documents")])
    # The footer hint reads this on every tab, so it must not depend on the Lines tab being open.
    ctx["issues"] = po_issues(po, cfg, lines) if po.stage == "new" else 0
    if tab == "lines":
        ctx["lrows"] = [dict(l=l, c=line_checks(l, cfg), need_reason=l.decision == "accept" and not line_checks(l, cfg)["price_ok"] and not l.reason) for l in lines]
        ctx["issues"] = sum(1 for r in ctx["lrows"] if r["c"]["tone"] != "ok")
        ctx["tot_ordered"] = sum(l.qty_ordered for l in lines)
        ctx["tot_conf"] = sum(l.qty_confirmed for l in lines)
        ctx["tot_value"] = sum(l.qty_confirmed * l.cost_h for l in lines)
    from fulfilment.services import open_qty, shipped_qty
    ctx.update(shipped=shipped_qty(po), any_bo=any(l.qty_backorder for l in lines), open_units=open_qty(po) if po.stage not in ("new", "rejected", "cancelled") else 0,
               invs=list(po.invoices.order_by("seq").prefetch_related("payments", "credit_memos")),
               ships=list(po.shipments.order_by("seq")),
               bo_eta=min((l.backorder_eta for l in lines if l.qty_backorder and l.backorder_eta), default=None))
    if tab == "shipment" or po.stage == "released":
        ck = asn_checks(po, cfg)
        ctx.update(asn=ck, asn_blocked=any(not c["asn_ok"] for c in ck), asn_short=any(not c["del_ok"] for c in ck),
                   slot_risk=slot_at_risk(po, cfg))
    if tab == "invoice" or po.stage == "delivered":
        ic = invoice_checks(po, cfg)
        net = sum(c["net_h"] for c in ic)
        ctx.update(ic=ic, inv_blocked=any(not c["qty_ok"] or not c["price_ok"] for c in ic), inv_net=net, inv_vat=round(net * 0.15), inv_total=net + round(net * 0.15),
                   paid=sum(p.paid_h for p in pays if p.status in ("matched", "short", "accepted", "disputed", "recovered")))
        if tab == "invoice":
            from matching.ai import available
            from matching.views import deduction_for, suggestions_for
            shorts = [p for p in pays if p.status == "short"]
            for p in shorts:
                p.ded, p.dn_sugs = deduction_for(p), suggestions_for("pay_dn", p)
            ctx.update(shorts=shorts, ai_on=available() if shorts else False)
    if tab == "checks":
        rows = []
        for l in lines:
            c = line_checks(l, cfg)
            rows.append(dict(name=f"R1 Price · {l.sku.model_no}", exp=f"{c['agreed_h'] / 100:,.2f}", act=f"{l.cost_h / 100:,.2f}", gap=f"{c['diff_h'] / 100:,.2f}", ok=c["price_ok"]))
        if po.stage == "new":
            for l in lines:
                c = line_checks(l, cfg)
                rows.append(dict(name=f"R2 Stock · {l.sku.model_no}", exp=f"≤ {c['stock']:,}", act=f"{l.qty_ordered:,}", gap="0" if c["stock_ok"] else f"{l.qty_ordered - c['stock']:,}",
                                 ok=c["stock_ok"], warn=not c["stock_ok"]))
        rows.append(dict(name="R3 Confirmation deadline", exp=timezone.localtime(po.confirm_by).strftime("%d %b, %H:%M"),
                         act=timezone.localtime(po.confirmed_at).strftime("%d %b, %H:%M") if po.confirmed_at else "—", gap="",
                         ok=(po.confirmed_at <= po.confirm_by) if po.confirmed_at else (False if po.confirm_by < now else None)))
        for c in asn_checks(po, cfg):
            rows.append(dict(name=f"R4 ASN · {c['sku'].model_no}", exp=c["delivery"], act=c["asn"], gap=c["asn"] - c["delivery"], ok=c["asn_ok"] and c["del_ok"], warn=c["asn_ok"] and not c["del_ok"]))
        if sh:
            rows.append(dict(name="R5 Slot booked before dispatch", exp="Slot", act=sh.slot_id or "None", gap="", ok=True if sh.slot_id else (False if slot_at_risk(po, cfg) else None)))
        if po.stage == "delivered":
            for c in invoice_checks(po, cfg):
                rows.append(dict(name=f"R6 Invoice · {c['sku'].model_no}", exp=c["asn_qty"], act=c["bill_qty"], gap=c["bill_qty"] - c["asn_qty"], ok=c["qty_ok"] and c["price_ok"]))
        for p in pays:
            rows.append(dict(name=f"R7 Payment {p.payment_no}", exp=f"{inv.total_h / 100:,.0f}", act=f"{p.paid_h / 100:,.0f}", gap=f"{(p.paid_h - inv.total_h) / 100:,.0f}",
                             ok=p.status in ("matched", "recovered"), warn=p.status == "accepted"))
        ctx["checks"] = rows
    if tab == "timeline":
        ctx["events"] = timeline("po", po.po_no)
    if tab == "notes":
        ctx.update(notes=Note.objects.filter(entity="po", entity_id=po.po_no), entity="po", key=po.po_no)
    if tab == "docs":
        from core.views import documents
        ctx.update(docs=documents("po", po.po_no, po), doc_entity="po", doc_key=po.po_no)
    return render(request, "records/po.html", ctx)


def _line_values(post):
    vals = {}
    for k, v in post.items():
        if k[:2] in ("d-", "q-", "r-", "e-"):
            lid = k[2:]
            cur = list(vals.get(lid, (None, None, None, None)))
            cur["dqre".index(k[0])] = v
            vals[lid] = tuple(cur)
    return vals


@require_POST
def lines_save(request, po_no):
    """Autosave of the lines form. Only a changed decision re-renders the drawer (it changes qty and reason options)."""
    before = dict(PoLine.objects.filter(po__po_no=po_no).values_list("pk", "decision"))
    po = svc.save_lines(request.user, po_no, _line_values(request.POST), request.POST.get("version"))
    # A new decision changes which inputs are editable (qty, date) and the reason list, so the drawer re-renders.
    changed = before != dict(PoLine.objects.filter(po=po).values_list("pk", "decision"))
    return htmx.done(request, refresh=False, drawer=changed, version=("po-lines-v", po.version))


@require_POST
def accept_green(request, po_no):
    n = svc.accept_all_green(request.user, po_no)
    # The PO's version moved on: tell the open drawer at once, so a quick "Confirm PO" is not refused as stale
    return htmx.done(request, f"{n} green line{'s' if n != 1 else ''} set to accept", refresh=False,
                     version=("po-lines-v,po-confirm-v", get_po(po_no).version))


@require_POST
def confirm(request, po_no):
    vals = _line_values(request.POST)
    version = request.POST.get("version")
    if vals:
        version = svc.save_lines(request.user, po_no, vals, version).version
    po, f = svc.confirm_po(request.user, po_no, version)
    return htmx.done(request, f"PO {po_no} confirmed", file=f)


@require_POST
def book(request, po_no):
    po = svc.book_po(request.user, po_no, request.POST.get("version"), request.POST.get("sap_order_no", "").strip(),
                     request.POST.get("rfpo", ""))
    return htmx.done(request, f"SAP order {po.sap_order_no}" + (f" and Salesforce {po.sf_order_id}" if po.sf_order_id else "") + " created")


@require_POST
def release(request, po_no):
    svc.release_po(request.user, po_no, request.POST.get("version"))
    d = delivery_of(get_po(po_no))
    return htmx.done(request, "Released." + (f" SAP delivery {d.delivery_no} received (simulated sync)" if d else " Waiting for the SAP delivery."))


@require_POST
def hold(request, po_no):
    po = svc.hold_po(request.user, po_no, request.POST.get("reason", ""), request.POST.get("version"))
    return htmx.done(request, f"{po.po_no} on credit hold", "info")


@require_POST
def sync_delivery(request, po_no):
    d = ful.sync_delivery(request.user, po_no)
    return htmx.done(request, f"SAP delivery {d.delivery_no} received (simulated)")


@require_POST
def submit_asn(request, po_no):
    for k, v in request.POST.items():
        if k.startswith("a-"):
            ful.set_asn_qty(request.user, po_no, k[2:], v)
    if request.POST.get("save_only"):
        return htmx.done(request, refresh=False)
    sh, f = ful.submit_asn(request.user, po_no, request.POST.get("version"))
    return htmx.done(request, f"ASN {sh.asn_no} created", file=f)


def slot(request, po_no):
    po = get_po(po_no)
    sh = shipment_of(po)
    if request.method == "POST":
        date = request.POST.get("date")
        win = request.POST.get("window", "08:00–12:00")
        try:
            day = datetime.strptime(date, "%Y-%m-%d")
        except (TypeError, ValueError):
            day = timezone.localtime(sh.ship_date).replace(tzinfo=None)
        start = timezone.make_aware(day.replace(hour=int(win[:2]), minute=0))
        ful.book_slot(request.user, po_no, request.POST.get("slot_id", ""), start, win, request.POST.get("version"),
                      freight=request.POST.get("freight", "prepaid"), reason=request.POST.get("reason", ""))
        return htmx.done(request, f"{'Pickup' if request.POST.get('freight') == 'collect' else 'Slot'} {request.POST.get('slot_id')} saved", close_modal=True)
    from core.services import peek_number
    return render(request, "dialogs/slot.html", dict(po=po, sh=sh, next_slot=f"CC{peek_number('slot', 66120)}", again=po.stage == "slot" or bool(sh.slot_outcome),
                  units=sum(l.qty for l in sh.lines.all()), day=timezone.localtime(sh.ship_date + timedelta(hours=10)).date().isoformat()))


@require_POST
def references(request, po_no):
    po = svc.set_references(request.user, po_no, request.POST.get("rfpo", ""))
    return htmx.done(request, f"RFPO saved for {po.po_no}", refresh=False)


@require_POST
def ship_backorder(request, po_no):
    po = svc.ship_backorder(request.user, po_no, request.POST.get("version"), request.POST.get("sales_order", ""))
    return htmx.done(request, f"{po.po_no}: next shipment opened" + (". SAP delivery received" if delivery_of(po) else ". Upload the SAP delivery (U5)"))


@require_POST
def close_backorder(request, po_no):
    svc.close_backorder(request.user, po_no, request.POST.get("reason", ""), request.POST.get("version"))
    return htmx.done(request, f"{po_no}: backorder closed", "info")


def change(request, po_no):
    """Apply a change or cancellation Amazon sent for the PO."""
    po = get_po(po_no)
    if request.method == "POST":
        P = request.POST
        we = None
        if P.get("window_end"):
            try:
                we = timezone.make_aware(datetime.strptime(P["window_end"], "%Y-%m-%d").replace(hour=23, minute=59))
            except ValueError:
                raise CommandError("Pick a valid ship-window date.")
        qty = {k[2:]: v for k, v in P.items() if k.startswith("n-")}
        po, ch = svc.amazon_change(request.user, po_no, qty, cancel=P.get("cancel") == "1", reason=P.get("reason", "").strip()[:200],
                                   window_end=we, version=P.get("version"))
        return htmx.done(request, f"{po.po_no} cancelled" if po.stage == "cancelled" else f"Amazon's change applied to {po.po_no}", "info", close_modal=True)
    if po.stage not in svc.CHANGEABLE:
        raise CommandError("Changes can only be applied before the ASN is sent.")
    return render(request, "dialogs/po_change.html", dict(po=po, lines=svc.po_lines(po), we=timezone.localtime(po.window_end).date().isoformat() if po.window_end else ""))


def slot_failed(request, po_no):
    po = get_po(po_no)
    if request.method == "POST":
        ful.slot_failed(request.user, po_no, request.POST.get("outcome"), request.POST.get("reason"), request.POST.get("version"))
        return htmx.done(request, f"{po_no}: appointment released. Book a new slot", "info", close_modal=True)
    return render(request, "dialogs/slot_failed.html", dict(po=po, sh=shipment_of(po)))


@require_POST
def deliver(request, po_no):
    ful.mark_delivered(request.user, po_no, request.POST.get("version"))
    return htmx.done(request, f"{po_no} delivered. SAP billing received")


@require_POST
def fix_billing(request, po_no):
    billing.fix_billing(request.user, po_no)
    return htmx.done(request, "Billing corrected. Invoice check passed")


@require_POST
def invoice_status(request, po_no):
    inv = billing.set_invoice_status(request.user, po_no, request.POST.get("status", ""), request.POST.get("note", ""),
                                     request.POST.get("invoice_no") or None)
    return htmx.done(request, f"Invoice {inv.invoice_no}: {inv.get_amazon_status_display().lower()}", "info")


@require_POST
def invoice_resubmit(request, po_no):
    inv, f = billing.resubmit_invoice(request.user, po_no, request.POST.get("note", ""), request.POST.get("invoice_no") or None)
    return htmx.done(request, f"Invoice {inv.invoice_no} sent again (revision {inv.revision})", file=f)


@require_POST
def credit_memo(request, po_no):
    from core.services import to_h
    amt = request.POST.get("amount")
    m = billing.issue_credit_memo(request.user, po_no, to_h(amt) if amt not in (None, "") else None, request.POST.get("reason", ""),
                                  request.POST.get("memo_no", ""), request.POST.get("invoice_no") or None)
    return htmx.done(request, f"Credit memo {m.memo_no} recorded")


@require_POST
def submit_invoice(request, po_no):
    inv, f = billing.submit_invoice(request.user, po_no, request.POST.get("version"))
    return htmx.done(request, f"Invoice {inv.invoice_no} submitted", file=f)
