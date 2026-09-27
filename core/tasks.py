"""Background jobs (Procrastinate, queue stored in PostgreSQL).

Run the worker with:  python manage.py procrastinate worker
"""
import logging

from django.db import transaction
from django.utils import timezone
from procrastinate.contrib.django import app

log = logging.getLogger("hub.jobs")


@app.task(queue="rules", retry=3)
def run_po_checks(po_no: str):
    """Persist R1-R3 check results for one PO."""
    from orders.models import PurchaseOrder
    from orders.services import line_checks
    from rules import engine
    from rules.services import get_cfg, record

    po = PurchaseOrder.objects.filter(po_no=po_no).first()
    if not po:
        return
    cfg = get_cfg()
    checks = []
    for l in po.lines.select_related("sku"):
        c = line_checks(l, cfg)
        checks.append({"rule": "R1", "subject": l.sku.model_no, "ok": c["price_ok"], "exp": c["agreed_h"],
                       "act": l.cost_h, "gap": c["diff_h"]})
        checks.append({"rule": "R2", "subject": l.sku.model_no, "ok": c["stock_ok"], "exp": c["stock"],
                       "act": l.qty_ordered, "gap": max(0, l.qty_ordered - c["stock"])})
    checks.append({"rule": "R3", "subject": "confirm by", "ok": engine.confirm_state(po.confirm_by, timezone.now(), cfg) != "overdue"
                   or po.confirmed_at is not None, "exp": po.confirm_by.isoformat()})
    with transaction.atomic():
        record("po", po_no, checks, cfg)


@app.task(queue="rules", retry=3)
def run_dn_checks(dn_no: str):
    from debitnotes.models import DebitNote
    from debitnotes.services import evaluate
    from rules.services import get_cfg, record

    dn = DebitNote.objects.filter(dn_no=dn_no).first()
    if not dn:
        return
    cfg = get_cfg()
    ev = evaluate(dn, cfg)
    checks = [{"rule": "R10", "subject": l["sku_obj"].model_no, "ok": l.get("rate_ok", False) and l.get("units_ok", False),
               "exp": l.get("expected_h", ""), "act": l["charged_h"], "gap": l.get("gap_h", "")} for l in ev["lines"]]
    with transaction.atomic():
        record("dn", dn_no, checks, cfg)


@app.task(queue="rules")
def rerun_open_checks():
    """After a rule setting changes: refresh suggestions and check results on open records."""
    from orders.models import PurchaseOrder
    from orders.services import refresh_open_pos

    refresh_open_pos()
    for po_no in PurchaseOrder.objects.filter(stage="new").values_list("po_no", flat=True):
        run_po_checks.defer(po_no=po_no)


@app.periodic(cron="5 * * * *")
@app.task(queue="schedule")
def sweep_slots(timestamp: int):
    """R5, hourly: alert logistics when a truck ships within the window and no slot is booked."""
    from core.services import notify
    from fulfilment.services import slot_at_risk
    from orders.models import PurchaseOrder

    for po in PurchaseOrder.objects.filter(stage="asn"):
        if slot_at_risk(po):
            notify(f"No delivery slot for {po.po_no}; the truck ships soon", "bad", ("po", po.po_no, "shipment"))


@app.periodic(cron="0 4 * * *")
@app.task(queue="schedule")
def sweep_dn_timing(timestamp: int):
    """R9, daily: flag promotions whose debit note is overdue."""
    from core.services import notify
    from promotions.models import Promotion
    from promotions.services import stage_of

    for p in Promotion.objects.filter(stage="approved"):
        if stage_of(p) == "dn_overdue":
            notify(f"No debit note yet for {p.mecl_ref}. It was due {timezone.localtime(p.dn_due):%d %b}", "warn",
                   ("promo", p.mecl_ref, "dn"))


@app.periodic(cron="0 5 * * *")  # 08:00 Riyadh
@app.task(queue="schedule")
def daily_digest(timestamp: int):
    from core.actions import action_items
    from identity.models import User
    from integrations.connectors import get_adapter

    mail = get_adapter("email")
    for u in User.objects.filter(is_active=True).exclude(email=""):
        items = action_items(u, mine=True)
        if items:
            mail.send(u.email, f"Vendor Hub: {len(items)} items need you today",
                      "\n".join(f"- {i['title']}" for i in items[:20]))
