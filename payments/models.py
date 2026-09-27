from django.db import models
from django.utils import timezone

from billing.models import Invoice
from core.models import Base
from orders.models import PurchaseOrder

PAYMENT_STATUS = [("imported", "Imported"), ("matched", "Matched"), ("short", "Short-paid"),
                  ("unmatched", "To match"), ("accepted", "Deduction accepted"), ("disputed", "Disputed"),
                  ("recovered", "Recovered")]
DISPUTE_TYPES = [("shortage", "Shortage"), ("price", "Price"), ("promo", "Promo deduction"),
                 ("damage", "Damage"), ("other", "Other")]
DISPUTE_STATUS = [("open", "Open"), ("submitted", "With Amazon"), ("won", "Won"), ("lost", "Lost")]


class Payment(Base):
    payment_no = models.CharField(max_length=30, unique=True)
    remit_date = models.DateTimeField()
    invoice_ref = models.CharField(max_length=40, help_text="Invoice number exactly as Amazon sent it")
    invoice = models.ForeignKey(Invoice, null=True, blank=True, on_delete=models.SET_NULL, related_name="payments")
    po = models.ForeignKey(PurchaseOrder, null=True, blank=True, on_delete=models.SET_NULL, related_name="payments")
    paid_h = models.BigIntegerField()
    deduction_h = models.BigIntegerField(default=0)
    reason = models.CharField(max_length=200, blank=True)
    status = models.CharField(max_length=10, choices=PAYMENT_STATUS, default="imported", db_index=True)
    hint = models.CharField(max_length=40, blank=True)

    class Meta:
        ordering = ["-remit_date"]


class Dispute(Base):
    case_no = models.CharField(max_length=20, unique=True)
    type = models.CharField(max_length=10, choices=DISPUTE_TYPES)
    ref = models.CharField(max_length=40, help_text="Payment or debit note number")
    po = models.ForeignKey(PurchaseOrder, null=True, blank=True, on_delete=models.SET_NULL)
    promotion = models.ForeignKey("promotions.Promotion", null=True, blank=True, on_delete=models.SET_NULL)
    amount_h = models.BigIntegerField()
    status = models.CharField(max_length=10, choices=DISPUTE_STATUS, default="open")
    due = models.DateTimeField(default=timezone.now)
    note = models.TextField(blank=True)

    class Meta:
        ordering = ["-created_at"]


class DisputeEvidence(Base):
    dispute = models.ForeignKey(Dispute, on_delete=models.CASCADE, related_name="evidence")
    filename = models.CharField(max_length=200)
    file = models.FileField(upload_to="evidence/%Y/%m/", null=True, blank=True)
