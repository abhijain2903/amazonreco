import time
from datetime import timedelta
from urllib.parse import quote

from django.conf import settings
from django.contrib.auth import login, logout
from django.contrib.auth.decorators import login_not_required
from django.db.models import Max, Q, Sum
from django.http import Http404, HttpResponse, JsonResponse, StreamingHttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from billing.models import Invoice
from catalog.models import CATEGORY_NAMES, FulfilmentCentre, Sku
from claims.models import Claim
from debitnotes.models import DebitNote
from debitnotes.services import evaluate
from identity.models import ROLE_HOME, ROLE_TITLES, User
from identity.permissions import PERM_LABELS, PERMS
from orders.models import STAGE_LABELS, STAGES, PurchaseOrder
from orders.services import po_value_h
from payments.models import Dispute, Payment
from promotions.models import STAGE_LABELS as P_LABELS
from promotions.models import Promotion
from promotions.services import stage_of, support_h
from rules.services import rule_rows, update_rule

from . import htmx
from .actions import action_items
from .models import AuditEvent, GeneratedFile, Note, Notification
from .nav import NAV, visible
from .services import CommandError, peek_number, require


# ---------- dashboard ----------
def dashboard(request):
    now = timezone.now()
    pos = list(PurchaseOrder.objects.prefetch_related("lines"))
    to_conf = [p for p in pos if p.stage == "new"]
    overdue = [p for p in to_conf if p.confirm_by < now]
    open_po = [p for p in pos if p.stage not in ("paid", "rejected", "cancelled")]
    unpaid = Invoice.objects.filter(po__stage="invoiced")
    disp = Dispute.objects.filter(status__in=["open", "submitted"])
    promos = list(Promotion.objects.prefetch_related("lines"))
    pstage = {p.pk: stage_of(p) for p in promos}
    open_promos = [p for p in promos if pstage[p.pk] not in ("closed", "rejected")]
    evs = [evaluate(d) for d in DebitNote.objects.filter(validated=False)]
    mism = [e for e in evs if e["status"] == "mismatch"]
    short = Claim.objects.filter(status="shortfall")
    kpis = [
        dict(l="Open POs", v=f"{len(open_po):,}", s=f"SAR {round(sum(po_value_h(p) for p in open_po) / 100):,} in flow", url="/pos/?tab=all"),
        dict(l="Awaiting confirmation", v=len(to_conf), s=f"{len(overdue)} overdue" if overdue else "None overdue", url="/pos/?tab=new", alert=bool(overdue), bad_sub=bool(overdue)),
        dict(l="Invoices unpaid", v=f"{round((unpaid.aggregate(s=Sum('total_h'))['s'] or 0) / 100):,}", s=f"SAR · {unpaid.count()} invoices", url="/ship/?tab=submitted"),
        dict(l="Open disputes", v=f"{round((disp.aggregate(s=Sum('amount_h'))['s'] or 0) / 100):,}", s=f"SAR · {disp.count()} case{'' if disp.count() == 1 else 's'}", url="/pay/?tab=disputes"),
        dict(l="Open promotions", v=len(open_promos), s=f"{sum(1 for p in promos if pstage[p.pk] == 'live')} live now", url="/promos/"),
        dict(l="DN variance", v=f"{round(sum(e['variance_h'] for e in mism) / 100):,}", s=f"SAR · {len(mism)} debit note{'' if len(mism) == 1 else 's'}", url="/dns/?tab=mismatch", alert=bool(mism)),
        dict(l="CN shortfall", v=f"{round(sum(c.gap_h for c in short) / 100):,}", s=f"SAR · {short.count()} claim{'' if short.count() == 1 else 's'}", url="/claims/?tab=shortfall", alert=short.exists()),
    ]
    ramp = lambda i, n: f"color-mix(in srgb, var(--primary) {round(28 + i * (72 / (n - 1)))}%, var(--sunk))"
    tabmap = {"new": "new", "confirmed": "book", "booked": "release", "released": "ship", "asn": "ship", "slot": "ship", "delivered": "ship"}
    pipe_in = [dict(l=STAGE_LABELS[s], n=sum(1 for p in pos if p.stage == s), c=ramp(i, len(STAGES)), url=f"/pos/?tab={tabmap.get(s, 'done')}") for i, s in enumerate(STAGES)]
    so = ["draft", "submitted", "approved", "live", "waiting_dn", "dn_overdue", "dn_received", "dn_validated", "claimed", "cn_shortfall", "closed"]
    pipe_out = [dict(l=P_LABELS[s], n=sum(1 for p in promos if pstage[p.pk] == s), c="var(--bad)" if s in ("dn_overdue", "cn_shortfall") else ramp(i, len(so)), url="/promos/") for i, s in enumerate(so)]
    # invoice-to-cash chart
    recent = Payment.objects.filter(status="matched", remit_date__gt=now - timedelta(days=75), invoice__isnull=False).select_related("invoice")
    days = [(p.remit_date - p.invoice.invoice_date).days for p in recent]
    cur = round(sum(days) / len(days)) if days else 44
    months = [(now.replace(day=1) - timedelta(days=31 * (5 - i))).strftime("%b") for i in range(6)]
    vals = [58, 55, 52, 49, 47, cur]
    W, H, pl, pr, pt, pb, mn, mx = 520, 190, 34, 16, 14, 26, 20, 70
    X = lambda i: pl + i * (W - pl - pr) / 5
    Y = lambda v: pt + (mx - min(max(v, mn), mx)) * (H - pt - pb) / (mx - mn)
    line = "".join(f"{'M' if i == 0 else 'L'}{X(i):.1f},{Y(v):.1f}" for i, v in enumerate(vals))
    chart = dict(W=W, H=H, line=line, area=f"{line}L{X(5)},{Y(mn)}L{X(0)},{Y(mn)}Z", grid=[dict(v=g, y=Y(g), x1=pl, x2=W - pr, tx=pl - 8) for g in (30, 40, 50, 60)],
                 pts=[dict(x=X(i), y=Y(v), last=i == 5) for i, v in enumerate(vals)], labels=[dict(x=X(i), t=m) for i, m in enumerate(months)],
                 cur=cur, lx=X(5) - 8, ly=Y(cur) - 12, by=H - 6)
    cc = [(c, sum(1 for p in open_promos if p.category == c)) for c in CATEGORY_NAMES]
    cmax = max([n for _, n in cc] + [1])
    checked = sum(p.lines.count() for p in pos)
    retypes = sum(1 for p in pos if p.stage_index >= 1) * 6
    dnv = DebitNote.objects.filter(validated=True).count()
    hours = round((retypes * 3 + checked * 1.5 + dnv * 25) / 60)
    first = request.user.name.split()[0]
    greeting = "morning" if timezone.localtime(now).hour < 12 else "afternoon"
    return render(request, "pages/dashboard.html", dict(kpis=kpis, pipe_in=pipe_in, pipe_out=pipe_out, n_pos=len(pos), n_promos=len(promos),
                  chart=chart, cats=[dict(c=c, n=n, w=n / cmax * 100) for c, n in cc], auto=dict(checked=checked, retypes=retypes, dnv=dnv, hours=hours),
                  items=action_items(request.user)[:5], greeting=f"Good {greeting}, {first}", today=now))


# ---------- action center ----------
def action(request):
    mine = request.GET.get("mine", "1") == "1"
    tab = htmx.pick(request, "tab", ["all", "today", "mismatch", "waiting"], "all")
    everyone = action_items(request.user, mine=False)
    base = action_items(request.user, mine=True) if mine else everyone
    eod = timezone.localtime().replace(hour=23, minute=59, second=59)
    roles = set(request.user.roles or [])
    groups = {
        "all": base,
        "today": [i for i in base if i["due"] and i["due"] <= eod],
        "mismatch": [i for i in base if i["mismatch"]],
        "waiting": [i for i in everyone if i["waiting"] or not (roles & set(i["roles"]))],
    }
    from .exports import sar, wants_export, xlsx
    if wants_export(request):
        return xlsx(f"ActionCenter_{tab}", ["Item", "Detail", "Amount SAR", "Due", "Roles"],
                    [[i["title"], i["sub"], sar(i["amt"]), i["due"], i["role_titles"]] for i in groups.get(tab, base)])
    auto = sum(p.lines.count() for p in PurchaseOrder.objects.exclude(stage="new")) + DebitNote.objects.filter(validated=True).count()
    tabs = [("all", "All"), ("today", "Due today"), ("mismatch", "Mismatches"), ("waiting", "Waiting on others")]
    return render(request, "pages/action.html", dict(items=groups.get(tab, base), tab=tab, mine=mine, auto=auto,
                  tabs=[dict(id=k, label=l, count=len(groups[k]), url=f"?tab={k}&mine={'1' if mine else '0'}") for k, l in tabs]))


def reports(request):
    from .reports import report, rows
    days = int(htmx.pick(request, "days", ["7", "30", "90"], "30"))
    sections = report(days)
    from .exports import wants_export, xlsx
    if wants_export(request):
        return xlsx(f"Management_report_{days}d", ["Area", "Measure", "Value", "Detail"], rows(sections))
    return render(request, "pages/reports.html", dict(sections=sections, days=days, periods=[7, 30, 90]))


# ---------- shell partials ----------
def nav(request):
    return render(request, "partials/nav.html", {"path": request.GET.get("path", "/")})


def topbar(request):
    return render(request, "partials/bell.html")


def _alerts(request):
    from .services import visible_alerts
    items = list(visible_alerts(request.user).prefetch_related("read_by")[:25])
    for n in items:
        n.seen = n.read or request.user in n.read_by.all()
    return render(request, "partials/notifications.html", {"items": items, "scope": request.user.alert_scope})


def notifications(request):
    return _alerts(request)


@require_POST
def notifications_read(request):
    from .services import unread_alerts
    Through = Notification.read_by.through
    Through.objects.bulk_create([Through(notification_id=i, user_id=request.user.pk) for i in unread_alerts(request.user).values_list("id", flat=True)],
                                ignore_conflicts=True)
    return _alerts(request)


@require_POST
def notifications_scope(request):
    u = request.user
    u.alert_scope = "all" if u.alert_scope == "mine" else "mine"
    u.save(update_fields=["alert_scope"])
    return _alerts(request)


def notification_open(request, pk):
    from .services import visible_alerts
    n = get_object_or_404(visible_alerts(request.user), pk=pk)
    n.read_by.add(request.user)
    url = _record_url(n.link_type, n.link_id, n.link_tab) if n.link_type else None
    resp = HttpResponse("")
    import json
    trig = {"refresh": True}
    if url:
        trig["openDrawer"] = {"url": url}
    resp["HX-Trigger"] = json.dumps(trig)
    return resp


def _record_url(t, i, tab=""):
    return f"/records/{t}/{i}/" + (f"?tab={tab}" if tab else "")


def events(request):
    """Server-sent events: tells the browser when anything changed. One subscription per tab; the browser reconnects
    when a stream ends.

    Under ASGI (production: gunicorn + uvicorn workers) the stream is async, so an open tab does not hold a thread
    and a stream can stay open ~5 minutes. Under WSGI (runserver) it falls back to a plain generator that ends after
    ~25 s, because there each open stream holds a thread.
    """
    latest = lambda: AuditEvent.objects.aggregate(m=Max("id"))["m"] or 0

    def sync_stream():
        last = latest()
        yield "retry: 3000\n\n"
        for _ in range(12):
            time.sleep(2)
            m = latest()
            yield f"event: changed\ndata: {m}\n\n" if m != last else ": ping\n\n"
            last = m

    async def async_stream():
        import asyncio

        from asgiref.sync import sync_to_async
        alatest = sync_to_async(latest)
        last = await alatest()
        yield "retry: 3000\n\n"
        for _ in range(150):
            await asyncio.sleep(2)
            m = await alatest()
            yield f"event: changed\ndata: {m}\n\n" if m != last else ": ping\n\n"
            last = m

    resp = StreamingHttpResponse(async_stream() if hasattr(request, "scope") else sync_stream(), content_type="text/event-stream")
    resp["Cache-Control"] = "no-cache"
    resp["X-Accel-Buffering"] = "no"
    return resp


# ---------- search ----------
def search(request):
    q = request.GET.get("q", "").strip()
    res = []
    add = lambda g, label, sub, url, ic: res.append(dict(g=g, label=label, sub=sub, url=url, icon=ic))
    for _, items in NAV:
        for key, url, label, ic in items:
            if visible(request.user, key) and (not q or q.lower() in label.lower()):
                res.append(dict(g="Go to", label=label, sub="", url=url, icon=ic, nav=True))
    if q:
        ql = q.lower()
        for p in PurchaseOrder.objects.filter(Q(po_no__icontains=q) | Q(sap_order_no__icontains=q) | Q(sf_order_id__icontains=q) |
                                               Q(sap_delivery__delivery_no__icontains=q) | Q(shipment__asn_no__icontains=q) | Q(invoice__invoice_no__icontains=q)).distinct()[:8]:
            add("Purchase orders", f"PO {p.po_no}", STAGE_LABELS.get(p.stage, p.stage), _record_url("po", p.po_no), "po")
        for p in Payment.objects.filter(payment_no__icontains=q)[:5]:
            add("Payments", p.payment_no, f"SAR {round(p.paid_h / 100):,} · {p.get_status_display()}", _record_url("payment", p.payment_no), "wallet")
        for p in Promotion.objects.filter(Q(mecl_ref__icontains=q) | Q(agreement_no__icontains=q) | Q(name__icontains=q))[:8]:
            add("Promotions", p.mecl_ref, f"{p.name} · {P_LABELS[stage_of(p)]}", _record_url("promo", p.mecl_ref), "tag")
        for d in DebitNote.objects.filter(dn_no__icontains=q)[:5]:
            add("Debit notes", d.dn_no, f"Agreement {d.agreement_no}", _record_url("dn", d.dn_no), "receipt")
        for c in Claim.objects.filter(Q(claim_no__icontains=q) | Q(cn_no__icontains=q) | Q(credit_notes__cn_no__icontains=q)).distinct().select_related("promotion")[:5]:
            add("Claims", c.claim_no, f"{c.promotion.mecl_ref}{' · ' + c.cn_no if c.cn_no else ''}", _record_url("promo", c.promotion.mecl_ref, "claim"), "claim")
        for d in Dispute.objects.filter(case_no__icontains=q)[:3]:
            add("Disputes", d.case_no, f"SAR {round(d.amount_h / 100):,}", _record_url("dispute", d.case_no), "alert")
        for s in Sku.objects.filter(Q(sku_code__icontains=q) | Q(model_no__icontains=q) | Q(asin__icontains=q))[:6]:
            add("Products", s.model_no, f"{s.sku_code} · {s.asin}", _record_url("sku", s.sku_code), "box")
    tpl = "partials/search_results.html" if request.GET.get("partial") else "partials/search.html"
    return render(request, tpl, {"res": res, "q": q})


# ---------- records ----------
def record(request, kind, key):
    from debitnotes.views import drawer as dn_drawer
    from integrations.views import drawer as conn_drawer
    from orders.views import drawer as po_drawer
    from payments.views import dispute_drawer, payment_drawer
    from promotions.views import drawer as promo_drawer
    views = {"po": po_drawer, "promo": promo_drawer, "dn": dn_drawer, "payment": payment_drawer, "dispute": dispute_drawer,
             "sku": sku_drawer, "conn": conn_drawer}
    if kind not in views:
        raise Http404
    if not htmx.is_htmx(request):
        # A bookmarked or shared record link: show the record's list page and open the drawer on top of it (hub.js).
        page = RECORD_PAGES[kind]
        return redirect(f"{page}{'&' if '?' in page else '?'}open={quote(request.get_full_path())}")
    return views[kind](request, key)


RECORD_PAGES = {"po": "/pos/?tab=all", "promo": "/promos/", "dn": "/dns/", "payment": "/pay/", "dispute": "/pay/?tab=disputes",
                "sku": "/pos/?tab=all", "conn": "/integrations/"}


def sku_drawer(request, key):
    s = get_object_or_404(Sku, sku_code=key)
    pos = PurchaseOrder.objects.filter(lines__sku=s).exclude(stage__in=["paid", "rejected", "cancelled"]).distinct()
    promos = Promotion.objects.filter(lines__sku=s).distinct()
    return render(request, "records/sku.html", {"s": s, "pos": [(p, p.lines.filter(sku=s).first()) for p in pos],
                                                "promos": [(p, p.lines.filter(sku=s).first()) for p in promos],
                                                "url": request.get_full_path()})


@require_POST
def add_note(request, entity, key):
    text = request.POST.get("text", "").strip()
    if not text:
        raise CommandError("Type a note first.")
    Note.objects.create(entity=entity, entity_id=key, text=text, author_name=request.user.name)
    from .models import Assignment
    from .services import LABEL, mentioned_users, notify
    told = []
    for u in mentioned_users(text):
        if u != request.user:
            notify(f"{request.user.name} mentioned you on {LABEL.get(entity, entity)} {key}: “{text[:90]}”", "info", (entity, key, "notes"), user=u)
            told.append(u)
    a = Assignment.objects.filter(entity=entity, entity_id=key).select_related("user").first()
    if a and a.user != request.user and a.user not in told:
        notify(f"{request.user.name} added a note on {LABEL.get(entity, entity)} {key}: “{text[:90]}”", "info", (entity, key, "notes"), user=a.user)
    return htmx.done(request, "Note added" + (f" · {', '.join(u.name for u in told)} notified" if told else ""), refresh=False)


@require_POST
def assign(request, entity, key):
    from .services import assign as do_assign
    if entity not in ("po", "promo", "dispute", "dn"):
        raise Http404
    to = do_assign(request.user, entity, key, request.POST.get("user", ""))
    return htmx.done(request, f"Assigned to {to.name}" if to else "Back with the role", refresh=False)


def owner_ctx(entity, key):
    """Context for the 'Owner' picker in a drawer header."""
    from identity.models import User
    from .models import Assignment
    a = Assignment.objects.filter(entity=entity, entity_id=key).select_related("user").first()
    people = [u for u in User.objects.filter(is_active=True).order_by("first_name", "username") if u.roles and "Admin" not in u.roles]
    return dict(owner=a.user if a else None, people=people, owner_entity=entity, owner_key=key)


def documents(entity, key, po=None):
    """Everything to come back to on a record: generated files, attached documents and (for a PO) dispute evidence.
    entity: "po" or "promo" (generated promotion and claim files are stored against "promotion")."""
    from .models import Attachment
    docs = [dict(name=f.filename, kind=KIND_LABELS.get(f.kind, f.kind), at=f.created_at, by="Generated by the hub",
                 url=f"/files/{f.pk}/", preview=f"/files/{f.pk}/?preview=1")
            for f in GeneratedFile.objects.filter(entity="promotion" if entity == "promo" else entity, entity_id=key)]
    docs += [dict(name=a.filename, kind=a.get_kind_display(), at=a.created_at, by=a.uploaded_by_name, url=f"/attachments/{a.pk}/")
             for a in Attachment.objects.filter(entity=entity, entity_id=key)]
    if po is not None:
        from payments.models import DisputeEvidence
        docs += [dict(name=e.filename, kind=f"Evidence · {e.dispute.case_no}", at=e.created_at, by="Dispute", url=f"/attachments/evidence/{e.pk}/")
                 for e in DisputeEvidence.objects.filter(dispute__po=po).select_related("dispute") if e.file]
    return sorted(docs, key=lambda d: d["at"], reverse=True)


KIND_LABELS = {"po_ack": "PO acknowledgement", "asn": "ASN", "invoice": "Invoice", "promotion": "Promotion file", "claim": "Claim file",
               "labels": "Carton labels", "credit_memo": "Credit memo"}


@require_POST
def add_document(request, entity, key):
    from .models import Attachment
    from orders.models import PurchaseOrder
    from promotions.models import Promotion
    if entity not in ("po", "promo"):
        raise Http404
    from .services import require
    require(request.user, "upload")
    if not (PurchaseOrder if entity == "po" else Promotion).objects.filter(**{"po_no" if entity == "po" else "mecl_ref": key}).exists():
        raise Http404
    f = request.FILES.get("file")
    if not f:
        raise CommandError("Choose a file to attach.")
    if f.size > settings.HUB_MAX_UPLOAD_BYTES:
        raise CommandError("The file is larger than 25 MB.")
    kind = request.POST.get("kind", "other")
    if kind not in dict(Attachment.KINDS):
        kind = "other"
    Attachment.objects.create(entity=entity, entity_id=key, kind=kind, filename=f.name[:200], file=f, uploaded_by_name=request.user.name)
    from .services import audit
    audit("po" if entity == "po" else "promotion", key, f"Document attached: {f.name} ({dict(Attachment.KINDS)[kind]})", request.user, action="attach")
    return htmx.done(request, f"{f.name} attached", refresh=False)


def attachment_view(request, pk):
    from django.http import FileResponse
    from .models import Attachment
    a = get_object_or_404(Attachment, pk=pk)
    return FileResponse(a.file.open("rb"), as_attachment=True, filename=a.filename)


def evidence_view(request, pk):
    from django.http import FileResponse
    from payments.models import DisputeEvidence
    e = get_object_or_404(DisputeEvidence, pk=pk)
    if not e.file:
        raise Http404
    return FileResponse(e.file.open("rb"), as_attachment=True, filename=e.filename)


def file_view(request, pk):
    f = get_object_or_404(GeneratedFile, pk=pk)
    if request.GET.get("preview"):
        return render(request, "dialogs/file.html", {"f": f, "rows": htmx._rows(f.content)})
    resp = HttpResponse(f.content, content_type="text/csv; charset=utf-8")
    resp["Content-Disposition"] = f'attachment; filename="{f.filename}"'
    return resp


# ---------- sign-in ----------
@login_not_required
def login_view(request):
    if request.user.is_authenticated:
        return redirect(_home(request.user))
    err = ""
    if request.method == "POST" and settings.DEV_LOGIN:
        import hmac
        if settings.DEMO_PASSWORD and not hmac.compare_digest(request.POST.get("code", ""), settings.DEMO_PASSWORD):
            err = "That access code is not right."
        else:
            u = get_object_or_404(User, username=request.POST.get("user"), is_active=True)
            login(request, u, backend="django.contrib.auth.backends.ModelBackend")
            return redirect(request.GET.get("next") or _home(u))
    users = list(User.objects.filter(is_active=True).order_by("date_joined")) if settings.DEV_LOGIN else []
    return render(request, "registration/login.html", {"users": users, "oidc": settings.OIDC_ENABLED, "dev": settings.DEV_LOGIN,
                                                       "need_code": bool(settings.DEMO_PASSWORD), "err": err})


def _home(u):
    return {"action": "/action/", "pos": "/pos/", "ship": "/ship/", "promos": "/promos/", "pay": "/pay/",
            "dashboard": "/", "settings": "/settings/"}[ROLE_HOME.get(u.primary_role, "dashboard")]


@require_POST
def logout_view(request):
    logout(request)
    return redirect("/login/")


@require_POST
def dev_switch(request):
    if not settings.DEV_LOGIN:
        raise Http404
    u = get_object_or_404(User, username=request.POST.get("user"), is_active=True)
    login(request, u, backend="django.contrib.auth.backends.ModelBackend")
    resp = HttpResponse(status=204)
    resp["HX-Redirect"] = request.headers.get("HX-Current-URL") or "/"
    return resp


@login_not_required
def healthz(request):
    from django.db import connection
    with connection.cursor() as c:
        c.execute("select 1")
    return JsonResponse({"ok": True})


# ---------- settings ----------
def settings_page(request):
    require(request.user, "settings")
    tabs = [("skus", "SKU master"), ("prices", "Price list"), ("rules", "Rules & tolerances"), ("cats", "Categories"),
            ("matching", "Matching"), ("calendar", "Calendar"), ("fcs", "Amazon FCs"), ("vendors", "Vendor codes"), ("users", "Users & roles"), ("notify", "Notifications"), ("numbering", "Numbering")]
    tab = htmx.pick(request, "tab", [k for k, _ in tabs], "skus")
    ctx = {"tab": tab, "tabs": tabs}
    if tab in ("skus", "prices"):
        q = request.GET.get("q", "").strip()
        qs = Sku.objects.all()
        if q:
            qs = qs.filter(Q(sku_code__icontains=q) | Q(model_no__icontains=q) | Q(asin__icontains=q) | Q(description__icontains=q))
        from django.core.paginator import Paginator
        page = Paginator(qs, 25).get_page(request.GET.get("page"))
        ctx.update(page=page, q=q, total=Sku.objects.count(), prices={p.sku_id: p for p in _open_prices(page.object_list)},
                   cat_codes=list(CATEGORY_NAMES))
    if tab == "rules":
        ctx["rules"] = rule_rows()
    if tab == "matching":
        ctx.update(_matching_ctx())
    if tab == "calendar":
        from .models import Holiday
        ctx["holidays"] = Holiday.objects.filter(day__gte=timezone.localdate() - timedelta(days=60))
    if tab == "cats":
        ctx["cats"] = [dict(c=c, name=n, skus=Sku.objects.filter(category=c).count(), promos=Promotion.objects.filter(category=c).count(),
                            typical={"PA": 4, "DI": 6, "TV": 1, "HAV": 1, "Bundle": 1}[c]) for c, n in CATEGORY_NAMES.items()]
    if tab == "fcs":
        ctx["fcs"] = [(f, f.pos.exclude(stage__in=["paid", "rejected", "cancelled"]).count()) for f in FulfilmentCentre.objects.all()]
    if tab == "vendors":
        from .models import VendorCode
        ctx["vcodes"] = [(v, PurchaseOrder.objects.filter(vendor_code=v.code).exclude(stage__in=["paid", "rejected", "cancelled"]).count())
                         for v in VendorCode.objects.all()]
    if tab == "users":
        ctx.update(roles=list(ROLE_TITLES.items()), perms=[(PERM_LABELS[p], [r in rs for r in ROLE_TITLES]) for p, rs in PERMS.items()],
                   people=User.objects.filter(is_active=True).order_by("date_joined"))
    if tab == "notify":
        ctx["alerts"] = [("PO confirm deadline", "12 h and 2 h before confirm-by", "PIC", 1), ("Price or stock mismatch", "On import", "PIC", 1),
                         ("Order waiting for release", "Over 4 working hours", "Credit control", 1), ("No slot booked", "48 h before dispatch", "Logistics", 1),
                         ("Invoice blocked", "On check failure", "PIC", 0), ("Short payment", "On remittance import", "Finance, PIC", 1),
                         ("DN overdue", "15 days after end + 30 days", "PIC", 1), ("DN variance above tolerance", "On validation", "PIC", 1),
                         ("CN shortfall", "On CN record", "Product team, Finance", 1), ("Daily digest", "08:00 Riyadh time", "Everyone, by role", 0)]
    if tab == "numbering":
        ctx["numbers"] = [("MECL promotion ref", "MECL-PR-{YYYY}-{0000}", f"MECL-PR-2026-{peek_number('promotion', 137):04d}"),
                          ("Claim number", "CLM-{YYYY}-{0000}", f"CLM-2026-{peek_number('claim', 88):04d}"),
                          ("Dispute case", "DSP-{0000}", f"DSP-{peek_number('dispute', 41):04d}"),
                          ("ME invoice number", "MEI-{YYYY}-{00000}", f"MEI-2026-{peek_number('invoice', 4310):05d}")]
    return render(request, "pages/settings.html", ctx)


def _matching_ctx():
    """Settings, AI provider status and how suggestions have fared over the last 90 days."""
    from django.db.models import Count
    from matching.models import KINDS, MatchSettings, MatchSuggestion
    since = timezone.now() - timedelta(days=90)
    decided = MatchSuggestion.objects.filter(decided_at__gte=since, status__in=["accepted", "rejected", "auto"])
    by = {(r["kind"], r["method"], r["status"]): r["n"] for r in decided.values("kind", "method", "status").annotate(n=Count("id"))}
    stats = []
    for k, label in KINDS:
        for m, ml in (("rules", "Rules"), ("ai", "Claude")):
            acc = by.get((k, m, "accepted"), 0) + by.get((k, m, "auto"), 0)
            rej = by.get((k, m, "rejected"), 0)
            if acc + rej:
                stats.append(dict(kind=label, method=ml, accepted=acc, rejected=rej, rate=round(acc / (acc + rej) * 100)))
    from matching.ai import connection
    conn = {k: v for k, v in connection().items() if k != "key"}  # the key itself never reaches a template
    return dict(mcfg=MatchSettings.get(), conn=conn, mstats=stats, pending_n=MatchSuggestion.objects.filter(status="pending").count())


def _open_prices(skus):
    from catalog.models import Price
    return Price.objects.filter(sku__in=list(skus), valid_to__isnull=True)


@require_POST
def sku_save(request):
    from catalog.services import upsert_sku
    P = request.POST
    s, new = upsert_sku(request.user, P.get("sku_code"), P.get("model_no"), P.get("asin"), P.get("category"), P.get("ean"),
                        P.get("description"), P.get("case_pack"))
    return htmx.done(request, f"SKU {s.sku_code} {'added' if new else 'updated'}", drawer=False)


@require_POST
def price_save(request):
    from catalog.services import set_price
    s = set_price(request.user, request.POST.get("sku_code"), request.POST.get("cost"), request.POST.get("valid_from", ""))
    return htmx.done(request, f"Agreed cost for {s.model_no} saved. POs waiting for confirmation re-checked", drawer=False)


@require_POST
def holiday_add(request):
    from datetime import date
    from .models import Holiday
    from .services import audit
    from .workcal import clear_cache
    require(request.user, "settings")
    try:
        day = date.fromisoformat(request.POST.get("day", ""))
    except ValueError:
        raise CommandError("Pick the holiday's date.")
    name = request.POST.get("name", "").strip()[:80]
    if not name:
        raise CommandError("Give the holiday a name, e.g. Eid al-Fitr.")
    Holiday.objects.update_or_create(day=day, defaults={"name": name})
    clear_cache()
    audit("settings", "calendar", f"Holiday added: {name} ({day:%d %b %Y})", request.user, action="configure")
    return htmx.done(request, f"{name} added. Deadlines skip {day:%d %b %Y}", drawer=False)


@require_POST
def holiday_remove(request, pk):
    from .models import Holiday
    from .services import audit
    from .workcal import clear_cache
    require(request.user, "settings")
    h = get_object_or_404(Holiday, pk=pk)
    audit("settings", "calendar", f"Holiday removed: {h.name} ({h.day:%d %b %Y})", request.user, action="configure")
    h.delete()
    clear_cache()
    return htmx.done(request, "Holiday removed", "info", drawer=False)


@require_POST
def fc_add(request):
    from catalog.services import add_fc
    fc = add_fc(request.user, request.POST.get("code"), request.POST.get("name"), request.POST.get("city"))
    return htmx.done(request, f"FC {fc.code} added", drawer=False)


@require_POST
def vendor_code_add(request):
    import re as _re
    from .models import VendorCode
    from .services import audit, require
    require(request.user, "settings")
    code = request.POST.get("code", "").strip().upper()
    if not _re.fullmatch(r"[A-Z0-9]{3,12}", code):
        raise CommandError("A vendor code is 3–12 letters or digits, as in Vendor Central (e.g. MODEL).")
    if VendorCode.objects.filter(code=code).exists():
        raise CommandError(f"Vendor code {code} is already listed.")
    VendorCode.objects.create(code=code, name=request.POST.get("name", "").strip()[:120])
    audit("settings", "vendor_codes", f"Vendor code {code} added", request.user, action="configure")
    return htmx.done(request, f"Vendor code {code} added", drawer=False)


@require_POST
def rule_update(request, rule_id):
    f = request.POST.get("field")
    if f == "enabled":
        update_rule(request.user, rule_id, enabled=request.POST.get("value") in ("on", "true", "1"))
    else:
        update_rule(request.user, rule_id, **{f: request.POST.get("value") or 0})
    return htmx.done(request, f"{rule_id} updated. Open records are re-checked.", refresh=False, drawer=False)
