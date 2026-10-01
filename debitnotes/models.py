from django.db import models

from catalog.models import Sku
from core.models import Base


class DebitNote(Base):
    dn_no = models.CharField(max_length=30, unique=True)
    agreement_no = models.CharField(max_length=20, db_index=True, help_text="As received from Amazon")
    dn_date = models.DateTimeField()
    validated = models.BooleanField(default=False)
    validated_at = models.DateTimeField(null=True, blank=True)
    approved_h = models.BigIntegerField(default=0)
    disputed_h = models.BigIntegerField(default=0)
    override_reason = models.TextField(blank=True)

    class Meta:
        ordering = ["-dn_date"]

    def __str__(self):
        return self.dn_no


class DnLine(Base):
    dn = models.ForeignKey(DebitNote, on_delete=models.CASCADE, related_name="lines")
    sku = models.ForeignKey(Sku, on_delete=models.PROTECT, null=True, blank=True, help_text="Empty for a fixed-fee line")
    label = models.CharField(max_length=80, blank=True, help_text="Fixed-fee lines: what the fee is")
    units = models.IntegerField()
    rate_h = models.BigIntegerField()

    class Meta:
        ordering = ["created_at"]  # creation order: stable across databases (ids are random UUIDs)

    @property
    def charged_h(self):
        return self.units * self.rate_h
