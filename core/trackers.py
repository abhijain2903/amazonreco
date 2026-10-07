"""ME's own trackers, produced from the hub's records with the same columns, in the same order, as the Excel sheets the
team keeps today: the PO tracker, the sell-out tracker and the claim tracker. The screen and the Excel export use the
same rows. Each cell is (value, kind): kind text | int | sar (halalas, no decimals) | sar2 (halalas, 2 decimals) |
date | dmon (10-Sep) | pct; extra columns of the hub's own come after ME's."""
from datetime import date, timedelta

from django.db.models import Sum
from django.utils import timezone

from .services import vat_h
from .workcal import add_working_days

MONTHS = ["Jan", "Feb", "March", "April", "May", "June", "July", "Aug", "Sept", "Oct", "Nov", "Dec"]


def ordinal(n):
    return f"{n}{'th' if 11 <= n % 100 <= 13 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


# ---------- PO tracker ----------
PO_COLS = [("PO Number", "text"), ("Article", "text"), ("ASIN", "text"), ("CAT", "text"), ("WH", "text"), ("Qty", "int"),
           ("Cost", "sar2"), (" Delivery value", "sar2"), ("Max Hand Off Date", "dmon"), ("RFPO", "text"), ("Sales Order", "text"),
           ("ASN", "text"), ("DEL ID", "text"), ("Del Date", "dmon"), ("INVOICE NO", "text"), ("Inv submission", "dmon"),
           ("Stage", "text"), ("Vendor code", "text")]
PO_ME = 16          # ME's own columns; the rest are the hub's


def po_tracker(status="open", vc="", fc="", cat="", q=""):
    """One row per PO line and booking portion (shipment), plus a row for what is not yet shipped."""
    from orders.models import PurchaseOrder
    now = timezone.now()
    qs = PurchaseOrder.objects.select_related("fc").prefetch_related("lines__sku", "shipments__lines", "shipments__invoices",
                                                                      "sap_deliveries")
    if status == "open":
        qs = qs.exclude(stage__in=["new", "paid", "rejected", "cancelled"])
    elif status == "invoiced":
        qs = qs.filter(stage__in=["invoiced", "backorder", "paid"])
    else:
        qs = qs.exclude(stage__in=["rejected"])
    if vc:
        qs = qs.filter(vendor_code=vc)
    if fc:
        qs = qs.filter(fc__code=fc)
    if q:
        qs = qs.filter(po_no__icontains=q.strip())
    rows = []
    for po in qs.order_by("-order_date"):
        ships = sorted(po.shipments.all(), key=lambda s: s.seq)
        pending = next((d for d in po.sap_deliveries.all() if not any(s.seq == d.seq for s in ships)), None)
        for l in po.lines.all():
            if cat and l.sku.category != cat:
                continue
            want = l.qty_ordered if po.stage == "new" else l.committed
            done = 0
            for sh in ships:
                sl = next((x for x in sh.lines.all() if x.sku_id == l.sku_id), None)
                if not sl or not sl.qty:
                    continue
                done += sl.qty
                inv = next(iter(sh.invoices.all()), None)
                delivered = sh.delivered_at
                flag = ""
                if not delivered and po.window_end and po.window_end < now:
                    flag = "bad"
                elif delivered and not inv and add_working_days(delivered, 2) < now:
                    flag = "warn"
                rows.append(dict(po=po.po_no, flag=flag, cells=[
                    po.po_no, l.sku.model_no, l.asin, l.sku.category, po.fc.code, sl.qty, l.cost_h, sl.qty * l.cost_h,
                    _local(po.window_end), po.rfpo, sh.sales_order or po.sap_order_no, sh.asn_no, sh.sap_delivery_no,
                    _local(delivered or sh.ship_date), inv.invoice_no if inv else "", _local(inv.invoice_date) if inv else None,
                    po.get_stage_display(), po.vendor_code]))
            rest = want - done
            if rest > 0:
                so = (pending.sales_order if pending else (po.sap_order_no if not ships else ""))
                flag = "bad" if po.window_end and po.window_end < now and po.stage not in ("new", "confirmed") else ""
                rows.append(dict(po=po.po_no, flag=flag, open=True, cells=[
                    po.po_no, l.sku.model_no, l.asin, l.sku.category, po.fc.code, rest, l.cost_h, rest * l.cost_h,
                    _local(po.window_end), po.rfpo, so, "", pending.delivery_no if pending else "", None, "", None,
                    ("Backorder" if l.qty_backorder and ships else po.get_stage_display()), po.vendor_code]))
    return PO_COLS, rows


def _local(dt):
    if dt is None:
        return None
    return timezone.localtime(dt).date() if hasattr(dt, "hour") else dt


# ---------- Sell-out tracker ----------
def sellout_tracker(year=None, cat="", status=""):
    """One row per model: Amazon stock on hand, sell-out per month (net of returns), this month's forecasts."""
    from catalog.models import AmazonStock, Forecast, SellOut, Sku
    from orders.models import PoLine
    today = timezone.localdate()
    year = year or today.year
    last = SellOut.objects.filter(day__year=year).order_by("-day").values_list("day", flat=True).first()
    cur_month = (last or today).month if year != today.year else today.month
    as_of = AmazonStock.objects.order_by("-as_of").values_list("as_of", flat=True).first()
    months = list(range(1, cur_month + 1))
    cols = [("Model", "text"), ("ASIN", "text"), ("Cat", "text"), ("Status", "text"), (" RRP", "sar"), (" PO Cost", "sar2"),
            (f"SOH {ordinal(as_of.day)} {MONTHS[as_of.month - 1]}" if as_of else "SOH", "int")]
    for m in months:
        end = (date(year, m + 1, 1) - timedelta(days=1)) if m < 12 else date(year, 12, 31)
        if m == cur_month:
            d = min(last or today, end) if last and last.month == m else today
            cols.append((f"MTD {ordinal(d.day)} {MONTHS[m - 1]}", "int"))
        elif m == cur_month - 1:
            cols.append((f"MTD {ordinal(end.day)} {MONTHS[m - 1]}", "int"))
        else:
            cols.append((f"MTD {MONTHS[m - 1]}{',' if m == 1 else ''} {year}", "int"))
    mname = MONTHS[cur_month - 1]
    cols += [(f"{mname}. Sellin FCT", "int"), (f"{mname}. Sellout FCT", "int"), (f"{mname}. Sell-in so far", "int"), ("Weeks of cover", "num")]
    skus = Sku.objects.filter(active=True)
    if cat:
        skus = skus.filter(category=cat)
    if status:
        skus = skus.filter(lifecycle=status)
    skus = list(skus.order_by("category", "model_no"))
    by = {}
    for sku_id, d, u in SellOut.objects.filter(day__year=year, sku__in=skus).values_list("sku_id", "day", "units"):
        by.setdefault(sku_id, {}).setdefault(d.month, 0)
        by[sku_id][d.month] += u
    soh = dict(AmazonStock.objects.filter(as_of=as_of).values_list("sku_id", "units")) if as_of else {}
    month1 = date(year, cur_month, 1)
    fct = {f.sku_id: f for f in Forecast.objects.filter(month=month1)}
    recent = dict(SellOut.objects.filter(day__gt=(last or today) - timedelta(days=28), day__lte=last or today)
                  .values_list("sku_id").annotate(n=Sum("units")))
    sellin = dict(PoLine.objects.filter(po__order_date__year=year, po__order_date__month=cur_month)
                  .exclude(po__stage__in=["rejected", "cancelled"]).values_list("sku_id").annotate(n=Sum("qty_ordered")))
    lifecycle = dict(Sku._meta.get_field("lifecycle").choices)
    rows = []
    for s in skus:
        sold = by.get(s.pk, {})
        if not sold and s.pk not in soh and s.pk not in fct:
            continue
        f = fct.get(s.pk)
        stock = soh.get(s.pk)
        weekly = recent.get(s.pk, 0) / 4
        est = False
        if f and f.sellin_units is not None:
            si = f.sellin_units
        elif f:
            si, est = max(0, f.sellout_units - (stock or 0) + round(f.sellout_units * 28 / 30)), True
        else:
            si = None
        rows.append(dict(sku=s.sku_code, est=est, flag="warn" if stock is not None and weekly and stock / weekly < 2 else "", cells=[
            s.model_no, s.asin, s.category, lifecycle.get(s.lifecycle, s.lifecycle), s.rrp_h, s.cost_h, stock,
            *[sold.get(m, 0) for m in months], si, f.sellout_units if f else None, sellin.get(s.pk, 0),
            round(stock / weekly, 1) if stock is not None and weekly else None]))
    return cols, rows, dict(year=year, as_of=as_of, last=last, month=mname)


# ---------- Claim tracker ----------
CLAIM_COLS = [("Cat.", "text"), ("VC", "text"), ("Promotion Title", "text"), ("MECL Ref. no.", "text"), ("MECL Ref. no.", "text"),
              ("AMZ Agreement ID", "text"), ("Agr. Status", "text"), ("Start Date", "dmy"), ("End Date", "dmy"), ("Date Today", "dmy"),
              ("No. of days pending Sub", "text"), ("MECL Claim submitted", "text"), ("Submitted Date", "dmy"),
              ("No. of Days for submission", "int"), ("Submitted Value w/o Vat", "sar2"), (" Submitted Value w/Vat", "sar2"),
              (" AMZ Claim value", "sar2"), ("Credited (CN)", "sar2"), ("Gap", "sar2")]
CLAIM_ME = 17


def claim_tracker(status="", vc="", cat=""):
    from claims.models import Claim
    from debitnotes.models import DebitNote
    from promotions.models import Promotion
    from promotions.services import stage_of
    today = timezone.localdate()
    ps = Promotion.objects.exclude(stage="draft").prefetch_related("claims")
    if vc:
        ps = ps.filter(vendor_code=vc)
    if cat:
        ps = ps.filter(category=cat)
    charged = {}
    for d in DebitNote.objects.prefetch_related("lines"):
        charged[d.agreement_no] = charged.get(d.agreement_no, 0) + sum(l.charged_h for l in d.lines.all())
    rows = []
    for p in ps.order_by("-end"):
        st = stage_of(p)
        cs = sorted(p.claims.all(), key=lambda c: c.sent_at)
        end = timezone.localtime(p.end).date()
        if cs:
            pending, sub = "Done", "Submitted"
        elif end < today and st not in ("submitted", "rejected"):
            pending, sub = str((today - end).days), "Pending"
        else:
            pending, sub = "", "Not due"
        if status == "pending" and sub != "Pending":
            continue
        if status == "submitted" and sub != "Submitted":
            continue
        sent = timezone.localtime(cs[0].sent_at).date() if cs else None
        net = sum(c.amount_h for c in cs)
        credited = sum(c.cn_h or 0 for c in cs)
        agr = {"submitted": "Proposed", "rejected": "Rejected"}.get(st, "Accepted")
        flag = "bad" if sub == "Pending" and (today - end).days > 30 else ""
        rows.append(dict(promo=p.mecl_ref, flag=flag, cells=[
            p.subcat or p.category, p.vendor_code, p.name, p.sf_ref or p.mecl_ref, p.brand_ref, p.agreement_no or "", agr,
            timezone.localtime(p.start).date(), end, today, pending, sub, sent, (sent - end).days if sent else None,
            net if cs else None, net + vat_h(net) if cs else None, charged.get(p.agreement_no) if p.agreement_no else None,
            credited if cs else None, (net - credited) if cs else None]))
    return CLAIM_COLS, rows


def excel(cols, rows):
    """Cells for Excel: money in SAR, dates as dates."""
    from .exports import sar
    conv = {"sar": sar, "sar2": sar}
    return [c[0] for c in cols], [[conv[k](v) if k in conv and v is not None else v for v, (_, k) in zip(r["cells"], cols)] for r in rows]
