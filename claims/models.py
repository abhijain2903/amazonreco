from django.db import models

from core.models import Base
from debitnotes.models import DebitNote
from promotions.models import Promotion

CLAIM_STATUS = [("sent", "Waiting for CN"), ("closed", "Closed"), ("shortfall", "CN shortfall"),
                ("written_off", "Written off")]


class Claim(Base):
    """Sell-out claim to the product team, and the credit note that settles it."""

    claim_no = models.CharField(max_length=20, unique=True)
    promotion = models.ForeignKey(Promotion, on_delete=models.PROTECT, related_name="claims")
    debit_note = models.ForeignKey(DebitNote, on_delete=models.PROTECT, related_name="claims")  # one claim per debit note
    amount_h = models.BigIntegerField()
    sent_at = models.DateTimeField()
    status = models.CharField(max_length=12, choices=CLAIM_STATUS, default="sent", db_index=True)
    cn_no = models.CharField(max_length=30, blank=True)
    cn_h = models.BigIntegerField(null=True, blank=True)
    cn_date = models.DateTimeField(null=True, blank=True)
    batch_no = models.CharField(max_length=20, blank=True, db_index=True, help_text="Claims sent together in one batch")

    class Meta:
        ordering = ["-sent_at"]

    @property
    def gap_h(self):
        return None if self.cn_h is None else self.amount_h - self.cn_h


class CreditNote(Base):
    """One credit note from the product team. A claim can be settled by several (e.g. a top-up after a shortfall)."""

    claim = models.ForeignKey(Claim, on_delete=models.CASCADE, related_name="credit_notes")
    cn_no = models.CharField(max_length=30)
    amount_h = models.BigIntegerField()
    cn_date = models.DateTimeField()

    class Meta:
        ordering = ["created_at"]
        constraints = [models.UniqueConstraint(fields=["claim", "cn_no"], name="uniq_cn_per_claim")]
