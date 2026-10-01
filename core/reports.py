"""Management report pack: how well each step of the Amazon business ran over a period."""
from datetime import timedelta

from django.db.models import Sum
from django.utils import timezone

from .workcal import add_working_days


def _pct(a, b):
    return None if not b else round(a * 100 / b, 1)


def _k(label, value, sub="", tone="", unit=""):
    return dict(label=label, value=value, sub=sub, tone=tone, unit=unit)


def _rate(label, a, b, sub, good=95, ok=85, invert=False):
    p = _pct(a, b)
    if p is None:
        return _k(label, "—", "Nothing in this period")
    tone = (("ok" if p <= good else "warn" if p <= ok else "bad") if invert
            else ("ok" if p >= good else "warn" if p >= ok else "bad"))
    return _k(label, f"{p:g}", sub, tone, "%")


def _sar(h):
    return f"{round((h or 0) / 100):,}"


def report(days=30, now=None):
    from billing.models import Invoice
    from claims.models import Claim
    from debitnotes.models import DebitNote
    from fulfilment.models import Shipment
    from orders.models import PoLine, PurchaseOrder
    from payments.models import Dispute, Payment
    from payments.views import ageing
    from promotions.models import Promotion

    from .models import AuditEvent
    now = now or timezone.now()
    since = now - timedelta(days=days)
    acts = lambda *a: AuditEvent.objects.filter(action__in=a, at__gte=since).count()
    out = []

    # Orders
    pos = list(PurchaseOrder.objects.filter(confirmed_at__gte=since))
    lines = PoLine.objects.filter(po__in=pos).aggregate(o=Sum("qty_ordered"), c=Sum("qty_confirmed"))
    out.append(dict(title="Orders", kpis=[
        _rate("Confirmed on time", sum(1 for p in pos if p.confirmed_at <= p.confirm_by), len(pos), f"of {len(pos)} POs confirmed"),
        _rate("Fill rate", lines["c"] or 0, lines["o"] or 0, f"{lines['c'] or 0:,} of {lines['o'] or 0:,} units confirmed", good=90, ok=75),
        _k("Rejected in full", sum(1 for p in pos if p.stage == "rejected"), "POs"),
        _k("Amazon changes / cancellations", acts("amazon_change", "amazon_cancel"), "applied to POs"),
    ]))

    # Shipping
    shs = list(Shipment.objects.filter(submitted_at__gte=since))
    out.append(dict(title="Shipping", kpis=[
        _rate("ASN sent on time", sum(1 for s in shs if s.submitted_at <= s.ship_date), len(shs), f"of {len(shs)} ASNs, before the truck left"),
        _k("Appointments missed / refused", acts("missed", "refused"), "each can bring a chargeback", "bad" if acts("missed", "refused") else "ok"),
        _k("Appointments rescheduled", acts("reschedule"), "slots moved"),
    ]))

    # Invoicing and cash
    invs = list(Invoice.objects.filter(invoice_date__gte=since).select_related("po"))
    on_time = sum(1 for i in invs if i.po.delivered_at and i.invoice_date <= add_working_days(i.po.delivered_at, 2))
    pays = Payment.objects.filter(remit_date__gte=since).exclude(status="unmatched").aggregate(p=Sum("paid_h"), d=Sum("deduction_h"))
    paid, ded = pays["p"] or 0, pays["d"] or 0
    closed = list(Dispute.objects.filter(status__in=["won", "lost"], updated_at__gte=since))
    disputed = sum(d.amount_h for d in closed)
    recovered = sum(d.recovered_h or 0 for d in closed if d.status == "won")
    aged, _, terms = ageing(now)
    out.append(dict(title="Invoicing and cash", kpis=[
        _rate("Invoiced within 2 working days", on_time, len(invs), f"of {len(invs)} invoices after delivery"),
        _k("Invoices rejected / held by Amazon", acts("invoice_rejected", "invoice_on_hold"), "need a correction"),
        _k("Received from Amazon", _sar(paid), f"SAR in {days} days", unit=""),
        _rate("Deduction rate", ded, paid + ded, f"SAR {_sar(ded)} deducted", good=2, ok=5, invert=True),
        _rate("Recovered in disputes", recovered, disputed, f"SAR {_sar(recovered)} of {_sar(disputed)} closed", good=60, ok=30),
        _k("Owed past terms", _sar(sum(r["owed"] for r in aged if r["overdue"])), f"SAR · {terms}-day terms · {sum(1 for r in aged if r['overdue'])} invoices",
           "bad" if any(r["overdue"] for r in aged) else "ok"),
    ]))

    # Promotions
    dns = DebitNote.objects.filter(validated_at__gte=since).aggregate(a=Sum("approved_h"), d=Sum("disputed_h"))
    cl = Claim.objects.filter(sent_at__gte=since).aggregate(a=Sum("amount_h"), c=Sum("cn_h"))
    ended = Promotion.objects.filter(end__gte=since, end__lte=now).prefetch_related("lines__sku", "fees")
    sales = support = 0
    for p in ended:
        for l in p.lines.all():
            if l.sold_units is not None:
                sales += l.sold_units * l.sku.cost_h
                support += l.sold_units * l.support_h
        support += sum(f.amount_h for f in p.fees.all())
    out.append(dict(title="Promotions", kpis=[
        _k("Billed by Amazon", _sar(dns["a"]), f"SAR in validated debit notes · SAR {_sar(dns['d'])} disputed"),
        _rate("Recovered from brands", cl["c"] or 0, cl["a"] or 0, f"SAR {_sar(cl['c'])} of {_sar(cl['a'])} claimed", good=98, ok=90),
        _k("Sales per SAR of support", "—" if not support else f"{sales / support:.1f}", "promotions that ended: units sold × cost ÷ support",
           "" if not support else ("ok" if sales / support >= 8 else "warn")),
    ]))
    return out


def rows(sections):
    return [[s["title"], k["label"], f"{k['value']}{k['unit']}", k["sub"]] for s in sections for k in s["kpis"]]
