"""Template helpers: money, dates, chips, icons and permission-aware buttons."""
from datetime import timedelta

from django import template
from django.utils import timezone
from django.utils.html import escape, format_html
from django.utils.safestring import mark_safe

register = template.Library()

ICONS = {
    "home": '<path d="M3 10.5 12 3l9 7.5V20a1 1 0 0 1-1 1h-5v-6H9v6H4a1 1 0 0 1-1-1z"/>',
    "inbox": '<path d="M22 12h-6l-2 3h-4l-2-3H2"/><path d="M5.45 5.11 2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z"/>',
    "po": '<rect x="8" y="2" width="8" height="4" rx="1"/><path d="M16 4h2a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h2"/><path d="M9 12h6M9 16h4"/>',
    "truck": '<path d="M1 4h14v12H1z"/><path d="M15 8h4l3 3v5h-7z"/><circle cx="5.5" cy="18.5" r="2.2"/><circle cx="18.5" cy="18.5" r="2.2"/>',
    "wallet": '<path d="M20 7V5a2 2 0 0 0-2-2H5a2 2 0 0 0 0 4h15a1 1 0 0 1 1 1v4h-3a2 2 0 0 0 0 4h3v4a1 1 0 0 1-1 1H5a2 2 0 0 1-2-2V5"/>',
    "tag": '<path d="M12.6 2.6A2 2 0 0 0 11.2 2H4a2 2 0 0 0-2 2v7.2a2 2 0 0 0 .6 1.4l8.7 8.7a2.4 2.4 0 0 0 3.4 0l6.6-6.6a2.4 2.4 0 0 0 0-3.4z"/><circle cx="7.5" cy="7.5" r="1.5"/>',
    "receipt": '<path d="M4 2v20l3-2 3 2 3-2 3 2 3-2V2l-3 2-3-2-3 2-3-2-3 2z"/><path d="M8 8h8M8 12h8M8 16h5"/>',
    "claim": '<path d="M9 14 4 9l5-5"/><path d="M4 9h11a5 5 0 0 1 0 10h-3"/>',
    "upload": '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="m17 8-5-5-5 5"/><path d="M12 3v12"/>',
    "download": '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="m7 10 5 5 5-5"/><path d="M12 15V3"/>',
    "plug": '<path d="M12 22v-5"/><path d="M9 8V2M15 8V2"/><path d="M18 8v5a4 4 0 0 1-4 4h-4a4 4 0 0 1-4-4V8z"/>',
    "sliders": '<path d="M4 21v-7M4 10V3M12 21v-9M12 8V3M20 21v-5M20 12V3M1 14h6M9 8h6M17 16h6"/>',
    "search": '<circle cx="11" cy="11" r="7"/><path d="m21 21-4.3-4.3"/>',
    "bell": '<path d="M6 8a6 6 0 0 1 12 0c0 7 3 9 3 9H3s3-2 3-9"/><path d="M10.3 21a1.94 1.94 0 0 0 3.4 0"/>',
    "x": '<path d="M18 6 6 18M6 6l12 12"/>',
    "check": '<path d="M20 6 9 17l-5-5"/>',
    "alert": '<path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"/><path d="M12 9v4M12 17h.01"/>',
    "info": '<circle cx="12" cy="12" r="9"/><path d="M12 16v-4M12 8h.01"/>',
    "chev": '<path d="m9 18 6-6-6-6"/>',
    "chevl": '<path d="m15 18-6-6 6-6"/>',
    "plus": '<path d="M12 5v14M5 12h14"/>',
    "file": '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/>',
    "copy": '<rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>',
    "clock": '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
    "menu": '<path d="M4 6h16M4 12h16M4 18h16"/>',
    "board": '<rect x="3" y="3" width="7" height="18" rx="1"/><rect x="14" y="3" width="7" height="11" rx="1"/>',
    "chart": '<path d="M3 3v18h18"/><path d="m7 15 4-4 3 3 5-6"/>',
    "table": '<rect x="3" y="3" width="18" height="18" rx="2"/><path d="M3 9h18M3 15h18M9 3v18"/>',
    "cal": '<rect x="3" y="4" width="18" height="18" rx="2"/><path d="M16 2v4M8 2v4M3 10h18"/>',
    "refresh": '<path d="M21 12a9 9 0 1 1-3-6.7L21 8"/><path d="M21 3v5h-5"/>',
    "link": '<path d="M10 13a5 5 0 0 0 7.5.5l3-3a5 5 0 0 0-7-7l-1.7 1.7"/><path d="M14 11a5 5 0 0 0-7.5-.5l-3 3a5 5 0 0 0 7 7l1.7-1.7"/>',
    "arrow": '<path d="M5 12h14M12 5l7 7-7 7"/>',
    "zap": '<path d="M13 2 3 14h9l-1 8 10-12h-9z"/>',
    "box": '<path d="M21 8 12 3 3 8v8l9 5 9-5z"/><path d="m3 8 9 5 9-5M12 13v8"/>',
    "send": '<path d="m22 2-7 20-4-9-9-4z"/><path d="M22 2 11 13"/>',
    "user": '<circle cx="12" cy="8" r="4"/><path d="M4 21a8 8 0 0 1 16 0"/>',
    "logout": '<path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><path d="m16 17 5-5-5-5M21 12H9"/>',
}
CAT_COLORS = {"PA": "var(--c-pa)", "DI": "var(--c-di)", "TV": "var(--c-tv)", "HAV": "var(--c-hav)", "Bundle": "var(--c-bd)"}


@register.simple_tag
def icon(name, cls=""):
    return mark_safe(f'<svg class="i {escape(cls)}" viewBox="0 0 24 24" aria-hidden="true">{ICONS.get(name, "")}</svg>')


# ---------- numbers ----------
@register.filter
def sar(h):
    if h is None or h == "":
        return "—"
    return f"SAR {round(h / 100):,}"


@register.filter
def n(h):
    """Halalas -> whole SAR with thousands separators."""
    if h is None or h == "":
        return "—"
    return f"{round(h / 100):,}"


@register.filter
def n2(h):
    if h is None or h == "":
        return "—"
    return f"{h / 100:,.2f}"


@register.filter
def num(v):
    if v is None or v == "":
        return "—"
    try:
        return f"{round(v):,}"
    except TypeError:
        return v


@register.filter
def signed(h):
    if h is None:
        return "—"
    s = f"{round(h / 100):,}"
    return ("+" if h > 0 else "") + s


# ---------- dates ----------
def _lt(dt):
    return timezone.localtime(dt) if timezone.is_aware(dt) else dt


@register.filter
def fd(dt):
    if not dt:
        return "—"
    d = _lt(dt)
    return f"{d.day} {d:%b}"


@register.filter
def fdy(dt):
    if not dt:
        return "—"
    d = _lt(dt)
    return f"{d.day} {d:%b %Y}"


@register.filter
def fdt(dt):
    if not dt:
        return "—"
    d = _lt(dt)
    return f"{d.day} {d:%b}, {d:%H:%M}"


@register.filter
def isodate(dt):
    return _lt(dt).date().isoformat() if dt else ""


def span(td):
    s = abs(td.total_seconds())
    if s < 3600:
        return f"{max(1, round(s / 60))} min"
    if s < 48 * 3600:
        return f"{round(s / 3600)} h"
    return f"{round(s / 86400)} days"


@register.filter
def due(dt):
    if not dt:
        return ""
    now = timezone.now()
    return f"in {span(dt - now)}" if dt >= now else f"{span(now - dt)} overdue"


@register.filter
def ago(dt):
    if not dt:
        return "never"
    now = timezone.now()
    return f"in {span(dt - now)}" if dt > now else f"{span(now - dt)} ago"


@register.filter
def past(dt):
    return bool(dt and dt < timezone.now())


@register.filter
def within_hours(dt, hrs):
    return bool(dt and timedelta(0) <= dt - timezone.now() < timedelta(hours=float(hrs)))


# ---------- chips & badges ----------
@register.simple_tag
def chip(label, tone=""):
    return format_html('<span class="chip {}">{}</span>', tone, label)


@register.simple_tag
def cat(code):
    return format_html('<span class="catb"><i style="background:{}"></i>{}</span>', CAT_COLORS.get(code, "var(--faint)"), code)


@register.simple_tag
def po_chip(po):
    from billing.services import invoice_blocked
    from fulfilment.services import slot_at_risk
    from orders.models import STAGE_LABELS, STAGE_TONES
    if po.stage == "new" and po.confirm_by < timezone.now():
        return chip("Overdue", "bad")
    if po.stage == "invoiced":
        sts = set(po.payments.values_list("status", flat=True))
        if "disputed" in sts:
            return chip("Disputed", "bad")
        if "short" in sts:
            return chip("Short-paid", "warn")
    if po.stage == "delivered" and invoice_blocked(po):
        return chip("Invoice blocked", "bad")
    if po.stage == "asn" and slot_at_risk(po):
        return chip("Slot needed", "warn")
    return chip(STAGE_LABELS.get(po.stage, po.stage), STAGE_TONES.get(po.stage, ""))


@register.simple_tag
def promo_chip(p, st=None):
    from promotions.models import STAGE_LABELS, STAGE_TONES
    from promotions.services import stage_of
    st = st or stage_of(p)
    return chip(STAGE_LABELS[st], STAGE_TONES[st])


@register.simple_tag(takes_context=True)
def perm(context, name):
    """Adds `disabled` and a tooltip to a button when the user lacks the permission."""
    from identity.permissions import can, who_can
    if can(context["request"].user, name):
        return ""
    return format_html(' disabled title="Needs the {} role."', who_can(name))


@register.filter
def get(d, key):
    try:
        return d.get(key)
    except AttributeError:
        return None


@register.filter
def mul(a, b):
    return (a or 0) * (b or 0)


@register.filter
def pct(a, b):
    return 0 if not b else round(a / b * 100, 1)


@register.simple_tag(takes_context=True)
def nav_menu(context, path=None):
    from core.nav import build
    req = context["request"]
    return build(req.user, path or req.path)


@register.simple_tag(takes_context=True)
def page_title(context):
    from core.nav import TITLES, section_for
    return TITLES.get(section_for(context["request"].path), "")


@register.filter
def catcolor(code):
    return CAT_COLORS.get(code, "var(--faint)")


@register.filter
def catname(code):
    from catalog.models import CATEGORY_NAMES
    return CATEGORY_NAMES.get(code, code)
