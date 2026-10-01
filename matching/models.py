from django.conf import settings
from django.db import models

from core.models import Base

KINDS = [("pay_inv", "Payment → invoice"), ("pay_dn", "Short payment → debit note"), ("dn_promo", "Debit note → promotion"),
         ("deduction", "Deduction reason")]
STATUS = [("pending", "To review"), ("accepted", "Accepted"), ("rejected", "Rejected"), ("auto", "Applied automatically"),
          ("superseded", "Superseded")]


class MatchSuggestion(Base):
    """One proposed match for one record, with its score and reasons. Kept after a decision: accepted and rejected
    suggestions are the feedback that later suggestions learn from (a rejected pair is never proposed again)."""

    kind = models.CharField(max_length=10, choices=KINDS)
    source = models.CharField(max_length=40, help_text="Payment or debit note number")
    targets = models.JSONField(default=list, help_text="Invoice numbers, DN number, MECL ref, or a deduction type")
    label = models.CharField(max_length=200)
    score = models.PositiveSmallIntegerField()
    reasons = models.JSONField(default=list)
    method = models.CharField(max_length=5, default="rules", help_text="rules, or ai when Claude picked it")
    ai_model = models.CharField(max_length=60, blank=True)
    ai_rationale = models.TextField(blank=True)
    extra = models.JSONField(default=dict, blank=True, help_text="e.g. recommended action and draft note for a deduction")
    status = models.CharField(max_length=10, choices=STATUS, default="pending", db_index=True)
    decided_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    decided_by_name = models.CharField(max_length=120, blank=True)
    decided_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-score", "created_at"]
        indexes = [models.Index(fields=["kind", "source", "status"])]


class MatchSettings(Base):
    """Single row. The AI provider itself is configured on the server (HUB_AI_PROVIDER); this is what admins tune."""

    auto_apply = models.BooleanField(default=False, help_text="Apply a payment → invoice match without review")
    auto_threshold = models.PositiveSmallIntegerField(default=95)
    show_threshold = models.PositiveSmallIntegerField(default=50)
    ai_enabled = models.BooleanField(default=True)
    # Connection set in the app (used only when the server's HUB_AI_PROVIDER is "off"). The key is write-only:
    # stored encrypted (matching/secrets.py), shown back only as its last 4 characters.
    ai_provider = models.CharField(max_length=10, default="off",
                                   choices=[("off", "Not connected"), ("anthropic", "Anthropic API"), ("openai", "OpenAI"),
                                            ("bedrock", "Amazon Bedrock")])
    ai_model = models.CharField(max_length=80, blank=True, help_text="Model name; required for OpenAI, optional otherwise")
    api_key_enc = models.TextField(blank=True)
    api_key_for = models.CharField(max_length=10, blank=True, help_text="Provider the saved key belongs to")
    api_key_hint = models.CharField(max_length=8, blank=True)
    api_key_set_at = models.DateTimeField(null=True, blank=True)
    api_key_set_by = models.CharField(max_length=120, blank=True)

    @classmethod
    def get(cls):
        return cls.objects.order_by("created_at").first() or cls.objects.create()
