"""Upload types U1-U9 (Prototype Spec section 8): columns, validation, import and sample files."""
import random
import re
from collections import OrderedDict
from datetime import datetime, time, timedelta

from django.conf import settings
from django.utils import timezone

from catalog.models import CATEGORY_NAMES, FulfilmentCentre, Price, Sku, resolve_sku
from core.services import fmt_sar, next_number, notify, peek_number, to_h

def is_fee(code):
    """A debit-note row for a fixed fee (deal fee, co-op) rather than a model: the SKU column says FEE / … fee."""
    k = "".join(ch for ch in str(code or "").upper() if ch.isalnum())
    return k in ("FEE", "FIXEDFEE", "DEALFEE", "COOP", "COOPFEE", "MARKETINGFEE") or (k.endswith("FEE") and not k[:-3].isdigit())


def lifecycle_of(v):
    k = "".join(ch for ch in str(v or "").lower() if ch.isalnum())
    return {"new": "new", "launch": "new", "active": "active", "running": "active", "regular": "active", "phaseout": "phase_out",
            "phasingout": "phase_out", "clearance": "phase_out", "eol": "eol", "discontinued": "eol"}.get(k, "active")


NUM_FIELDS = {"case_pack", "agreed_cost_sar", "free_stock", "qty_ordered", "unit_cost_sar", "qty", "cartons", "amount_paid_sar",
              "deduction_sar", "support_per_unit_sar", "expected_units", "units", "rate_sar", "amount_sar",
              "units_sold", "stock_on_hand", "sellout_forecast", "sellin_forecast", "rrp_sar"}
DATE_FIELDS = {"valid_from", "valid_to", "order_date", "ship_window_start", "ship_window_end", "ship_date",
               "remit_date", "start_date", "end_date", "dn_date", "cn_date", "request_date", "date", "month"}

# (field, required, synonyms)
TYPES = OrderedDict([
    ("U1", dict(name="SKU master & ASIN map", src="SAP + Vendor Central catalog", go="/settings/?tab=skus", cols=[
        ("sku_code", 1, ["sku", "material", "materialno", "itemcode", "mesku"]), ("model_no", 1, ["model", "modelnumber"]),
        ("asin", 1, ["amazonasin"]), ("category", 1, ["cat", "productcategory"]), ("ean", 0, ["barcode", "gtin", "upc"]),
        ("description", 0, ["desc", "name", "productname", "title"]),
        ("case_pack", 0, ["casepack", "caseqty", "packsize", "unitspercase", "innerpack"]),
        ("rrp_sar", 0, ["rrp", "retailprice", "msrp"]), ("status", 0, ["lifecycle", "modelstatus"])])),
    ("U2", dict(name="Agreed price list", src="Commercial team", go="/settings/?tab=prices", cols=[
        ("sku_code", 1, ["sku", "material", "asin", "model"]), ("agreed_cost_sar", 1, ["cost", "agreedcost", "netcost", "price", "costsar"]),
        ("valid_from", 1, ["from", "startdate", "validfrom"]), ("valid_to", 0, ["to", "enddate", "validto"])])),
    ("U3", dict(name="Stock snapshot", src="SAP", go="/pos/?tab=new", cols=[
        ("sku_code", 1, ["sku", "material"]), ("free_stock", 1, ["stock", "qty", "available", "freestock", "unrestricted"])])),
    ("U4", dict(name="Amazon POs", src="Vendor Central export", go="/pos/?tab=new", cols=[
        ("po_no", 1, ["po", "ponumber", "purchaseorder", "order"]), ("fc_code", 1, ["fc", "shipto", "shiptolocation", "warehouse", "fulfillmentcenter"]),
        ("order_date", 1, ["orderdate", "ordered", "podate", "orderedon"]), ("ship_window_end", 1, ["windowend", "shipwindowend", "latestship"]),
        ("ship_window_start", 0, ["windowstart", "shipwindowstart"]), ("asin", 1, []), ("model_no", 0, ["model", "modelnumber", "externalid"]),
        ("qty_ordered", 1, ["qty", "quantity", "quantityrequested", "orderedqty"]), ("unit_cost_sar", 1, ["unitcost", "cost", "price", "netcost"]),
        ("vendor_code", 0, ["vendor", "vendorcode"])])),
    ("U5", dict(name="SAP deliveries", src="SAP", go="/ship/?tab=asn", cols=[
        ("sap_delivery_no", 1, ["delivery", "deliveryno", "outbounddelivery"]), ("po_no", 1, ["po", "ponumber", "customerpo"]),
        ("sku_code", 1, ["sku", "material"]), ("qty", 1, ["quantity", "deliveredqty"]), ("cartons", 1, ["cases", "boxes"]),
        ("ship_date", 1, ["shipdate", "gidate", "goodsissue"]), ("sales_order", 0, ["so", "salesorder", "salesorderno"])])),
    ("U6", dict(name="Remittance / payments", src="Vendor Central payments", go="/pay/?tab=short", cols=[
        ("payment_no", 1, ["payment", "paymentnumber", "remittance", "paymentid"]), ("remit_date", 1, ["date", "paymentdate"]),
        ("invoice_no", 1, ["invoice", "invoicenumber"]), ("amount_paid_sar", 1, ["amountpaid", "paid", "amount"]),
        ("deduction_sar", 1, ["deduction", "deductions"]), ("deduction_reason", 0, ["reason"]), ("vendor_code", 0, ["vendor", "vendorcode"])])),
    ("U7", dict(name="Promotions (bulk)", src="Product team Excel", go="/promos/?tab=pre", perm="promo", cols=[
        ("promo_name", 1, ["name", "promotion"]), ("category", 1, ["cat"]), ("start_date", 1, ["start"]), ("end_date", 1, ["end"]),
        ("sku_code", 1, ["sku", "model", "asin"]), ("support_per_unit_sar", 1, ["support", "supportperunit", "fundingperunit"]),
        ("expected_units", 1, ["units", "expected"]), ("promo_type", 0, ["type", "promotiontype", "dealtype"]),
        ("vendor_code", 0, ["vendor", "vendorcode"]), ("sf_ref", 0, ["meclref", "salesforceref", "sfref", "mecl"]),
        ("brand_ref", 0, ["brandref", "activityref", "secondref"]), ("subcat", 0, ["subcategory", "subcat", "catcode"])])),
    ("U8", dict(name="Debit notes", src="Vendor Central", go="/dns/?tab=todo", cols=[
        ("dn_no", 1, ["dn", "debitnote", "debitnoteno"]), ("agreement_no", 1, ["agreement", "agreementnumber", "agreementid"]),
        ("dn_date", 1, ["date"]), ("sku_code", 1, ["sku", "asin", "model"]), ("units", 1, ["qty", "quantity"]),
        ("rate_sar", 1, ["rate", "amountperunit"]), ("amount_sar", 0, ["amount", "total"])])),
    ("U9", dict(name="Credit notes", src="Finance / SAP", go="/claims/?tab=closed", perm="cn", cols=[
        ("cn_no", 1, ["cn", "creditnote"]), ("claim_no", 1, ["claim"]), ("cn_date", 1, ["date"]), ("amount_sar", 1, ["amount", "value"])])),
    ("U10", dict(name="Returns (RTV)", src="Vendor Central returns", go="/returns/?tab=todo", cols=[
        ("rtv_no", 1, ["rtv", "return", "returnid", "authorization", "ra", "rano"]), ("request_date", 1, ["date", "requested", "returndate"]),
        ("sku_code", 1, ["sku", "asin", "model"]), ("qty", 1, ["quantity", "units"]), ("unit_cost_sar", 0, ["cost", "unitcost", "price"]),
        ("reason", 0, ["returnreason"]), ("fc_code", 0, ["fc", "warehouse", "shipfrom"]), ("vendor_code", 0, ["vendor", "vendorcode"])])),
    ("U11", dict(name="Amazon sell-out & stock", src="Vendor Central sales + inventory reports", go="/?view=sellout", cols=[
        ("asin", 1, ["sku", "model", "sku_code"]), ("date", 1, ["day", "reportdate", "weekending", "asof", "period"]),
        ("units_sold", 1, ["unitssold", "sellout", "netunits", "shippedunits", "orderedunits", "units"]),
        ("stock_on_hand", 0, ["soh", "sellableonhand", "onhand", "stock", "sellableonhandunits"])])),
    ("U12", dict(name="Amazon forecast", src="Vendor Central forecasting report", go="/?view=sellout", cols=[
        ("asin", 1, ["sku", "model", "sku_code"]), ("month", 1, ["date", "week", "forecastmonth", "period", "weekstart"]),
        ("sellout_forecast", 1, ["forecast", "meanforecast", "sellout", "forecastunits", "mean"]),
        ("sellin_forecast", 0, ["sellin", "sellinforecast", "orderforecast"])])),
])


def norm_h(s):
    return re.sub(r"[^a-z0-9]", "", str(s or "").lower())


def num(v):
    try:
        return float(str(v).replace(",", "").replace("SAR", "").strip())
    except ValueError:
        return None


def parse_date(v):
    """Accepts YYYY-MM-DD, DD/MM/YYYY, DD-Mon-YYYY, ISO datetimes and Excel serials. Returns an aware datetime (09:00 Riyadh)."""
    if v in (None, ""):
        return None
    tz = timezone.get_current_timezone()
    if isinstance(v, datetime):
        return v if timezone.is_aware(v) else timezone.make_aware(v, tz)
    s = str(v).strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%d-%b-%Y", "%d %b %Y", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            d = datetime.strptime(s[:19] if "T" in s or ":" in s else s, fmt)
            if d.time() == time(0, 0):
                d = d.replace(hour=9)
            return timezone.make_aware(d, tz)
        except ValueError:
            pass
    n = num(s)
    if n and 20000 < n < 80000:
        return timezone.make_aware(datetime(1899, 12, 30) + timedelta(days=n, hours=9), tz)
    return None


def cat_of(v):
    s = str(v or "").strip()
    if s in CATEGORY_NAMES:
        return s
    if s.upper() in CATEGORY_NAMES:
        return s.upper()
    for k, name in CATEGORY_NAMES.items():
        if name.lower() == s.lower():
            return k
    if re.match(r"^(bund|bd$)", s, re.I):
        return "Bundle"
    return None


# ---------- validation ----------
def _did_you_mean_sku(value):
    from matching.matchers import closest_sku
    hit = closest_sku(value)
    return f". Did you mean {hit}?" if hit else ""


def validate(tid, o, ctx, cfg):
    """Return (errors, warnings) for one row dict."""
    e, w = [], []
    for f, req, _ in TYPES[tid]["cols"]:
        v = o.get(f, "")
        if req and v == "":
            e.append(f"{f} is empty")
            continue
        if v == "":
            continue
        if f in NUM_FIELDS and num(v) is None:
            e.append(f'{f} "{v}" is not a number')
        if f in DATE_FIELDS and parse_date(v) is None:
            e.append(f'{f} "{v}" is not a date')
    if e:
        return e, w
    from rules import engine
    sk = lambda: resolve_sku(o.get("sku_code") or o.get("asin") or o.get("model_no"))
    if tid == "U1":
        if not cat_of(o["category"]):
            e.append(f'Category "{o["category"]}" is not PA, DI, TV, HAV or Bundle')
        if Sku.objects.filter(sku_code=o["sku_code"]).exists():
            w.append("Existing SKU will be updated")
    elif tid == "U2":
        s = sk()
        if not s:
            e.append(f'SKU "{o["sku_code"]}" is not in the SKU master' + _did_you_mean_sku(o["sku_code"]))
        elif num(o["agreed_cost_sar"]) <= 0:
            e.append("Cost must be above 0")
        elif s.cost_h and abs(s.cost_h - to_h(o["agreed_cost_sar"])) > 0:
            w.append(f"Cost changes from {s.cost_h / 100:,.2f} to {num(o['agreed_cost_sar']):,.2f}")
    elif tid == "U3":
        if not sk():
            e.append(f'SKU "{o["sku_code"]}" is not in the SKU master' + _did_you_mean_sku(o["sku_code"]))
        elif num(o["free_stock"]) < 0:
            e.append("Stock cannot be negative")
    elif tid == "U4":
        from orders.models import PurchaseOrder
        s = resolve_sku(o["asin"]) or resolve_sku(o.get("model_no"))
        old = PurchaseOrder.objects.filter(po_no=o["po_no"]).first()
        exists = bool(old)
        if not s:
            e.append(f"ASIN {o['asin']} is not in the SKU master" + (_did_you_mean_sku(o.get("model_no") or o["asin"]) or ". An admin adds it in Settings → SKU master (or with U1), then re-upload"))
        if exists:
            from orders.services import CHANGEABLE
            ol = old.lines.filter(sku=s).first() if s else None
            q = int(num(o["qty_ordered"]))
            if not ol or q == ol.qty_ordered:
                w.append("PO already imported. Row will be skipped")
            elif old.stage not in CHANGEABLE:
                w.append(f"Amazon changed the quantity ({ol.qty_ordered} → {q}) but the PO is past the ASN. Row skipped; raise it with Amazon")
            elif q > ol.qty_ordered:
                w.append(f"Quantity went up ({ol.qty_ordered} → {q}). Amazon sends extra units as a new PO. Row skipped")
            else:
                w.append(f"Amazon change: {ol.qty_ordered} → {q} units{' (line cancelled)' if q == 0 else ''}. Applied on import")
        # Master data (SKUs, FCs) is never created as a side effect of a transaction import: a typo in a
        # Vendor Central export must not add a fulfilment centre. An admin adds new FCs in Settings → Amazon FCs.
        if not FulfilmentCentre.objects.filter(code=o["fc_code"]).exists():
            e.append(f"FC code {o['fc_code']} is not in the FC master. An admin adds it in Settings → Amazon FCs")
        if s and not exists:
            from catalog.models import agreed_cost_h
            agreed = agreed_cost_h(s, parse_date(o["order_date"]) or timezone.now())
            ok, _ = engine.price_check(to_h(o["unit_cost_sar"]), agreed, cfg)
            if not ok:
                w.append(f"Price check R1: cost {num(o['unit_cost_sar']):,.2f} vs agreed {agreed / 100:,.2f} on the order date")
            if not engine.stock_check(int(num(o["qty_ordered"])), s.free_stock, cfg):
                w.append(f"Stock check R2: {int(num(o['qty_ordered']))} ordered, {s.free_stock} free")
    elif tid == "U5":
        from orders.models import PurchaseOrder
        po = PurchaseOrder.objects.filter(po_no=o["po_no"]).first()
        s = sk()
        if not po:
            e.append(f"PO {o['po_no']} not found")
        elif po.stage != "released":
            e.append(f'PO {o["po_no"]} is "{po.stage_label}", not released')
        elif not s or not po.lines.filter(sku=s).exists():
            e.append(f"SKU {o['sku_code']} is not on PO {o['po_no']}")
        elif _has_delivery(po):
            e.append(f"PO {o['po_no']} already has a SAP delivery")
    elif tid == "U6":
        from payments.models import Payment
        from payments.services import find_invoice
        if Payment.objects.filter(payment_no=o["payment_no"]).exists():
            e.append(f"Payment {o['payment_no']} already imported")
        elif not find_invoice(o["invoice_no"]):
            # A warning, not an error: Amazon often reformats invoice numbers, and an unmatched payment is a
            # designed state ("To match"). Unknown master data (SKU, FC) and unknown claims (U9) are errors.
            w.append(f"Invoice {o['invoice_no']} not found. Payment will wait in \"To match\"")
        elif (num(o["deduction_sar"]) or 0) > 0:
            w.append(f"Short by {num(o['deduction_sar']):,.2f}")
    elif tid == "U7":
        c = cat_of(o["category"])
        if not c:
            e.append(f'Category "{o["category"]}" is not valid')
        s = sk()
        if not s:
            e.append(f'SKU "{o["sku_code"]}" not found')
        elif c and s.category != c:
            w.append(f"{s.model_no} is in {s.category}, not {c}")
        if parse_date(o["end_date"]) < parse_date(o["start_date"]):
            e.append("End date is before start date")
    elif tid == "U8":
        from debitnotes.models import DebitNote
        from promotions.models import Promotion
        if DebitNote.objects.filter(dn_no=o["dn_no"]).exists():
            e.append(f"DN {o['dn_no']} already imported")
        if is_fee(o["sku_code"]):
            pass                                    # a fixed-fee line: no model
        elif not sk():
            e.append(f"SKU/ASIN {o['sku_code']} not found" + _did_you_mean_sku(o["sku_code"]))
        if not Promotion.objects.filter(agreement_no=o["agreement_no"]).exists():
            w.append(f"Agreement {o['agreement_no']} not in tracker. DN will be unlinked")
    elif tid == "U10":
        from returns.models import ReturnAuth
        if ReturnAuth.objects.filter(rtv_no=o["rtv_no"]).exists():
            e.append(f"Return {o['rtv_no']} already imported")
        if not sk():
            e.append(f"SKU/ASIN {o['sku_code']} not found" + _did_you_mean_sku(o["sku_code"]))
        elif num(o["qty"]) is not None and num(o["qty"]) <= 0:
            e.append("Quantity must be above 0")
    elif tid in ("U11", "U12"):
        if not sk():
            e.append(f"ASIN / model {o['asin']} not found" + _did_you_mean_sku(o["asin"]))
    elif tid == "U9":
        from claims.models import Claim
        c = Claim.objects.filter(claim_no=o["claim_no"]).first()
        if not c:
            from matching.matchers import closest_claim
            hit = closest_claim(o["claim_no"])
            e.append(f"Claim {o['claim_no']} not found" + (f". Did you mean {hit}?" if hit else ""))
        elif c.status != "sent":
            e.append(f"Claim {o['claim_no']} already has a credit note")
        elif abs(c.amount_h - to_h(o["amount_sar"])) > cfg.tol_h():
            w.append(f"CN is {(c.amount_h - to_h(o['amount_sar'])) / 100:,.2f} short of the claim")
    return e, w


def _suggest_payments(ps):
    """Match suggestions for imported payments that need a person (rules now; Claude in the background if on)."""
    from matching import ai
    from matching import services as ms
    from matching.tasks import ai_review
    n = 0
    for p in ps:
        if p.status == "unmatched":
            n += bool(ms.refresh("pay_inv", p))
        else:
            ms.refresh_deduction(p)
            ms.refresh("pay_dn", p)
        if ai.available():
            ai_review.defer(kind="pay_inv" if p.status == "unmatched" else "deduction", source=p.payment_no)
    return [f"{n} of the payments to match have a suggested invoice"] if n else []


def _suggest_dns(dns):
    from matching import ai
    from matching import services as ms
    from matching.tasks import ai_review
    n = 0
    for dn in dns:
        n += bool(ms.refresh("dn_promo", dn))
        if ai.available():
            ai_review.defer(kind="dn_promo", source=dn.dn_no)
    return [f"{n} of the unlinked debit notes have a suggested promotion"] if n else []


def _has_delivery(po):
    from fulfilment.services import delivery_of
    return delivery_of(po) is not None


# ---------- import ----------
def group(rows, key):
    g = OrderedDict()
    for o in rows:
        g.setdefault(o[key], []).append(o)
    return g


def apply(tid, rows, user):
    """Write valid rows. Returns (created, updated, summary lines)."""
    from core.tasks import run_dn_checks, run_po_checks
    from orders.services import refresh_open_pos
    created = updated = 0
    lines = []
    now = timezone.now()
    if tid == "U1":
        for o in rows:
            s = Sku.objects.filter(sku_code=o["sku_code"]).first()
            vals = dict(model_no=o["model_no"], asin=o["asin"].upper(), category=cat_of(o["category"]))
            if o.get("ean"):
                vals["ean"] = o["ean"]
            if o.get("description"):
                vals["description"] = o["description"]
            if o.get("case_pack") and num(o["case_pack"]) and num(o["case_pack"]) >= 1:
                vals["case_pack"] = int(num(o["case_pack"]))
            if o.get("rrp_sar") and num(o["rrp_sar"]):
                vals["rrp_h"] = to_h(o["rrp_sar"])
            if o.get("status"):
                vals["lifecycle"] = lifecycle_of(o["status"])
            if s:
                for k, v in vals.items():
                    setattr(s, k, v)
                s.save()
                updated += 1
            else:
                Sku.objects.create(sku_code=o["sku_code"], **vals)
                created += 1
        lines.append(f"{created} SKUs added, {updated} updated")
    elif tid == "U2":
        for o in rows:
            s = resolve_sku(o["sku_code"])
            vf = parse_date(o["valid_from"]).date()
            Price.objects.filter(sku=s, valid_to__isnull=True).update(valid_to=vf - timedelta(days=1))
            Price.objects.create(sku=s, cost_h=to_h(o["agreed_cost_sar"]), valid_from=vf,
                                 valid_to=parse_date(o["valid_to"]).date() if o.get("valid_to") else None)
            s.cost_h = to_h(o["agreed_cost_sar"])
            s.save(update_fields=["cost_h", "updated_at"])
            updated += 1
        refresh_open_pos()
        lines += [f"{updated} prices updated", "Price check R1 re-ran on every PO waiting for confirmation"]
    elif tid == "U3":
        for o in rows:
            s = resolve_sku(o["sku_code"])
            s.free_stock, s.stock_as_of = int(num(o["free_stock"])), now
            s.save(update_fields=["free_stock", "stock_as_of", "updated_at"])
            updated += 1
        refresh_open_pos()
        lines += [f"{updated} stock levels updated", "Stock check R2 re-ran on every PO waiting for confirmation"]
    elif tid == "U4":
        from orders.models import PurchaseOrder
        from orders.services import create_po, notify_issues
        issues = skipped = 0
        changed = 0
        for no, ls in group(rows, "po_no").items():
            old = PurchaseOrder.objects.filter(po_no=no).first()
            if old:
                from orders.services import CHANGEABLE, amazon_change
                want = {}
                for o in ls:
                    s = resolve_sku(o["asin"]) or resolve_sku(o.get("model_no"))
                    ol = old.lines.filter(sku=s).first() if s else None
                    if ol and int(num(o["qty_ordered"])) < ol.qty_ordered:
                        want[s.sku_code] = int(num(o["qty_ordered"]))
                if want and old.stage in CHANGEABLE:
                    amazon_change(user, no, want, source="file upload")
                    changed += 1
                skipped += len(ls)
                continue
            fc = FulfilmentCentre.objects.get(code=ls[0]["fc_code"])  # validated above: unknown FCs are errors
            od = parse_date(ls[0]["order_date"]) or now
            po = create_po(no, fc, od, max(od + timedelta(days=2), now + timedelta(hours=6)),
                           [((resolve_sku(o["asin"]) or resolve_sku(o.get("model_no"))), int(num(o["qty_ordered"])), to_h(o["unit_cost_sar"])) for o in ls],
                           window_start=parse_date(ls[0].get("ship_window_start")), window_end=parse_date(ls[0]["ship_window_end"]),
                           user=user, source="file upload")
            vc = str(ls[0].get("vendor_code") or "").strip().upper()[:12]
            if vc:
                po.vendor_code = vc
                po.save(update_fields=["vendor_code"])
            created += 1
            issues += 1 if notify_issues(po) else 0
            run_po_checks.defer(po_no=no)
        if changed:
            lines.append(f"{changed} existing PO{'s' if changed > 1 else ''} updated with Amazon's changes")
        lines += [f"{created} purchase orders created, {len(rows) - skipped} lines checked",
                  f"{issues} PO{' needs' if issues == 1 else 's need'} attention in the Action Center"]
    elif tid == "U5":
        from fulfilment.services import make_delivery
        from orders.models import PurchaseOrder
        for no, ls in group(rows, "sap_delivery_no").items():
            po = PurchaseOrder.objects.get(po_no=ls[0]["po_no"])
            d = make_delivery(po, now, delivery_no=no, ship_date=parse_date(ls[0]["ship_date"]),
                              sales_order=str(ls[0].get("sales_order") or "").strip()[:20])
            d.cartons = int(sum(num(o["cartons"]) for o in ls))
            d.save()
            qty = {resolve_sku(o["sku_code"]).pk: int(num(o["qty"])) for o in ls}
            for dl in d.lines.all():
                dl.qty = qty.get(dl.sku_id, 0)
                dl.save()
            created += 1
        lines.append(f"{created} deliveries loaded. ASNs are ready to build")
    elif tid == "U6":
        from payments.models import Payment
        from payments.services import import_payment
        m = s = u = 0
        todo = []
        for o in rows:
            p = import_payment(o["payment_no"], parse_date(o["remit_date"]), o["invoice_no"], to_h(o["amount_paid_sar"]),
                               to_h(o["deduction_sar"] or 0), o.get("deduction_reason", ""))
            if o.get("vendor_code"):
                Payment.objects.filter(pk=p.pk).update(vendor_code=str(o["vendor_code"]).strip().upper()[:12])
            created += 1
            m += p.status == "matched"
            s += p.status == "short"
            u += p.status == "unmatched"
            if p.status in ("unmatched", "short"):
                todo.append(p)
        lines += [f"{created} payments imported", f"{m} matched · {s} short-paid · {u} to match"]
        lines += _suggest_payments(todo)
    elif tid == "U7":
        from promotions.services import create_promotion

        def type_of(v):
            from promotions.models import PROMO_TYPES
            k = "".join(ch for ch in (v or "").lower() if ch.isalnum())
            for code, label in PROMO_TYPES:
                if k and (k == code.replace("_", "") or k in "".join(ch for ch in label.lower() if ch.isalnum())):
                    return code
            return "price_discount"
        for name, ls in group(rows, "promo_name").items():
            st, en = parse_date(ls[0]["start_date"]), parse_date(ls[0]["end_date"])
            en = en.replace(hour=23, minute=59)
            create_promotion(user, name, cat_of(ls[0]["category"]), st, en, "Product team",
                             [(resolve_sku(o["sku_code"]), to_h(o["support_per_unit_sar"]), int(num(o["expected_units"]))) for o in ls],
                             source="bulk upload", promo_type=type_of(ls[0].get("promo_type")),
                             vendor_code=str(ls[0].get("vendor_code") or "").strip().upper()[:12],
                             refs={k: str(ls[0].get(k) or "").strip() for k in ("sf_ref", "brand_ref", "subcat")})
            created += 1
        lines += [f"{created} promotions created as drafts", "Submit them to Amazon from the Promotions page"]
    elif tid == "U8":
        from debitnotes.services import create_dn, notify_status
        mm = ul = 0
        unlinked = []
        for no, ls in group(rows, "dn_no").items():
            dn = create_dn(no, ls[0]["agreement_no"], parse_date(ls[0]["dn_date"]),
                           [(None, 1, to_h(o.get("amount_sar") or o["rate_sar"]), str(o["sku_code"]).strip()) if is_fee(o["sku_code"])
                            else (resolve_sku(o["sku_code"]), int(num(o["units"])), to_h(o["rate_sar"])) for o in ls], user=user)
            created += 1
            ev = notify_status(dn)
            mm += ev["status"] == "mismatch"
            ul += ev["status"] == "unlinked"
            run_dn_checks.defer(dn_no=no)
            if ev["status"] == "unlinked":
                unlinked.append(dn)
        lines += [f"{created} debit notes imported and checked (R10)", f"{mm} mismatch · {ul} unlinked · {created - mm - ul} match"]
        lines += _suggest_dns(unlinked)
    elif tid == "U9":
        from claims.services import get_claim, _record_cn
        sh = 0
        for o in rows:
            c = get_claim(o["claim_no"])
            _record_cn(c, o["cn_no"], to_h(o["amount_sar"]), parse_date(o["cn_date"]) or now, now, user)
            created += 1
            if c.status == "shortfall":
                sh += 1
                notify(f"Credit note short on {c.claim_no}: {fmt_sar(c.gap_h)}", "bad", ("promo", c.promotion.mecl_ref, "claim"))
        lines += [f"{created} credit notes recorded (R11)", f"{created - sh} claims closed · {sh} shortfall"]
    elif tid == "U11":
        from catalog.models import AmazonStock, SellOut
        stock = 0
        for o in rows:
            s, day = resolve_sku(o["asin"]), parse_date(o["date"]).date()
            _, new = SellOut.objects.update_or_create(sku=s, day=day, defaults={"units": int(num(o["units_sold"]))})
            created, updated = created + new, updated + (not new)
            if o.get("stock_on_hand") not in (None, ""):
                AmazonStock.objects.update_or_create(sku=s, as_of=day, defaults={"units": int(num(o["stock_on_hand"]))})
                stock += 1
        lines += [f"{len(rows)} sell-out rows loaded ({created} new, {updated} updated)", f"{stock} stock-on-hand figures"]
    elif tid == "U12":
        from catalog.models import Forecast
        acc = {}
        for o in rows:                                 # weekly rows add up into their month
            s, d = resolve_sku(o["asin"]), parse_date(o["month"]).date()
            k = (s.pk, d.replace(day=1))
            so, si = acc.get(k, (0, None))
            sin = o.get("sellin_forecast")
            acc[k] = (so + int(num(o["sellout_forecast"])), (si or 0) + int(num(sin)) if sin not in (None, "") else si)
        for (sku_id, month), (so, si) in acc.items():
            _, new = Forecast.objects.update_or_create(sku_id=sku_id, month=month, defaults={"sellout_units": so, "sellin_units": si})
            created, updated = created + new, updated + (not new)
        lines += [f"Forecast for {len(acc)} model-months loaded", "See it in Dashboard → Sell-out tracker"]
    elif tid == "U10":
        from returns.models import RTV_REASONS
        from returns.services import create_rtv
        reasons = {k: k for k, _ in RTV_REASONS} | {"defect": "defective", "damaged": "defective", "customer": "defective",
                                                    "overstock": "overstock", "excess": "overstock", "recall": "recall", "wrong": "wrong_item"}
        for no, ls in group(rows, "rtv_no").items():
            txt = str(ls[0].get("reason") or "").lower()
            reason = next((v for k, v in reasons.items() if k in txt), "other" if txt else "defective")
            create_rtv(user, no, [(resolve_sku(o["sku_code"]), int(num(o["qty"])), to_h(o["unit_cost_sar"]) if o.get("unit_cost_sar") else None) for o in ls],
                       reason=reason, fc_code=str(ls[0].get("fc_code") or "").strip(), requested_at=parse_date(ls[0]["request_date"]),
                       vendor_code=str(ls[0].get("vendor_code") or ""), source="file upload")
            created += 1
        lines += [f"{created} return request{'s' if created != 1 else ''} imported", "Authorise or refuse them on the Returns page"]
    return created, updated, lines


# ---------- sample files built from current data, so each one tells a story ----------
def _code(rnd, n):
    alnum = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(rnd.choice(alnum) for _ in range(n))


def _flagged_skus():
    """SKUs on a failing line of a PO awaiting confirmation. Those lines are the live R1/R2 examples (and real work
    for the PIC), so a generated sample file must never touch their price or stock."""
    from orders.models import PoLine
    from orders.services import line_checks
    return {l.sku_id for l in PoLine.objects.filter(po__stage="new").select_related("sku") if line_checks(l)["tone"] != "ok"}


def sample_rows(tid, dry=False):
    from claims.models import Claim
    from orders.models import PurchaseOrder
    from payments.models import Payment
    from promotions.models import Promotion
    from promotions.services import stage_of
    rnd = random.Random()
    H = [c[0] for c in TYPES[tid]["cols"]]
    rows = [H]
    today = timezone.localdate().isoformat()
    in_days = lambda n: (timezone.localdate() + timedelta(days=n)).isoformat()
    if tid == "U1":
        for s in Sku.objects.all()[:3]:
            rows.append([s.sku_code, s.model_no, s.asin, s.category, s.ean, s.description + " (updated)"])
        n = Sku.objects.count()
        rows.append([f"ME{10001 + n}", f"PA-WH{rnd.randint(1100, 1300)}B", "B0" + _code(rnd, 8), "PA", "628" + str(rnd.randint(10**9, 10**10 - 1)), "Wireless headphones"])
        rows.append([f"ME{10002 + n}", f"DI-LN{rnd.randint(800, 990)}", "B0" + _code(rnd, 8), "DI", "628" + str(rnd.randint(10**9, 10**10 - 1)), "Camera lens"])
    elif tid == "U2":
        for s in Sku.objects.exclude(pk__in=_flagged_skus())[10:15]:
            rows.append([s.sku_code, f"{s.cost_h / 100:.2f}", today, ""])
    elif tid == "U3":
        for s in Sku.objects.exclude(pk__in=_flagged_skus())[20:24]:
            rows.append([s.sku_code, s.free_stock + 10])
    elif tid == "U4":
        pool = list(Sku.objects.filter(category__in=["PA", "DI", "HAV"]).exclude(pk__in=_flagged_skus()))
        fcs = list(FulfilmentCentre.objects.values_list("code", flat=True)) or ["RUH-FC1"]
        we, ws = in_days(10), in_days(3)
        pos = []
        for n in (4, 4, 3):
            pos.append((_code(rnd, 8), rnd.choice(fcs), rnd.sample(pool, n)))
        for k, (no, fc, skus) in enumerate(pos):
            for i, s in enumerate(skus):
                cost = s.cost_h
                q = {"PA": rnd.randint(20, 120), "DI": rnd.randint(6, 40), "HAV": rnd.randint(8, 40)}.get(s.category, 10)
                if k == 0 and i == 1:
                    cost = round(cost * 0.94)
                if k == 1 and i == 2:
                    q = s.free_stock + 15
                elif s.free_stock < q and not dry:
                    s.free_stock = q + 30
                    s.save(update_fields=["free_stock"])
                rows.append([no, fc, today, we, ws, s.asin, s.model_no, q, f"{cost / 100:.2f}"])
        rows.append([pos[2][0], pos[2][1], today, we, ws, "B0" + _code(rnd, 8), "", 12, "899.00"])
        rows.append([pos[1][0], pos[1][1], today, we, ws, "B0" + _code(rnd, 8), "", 6, "1450.00"])
    elif tid == "U5":
        from fulfilment.services import delivery_of
        ps = [p for p in PurchaseOrder.objects.filter(stage="released") if not delivery_of(p)] or list(PurchaseOrder.objects.filter(stage="booked")[:1])
        for p in ps:
            no = str(peek_number("sap_delivery", 8000331100)) if dry else str(next_number("sap_delivery", 8000331100))
            for l in p.lines.select_related("sku").filter(qty_confirmed__gt=0):
                rows.append([no, p.po_no, l.sku.sku_code, l.qty_confirmed, -(-l.qty_confirmed // 8), in_days(2)])
    elif tid == "U6":
        paid = set(Payment.objects.values_list("invoice_id", flat=True))
        from billing.models import Invoice
        invs = [i for i in Invoice.objects.exclude(po__stage__in=["paid", "rejected", "cancelled"]).order_by("invoice_date") if i.pk not in paid][:3]
        base = peek_number("payment", 9102200)
        for i, inv in enumerate(invs):
            ded = round(inv.total_h * 0.03) if i == 2 else 0
            rows.append([f"RMT-{base + 10 + i}", today, inv.invoice_no, f"{(inv.total_h - ded) / 100:.2f}", f"{ded / 100:.2f}",
                         "Shortage — carton damaged" if ded else ""])
        rows.append([f"RMT-{base + 20}", today, f"INV-UNKNOWN-{rnd.randint(1000, 9999)}", "4210.50", "0", ""])
    elif tid == "U7":
        s1, e1 = in_days(25), in_days(32)
        for s in Sku.objects.filter(category="PA")[5:7]:
            rows.append(["Mega deals week · Earbuds", "PA", s1, e1, s.sku_code, f"{max(10, round(s.cost_h * 0.08 / 500) * 5)}", 120])
        for s in Sku.objects.filter(category="DI")[3:5]:
            rows.append(["Mega deals week · Cameras", "DI", s1, e1, s.sku_code, f"{max(20, round(s.cost_h * 0.06 / 500) * 5)}", 40])
    elif tid == "U8":
        from debitnotes.models import DebitNote
        have = set(DebitNote.objects.values_list("agreement_no", flat=True))
        waiting = [p for p in Promotion.objects.filter(stage="approved").exclude(agreement_no__in=have) if stage_of(p) == "waiting_dn"][:2]
        for k, p in enumerate(waiting):
            no = "VCDN-" + str(rnd.randint(10**6, 10**7 - 1))
            for i, l in enumerate(p.lines.select_related("sku")):
                u = (l.sold_units if l.sold_units is not None else l.expected_units) + (8 if k == 1 and i == 0 else 0)
                if l.sold_units is None and not dry:
                    l.sold_units = l.expected_units
                    l.save(update_fields=["sold_units"])
                rows.append([no, p.agreement_no, today, l.sku.sku_code, u, f"{l.support_h / 100:.2f}", f"{u * l.support_h / 100:.2f}"])
        any_sku = Sku.objects.all()[40]
        rows.append(["VCDN-" + str(rnd.randint(10**6, 10**7 - 1)), "7199" + str(rnd.randint(1000, 9999)), today, any_sku.asin, 30, "25.00", "750.00"])
    elif tid == "U9":
        base = peek_number("credit_note", 552010)
        for i, c in enumerate(Claim.objects.filter(status="sent").order_by("sent_at")[:2]):
            amt = c.amount_h * 0.9 if i == 1 else c.amount_h
            rows.append([f"CN-{base + i}", c.claim_no, today, f"{amt / 100:.2f}"])
    elif tid == "U11":
        for s in Sku.objects.filter(category="PA")[:3]:
            for k in range(3):
                rows.append([s.asin, (timezone.localdate() - timedelta(days=7 * k + 1)).isoformat(), rnd.randint(2, 30), rnd.randint(10, 80) if k == 0 else ""])
    elif tid == "U12":
        m = timezone.localdate().replace(day=1).isoformat()
        for s in Sku.objects.filter(category="PA")[:3]:
            rows.append([s.asin, m, rnd.randint(20, 90), ""])
    elif tid == "U10":
        n = rnd.randint(110000, 119999)
        for i, s in enumerate(Sku.objects.filter(category__in=["PA", "HAV"])[:3]):
            rows.append([f"RTV-{n}", today, s.sku_code, 2 + i, "", "Defective - customer return", "RUH-FC1", ""])
    name = re.sub(r"[^a-z]+", "_", TYPES[tid]["name"].lower()).strip("_")
    return f"sample_{tid}_{name}.csv", rows
