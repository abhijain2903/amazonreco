"""Purchase order checks and commands (flows F1-F2 and booking/release)."""
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from core.services import (CommandError, actor_name, audit, check_version, notify, require, save_file)
from rules import engine
from rules.services import get_cfg

from .models import PurchaseOrder

REASONS = {
    "accept": ["", "Override: new price agreed with buyer", "Override: difference accepted"],
    "partial": ["Limited stock", "Case-pack rounding", "Other"],
    "reject": ["Cost differs from agreed price", "Out of stock", "Discontinued", "Other"],
}


# ---------- queries ----------
def line_checks(line, cfg=None):
    """R1 price (against the price valid on the PO's order date), R2 stock, and the case-pack hint."""
    from catalog.models import agreed_cost_h
    cfg = cfg or get_cfg()
    agreed = agreed_cost_h(line.sku, line.po.order_date)
    price_ok, diff = engine.price_check(line.cost_h, agreed, cfg)
    stock = line.sku.free_stock
    cp = line.sku.case_pack or 1
    return {"case_pack": cp, "case_ok": cp <= 1 or line.qty_ordered % cp == 0,
            "case_qty": (min(line.qty_ordered, max(stock, 0)) // cp) * cp,
            "agreed_h": agreed, "diff_h": diff, "price_ok": price_ok, "stock": stock,
            "stock_ok": engine.stock_check(line.qty_ordered, stock, cfg),
            "tone": engine.line_tone(line.cost_h, agreed, line.qty_ordered, stock, cfg)}


def po_lines(po):
    return list(po.lines.select_related("sku").all())


def po_issues(po, cfg=None, lines=None):
    cfg = cfg or get_cfg()
    return sum(1 for l in (lines or po_lines(po)) if line_checks(l, cfg)["tone"] != "ok")


def po_value_h(po, lines=None):
    lines = lines if lines is not None else po_lines(po)
    if po.stage == "new":
        return sum(l.qty_ordered * l.cost_h for l in lines)
    return sum(l.qty_confirmed * l.cost_h for l in lines)


def po_units(po, lines=None):
    lines = lines if lines is not None else po_lines(po)
    return sum((l.qty_ordered if po.stage == "new" else l.qty_confirmed) for l in lines)


def refresh_suggestions(po, cfg=None):
    from catalog.models import agreed_cost_h
    cfg = cfg or get_cfg()
    for l in po_lines(po):
        if l.touched:
            continue
        d, q, r = engine.suggest(l.cost_h, agreed_cost_h(l.sku, po.order_date), l.qty_ordered, l.sku.free_stock, cfg)
        if (l.decision, l.qty_confirmed, l.reason) != (d, q, r):
            l.decision, l.qty_confirmed, l.reason = d, q, r
            l.save(update_fields=["decision", "qty_confirmed", "reason", "updated_at"])


def refresh_open_pos(cfg=None):
    for po in PurchaseOrder.objects.filter(stage="new"):
        refresh_suggestions(po, cfg)


def get_po(po_no, lock=False):
    qs = PurchaseOrder.objects.select_related("fc")
    if lock:
        qs = qs.select_for_update()
    try:
        return qs.get(po_no=po_no)
    except PurchaseOrder.DoesNotExist:
        raise CommandError(f"PO {po_no} was not found.")


# ---------- line edits (drawer) ----------
@transaction.atomic
def set_line(user, po_no, line_id, decision=None, qty=None, reason=None):
    require(user, "confirm")
    po = get_po(po_no, lock=True)
    if po.stage != "new":
        raise CommandError("This PO is already confirmed.")
    line = po.lines.select_related("sku").get(pk=line_id)
    if decision and decision != line.decision:
        line.decision = decision
        if decision == "accept":
            line.qty_confirmed = line.qty_ordered
        elif decision == "reject":
            line.qty_confirmed = 0
        else:
            line.qty_confirmed = min(line.qty_ordered, max(1, line.sku.free_stock))
        line.reason = REASONS[decision][0]
    if qty is not None and line.decision == "partial":
        line.qty_confirmed = max(0, min(line.qty_ordered, int(qty or 0)))
    if reason is not None and not decision:
        line.reason = reason
    line.touched = True
    line.save()
    return po


@transaction.atomic
def save_lines(user, po_no, values, version=None):
    """values: {line_id: (decision|None, qty|None, reason|None)} from the drawer form.

    A change bumps the PO version, so another person's older view of the lines is refused (StaleRecord)."""
    require(user, "confirm")
    po = get_po(po_no, lock=True)
    check_version(po, version)
    if po.stage != "new":
        raise CommandError("This PO is already confirmed.")
    changed = False
    for l in po_lines(po):
        if str(l.pk) not in values:
            continue
        d, q, r = values[str(l.pk)]
        before = (l.decision, l.qty_confirmed, l.reason)
        if d and d != l.decision:
            l.decision = d
            l.qty_confirmed = l.qty_ordered if d == "accept" else 0 if d == "reject" else min(l.qty_ordered, max(1, l.sku.free_stock))
            l.reason = REASONS[d][0]
        else:
            if l.decision == "partial" and q not in (None, ""):
                l.qty_confirmed = max(0, min(l.qty_ordered, int(q)))
            if r is not None and r in REASONS[l.decision]:
                l.reason = r
        if (l.decision, l.qty_confirmed, l.reason) != before:
            l.touched = True
            l.save()
            changed = True
    if changed:
        po.bump()
        po.save(update_fields=["version", "updated_at"])
    return po


@transaction.atomic
def accept_all_green(user, po_no):
    require(user, "confirm")
    po = get_po(po_no, lock=True)
    cfg = get_cfg()
    n = 0
    for l in po_lines(po):
        if line_checks(l, cfg)["tone"] == "ok":
            l.decision, l.qty_confirmed, l.reason, l.touched = "accept", l.qty_ordered, "", True
            l.save()
            n += 1
    if n:
        po.bump()
        po.save(update_fields=["version", "updated_at"])
    return n


# ---------- stage commands ----------
def _confirm(po, at, user=None, name=None):
    lines = po_lines(po)
    accepted = [l for l in lines if l.qty_confirmed > 0]
    po.stage = "confirmed" if accepted else "rejected"
    po.confirmed_at = at
    po.bump()
    po.save()
    text = ("PO rejected in full. Acknowledgement sent to Amazon" if not accepted else
            f"PO confirmed: {len(accepted)} of {len(lines)} lines accepted, "
            f"{sum(l.qty_confirmed for l in lines):,} units. Acknowledgement sent to Amazon")
    audit("po", po.po_no, text, user, name=name, action="confirm", at=at,
          after={"lines": [[l.sku.sku_code, l.decision, l.qty_confirmed, l.reason] for l in lines]})
    return lines


@transaction.atomic
def confirm_po(user, po_no, version=None):
    require(user, "confirm")
    po = get_po(po_no, lock=True)
    check_version(po, version)
    if po.stage != "new":
        raise CommandError("This PO is already confirmed.")
    cfg = get_cfg()
    lines = po_lines(po)
    for l in lines:
        if l.decision == "accept" and not line_checks(l, cfg)["price_ok"] and not l.reason:
            raise CommandError(f"Pick an override reason for {l.sku.model_no}, or reject the line.")
    now = timezone.now()
    _confirm(po, now, user)
    if any(l.decision == "accept" and not line_checks(l, cfg)["price_ok"] for l in lines):
        audit("po", po.po_no, "Price check overridden on at least one line (reason recorded)", user,
              action="override")
    status = {"accept": "Accepted", "partial": "Backordered/partial", "reject": "Rejected"}
    rows = [["po_no", "asin", "model_no", "qty_ordered", "qty_confirmed", "status", "reason"]] + [
        [po.po_no, l.asin, l.sku.model_no, l.qty_ordered, l.qty_confirmed, status[l.decision], l.reason] for l in lines]
    f = save_file("po_ack", f"PO_ack_{po.po_no}.csv", rows, "po", po.po_no)
    from integrations.connectors import get_adapter
    get_adapter("amazon_vc").push("po_ack", f)
    return po, f


def _book(po, at, user=None, name=None, sap_order_no=None):
    from integrations.connectors import get_adapter
    sap_no, sf_id = get_adapter("sap").create_sales_order(po, sap_order_no)
    po.sap_order_no, po.sf_order_id = sap_no, sf_id
    po.stage, po.booked_at = "booked", at
    po.bump()
    po.save()
    audit("po", po.po_no, f"Sales order {sap_no} created in SAP"
          + (f" and order {sf_id} logged in Salesforce" if sf_id else ""), user, name=name, action="book", at=at)


@transaction.atomic
def book_po(user, po_no, version=None, sap_order_no=None):
    require(user, "book")
    po = get_po(po_no, lock=True)
    check_version(po, version)
    if po.stage != "confirmed":
        raise CommandError("Only confirmed POs can be booked.")
    _book(po, timezone.now(), user, sap_order_no=sap_order_no or None)
    return po


def _release(po, at, user=None, name=None, with_delivery=True):
    po.stage, po.released_at = "released", at
    po.bump()
    po.save()
    audit("po", po.po_no, "Credit check passed. Order released for shipment", user, name=name, action="release", at=at)
    if with_delivery:
        from fulfilment.services import make_delivery
        make_delivery(po, at + timedelta(hours=2))


@transaction.atomic
def release_po(user, po_no, version=None):
    require(user, "release")
    po = get_po(po_no, lock=True)
    check_version(po, version)
    if po.stage != "booked":
        raise CommandError("Only booked orders can be released.")
    if po.credit_hold:
        audit("po", po.po_no, f"Credit hold lifted ({po.credit_hold})", user, action="unhold")
        po.credit_hold = ""
    _release(po, timezone.now(), user, with_delivery=settings.DEMO_SIMULATIONS)
    return po


@transaction.atomic
def hold_po(user, po_no, reason, version=None):
    """Credit control holds a booked order (credit limit, overdue balance …) with a reason; releasing lifts it."""
    require(user, "release")
    po = get_po(po_no, lock=True)
    check_version(po, version)
    if po.stage != "booked":
        raise CommandError("Only booked orders can be put on hold.")
    reason = (reason or "").strip()
    if not reason:
        raise CommandError("Give a reason for the hold, e.g. over credit limit.")
    po.credit_hold = reason[:200]
    po.bump()
    po.save()
    audit("po", po.po_no, f"Put on credit hold: {po.credit_hold}", user, action="hold", reason=po.credit_hold)
    return po


def create_po(po_no, fc, order_date, confirm_by, lines, *, window_start=None, window_end=None, user=None,
              name=None, source="file upload"):
    """Create a PO with lines [(sku, qty, cost_h)] and run R1-R3 suggestions."""
    po = PurchaseOrder.objects.create(po_no=po_no, fc=fc, order_date=order_date, confirm_by=confirm_by,
                                      window_start=window_start or order_date + timedelta(days=3),
                                      window_end=window_end or order_date + timedelta(days=10))
    for i, (sku, qty, cost_h) in enumerate(lines):
        po.lines.create(position=i, sku=sku, asin=sku.asin, qty_ordered=qty, cost_h=cost_h)
    refresh_suggestions(po)
    audit("po", po_no, f"PO received from Amazon ({len(lines)} lines) via {source}", user, name=name,
          system=user is None, action="import", at=order_date + timedelta(hours=1) if user is None else None)
    return po


def notify_issues(po):
    n = po_issues(po)
    if n:
        notify(f"PO {po.po_no}: {n} line{'s' if n > 1 else ''} need a decision", "warn", ("po", po.po_no, "lines"))
    return n


def actor(user):
    return actor_name(user)
