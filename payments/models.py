from django.db import models
from django.utils import timezone

from billing.models import Invoice
from core.models import Base
from orders.models import PurchaseOrder

PAYMENT_STATUS = [("imported", "Imported"), ("matched", "Matched"), ("short", "Short-paid"),
                  ("unmatched", "To match"), ("accepted", "Deduction accepted"), ("disputed", "Disputed"),
                  ("recovered", "Recovered")]
DISPUTE_TYPES = [("shortage", "Shortage"), ("price", "Price"), ("promo", "Promo deduction"), ("damage", "Damage"),
                 ("chargeback", "Chargeback"), ("returns", "Returns (RTV)"), ("coop", "Co-op / advertising"), ("other", "Other")]
# Amazon's operational chargebacks (Vendor Central → Chargebacks); each has its own evidence
CHARGEBACK_TYPES = [("asn_accuracy", "ASN accuracy"), ("asn_ontime", "ASN on-time"), ("labels", "Carton / pallet labels"),
                    ("po_ontime", "PO on-time accuracy"), ("prep", "Prep / packaging"), ("overweight", "Overweight / oversize carton"),
                    ("appointment", "Missed or late appointment"), ("other", "Other chargeback")]
EVIDENCE_HINTS = {
    "shortage": "Proof of delivery (signed POD / GRN), carrier receipt, ASN.",
    "price": "Agreed price list valid on the PO date, the PO and the invoice.",
    "promo": "The promotion agreement and the validated debit note.",
    "damage": "Carrier receipt showing goods received in good condition, packing photos.",
    "chargeback": "ASN submission time, carton label file, appointment confirmation — whatever the chargeback type says failed.",
    "returns": "The return authorisation (RTV) and what was actually received back.",
    "coop": "The co-op / advertising agreement and its agreed percentage or amount.",
    "other": "Anything that shows the invoice was correct.",
}
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
    vendor_code = models.CharField(max_length=12, blank=True, db_index=True)

    class Meta:
        ordering = ["-remit_date"]


class Dispute(Base):
    case_no = models.CharField(max_length=20, unique=True)
    type = models.CharField(max_length=10, choices=DISPUTE_TYPES)
    subtype = models.CharField(max_length=20, blank=True, choices=CHARGEBACK_TYPES, help_text="Chargeback type")
    ref = models.CharField(max_length=40, help_text="Payment or debit note number")
    po = models.ForeignKey(PurchaseOrder, null=True, blank=True, on_delete=models.SET_NULL)
    promotion = models.ForeignKey("promotions.Promotion", null=True, blank=True, on_delete=models.SET_NULL)
    amount_h = models.BigIntegerField()
    status = models.CharField(max_length=10, choices=DISPUTE_STATUS, default="open")
    due = models.DateTimeField(default=timezone.now)
    note = models.TextField(blank=True)
    amazon_case_id = models.CharField(max_length=40, blank=True, help_text="Case ID from Vendor Central")
    recovered_h = models.BigIntegerField(null=True, blank=True, help_text="Amount Amazon gave back (may be part of the claim)")
    closed_at = models.DateTimeField(null=True, blank=True, help_text="When it was won or lost")
    recovered_in = models.CharField(max_length=30, blank=True, help_text="The later payment that brought the money back")

    class Meta:
        ordering = ["-created_at"]


class DisputeEvidence(Base):
    dispute = models.ForeignKey(Dispute, on_delete=models.CASCADE, related_name="evidence")
    filename = models.CharField(max_length=200)
    file = models.FileField(upload_to="evidence/%Y/%m/", null=True, blank=True)
