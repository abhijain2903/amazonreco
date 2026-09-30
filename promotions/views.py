from datetime import datetime, timedelta

from django.db.models import Q
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.http import require_POST

from catalog.models import CATEGORY_NAMES, Sku
from claims.models import Claim
from claims.services import generate_claim
from core import htmx
from core.models import Note
from core.services import CommandError, timeline, to_h
from debitnotes.models import DebitNote
from debitnotes.services import evaluate
from rules.services import get_cfg

from . import services as svc
from .models import STAGE_LABELS, STAGE_TONES, Promotion
from .services import get_promo, stage_of, support_h

GROUPS = [("all", "All", None), ("pre", "Draft & submitted", ["draft", "submitted", "rejected"]), ("run", "Approved & live", ["approved", "live"]),
          ("after", "After the promo", ["waiting_dn", "dn_overdue", "dn_received", "dn_validated"]), ("claims", "Claims", ["claimed", "cn_shortfall"]),
          ("closed", "Closed", ["closed"])]
ORDER = ["draft", "submitted", "approved", "live", "waiting_dn", "dn_overdue", "dn_received", "dn_validated", "claimed", "cn_shortfall", "closed"]


def promo_list(request):
    tab = htmx.pick(request, "tab", [k for k, _, _ in GROUPS], "all")
    view = htmx.pick(request, "view", ["table", "board", "timeline"], "table")
    cat, q = request.GET.get("cat", ""), request.GET.get("q", "").strip()
    cfg, now = get_cfg(), timezone.now()
    allp = list(Promotion.objects.prefetch_related("lines"))
    for p in allp:
        p.st, p.support = stage_of(p, cfg, now), support_h(p)
    rows = allp
    if cat:
        rows = [p for p in rows if p.category == cat]
    if q:
        ql = q.lower()
        rows = [p for p in rows if ql in p.name.lower() or ql in p.mecl_ref.lower() or ql in (p.agreement_no or "")]
    counts = {k: sum(1 for p in allp if s is None or p.st in s) for k, _, s in GROUPS}
    if view == "table":
        st = dict((k, s) for k, _, s in GROUPS).get(tab)
        if st:
            rows = [p for p in rows if p.st in st]
    rows.sort(key=lambda p: p.start, reverse=True)
    cnt = lambda sts: sum(1 for p in allp if p.st in sts)
    committed = [p for p in allp if p.st in ("approved", "live", "waiting_dn", "dn_overdue")]
    kpis = [dict(l="Live now", v=cnt(["live"]), s="Running on Amazon", url="?tab=run"),
            dict(l="Waiting for DN", v=cnt(["waiting_dn", "dn_overdue"]), s=f"{cnt(['dn_overdue'])} overdue", url="?tab=after", alert=cnt(["dn_overdue"]) > 0),
            dict(l="DN to validate", v=cnt(["dn_received"]), s="Debit notes received", url="/dns/?tab=todo"),
            dict(l="Support committed", v=f"{round(sum(p.support for p in committed) / 100):,}", s="SAR · approved and live", url="?tab=run")]
    ctx = dict(rows=rows, tab=tab, view=view, cat=cat, q=q, kpis=kpis, cats=list(CATEGORY_NAMES),
               tabs=[dict(id=k, label=l, count=counts[k]) for k, l, _ in GROUPS])
    if view == "board":
        ctx["board"] = [dict(st=s, label=STAGE_LABELS[s], tone=STAGE_TONES[s], items=[p for p in rows if p.st == s][:8],
                             more=max(0, sum(1 for p in rows if p.st == s) - 8), n=sum(1 for p in rows if p.st == s)) for s in ORDER]
    if view == "timeline":
        a, b = now - timedelta(days=60), now + timedelta(days=75)
        W = (b - a).total_seconds()
        pct = lambda x: max(0, min(100, (x - a).total_seconds() / W * 100))
        vis = sorted([p for p in rows if p.end > a and p.start < b], key=lambda p: p.start)
        ctx.update(gantt=[dict(p=p, left=pct(p.start), width=max(.6, pct(p.end) - pct(p.start)), dn=pct(p.dn_due) if p.st in ("waiting_dn", "dn_overdue") else None) for p in vis],
                   ticks=[dict(left=pct(a + timedelta(days=d)), d=a + timedelta(days=d)) for d in range(0, 136, 14)], today=pct(now))
    return render(request, "pages/promos.html", ctx)


def _steps(p, st, dn, c):
    ev = evaluate(dn) if dn else None
    arr = [dict(l="Receive promotion", st="done"), dict(l="Prep support", st="done" if p.stage != "draft" else ""),
           dict(l="Share to Amazon", st="done" if p.stage != "draft" else ""),
           dict(l="Agreement #", st="done" if p.agreement_no else "fail" if st == "rejected" else ""),
           dict(l="Log in tracker", st="done" if p.agreement_no else ""),
           dict(l="Promo runs (+30 days)", st="done" if p.agreement_no and timezone.now() > p.dn_due else ""),
           dict(l="Debit note raised", st="done" if dn else "fail" if st == "dn_overdue" else ""),
           dict(l="Validate claim", st="done" if dn and dn.validated else "fail" if ev and ev["status"] == "mismatch" else ""),
           dict(l="Share with product", st="done" if c else ""),
           dict(l="Reconcile CN", st="done" if c and c.status in ("closed", "written_off") else "fail" if c and c.status == "shortfall" else "")]
    cur = next((k for k, x in enumerate(arr) if x["st"] == ""), None)
    if cur is not None:
        arr[cur]["st"] = "cur"
    return arr


def dn_panel(dn, p):
    ev = evaluate(dn)
    rows = []
    for l in ev["lines"]:
        m = l["sku_obj"].model_no
        rows.append(dict(name=f"Model in promotion · {m}", exp="Yes", act="Yes" if l.get("in_promo") else "No", ok=l.get("in_promo")))
        if l.get("in_promo"):
            rows.append(dict(name=f"Rate per unit · {m}", exp=f"{l['support_h'] / 100:,.2f}", act=f"{l['rate_h'] / 100:,.2f}", gap=f"{(l['rate_h'] - l['support_h']) / 100:,.2f}", ok=l["rate_ok"]))
            rows.append(dict(name=f"Units vs Amazon sales report · {m}", sub="" if l["sold"] is not None else "Sales report not loaded",
                             exp="—" if l["sold"] is None else f"{l['sold']:,}", act=f"{l['units']:,}", gap="—" if l["sold"] is None else f"{l['units'] - l['sold']:,}",
                             ok=None if l["sold"] is None else l["units_ok"]))
    rows.append(dict(name="DN raised after promo end", exp=f"After {timezone.localtime(p.end):%d %b}", act=f"{timezone.localtime(dn.dn_date):%d %b}", ok=ev["date_ok"]))
    tol = get_cfg().tol_h()
    for l in ev["lines"]:
        l["bad"] = l["gap_h"] > tol if "gap_h" in l else False
    return dict(ev=ev, checks=rows, tol=tol, var_bad=ev["variance_h"] is not None and abs(ev["variance_h"]) > tol)


def drawer(request, ref):
    p = get_promo(ref)
    st = stage_of(p)
    tab = htmx.pick(request, "tab", ["models", "dn", "claim", "timeline", "notes"], "models")
    dn = DebitNote.objects.filter(agreement_no=p.agreement_no).first() if p.agreement_no else None
    c = Claim.objects.filter(promotion=p).first()
    base = f"/records/promo/{p.mecl_ref}/"
    ctx = dict(p=p, st=st, tab=tab, base=base, url=f"{base}?tab={tab}", dn=dn, c=c, support=support_h(p), lines=list(p.lines.select_related("sku")),
               steps=_steps(p, st, dn, c),
               chain=[dict(l="MECL ref", v=p.mecl_ref), dict(l="Amazon agreement", v=p.agreement_no), dict(l="Debit note", v=dn and dn.dn_no, tab="dn"),
                      dict(l="Claim", v=c and c.claim_no, tab="claim"), dict(l="Credit note", v=c and c.cn_no, tab="claim")],
               dtabs=[dict(id="models", label="Models", n=p.lines.count()), dict(id="dn", label="Debit note check"), dict(id="claim", label="Claim & credit note"),
                      dict(id="timeline", label="Timeline"), dict(id="notes", label="Notes", n=Note.objects.filter(entity="promo", entity_id=p.mecl_ref).count())])
    lines = ctx["lines"]
    ctx.update(exp_units=sum(l.expected_units for l in lines), missing_sold=any(l.sold_units is None for l in lines),
               sold_units=None if any(l.sold_units is None for l in lines) else sum(l.sold_units for l in lines),
               pre_stages=["draft", "submitted", "approved", "live", "rejected"])
    if tab == "dn" and not dn:
        ctx["has_unlinked"] = any(evaluate(d)["status"] == "unlinked" for d in DebitNote.objects.filter(validated=False))
    if dn and (tab == "dn" or st == "dn_received"):
        ctx.update(dn_panel(dn, p))
    if tab == "timeline":
        ids = [("promotion", p.mecl_ref)] + ([("dn", dn.dn_no)] if dn else []) + ([("claim", c.claim_no)] if c else [])
        from core.models import AuditEvent
        qq = Q()
        for e, i in ids:
            qq |= Q(entity=e, entity_id=i)
        ctx["events"] = AuditEvent.objects.filter(qq).order_by("-at", "-id")
    if tab == "notes":
        ctx.update(notes=Note.objects.filter(entity="promo", entity_id=p.mecl_ref), entity="promo", key=p.mecl_ref)
    return render(request, "records/promo.html", ctx)


# ---------- commands ----------
@require_POST
def submit(request, ref):
    p, f = svc.submit_promotion(request.user, ref, request.POST.get("version"))
    return htmx.done(request, f"{p.mecl_ref} submitted", file=f)


@require_POST
def approve(request, ref):
    p = svc.record_approval(request.user, ref, request.POST.get("agreement", ""), request.POST.get("version"))
    return htmx.done(request, f"Approval recorded. DN reminder set for {timezone.localtime(p.dn_due):%d %b}")


@require_POST
def reject(request, ref):
    svc.reject_promotion(request.user, ref, request.POST.get("version"))
    return htmx.done(request, f"{ref} marked rejected", "info")


@require_POST
def sold(request, ref):
    svc.pull_sold_units(request.user, ref)
    return htmx.done(request, "Sold units loaded")


@require_POST
def chase(request, ref):
    svc.chase_amazon(request.user, ref)
    return htmx.done(request, "Chase logged on the timeline")


@require_POST
def claim(request, ref):
    c, f = generate_claim(request.user, ref)
    return htmx.done(request, f"Claim {c.claim_no} created", file=f)


# ---------- new promotion wizard (state kept in the session) ----------
def _parse_day(s):
    d = datetime.strptime(s, "%Y-%m-%d")
    return timezone.make_aware(d.replace(hour=6))


def wizard(request):
    from core.services import require
    require(request.user, "promo")
    w = request.session.get("promo_wiz")
    if request.method == "GET" or not w:
        today = timezone.localdate()
        w = dict(step=1, name="", cat="DI", start=(today + timedelta(days=14)).isoformat(), end=(today + timedelta(days=21)).isoformat(),
                 owner="Product team", lines=[], err="")
    else:
        act = request.POST.get("act", "next")
        w["err"] = ""
        for f in ("name", "cat", "start", "end", "owner"):
            if f in request.POST:
                if f == "cat" and request.POST[f] != w["cat"]:
                    w["lines"] = []
                w[f] = request.POST[f]
        for i, l in enumerate(w["lines"]):
            for f in ("support", "expected"):
                v = request.POST.get(f"{f}-{i}")
                if v not in (None, ""):
                    l[f] = max(0.0, float(v))
        if act == "add" and request.POST.get("sku"):
            s = Sku.objects.get(sku_code=request.POST["sku"])
            w["lines"].append(dict(sku=s.sku_code, support=max(5, round(s.cost_h / 100 * 0.07 / 5) * 5), expected=40))
        elif act.startswith("rm-"):
            w["lines"].pop(int(act[3:]))
        elif act == "back":
            w["step"] = max(1, w["step"] - 1)
        elif act == "next":
            if w["step"] == 1:
                if not w["name"].strip():
                    w["err"] = "Give the promotion a name."
                elif w["end"] < w["start"]:
                    w["err"] = "End date must be on or after the start date."
            if w["step"] == 2:
                if not w["lines"]:
                    w["err"] = "Add at least one model."
                elif any(l["support"] <= 0 or l["expected"] <= 0 for l in w["lines"]):
                    w["err"] = "Every model needs support per unit and expected units above 0."
            if not w["err"]:
                w["step"] = min(3, w["step"] + 1)
        elif act in ("draft", "submit"):
            skus = {s.sku_code: s for s in Sku.objects.filter(sku_code__in=[l["sku"] for l in w["lines"]])}
            p = svc.create_promotion(request.user, w["name"], w["cat"], _parse_day(w["start"]), _parse_day(w["end"]).replace(hour=23, minute=59),
                                     w["owner"], [(skus[l["sku"]], to_h(l["support"]), int(l["expected"])) for l in w["lines"]], source="the hub")
            request.session.pop("promo_wiz", None)
            url = f"/records/promo/{p.mecl_ref}/"
            if act == "submit":
                p, f = svc.submit_promotion(request.user, p.mecl_ref)
                return htmx.done(request, f"{p.mecl_ref} submitted", file=f, open_drawer=url, drawer=False)
            return htmx.done(request, f"{p.mecl_ref} saved as draft", close_modal=True, open_drawer=url, drawer=False)
    request.session["promo_wiz"] = w
    skus = {s.sku_code: s for s in Sku.objects.filter(sku_code__in=[l["sku"] for l in w["lines"]])}
    lines = [dict(i=i, s=skus[l["sku"]], support=l["support"], expected=int(l["expected"]), total=l["support"] * l["expected"]) for i, l in enumerate(w["lines"])]
    total = sum(l["total"] for l in lines)
    opts = Sku.objects.filter(category=w["cat"]).exclude(sku_code__in=[l["sku"] for l in w["lines"]])
    warn = [l for l in lines if l["support"] * 100 > l["s"].cost_h * 0.25]
    end = _parse_day(w["end"]) if w["end"] else None
    return render(request, "dialogs/promo_wizard.html", dict(w=w, lines=lines, total=total, exp_total=sum(l["expected"] for l in lines), opts=opts, cats=CATEGORY_NAMES.items(), warn=warn,
                  dn_due=end + timedelta(days=30) if end else None, start=_parse_day(w["start"]) if w["start"] else None, end=end,
                  steps=["Basics", "Models & support", "Review"]))
