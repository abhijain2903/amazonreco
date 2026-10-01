from django.db.models import Prefetch
from django.shortcuts import render

from billing.services import invoice_blocked
from core import htmx
from orders.models import PoLine, PurchaseOrder
from orders.services import po_units
from rules.services import get_cfg

from .services import asn_checks, delivery_of, slot_at_risk

TABS = [("asn", "ASN to create", ["released"]), ("slot", "Slot to book", ["asn"]), ("transit", "In transit", ["slot"]),
        ("invoice", "Invoice to submit", ["delivered"]), ("submitted", "Submitted", ["invoiced", "paid"])]


def ship_list(request):
    tab = htmx.pick(request, "tab", [k for k, _, _ in TABS], "asn")
    cfg = get_cfg()
    qs = PurchaseOrder.objects.select_related("fc").prefetch_related(Prefetch("lines", queryset=PoLine.objects.select_related("sku")))
    stages = dict((k, s) for k, _, s in TABS)[tab]
    rows = []
    for p in qs.filter(stage__in=stages):
        r = dict(po=p, units=po_units(p))
        if tab == "asn":
            d = delivery_of(p)
            r.update(d=d, short=bool(d) and any(not c["del_ok"] for c in asn_checks(p, cfg)))
        elif tab in ("slot", "transit"):
            r.update(sh=p.shipment, risk=slot_at_risk(p, cfg), units=sum(l.qty for l in p.shipment.lines.all()))
        elif tab == "invoice":
            r.update(b=p.sap_billing, blocked=invoice_blocked(p, cfg))
            r["net"] = sum(sl.qty * next(l.cost_h for l in p.lines.all() if l.sku_id == sl.sku_id) for sl in p.shipment.lines.all())
        else:
            r.update(inv=p.invoice)
        rows.append(r)
    if tab == "submitted":
        rows.sort(key=lambda r: r["inv"].invoice_date, reverse=True)
    from core.exports import sar, wants_export, xlsx
    if wants_export(request):
        def info(r):
            p, sh = r["po"], getattr(r["po"], "shipment", None) if r["po"].stage != "released" else None
            return [p.po_no, p.fc.code, p.stage_label, r["d"].delivery_no if r.get("d") else "", sh.asn_no if sh else "",
                    sh.slot_id if sh else "", sh.ship_date if sh else None, r["units"], sar(r.get("net")),
                    r["inv"].invoice_no if r.get("inv") else ""]
        return xlsx(f"Shipments_{tab}", ["PO", "FC", "Stage", "SAP delivery", "ASN", "Slot", "Ships", "Units", "Net SAR", "Invoice"],
                    [info(r) for r in rows])
    counts = {k: PurchaseOrder.objects.filter(stage__in=s).count() for k, _, s in TABS}
    return render(request, "pages/ship.html", dict(rows=rows, tab=tab, tabs=[dict(id=k, label=l, count=counts[k]) for k, l, _ in TABS]))
