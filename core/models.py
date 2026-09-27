import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone


class Base(models.Model):
    """Common columns: UUID key, timestamps and a version for optimistic locking."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    version = models.PositiveIntegerField(default=1)

    class Meta:
        abstract = True

    def bump(self):
        self.version += 1


class AuditEvent(models.Model):
    """Append-only log of every command. Also feeds the record timelines.

    A database trigger (migration 0002) blocks UPDATE and DELETE on this table.
    """

    entity = models.CharField(max_length=30, db_index=True)
    entity_id = models.CharField(max_length=64, db_index=True)
    action = models.CharField(max_length=60, blank=True)
    text = models.TextField()
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    actor_name = models.CharField(max_length=120)
    system = models.BooleanField(default=False)
    before = models.JSONField(null=True, blank=True)
    after = models.JSONField(null=True, blank=True)
    reason = models.TextField(blank=True)
    request_id = models.CharField(max_length=64, blank=True)
    at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ["-at", "-id"]
        indexes = [models.Index(fields=["entity", "entity_id", "at"])]


class Note(Base):
    entity = models.CharField(max_length=30)
    entity_id = models.CharField(max_length=64)
    text = models.TextField()
    author_name = models.CharField(max_length=120)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["entity", "entity_id"])]


class Notification(models.Model):
    TONES = [("info", "Info"), ("ok", "OK"), ("warn", "Warning"), ("bad", "Problem")]
    text = models.TextField()
    tone = models.CharField(max_length=8, choices=TONES, default="info")
    link_type = models.CharField(max_length=20, blank=True)
    link_id = models.CharField(max_length=64, blank=True)
    link_tab = models.CharField(max_length=20, blank=True)
    read = models.BooleanField(default=False)
    at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ["-at", "-id"]


class Counter(models.Model):
    """Named number sequences for document numbers (MECL refs, claims, cases...)."""

    key = models.CharField(max_length=40, unique=True)
    value = models.BigIntegerField(default=0)


class GeneratedFile(Base):
    """Outbound files (PO acknowledgement, ASN, invoice, promotion, claim)."""

    kind = models.CharField(max_length=30)
    filename = models.CharField(max_length=200)
    content = models.TextField()
    entity = models.CharField(max_length=30)
    entity_id = models.CharField(max_length=64)

    class Meta:
        ordering = ["-created_at"]
