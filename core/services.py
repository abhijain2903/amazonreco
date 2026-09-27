"""Shared helpers for commands: errors, audit, notifications, numbering, files."""
import csv
import io
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


def notify(text, tone="info", link=None, at=None):
    lt, li, tab = (list(link) + ["", "", ""])[:3] if link else ("", "", "")
    return Notification.objects.create(text=text, tone=tone, link_type=lt, link_id=str(li),
                                       link_tab=tab or "", at=at or timezone.now())


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
