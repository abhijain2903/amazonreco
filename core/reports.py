"""Management reports: how the period went, against the previous period and week by week, and where problems and
money losses come from (by fulfilment centre, category, deduction type, promotion, team step). The dashboard shows
what is open now; these pages look back."""
from datetime import timedelta
from statistics import median

from django.utils import timezone

from .workcal import add_working_days, working_hours_between

TABS = [("summary", "Summary"), ("orders", "Orders & stock"), ("shipping", "Shipping"), ("cash", "Cash & deductions"),
        ("promos", "Promotions"), ("speed", "Team speed")]
WEEKS = 12


# ---------- small helpers ----------
def _pct(a, b):
    return None if not b else round(a * 100 / b, 1)


def _delivered(inv):
    """When the goods on this invoice were delivered (its own shipment; older data only has the PO date)."""
    return (inv.shipment.delivered_at if inv.shipment_id and inv.shipment.delivered_at else None) or inv.po.delivered_at


def _r(v):
    return None if v is None else round(v)


def _in(t, a, b):
    return t is not None and a <= t < b


def _kpi(label, cur, prev, unit="%", good="up", note=""):
    """A KPI with the previous period. good: which direction is better ("up" / "down")."""
    delta = None if cur is None or prev is None else round(cur - prev, 1)
    tone = "" if delta in (None, 0) else ("ok" if (delta > 0) == (good == "up") else "bad")
    return dict(label=label, value=cur, prev=prev, delta=delta, unit=unit, tone=tone, note=note)


def _table(title, cols, rows, note="", key=""):
    """cols: [(label, kind)] with kind text | num | sar | pct | hrs | days."""
    return dict(title=title, cols=cols, rows=rows, note=note, key=key or title)


def _weeks(now):
    """The last WEEKS Saudi weeks (Sunday to Saturday), oldest first: [(label, start, end)]."""
    d = timezone.localtime(now)
    start = (d - timedelta(days=(d.weekday() + 1) % 7)).replace(hour=0, minute=0, second=0, microsecond=0)
    out = []
    for i in range(WEEKS - 1, -1, -1):
        a = start - timedelta(weeks=i)
        out.append((f"{a:%d %b}", a, a + timedelta(weeks=1)))
    return out


def _trend(title, unit, weeks, fn, good="up"):
    pts = [dict(label=l, value=fn(a, b)) for l, a, b in weeks]
    top = max([p["value"] for p in pts if p["value"] is not None] or [0]) or 1
    for p in pts:
        p["h"] = 0 if p["value"] is None else max(3, round(p["value"] * 100 / top))
    return dict(title=title, unit=unit, points=pts, good=good)


def _deduction_type(p, dispute_types):
    """What a short-paid payment was for: the dispute's type if one was opened, else read from Amazon's reason."""
    import re

    from matching.matchers import KEYWORDS
    if p.payment_no in dispute_types:
        return dispute_types[p.payment_no]
    return next((k for k, rx in KEYWORDS if re.search(rx, p.reason or "", re.I)), "other")


# ---------- data, loaded once per request ----------
class Data:
    def __init__(self, now):
        from billing.models import Invoice
        from claims.models import Claim
        from debitnotes.models import DebitNote
        from fulfilment.models import Shipment
        from orders.models import PurchaseOrder
        from payments.models import Dispute, Payment
        from promotions.models import Promotion
        from returns.models import ReturnAuth

        from .models import AuditEvent
        self.now = now
        self.pos = list(PurchaseOrder.objects.select_related("fc").prefetch_related("lines__sku"))
        self.ships = list(Shipment.objects.select_related("po__fc").prefetch_related("lines"))
        self.invs = list(Invoice.objects.select_related("po", "shipment").prefetch_related("payments", "credit_memos"))
        self.pays = list(Payment.objects.exclude(status="unmatched").select_related("po__fc"))
        self.disputes = list(Dispute.objects.all())
        self.dns = list(DebitNote.objects.all())
        self.claims = list(Claim.objects.select_related("promotion").prefetch_related("credit_notes"))
        self.promos = list(Promotion.objects.prefetch_related("lines__sku", "fees"))
        self.rtvs = list(ReturnAuth.objects.prefetch_related("lines"))
        self.events = list(AuditEvent.objects.filter(at__gte=now - timedelta(days=200), action__in=[
            "amazon_change", "amazon_cancel", "missed", "refused", "reschedule", "invoice_rejected", "invoice_on_hold"]).values("action", "at", "entity_id"))
        self.dtype = {d.ref: d.type for d in self.disputes}

    # --- measures over a window [a, b) ---
    def confirmed(self, a, b):
        return [p for p in self.pos if _in(p.confirmed_at, a, b)]

    def on_time_confirm(self, a, b):
        ps = self.confirmed(a, b)
        return _pct(sum(1 for p in ps if p.confirmed_at <= p.confirm_by), len(ps))

    def fill_rate(self, a, b):
        ls = [l for p in self.confirmed(a, b) for l in p.lines.all()]
        return _pct(sum(l.committed for l in ls), sum(l.qty_ordered for l in ls))

    def asn_on_time(self, a, b):
        ss = [s for s in self.ships if _in(s.submitted_at, a, b)]
        return _pct(sum(1 for s in ss if s.submitted_at <= s.ship_date), len(ss))

    def invoiced_fast(self, a, b):
        iv = [i for i in self.invs if _in(i.invoice_date, a, b) and _delivered(i)]
        return _pct(sum(1 for i in iv if i.invoice_date <= add_working_days(_delivered(i), 2)), len(iv))

    def received(self, a, b):
        ps = [p for p in self.pays if _in(p.remit_date, a, b)]
        return sum(p.paid_h for p in ps) / 100 if ps else None

    def deduction_rate(self, a, b):
        ps = [p for p in self.pays if _in(p.remit_date, a, b)]
        ded = sum(p.deduction_h for p in ps)
        return _pct(ded, sum(p.paid_h for p in ps) + ded)

    def dispute_recovery(self, a, b):
        ds = [d for d in self.disputes if d.status in ("won", "lost") and _in(d.closed_at, a, b)]
        return _pct(sum(d.recovered_h or 0 for d in ds if d.status == "won"), sum(d.amount_h for d in ds))

    def brand_recovery(self, a, b):
        cs = [c for c in self.claims if _in(c.sent_at, a, b)]
        return _pct(sum(c.cn_h or 0 for c in cs), sum(c.amount_h for c in cs))

    def leakage(self, a, b):
        """Money ME gave up in the window, and money the hub protected."""
        from rules.services import get_cfg
        tol = get_cfg().tol_h()
        lost = [
            ("Deductions accepted", sum(p.deduction_h for p in self.pays if p.status == "accepted" and _in(p.remit_date, a, b)),
             "Short payments closed without a dispute (incl. linked to debit notes)"),
            ("Disputes lost", sum(d.amount_h for d in self.disputes if d.status == "lost" and _in(d.closed_at, a, b)), "Amazon kept the money"),
            ("Disputes part-won (shortfall)", sum(d.amount_h - (d.recovered_h or 0) for d in self.disputes
                                                  if d.status == "won" and _in(d.closed_at, a, b)), "Recovered less than claimed"),
            ("Credit-note write-offs", sum(c.amount_h - (c.cn_h or 0) for c in self.claims if c.status == "written_off" and _in(c.cn_date, a, b)),
             "Brand paid less than the claim"),
            ("Debit notes approved with override", sum(max(0, sum(l.charged_h for l in d.lines.all()) - d.approved_h) for d in self.dns
                                                         if d.override_reason and _in(d.validated_at, a, b)), "Accepted above the agreement"),
        ]
        saved = [
            ("Recovered in disputes", sum(d.recovered_h or 0 for d in self.disputes if d.status == "won" and _in(d.closed_at, a, b)), ""),
            ("Debit-note overcharges disputed", sum(d.disputed_h for d in self.dns if _in(d.validated_at, a, b)), "Caught by R10"),
            ("Collected from brands (credit notes)", sum(n.amount_h for c in self.claims for n in c.credit_notes.all() if _in(n.cn_date, a, b)), ""),
            ("Return over-deductions disputed", sum(d.amount_h for d in self.disputes if d.type == "returns" and _in(d.created_at, a, b)),
             "Charged for goods that never came back"),
        ]
        return lost, saved, tol


# ---------- tabs ----------
def summary(D, a, b, pa, pb, weeks):
    kpis = [
        _kpi("Confirmed on time", D.on_time_confirm(a, b), D.on_time_confirm(pa, pb), note="POs acknowledged before confirm-by"),
        _kpi("Fill rate", D.fill_rate(a, b), D.fill_rate(pa, pb), note="Units confirmed (now + backorder) ÷ ordered"),
        _kpi("ASN on time", D.asn_on_time(a, b), D.asn_on_time(pa, pb), note="ASN sent before the truck left"),
        _kpi("Invoiced within 2 working days", D.invoiced_fast(a, b), D.invoiced_fast(pa, pb)),
        _kpi("Deduction rate", D.deduction_rate(a, b), D.deduction_rate(pa, pb), good="down", note="Deducted ÷ (paid + deducted)"),
        _kpi("Recovered in disputes", D.dispute_recovery(a, b), D.dispute_recovery(pa, pb), note="Of disputes closed in the period"),
        _kpi("Recovered from brands", D.brand_recovery(a, b), D.brand_recovery(pa, pb), note="Credit notes ÷ claims sent"),
        _kpi("Received from Amazon", _r(D.received(a, b)), _r(D.received(pa, pb)), unit="SAR"),
    ]
    lost, saved, _ = D.leakage(a, b)
    tables = [_table("Where money leaked", [("Item", "text"), ("SAR", "sar"), ("What it means", "text")], [list(r) for r in lost],
                     note=f"Total SAR {sum(r[1] for r in lost) / 100:,.0f} given up in the period.", key="Leaked"),
              _table("What the checks protected", [("Item", "text"), ("SAR", "sar"), ("", "text")], [list(r) for r in saved],
                     note=f"Total SAR {sum(r[1] for r in saved) / 100:,.0f}.", key="Protected")]
    trends = [_trend("Confirmed on time, %", "%", weeks, D.on_time_confirm), _trend("Fill rate, %", "%", weeks, D.fill_rate),
              _trend("Deduction rate, %", "%", weeks, D.deduction_rate, good="down"), _trend("Received from Amazon, SAR", "SAR", weeks, D.received)]
    return dict(kpis=kpis, tables=tables, trends=trends)


def orders(D, a, b, pa, pb, weeks):
    from catalog.models import CATEGORY_NAMES
    ps = D.confirmed(a, b)
    by_fc = {}
    for p in ps:
        r = by_fc.setdefault(p.fc.code, [p.fc.code, 0, 0, 0, 0, 0])
        r[1] += 1
        r[2] += p.confirmed_at <= p.confirm_by
        for l in p.lines.all():
            r[3] += l.qty_ordered
            r[4] += l.committed
            r[5] += (l.qty_ordered - l.committed) * l.cost_h
    fc_rows = [[c, n, _pct(ot, n), o, k, _pct(k, o), lost] for c, n, ot, o, k, lost in sorted(by_fc.values())]
    by_cat = {}
    for p in ps:
        for l in p.lines.all():
            r = by_cat.setdefault(l.sku.category, [0, 0, 0])
            r[0] += l.qty_ordered * l.cost_h
            r[1] += l.committed * l.cost_h
            r[2] += l.qty_backorder
    cat_rows = [[f"{c} · {CATEGORY_NAMES.get(c, c)}", o, k, _pct(k, o), bo] for c, (o, k, bo) in sorted(by_cat.items())]
    lost = {}
    for p in ps:
        for l in p.lines.all():
            short = l.qty_ordered - l.committed
            if short > 0:
                r = lost.setdefault(l.sku_id, [l.sku.model_no, l.sku.category, 0, 0, 0, set()])
                r[2] += 1
                r[3] += short
                r[4] += short * l.cost_h
                r[5].add(l.reason or l.get_decision_display())
    lost_rows = [[m, c, n, u, v, ", ".join(sorted(rs))] for m, c, n, u, v, rs in sorted(lost.values(), key=lambda r: -r[4])[:12]]
    changes = [e for e in D.events if e["action"] in ("amazon_change", "amazon_cancel") and _in(e["at"], a, b)]
    kpis = [_kpi("POs confirmed", len(ps), len(D.confirmed(pa, pb)), unit=""),
            _kpi("Fill rate", D.fill_rate(a, b), D.fill_rate(pa, pb)),
            _kpi("Sales lost to stock / price", round(sum(r[4] for r in lost_rows) / 100), None, unit="SAR", good="down",
                 note="Units Amazon ordered that ME did not confirm"),
            _kpi("Amazon changes / cancellations", len(changes), sum(1 for e in D.events if e["action"] in ("amazon_change", "amazon_cancel")
                                                                       and _in(e["at"], pa, pb)), unit="", good="down")]
    return dict(kpis=kpis, trends=[_trend("Fill rate, %", "%", weeks, D.fill_rate), _trend("Confirmed on time, %", "%", weeks, D.on_time_confirm)], tables=[
        _table("By fulfilment centre", [("FC", "text"), ("POs", "num"), ("On time %", "pct"), ("Units ordered", "num"), ("Units confirmed", "num"),
                                        ("Fill rate %", "pct"), ("Value not confirmed SAR", "sar")], fc_rows),
        _table("By category", [("Category", "text"), ("Ordered SAR", "sar"), ("Confirmed SAR", "sar"), ("Fill rate %", "pct"), ("Units backordered", "num")], cat_rows),
        _table("Models most often short", [("Model", "text"), ("Cat.", "text"), ("PO lines", "num"), ("Units not confirmed", "num"), ("Value SAR", "sar"),
                                           ("Why", "text")], lost_rows, note="Where to look at stock planning or the agreed price."),
    ])


def shipping(D, a, b, pa, pb, weeks):
    ss = [s for s in D.ships if _in(s.submitted_at, a, b)]
    ev = [e for e in D.events if _in(e["at"], a, b)]
    by_fc = {}
    for s in ss:
        r = by_fc.setdefault(s.po.fc.code, dict(n=0, ot=0, days=[], miss=0, ref=0, res=0))
        r["n"] += 1
        r["ot"] += s.submitted_at <= s.ship_date
        if s.delivered_at:
            r["days"].append((s.delivered_at - s.submitted_at).total_seconds() / 86400)
    fc_of = {p.po_no: p.fc.code for p in D.pos}
    for e in ev:
        fc = fc_of.get(e["entity_id"])
        if fc in by_fc and e["action"] in ("missed", "refused", "reschedule"):
            by_fc[fc][{"missed": "miss", "refused": "ref", "reschedule": "res"}[e["action"]]] += 1
    rows = [[fc, r["n"], _pct(r["ot"], r["n"]), round(median(r["days"]), 1) if r["days"] else None, r["miss"], r["ref"], r["res"]]
            for fc, r in sorted(by_fc.items())]
    from fulfilment.services import open_qty
    bo = []
    for p in D.pos:
        if p.stage == "backorder":
            eta = min((l.backorder_eta for l in p.lines.all() if l.qty_backorder and l.backorder_eta), default=None)
            late = (timezone.localdate(D.now) - eta).days if eta else None
            bo.append([p.po_no, p.fc.code, open_qty(p), eta, late if late and late > 0 else 0])
    cb = {}
    for d in D.disputes:
        if d.type == "chargeback" and _in(d.created_at, a, b):
            r = cb.setdefault(d.get_subtype_display() or "Not given", [0, 0, 0])
            r[0] += 1
            r[1] += d.amount_h
            r[2] += d.recovered_h or 0
    kpis = [_kpi("ASNs sent", len(ss), sum(1 for s in D.ships if _in(s.submitted_at, pa, pb)), unit=""),
            _kpi("ASN on time", D.asn_on_time(a, b), D.asn_on_time(pa, pb)),
            _kpi("Appointments missed / refused", sum(1 for e in ev if e["action"] in ("missed", "refused")),
                 sum(1 for e in D.events if e["action"] in ("missed", "refused") and _in(e["at"], pa, pb)), unit="", good="down",
                 note="Each can bring a chargeback"),
            _kpi("Units on backorder", sum(r[2] for r in bo), None, unit="", good="down", note="Open now")]
    return dict(kpis=kpis, trends=[_trend("ASN on time, %", "%", weeks, D.asn_on_time)], tables=[
        _table("By fulfilment centre", [("FC", "text"), ("ASNs", "num"), ("On time %", "pct"), ("Days ASN → delivered (median)", "num"),
                                        ("Missed", "num"), ("Refused", "num"), ("Rescheduled", "num")], rows),
        _table("Open backorders", [("PO", "text"), ("FC", "text"), ("Units to ship", "num"), ("Expected", "date"), ("Days late", "num")], bo),
        _table("Chargebacks by type", [("Type", "text"), ("Disputes", "num"), ("SAR", "sar"), ("Recovered SAR", "sar")],
               [[k, *v] for k, v in sorted(cb.items(), key=lambda kv: -kv[1][1])], note="Disputes of type Chargeback opened in the period."),
    ])


def cash(D, a, b, pa, pb, weeks):
    from payments.models import DISPUTE_TYPES
    from payments.views import ageing
    names = dict(DISPUTE_TYPES)
    ded = {}
    for p in D.pays:
        if p.deduction_h and _in(p.remit_date, a, b):
            t = _deduction_type(p, D.dtype)
            r = ded.setdefault(t, [0, 0, 0, 0, 0])
            r[0] += 1
            r[1] += p.deduction_h
            r[2] += p.deduction_h if p.status == "accepted" else 0
            r[3] += p.deduction_h if p.status in ("disputed", "recovered") else 0
            r[4] += p.deduction_h if p.status == "short" else 0
    paid = sum(p.paid_h + p.deduction_h for p in D.pays if _in(p.remit_date, a, b))
    ded_rows = [[names.get(t, t), n, s, _pct(s, paid), acc, dis, open_] for t, (n, s, acc, dis, open_) in sorted(ded.items(), key=lambda kv: -kv[1][1])]
    dsp = {}
    for d in D.disputes:
        if _in(d.created_at, a, b) or (d.status in ("won", "lost") and _in(d.closed_at, a, b)):
            r = dsp.setdefault(d.type, dict(n=0, amt=0, won=0, lost=0, rec=0, days=[]))
            r["n"] += 1
            r["amt"] += d.amount_h
            if d.status == "won":
                r["won"] += 1
                r["rec"] += d.recovered_h or 0
            if d.status == "lost":
                r["lost"] += 1
            if d.status in ("won", "lost"):
                r["days"].append((d.closed_at - d.created_at).total_seconds() / 86400) if d.closed_at else None
    dsp_rows = [[names.get(t, t), r["n"], r["amt"], r["won"], r["lost"], _pct(r["won"], r["won"] + r["lost"]), r["rec"],
                 round(median(r["days"]), 1) if r["days"] else None] for t, r in sorted(dsp.items(), key=lambda kv: -kv[1]["amt"])]
    aged, buckets, terms = ageing(D.now)
    i2c = {}
    for p in D.pays:
        if p.status == "matched" and p.invoice_id and _in(p.remit_date, a, b):
            inv = next((i for i in D.invs if i.pk == p.invoice_id), None)
            if inv:
                i2c.setdefault(p.vendor_code or "—", []).append((p.remit_date - inv.invoice_date).days)
    kpis = [_kpi("Received from Amazon", _r(D.received(a, b)), _r(D.received(pa, pb)), unit="SAR"),
            _kpi("Deduction rate", D.deduction_rate(a, b), D.deduction_rate(pa, pb), good="down"),
            _kpi("Recovered in disputes", D.dispute_recovery(a, b), D.dispute_recovery(pa, pb)),
            _kpi("Owed past terms", round(sum(r["owed"] for r in aged if r["overdue"]) / 100), None, unit="SAR", good="down",
                 note=f"{terms}-day terms, open now")]
    return dict(kpis=kpis, trends=[_trend("Deduction rate, %", "%", weeks, D.deduction_rate, good="down"),
                                   _trend("Received from Amazon, SAR", "SAR", weeks, D.received)], tables=[
        _table("Deductions by type", [("Type", "text"), ("Payments", "num"), ("Deducted SAR", "sar"), ("% of invoiced", "pct"),
                                      ("Accepted SAR", "sar"), ("Disputed SAR", "sar"), ("Still open SAR", "sar")], ded_rows,
               note="Type from the dispute when one was opened, otherwise read from Amazon's reason."),
        _table("Disputes by type", [("Type", "text"), ("Cases", "num"), ("SAR", "sar"), ("Won", "num"), ("Lost", "num"), ("Win rate %", "pct"),
                                    ("Recovered SAR", "sar"), ("Days to close (median)", "num")], dsp_rows),
        _table("Ageing, open now", [("Bucket", "text"), ("Invoices", "num"), ("Owed SAR", "sar")], [[x["l"], x["n"], x["v"]] for x in buckets]),
        _table("Days from invoice to payment", [("Vendor code", "text"), ("Payments", "num"), ("Median days", "num"), ("Slowest", "num")],
               [[k, len(v), median(v), max(v)] for k, v in sorted(i2c.items())]),
    ])


def promos(D, a, b, pa, pb, weeks):
    from promotions.services import stage_of, support_h
    rows, by_cat = [], {}
    for p in D.promos:
        if not (_in(p.end, a, b) or any(_in(c.sent_at, a, b) for c in D.claims if c.promotion_id == p.pk)):
            continue
        agr = p.agreement_no
        billed = sum(d.approved_h for d in D.dns if agr and d.agreement_no == agr and d.validated)
        disputed = sum(d.disputed_h for d in D.dns if agr and d.agreement_no == agr)
        cs = [c for c in D.claims if c.promotion_id == p.pk]
        claimed, credited = sum(c.amount_h for c in cs), sum(c.cn_h or 0 for c in cs)
        sold = [(l.sold_units, l.sku.cost_h, l.support_h) for l in p.lines.all() if l.sold_units is not None]
        sales = sum(u * c for u, c, _ in sold)
        cost = sum(u * s for u, _, s in sold) + sum(f.amount_h for f in p.fees.all())
        rows.append([p.mecl_ref, p.name, p.category, p.get_promo_type_display(), support_h(p), billed, disputed, claimed, credited,
                     claimed - credited if cs else None, round(sales / cost, 1) if cost else None, stage_of(p)])
        r = by_cat.setdefault(p.category, [0, 0, 0, 0, 0])
        r[0] += 1
        r[1] += billed
        r[2] += claimed
        r[3] += credited
        r[4] += disputed
    rows.sort(key=lambda r: -(r[9] or 0))
    kpis = [_kpi("Billed by Amazon", round(sum(r[5] for r in rows) / 100), None, unit="SAR", note="Validated debit notes"),
            _kpi("Recovered from brands", D.brand_recovery(a, b), D.brand_recovery(pa, pb)),
            _kpi("Overcharges caught", round(sum(r[6] for r in rows) / 100), None, unit="SAR", note="Debit-note excess disputed (R10)"),
            _kpi("Still to recover", round(sum(r[9] or 0 for r in rows) / 100), None, unit="SAR", good="down", note="Claimed, not yet credited")]
    return dict(kpis=kpis, trends=[_trend("Recovered from brands, %", "%", weeks, D.brand_recovery)], tables=[
        _table("Promotion results", [("MECL ref", "text"), ("Promotion", "text"), ("Cat.", "text"), ("Type", "text"), ("Committed SAR", "sar"),
                                     ("Billed SAR", "sar"), ("Disputed SAR", "sar"), ("Claimed SAR", "sar"), ("Credited SAR", "sar"),
                                     ("Gap SAR", "sar"), ("Sales per SAR support", "num"), ("Stage", "text")], rows,
               note="Promotions that ended, or were claimed, in the period. Sales = units sold × agreed cost."),
        _table("By category", [("Category", "text"), ("Promotions", "num"), ("Billed SAR", "sar"), ("Claimed SAR", "sar"), ("Credited SAR", "sar"),
                               ("Disputed SAR", "sar")], [[c, *v] for c, v in sorted(by_cat.items())]),
    ])


def speed(D, a, b, pa, pb, weeks):
    """How long each hand-off takes, in working hours / days, by step."""
    def stat(vals):
        vals = [v for v in vals if v is not None]
        return [len(vals), round(median(vals), 1) if vals else None, round(max(vals), 1) if vals else None]

    def steps(x, y):
        ps = [p for p in D.pos if _in(p.confirmed_at, x, y)]
        out = [("PO arrived → confirmed", "working hours", "PIC", [working_hours_between(p.order_date, p.confirmed_at) for p in ps]),
               ("Confirmed → booked in SAP", "working hours", "Planning", [working_hours_between(p.confirmed_at, p.booked_at) for p in ps if p.booked_at]),
               ("Booked → released", "working hours", "Credit control", [working_hours_between(p.booked_at, p.released_at) for p in ps if p.released_at]),
               ("Delivered → invoiced", "working hours", "PIC / Finance",
                [working_hours_between(_delivered(i), i.invoice_date) for i in D.invs if _in(i.invoice_date, x, y) and _delivered(i)])]
        pay = {p.payment_no: p for p in D.pays}
        out.append(("Short payment → dispute opened", "days", "Finance",
                    [(d.created_at - pay[d.ref].remit_date).total_seconds() / 86400 for d in D.disputes if d.ref in pay and _in(d.created_at, x, y)]))
        out.append(("Debit note → validated", "days", "PIC / Finance",
                    [(d.validated_at - d.dn_date).total_seconds() / 86400 for d in D.dns if _in(d.validated_at, x, y)]))
        out.append(("Claim sent → credit note", "days", "Product team",
                    [(min(n.cn_date for n in c.credit_notes.all()) - c.sent_at).total_seconds() / 86400 for c in D.claims
                     if c.credit_notes.all() and _in(c.sent_at, x, y)]))
        return out
    cur, prev = steps(a, b), steps(pa, pb)
    rows = []
    for (name, unit, who, vals), (_, _, _, pv) in zip(cur, prev):
        n, med, mx = stat(vals)
        rows.append([name, who, unit, n, med, stat(pv)[1], mx])
    kpis = [_kpi("PO arrived → confirmed", rows[0][4], rows[0][5], unit=" h", good="down", note="Median, working hours"),
            _kpi("Delivered → invoiced", rows[3][4], rows[3][5], unit=" h", good="down", note="Median, working hours"),
            _kpi("Short payment → dispute", rows[4][4], rows[4][5], unit=" d", good="down", note="Median, days"),
            _kpi("Claim → credit note", rows[6][4], rows[6][5], unit=" d", good="down", note="Median, days")]
    return dict(kpis=kpis, trends=[], tables=[
        _table("Time per step", [("Step", "text"), ("Who", "text"), ("Unit", "text"), ("Cases", "num"), ("Median", "num"),
                                 ("Previous period", "num"), ("Slowest", "num")], rows,
               note="Working hours count Sunday–Thursday 08:00–17:00 and skip public holidays."),
    ])


SECTIONS = {"summary": summary, "orders": orders, "shipping": shipping, "cash": cash, "promos": promos, "speed": speed}


def report(tab="summary", days=30, now=None):
    now = now or timezone.now()
    a = now - timedelta(days=days)
    pa = a - timedelta(days=days)
    D = Data(now)
    out = SECTIONS[tab](D, a, now, pa, a, _weeks(now))
    out.update(period=(a, now), prev=(pa, a))
    return out


def export_sheets(days=30, now=None):
    """Every tab's tables for the Excel export, one sheet each."""
    from .exports import sar
    sheets = []
    for tab, label in TABS:
        r = report(tab, days, now)
        if r["kpis"]:
            sheets.append((f"{label} KPIs"[:31], ["Measure", "This period", "Previous period", "Unit"],
                           [[k["label"], k["value"], k["prev"], k["unit"].strip()] for k in r["kpis"]]))
        for t in r["tables"]:
            conv = lambda v, kind: sar(v) if kind == "sar" and v is not None else v
            sheets.append((f"{label[:12]} - {t['key']}"[:31], [c[0] for c in t["cols"]],
                           [[conv(v, c[1]) for v, c in zip(row, t["cols"])] for row in t["rows"]]))
    return sheets
