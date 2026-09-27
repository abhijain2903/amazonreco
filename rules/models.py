from django.db import models
from django.utils import timezone

from core.models import Base


class RuleConfig(Base):
    """Settings for rules R1-R12. Every change bumps `version`; check results keep the version they ran with."""

    rule_id = models.CharField(max_length=4, unique=True)
    name = models.CharField(max_length=60)
    description = models.CharField(max_length=160)
    enabled = models.BooleanField(default=True)
    params = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["id"]


class CheckResult(models.Model):
    entity = models.CharField(max_length=20)
    entity_id = models.CharField(max_length=64)
    rule_id = models.CharField(max_length=4)
    subject = models.CharField(max_length=80, blank=True)
    passed = models.BooleanField(null=True)
    expected = models.CharField(max_length=60, blank=True)
    actual = models.CharField(max_length=60, blank=True)
    gap = models.CharField(max_length=60, blank=True)
    config_version = models.PositiveIntegerField(default=1)
    run_at = models.DateTimeField(default=timezone.now)

    class Meta:
        indexes = [models.Index(fields=["entity", "entity_id"])]
