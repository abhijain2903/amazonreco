"""Shared helpers for commands: errors, audit, notifications, numbering, files."""
import csv
import io
import re
from decimal import ROUND_HALF_UP, Decimal

from django.db import transaction
from django.db.models import F
from django.utils import timezone

from .models import AuditEvent, Counter, GeneratedFile, Notification


class CommandError(Exception):
    """A business rule stopped the command. The message is shown to the user."""

    status = 400


class Forbidden(CommandError):
    status = 403


class StaleRecord(CommandError):
    status = 409

    def __init__(self, msg="Someone changed this record while you were working. It has been refreshed."):
        super().__init__(msg)


# ---------- money (integer halalas) ----------
def to_h(value) -> int:
    """SAR amount (str/float/Decimal) -> integer halalas, rounded half up."""
    if value is None or value == "":
        return 0
    d = Decimal(str(value).replace(",", "").replace("SAR", "").strip())
    return int((d * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def from_h(h) -> Decimal:
    return (Decimal(h or 0) / 100).quantize(Decimal("0.01"))


def vat_h(net_h: int, rate=Decimal("0.15")) -> int:
    return int((Decimal(net_h) * rate).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def fmt_sar(h) -> str:
    return f"SAR {round((h or 0) / 100):,}"


# ---------- people ----------
def actor_name(user):
    if user is None:
        return "System"
    return getattr(user, "display_name", "") or user.get_username()


def require(user, perm):
    from identity.permissions import can, who_can

    if not can(user, perm):
        raise Forbidden(f"This needs the {who_can(perm)} role.")


def check_version(obj, version):
    if version is not None and str(version) != "" and int(version) != obj.version:
        raise StaleRecord()


# ---------- audit & notifications ----------
def audit(entity, entity_id, text, user=None, *, action="", system=False, name=None,
          before=None, after=None, reason="", at=None):
    return AuditEvent.objects.create(
        entity=entity, entity_id=str(entity_id), action=action, text=text,
        actor=user if (user is not None and getattr(user, "pk", None)) else None,
        actor_name=name or actor_name(user), system=system, before=before, after=after,
        reason=reason, at=at or timezone.now(),
    )


# Who an alert is for, from what it links to: (record type, tab) → roles. Admins and managers see everything.
ALERT_ROLES = {("po", "lines"): ["PIC"], ("po", "shipment"): ["Logistics", "PIC"], ("po", "invoice"): ["Finance", "PIC"],
               ("po", ""): ["PIC"], ("promo", "claim"): ["Product", "Finance"], ("promo", "dn"): ["PIC", "Finance"],
               ("promo", ""): ["Product", "PIC"], ("dn", ""): ["PIC", "Finance"], ("dispute", ""): ["Finance", "PIC"], ("rtv", ""): ["PIC", "Finance", "Logistics"]}


def notify(text, tone="info", link=None, at=None, roles=None, user=None):
    """An in-app alert. Routed to the roles that own the linked work (or to one person for mentions and
    assignments); the person assigned to the record also gets it."""
    lt, li, tab = (list(link) + ["", "", ""])[:3] if link else ("", "", "")
    if roles is None and user is None:
        roles = ALERT_ROLES.get((lt, tab or ""), ALERT_ROLES.get((lt, ""), []))
    n = Notification.objects.create(text=text, tone=tone, link_type=lt, link_id=str(li), link_tab=tab or "",
                                    at=at or timezone.now(), roles=roles or [], user=user)
    if lt and user is None:
        from .models import Assignment
        a = Assignment.objects.filter(entity=lt, entity_id=str(li)).select_related("user").first()
        if a and not (set(a.user.roles or []) & set(roles or [])):
            Notification.objects.create(text=text, tone=tone, link_type=lt, link_id=str(li), link_tab=tab or "", at=n.at, user=a.user)
    return n


def visible_alerts(user):
    """Alerts this person sees: their own (mentions, assignments) plus those for their roles — or everything if they
    chose that, or are an admin / manager."""
    from django.db.models import Q
    qs = Notification.objects.all()
    if user.alert_scope == "all" or set(user.roles or []) & {"Admin", "Manager"} or user.is_superuser:
        return qs.filter(Q(user__isnull=True) | Q(user=user))
    return qs.filter(Q(user=user) | Q(user__isnull=True, roles=[]) | Q(user__isnull=True, roles__overlap=list(user.roles or [])))


def unread_alerts(user):
    return visible_alerts(user).filter(read=False).exclude(read_by=user)


MENTION = re.compile(r"@([A-Za-z][\w.-]{1,30})")


def mentioned_users(text):
    """@username or @firstname (case-insensitive) → active users."""
    from identity.models import User
    out = []
    for tok in dict.fromkeys(m.lower().rstrip(".") for m in MENTION.findall(text or "")):
        u = (User.objects.filter(is_active=True, username__iexact=tok).first()
             or User.objects.filter(is_active=True, first_name__iexact=tok).first()
             or User.objects.filter(is_active=True, display_name__istartswith=tok + " ").first())
        if u and u not in out:
            out.append(u)
    return out


@transaction.atomic
def assign(user, entity, key, to_username):
    """Give a record to one person (or back to the role with an empty name). Anyone who can work on records can."""
    from identity.models import User
    from .models import Assignment
    if not user.roles and not user.is_superuser:
        raise CommandError("Only team members can assign work.")
    if not to_username:
        Assignment.objects.filter(entity=entity, entity_id=key).delete()
        audit(AUDIT_ENTITY.get(entity, entity), key, "Unassigned: back to the role", user, action="assign")
        return None
    to = User.objects.filter(username=to_username, is_active=True).first()
    if not to:
        raise CommandError("Pick a person.")
    Assignment.objects.update_or_create(entity=entity, entity_id=key, defaults=dict(user=to, by_name=user.name))
    audit(AUDIT_ENTITY.get(entity, entity), key, f"Assigned to {to.name}", user, action="assign")
    if to != user:
        notify(f"{user.name} assigned {LABEL.get(entity, entity)} {key} to you", "info", (entity, key, ""), user=to)
    return to


AUDIT_ENTITY = {"promo": "promotion"}
LABEL = {"po": "PO", "promo": "promotion", "dispute": "dispute", "dn": "debit note", "rtv": "return"}


def timeline(entity, *ids):
    return AuditEvent.objects.filter(entity=entity, entity_id__in=[str(i) for i in ids]).order_by("-at", "-id")


# ---------- numbering ----------
def next_number(key, start=0):
    with transaction.atomic():
        c, _ = Counter.objects.select_for_update().get_or_create(key=key, defaults={"value": start})
        Counter.objects.filter(pk=c.pk).update(value=F("value") + 1)
        c.refresh_from_db()
        return c.value


def peek_number(key, start=0):
    c = Counter.objects.filter(key=key).first()
    return (c.value if c else start) + 1


# ---------- generated files ----------
def safe_cell(v):
    """Guard against CSV formula injection when a file is opened in Excel."""
    s = "" if v is None else str(v)
    return "'" + s if s[:1] in ("=", "+", "-", "@") and not _is_number(s) else s


def _is_number(s):
    try:
        float(s)
        return True
    except ValueError:
        return False


def make_csv(rows):
    buf = io.StringIO()
    w = csv.writer(buf)
    for r in rows:
        w.writerow([safe_cell(v) for v in r])
    return buf.getvalue()


def save_file(kind, filename, rows, entity, entity_id):
    return GeneratedFile.objects.create(kind=kind, filename=filename, content=make_csv(rows),
                                        entity=entity, entity_id=str(entity_id))
