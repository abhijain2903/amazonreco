"""Action Center: the list of items that need a person. It is a query over current data, not a stored copy."""
from datetime import timedelta

from django.db.models import Prefetch
from django.utils import timezone

from billing.services import invoice_blocked
from claims.models import Claim
from debitnotes.models import DebitNote
from debitnotes.services import evaluate
from fulfilment.services import asn_checks, delivery_of, shipment_of, slot_at_risk
from identity.models import ROLE_TITLES
from orders.models import PoLine, PurchaseOrder
from orders.services import po_issues, po_units, po_value_h
from payments.models import Payment
from promotions.models import Promotion
from promotions.services import stage_of, support_h
from rules import engine
from rules.services import get_cfg

RANK = {"bad": 0, "warn": 1, "info": 2, "": 3}


def _span(td):
    s = abs(td.total_seconds())
    if s < 3600:
        return f"{max(1, round(s / 60))} min"
    if s < 48 * 3600:
        return f"{round(s / 3600)} h"
    return f"{round(s / 86400)} days"


def _best_suggestions():
    """Top pending match suggestion per record, one query for the whole list."""
    from matching.models import MatchSuggestion
    best = {}
    for s in MatchSuggestion.objects.filter(status="pending", kind__in=["pay_inv", "dn_promo"]).order_by("-score"):
        best.setdefault((s.kind, s.source), s)
    return best


def _best(best, kind, source):
    s = best.get((kind, source))
    return f" · suggested: {s.label.split(' · ')[0]} ({s.score})" if s else ""


def action_items(user, mine=True):
    now = timezone.now()
    cfg = get_cfg()
    it = []
    pos = PurchaseOrder.objects.exclude(stage__in=["paid", "rejected", "invoiced"]).select_related("fc").prefetch_related(
        Prefetch("lines", queryset=PoLine.objects.select_related("sku")))
    for po in pos:
        lines = list(po.lines.all())
        v = po_value_h(po, lines)
        link = ("po", po.po_no, "")
        if po.stage == "new":
            bad = po_issues(po, cfg, lines)
            st = engine.confirm_state(po.confirm_by, now, cfg)
            it.append(dict(key="c" + po.po_no, sev="bad" if st == "overdue" else "warn" if (bad or st == "due_soon") else "info",
                           icon="po", title=f"Confirm PO {po.po_no}",
                           sub=(f"{bad} of {len(lines)} lines need a decision" if bad else f"All {len(lines)} lines passed price and stock checks") + f" · {po.fc.code}",
                           amt=v, due=po.confirm_by, roles=["PIC"], mismatch=bad > 0, open=("po", po.po_no, "lines"),
                           cta=dict(label="Review", kind="open", url=f"/records/po/{po.po_no}/?tab=lines")))
        elif po.stage == "confirmed":
            it.append(dict(key="b" + po.po_no, sev="info", icon="po", title=f"Book {po.po_no} in SAP and Salesforce",
                           sub=f"{po_units(po, lines):,} units", amt=v, due=po.confirmed_at + timedelta(hours=8),
                           roles=["PIC", "Planning"], open=link, cta=dict(label="Book", kind="post", url=f"/pos/{po.po_no}/book/", perm="book")))
        elif po.stage == "booked":
            it.append(dict(key="r" + po.po_no, sev="warn" if now - po.booked_at > timedelta(hours=4) else "info", icon="po",
                           title=f"Release {po.po_no} for shipment", sub=f"SAP order {po.sap_order_no} · waiting {_span(now - po.booked_at)}",
                           amt=v, due=po.booked_at + timedelta(hours=4), roles=["Credit"], open=link,
                           cta=dict(label="Release", kind="post", url=f"/pos/{po.po_no}/release/", perm="release")))
        elif po.stage == "released":
            d = delivery_of(po)
            short = d and any(not c["del_ok"] for c in asn_checks(po, cfg))
            it.append(dict(key="a" + po.po_no, sev="" if not d else "warn" if short else "info", icon="box",
                           title=f"Create ASN for {po.po_no}" if d else f"Waiting for SAP delivery · {po.po_no}",
                           sub=(f"SAP delivery {d.delivery_no} · ships {timezone.localtime(d.ship_date):%d %b}" + (" · delivery is short of confirmed qty" if short else ""))
                           if d else "Upload SAP deliveries or sync SAP to continue",
                           amt=v, due=d.ship_date - timedelta(days=1) if d else None, roles=["PIC", "Logistics"], mismatch=bool(short),
                           open=("po", po.po_no, "shipment"), cta=dict(label="Create ASN" if d else "Open", kind="open", url=f"/records/po/{po.po_no}/?tab=shipment")))
        elif po.stage == "asn":
            sh = shipment_of(po)
            it.append(dict(key="s" + po.po_no, sev="bad" if slot_at_risk(po, cfg) else "info", icon="truck",
                           title=f"Book delivery slot for {po.po_no}", sub=f"ASN {sh.asn_no} · ships {timezone.localtime(sh.ship_date):%d %b, %H:%M}",
                           amt=v, due=sh.ship_date - timedelta(hours=float(cfg.p('R5', 'hrs'))), roles=["Logistics"], open=("po", po.po_no, "shipment"),
                           cta=dict(label="Book slot", kind="dialog", url=f"/pos/{po.po_no}/slot/", perm="ship")))
        elif po.stage == "delivered":
            blk = invoice_blocked(po, cfg)
            it.append(dict(key="i" + po.po_no, sev="bad" if blk else "info", icon="receipt",
                           title=f"Invoice blocked for {po.po_no}" if blk else f"Submit invoice for {po.po_no}",
                           sub="SAP billing does not match ASN quantity" if blk else f"Delivered {_span(now - po.delivered_at)} ago",
                           amt=round(v * 1.15), due=po.delivered_at + timedelta(days=2), roles=["PIC"], mismatch=blk,
                           open=("po", po.po_no, "invoice"), cta=dict(label="Fix" if blk else "Review", kind="open", url=f"/records/po/{po.po_no}/?tab=invoice")))
    best = _best_suggestions()
    for p in Payment.objects.filter(status__in=["short", "unmatched"]).select_related("po"):
        if p.status == "short":
            it.append(dict(key="p" + p.payment_no, sev="bad", icon="wallet", title=f"Short payment on {p.invoice_ref}",
                           sub=f"{p.reason or 'No reason given'} · PO {p.po.po_no}", amt=p.deduction_h, amt_label="short",
                           due=p.remit_date + timedelta(days=10), roles=["Finance", "PIC"], mismatch=True, open=("po", p.po.po_no, "invoice"),
                           cta=dict(label="Resolve", kind="link", url="/pay/?tab=short")))
        else:
            it.append(dict(key="u" + p.payment_no, sev="warn", icon="wallet", title=f"Match payment {p.payment_no}",
                           sub=f'Invoice reference "{p.invoice_ref}" not found' + _best(best, "pay_inv", p.payment_no), amt=p.paid_h, due=p.remit_date + timedelta(days=5),
                           roles=["Finance"], open=("payment", p.payment_no, ""), cta=dict(label="Match", kind="open", url=f"/records/payment/{p.payment_no}/")))
    for pr in Promotion.objects.exclude(stage__in=["closed", "rejected", "claimed"]).prefetch_related("lines"):
        st = stage_of(pr, cfg, now)
        if st == "draft":
            it.append(dict(key="d" + pr.mecl_ref, sev="info", icon="tag", title=f"Submit {pr.mecl_ref} to Amazon", sub=pr.name,
                           amt=support_h(pr), amt_label="support", due=pr.start - timedelta(days=10), roles=["PIC"],
                           open=("promo", pr.mecl_ref, ""), cta=dict(label="Open", kind="open", url=f"/records/promo/{pr.mecl_ref}/")))
        elif st == "submitted":
            it.append(dict(key="g" + pr.mecl_ref, sev="", icon="tag", title=f"Waiting for Amazon approval · {pr.mecl_ref}",
                           sub=f"{pr.name} · record the agreement # when approved", amt=support_h(pr), amt_label="support",
                           due=pr.start - timedelta(days=3), roles=["PIC"], waiting=True, open=("promo", pr.mecl_ref, ""),
                           cta=dict(label="Record approval", kind="open", url=f"/records/promo/{pr.mecl_ref}/")))
        elif st == "dn_overdue":
            it.append(dict(key="o" + pr.mecl_ref, sev="bad", icon="receipt", title=f"Debit note overdue · {pr.mecl_ref}",
                           sub=f"{pr.name} · DN was due {timezone.localtime(pr.dn_due):%d %b}",
                           amt=sum((l.sold_units if l.sold_units is not None else l.expected_units) * l.support_h for l in pr.lines.all()),
                           amt_label="expected", due=pr.dn_due + timedelta(days=float(cfg.p('R9', 'days'))), roles=["PIC"],
                           open=("promo", pr.mecl_ref, "dn"), cta=dict(label="Open", kind="open", url=f"/records/promo/{pr.mecl_ref}/?tab=dn")))
        elif st == "dn_validated":
            dn = DebitNote.objects.filter(agreement_no=pr.agreement_no, validated=True).first()
            it.append(dict(key="l" + pr.mecl_ref, sev="info", icon="claim", title=f"Send claim for {pr.mecl_ref}", sub=pr.name,
                           amt=dn.approved_h if dn else None, due=None, roles=["PIC", "Product"], open=("promo", pr.mecl_ref, "claim"),
                           cta=dict(label="Generate claim", kind="post", url=f"/promos/{pr.mecl_ref}/claim/", perm="promo")))
    for dn in DebitNote.objects.filter(validated=False).prefetch_related("lines__sku"):
        ev = evaluate(dn, cfg)
        if ev["status"] == "mismatch":
            it.append(dict(key="m" + dn.dn_no, sev="bad", icon="receipt", title=f"DN {dn.dn_no} does not match the agreement",
                           sub=f"{ev['promo'].mecl_ref} · charged SAR {round(ev['charged_h'] / 100):,}, expected SAR {round(ev['expected_h'] / 100):,}",
                           amt=ev["variance_h"], amt_label="variance", due=dn.dn_date + timedelta(days=7), roles=["PIC", "Finance"],
                           mismatch=True, open=("promo", ev["promo"].mecl_ref, "dn"),
                           cta=dict(label="Validate", kind="open", url=f"/records/promo/{ev['promo'].mecl_ref}/?tab=dn")))
        elif ev["status"] == "to_validate":
            it.append(dict(key="v" + dn.dn_no, sev="info", icon="receipt", title=f"Approve DN {dn.dn_no}",
                           sub=f"{ev['promo'].mecl_ref} · matches the agreement", amt=ev["charged_h"], due=dn.dn_date + timedelta(days=7),
                           roles=["PIC", "Finance"], open=("promo", ev["promo"].mecl_ref, "dn"),
                           cta=dict(label="Approve", kind="post", url=f"/dns/{dn.dn_no}/approve/", perm="dn")))
        elif ev["status"] == "unlinked":
            it.append(dict(key="x" + dn.dn_no, sev="warn", icon="link", title=f"Link DN {dn.dn_no} to a promotion",
                           sub=f"Agreement # {dn.agreement_no} is not in the tracker" + _best(best, "dn_promo", dn.dn_no), amt=ev["charged_h"], due=dn.dn_date + timedelta(days=7),
                           roles=["PIC"], mismatch=True, open=("dn", dn.dn_no, ""), cta=dict(label="Link", kind="open", url=f"/records/dn/{dn.dn_no}/")))
    for c in Claim.objects.filter(status="shortfall").select_related("promotion"):
        it.append(dict(key="f" + c.claim_no, sev="bad", icon="claim", title=f"Credit note short on {c.claim_no}",
                       sub=f"{c.promotion.mecl_ref} · claimed SAR {round(c.amount_h / 100):,}, received SAR {round(c.cn_h / 100):,}",
                       amt=c.gap_h, amt_label="short", due=c.cn_date + timedelta(days=14), roles=["Product", "Finance"], mismatch=True,
                       open=("promo", c.promotion.mecl_ref, "claim"), cta=dict(label="Open", kind="open", url=f"/records/promo/{c.promotion.mecl_ref}/?tab=claim")))
    for i in it:
        i.setdefault("mismatch", False)
        i.setdefault("waiting", False)
        i.setdefault("amt_label", "")
        i["role_titles"] = " / ".join(ROLE_TITLES[r] for r in i["roles"])
        i["mine"] = bool(set(getattr(user, "roles", None) or []) & set(i["roles"])) if user else True
        i["overdue"] = bool(i["due"] and i["due"] < now)
        i["due_soon"] = bool(i["due"] and not i["overdue"] and i["due"] - now < timedelta(hours=12))
        i["due_text"] = (f"{_span(now - i['due'])} overdue" if i["overdue"] else f"due in {_span(i['due'] - now)}") if i["due"] else ""
    it.sort(key=lambda i: (RANK[i["sev"]], i["due"] or now + timedelta(days=3650)))
    if mine and user and not (set(user.roles or []) & {"Admin", "Manager"}):
        mine_roles = set(user.roles or [])
        it = [i for i in it if mine_roles & set(i["roles"])]
    return it
