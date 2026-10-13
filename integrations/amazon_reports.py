"""Amazon vendor reports through the Reports API, loaded into the hub every day.

Sales (sell-out net of returns, per day) -> SellOut          · replaces upload U11 (sales)
Inventory (sellable stock on hand)       -> AmazonStock      · replaces upload U11 (stock)
Forecasting (mean forecast per week)     -> Forecast (month) · replaces upload U12
Net pure product margin (per month)      -> ProductMargin    · sell-out tracker extra column
Promotion / coupon performance           -> AmazonPromoPerformance, and sold units on the promotion's lines for the
                                            debit-note check (R10) when they are not set yet

A report is asked for once a day; Amazon prepares it in the background; each sync pass picks up finished reports,
downloads the JSON document and loads it. Uploads U11 / U12 keep working as a fallback.
"""
import logging
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone as dt_tz
from decimal import Decimal, ROUND_HALF_UP

from django.db import transaction
from django.utils import timezone

from core.services import audit, notify

from .spapi import Client, SpApiError

log = logging.getLogger("hub.amazon")

SYSTEM = "Amazon (API)"
SALES, INVENTORY, FORECAST = "GET_VENDOR_SALES_REPORT", "GET_VENDOR_INVENTORY_REPORT", "GET_VENDOR_FORECASTING_REPORT"
MARGIN, PROMOS, COUPONS = "GET_VENDOR_NET_PURE_PRODUCT_MARGIN_REPORT", "GET_PROMOTION_PERFORMANCE_REPORT", "GET_COUPON_PERFORMANCE_REPORT"
LABEL = {SALES: "Sales", INVENTORY: "Inventory", FORECAST: "Forecast", MARGIN: "Product margin", PROMOS: "Promotion performance",
         COUPONS: "Coupon performance"}
RETAIL = {"distributorView": "MANUFACTURING", "sellingProgram": "RETAIL"}
LOOKBACK_DAYS = 7          # sales are restated for a few days after the fact: reload the last week every day
PROMO_DAYS = 90


def _utc(d):
    return datetime(d.year, d.month, d.day, tzinfo=dt_tz.utc)


def _day(v):
    return date.fromisoformat(str(v)[:10]) if v else None


def _h(v):
    return int((Decimal(str(v)) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP)) if v not in (None, "") else None


def _skus():
    from catalog.models import Sku
    return {a.upper(): pk for pk, a in Sku.objects.values_list("pk", "asin")}


def wanted(today=None):
    """(report type, start, end, options) to ask for today."""
    today = today or timezone.localdate()
    y = today - timedelta(days=1)
    first = today.replace(day=1)
    last_month_end = first - timedelta(days=1)
    now = timezone.now()
    out = [(SALES, _utc(today - timedelta(days=LOOKBACK_DAYS)), _utc(y), {"reportPeriod": "DAY", **RETAIL}),
           (INVENTORY, _utc(y), _utc(y), {"reportPeriod": "DAY", **RETAIL}),
           (FORECAST, None, None, {"sellingProgram": "RETAIL"}),
           (PROMOS, None, None, {"promotionStartDateFrom": _iso(now - timedelta(days=PROMO_DAYS)), "promotionStartDateTo": _iso(now)}),
           (COUPONS, None, None, {"campaignStartDateFrom": _iso(now - timedelta(days=PROMO_DAYS)), "campaignStartDateTo": _iso(now)})]
    from .models import AmazonReport
    m_start = _utc(last_month_end.replace(day=1))
    if not AmazonReport.objects.filter(report_type=MARGIN, data_start=m_start).exclude(status="failed").exists():
        out.append((MARGIN, m_start, _utc(last_month_end), {"reportPeriod": "MONTH"}))
    return out


def _iso(d):
    return d.astimezone(dt_tz.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def request_all(client=None, today=None):
    """Ask Amazon for today's reports (one of each kind). Returns the AmazonReport rows created."""
    from .models import AmazonReport
    client = client or Client()
    made = []
    for rtype, start, end, opts in wanted(today):
        r = AmazonReport(report_type=rtype, data_start=start, data_end=end, options=opts)
        try:
            r.report_id = client.create_report(rtype, start, end, opts)
        except SpApiError as e:
            r.status, r.message, r.finished_at = "failed", f"Not requested: {e}"[:300], timezone.now()
        r.save()
        made.append(r)
    return made


def process_pending(client=None):
    """Load every report Amazon has finished since the last pass. Returns the number loaded."""
    from .models import AmazonReport
    client = client or Client()
    n = 0
    for r in AmazonReport.objects.filter(status__in=["requested", "processing"]).exclude(report_id=""):
        try:
            n += process(r, client)
        except SpApiError as e:
            log.warning("report %s: %s", r.report_id, e)
    return n


def process(r, client):
    info = client.report(r.report_id)
    st = info.get("processingStatus")
    if st in ("IN_QUEUE", "IN_PROGRESS"):
        if r.status != "processing":
            r.status = "processing"
            r.save(update_fields=["status", "updated_at"])
        return 0
    r.finished_at = timezone.now()
    if st == "CANCELLED":                          # Amazon cancels a report that has no data for the period
        r.status, r.message = "no_data", "Amazon had no data for this period"
        r.save()
        return 0
    if st != "DONE" or not info.get("reportDocumentId"):
        r.status, r.message = "failed", f"Amazon could not prepare the report ({st})"
        r.save()
        notify(f"Amazon {LABEL.get(r.report_type, r.report_type).lower()} report failed ({st})", "bad", ("conn", "amazon_vc", ""), roles=["Admin"])
        return 0
    doc = client.report_json(info["reportDocumentId"])
    with transaction.atomic():
        rows, msg = LOADERS[r.report_type](doc)
        r.status, r.rows, r.message = "loaded", rows, msg[:300]
        r.save()
    return 1


# ---------- loaders: (rows loaded, message) ----------
def load_sales(doc):
    from catalog.models import SellOut
    skus, unknown, n = _skus(), set(), 0
    for row in doc.get("salesByAsin") or []:
        pk = skus.get((row.get("asin") or "").upper())
        if not pk:
            unknown.add(row.get("asin"))
            continue
        units = int(row.get("shippedUnits") or 0) - int(row.get("customerReturns") or 0)     # sell-out net of returns
        SellOut.objects.update_or_create(sku_id=pk, day=_day(row["startDate"]), defaults={"units": units})
        n += 1
    return n, f"{n} day-by-ASIN sell-out rows" + _unknown(unknown)


def load_inventory(doc):
    from catalog.models import AmazonStock
    skus, unknown, n = _skus(), set(), 0
    for row in doc.get("inventoryByAsin") or []:
        pk = skus.get((row.get("asin") or "").upper())
        if not pk:
            unknown.add(row.get("asin"))
            continue
        AmazonStock.objects.update_or_create(sku_id=pk, as_of=_day(row["endDate"]),
                                             defaults={"units": int(row.get("sellableOnHandInventoryUnits") or 0)})
        n += 1
    return n, f"Stock on hand for {n} ASINs" + _unknown(unknown)


def load_forecast(doc):
    """Weekly mean forecasts add up into the month the week starts in (as U12 does). Only the latest forecast run."""
    from catalog.models import Forecast
    rows = doc.get("forecastByAsin") or []
    latest = max((r.get("forecastGenerationDate") or "" for r in rows), default="")
    skus, unknown, acc = _skus(), set(), defaultdict(float)
    for row in rows:
        if (row.get("forecastGenerationDate") or "") != latest:
            continue
        pk = skus.get((row.get("asin") or "").upper())
        if not pk:
            unknown.add(row.get("asin"))
            continue
        acc[(pk, _day(row["startDate"]).replace(day=1))] += float(row.get("meanForecastUnits") or 0)
    for (pk, month), units in acc.items():
        Forecast.objects.update_or_create(sku_id=pk, month=month, defaults={"sellout_units": round(units)})
    return len(acc), f"Forecast for {len(acc)} model-months (generated {latest or '—'})" + _unknown(unknown)


def load_margin(doc):
    from catalog.models import ProductMargin
    skus, unknown, n = _skus(), set(), 0
    for row in doc.get("netPureProductMarginByAsin") or []:
        pk = skus.get((row.get("asin") or "").upper())
        if not pk or row.get("netPureProductMargin") is None:
            unknown.add(row.get("asin"))
            continue
        pct = (Decimal(str(row["netPureProductMargin"])) * 100).quantize(Decimal("0.01"))      # Amazon sends a fraction
        ProductMargin.objects.update_or_create(sku_id=pk, month=_day(row["startDate"]).replace(day=1), defaults={"pct": pct})
        n += 1
    return n, f"Margin for {n} ASINs" + _unknown(unknown)


def load_promotions(doc):
    from promotions.models import Promotion
    from .models import AmazonPromoPerformance
    n = linked = 0
    for p in doc.get("promotions") or []:
        agr = str(p.get("fundingAgreementId") or "")
        promo = Promotion.objects.filter(agreement_no=agr).first() if agr else None
        by = {(x.get("asin") or "").upper(): {"units": int(x.get("productUnitsSold") or 0), "spent_h": _h(x.get("productAmountSpent"))}
              for x in p.get("includedProducts") or []}
        rec, _ = AmazonPromoPerformance.objects.update_or_create(kind="promotion", amazon_id=str(p.get("promotionId")), defaults=dict(
            name=(p.get("promotionName") or "")[:200], vendor_code=(p.get("vendorCode") or "")[:12], agreement_id=agr[:30],
            start=_dt(p.get("startDateTime")), end=_dt(p.get("endDateTime")), status=(p.get("status") or "")[:30],
            units=int(p.get("unitsSold") or 0), spent_h=_h(p.get("amountSpent")), revenue_h=_h(p.get("revenue")), by_asin=by, promotion=promo))
        n += 1
        if promo:
            linked += 1
            _sold_units(promo, rec, "promotion performance report")
    return n, f"{n} promotions, {linked} linked to the hub by agreement"


def load_coupons(doc):
    from promotions.models import Promotion
    from .models import AmazonPromoPerformance
    n = linked = 0
    for camp in doc.get("campaigns") or []:
        for c in camp.get("coupons") or []:
            asins = {(a.get("asin") or "").upper() for a in c.get("asins") or []}
            start, end = _dt(c.get("startDateTime")), _dt(c.get("endDateTime"))
            promo = None
            if start and end:          # the hub's coupon promotion on the same ASINs whose dates overlap the coupon's
                promo = next((p for p in Promotion.objects.filter(promo_type="coupon", start__lte=end, end__gte=start).prefetch_related("lines__sku")
                              if asins & {l.sku.asin.upper() for l in p.lines.all()}), None)
            units = int(c.get("redemptions") or 0)
            rec, _ = AmazonPromoPerformance.objects.update_or_create(kind="coupon", amazon_id=str(c.get("couponId")), defaults=dict(
                name=(c.get("name") or camp.get("campaignName") or "")[:200], vendor_code=(camp.get("vendorCode") or "")[:12],
                start=start, end=end, status="", units=units, spent_h=_h(c.get("budgetSpent")), revenue_h=None,
                by_asin={a: {"units": units if len(asins) == 1 else None, "spent_h": None} for a in asins}, promotion=promo))
            n += 1
            if promo:
                linked += 1
                _sold_units(promo, rec, "coupon performance report")
    return n, f"{n} coupons, {linked} linked to a hub coupon promotion"


def _sold_units(promo, rec, source):
    """After the promotion ends, units sold at the promotional price set the lines' sold units (debit-note check R10),
    unless a person or an earlier source already set them."""
    if not rec.end or rec.end > timezone.now():
        return
    changed = []
    for l in promo.lines.select_related("sku").filter(sold_units__isnull=True):
        v = (rec.by_asin.get(l.sku.asin.upper()) or {}).get("units")
        if v is not None:
            l.sold_units = v
            l.save(update_fields=["sold_units", "updated_at"])
            changed.append(f"{l.sku.model_no} {v:,}")
    if changed:
        audit("promotion", promo.mecl_ref, f"Sold units from Amazon's {source}: {', '.join(changed)}", name=SYSTEM, system=True, action="sold_units")


def _dt(v):
    if not v:
        return None
    d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    return d if d.tzinfo else d.replace(tzinfo=dt_tz.utc)


def _unknown(asins):
    asins = sorted(a for a in asins if a)
    return f"; {len(asins)} ASIN{'s' if len(asins) != 1 else ''} not in the SKU master skipped ({', '.join(asins[:3])}{'…' if len(asins) > 3 else ''})" if asins else ""


LOADERS = {SALES: load_sales, INVENTORY: load_inventory, FORECAST: load_forecast, MARGIN: load_margin, PROMOS: load_promotions,
           COUPONS: load_coupons}
