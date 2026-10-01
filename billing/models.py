from django.db import models

from catalog.models import Sku
from core.models import Base
from orders.models import PurchaseOrder


class SapBilling(Base):
    po = models.ForeignKey(PurchaseOrder, on_delete=models.CASCADE, related_name="sap_billings")
    seq = models.PositiveIntegerField(default=1)
    shipment = models.ForeignKey("fulfilment.Shipment", null=True, blank=True, on_delete=models.SET_NULL, related_name="billings")
    billing_no = models.CharField(max_length=20)
    received_at = models.DateTimeField()


class SapBillingLine(Base):
    billing = models.ForeignKey(SapBilling, on_delete=models.CASCADE, related_name="lines")
    sku = models.ForeignKey(Sku, on_delete=models.PROTECT)
    qty = models.IntegerField()
    price_h = models.BigIntegerField()

    class Meta:
        ordering = ["created_at"]  # creation order: stable across databases (ids are random UUIDs)


class Invoice(Base):
    """Invoice sent to Amazon. SAP stays the legal tax invoice of record (ZATCA)."""

    po = models.ForeignKey(PurchaseOrder, on_delete=models.CASCADE, related_name="invoices")
    seq = models.PositiveIntegerField(default=1, help_text="Invoice per shipment: 1st, 2nd …")
    shipment = models.ForeignKey("fulfilment.Shipment", null=True, blank=True, on_delete=models.SET_NULL, related_name="invoices")
    invoice_no = models.CharField(max_length=30, unique=True)
    invoice_date = models.DateTimeField()
    net_h = models.BigIntegerField()
    vat_h = models.BigIntegerField()
    total_h = models.BigIntegerField()
    sap_billing_no = models.CharField(max_length=20)
    amazon_status = models.CharField(max_length=10, default="submitted", db_index=True, choices=[
        ("submitted", "Submitted"), ("accepted", "Accepted"), ("on_hold", "On hold at Amazon"), ("rejected", "Rejected by Amazon")])
    amazon_note = models.CharField(max_length=200, blank=True, help_text="Amazon's reason for a hold or rejection")
    revision = models.PositiveIntegerField(default=0, help_text="Times the invoice was corrected and sent again")

    class Meta:
        ordering = ["-invoice_date"]

    @property
    def memo_h(self):
        return sum(m.amount_h for m in self.credit_memos.all())

    @property
    def net_due_h(self):
        """What Amazon owes in total: the invoice less any credit memos issued against it."""
        return self.total_h - self.memo_h


class InvoiceLine(Base):
    invoice = models.ForeignKey(Invoice, on_delete=models.CASCADE, related_name="lines")
    sku = models.ForeignKey(Sku, on_delete=models.PROTECT)
    qty = models.IntegerField()
    price_h = models.BigIntegerField()
    net_h = models.BigIntegerField()

    class Meta:
        ordering = ["created_at"]  # creation order: stable across databases (ids are random UUIDs)


class CreditMemo(Base):
    """Correction after the invoice was sent (agreed price claim, billing error). Reduces what Amazon owes.
    The tax credit note itself is issued in SAP (ZATCA); the hub keeps the number and amount."""

    invoice = models.ForeignKey(Invoice, on_delete=models.CASCADE, related_name="credit_memos")
    memo_no = models.CharField(max_length=30, unique=True)
    amount_h = models.BigIntegerField(help_text="Including VAT")
    reason = models.CharField(max_length=200)
    by_name = models.CharField(max_length=120, blank=True)

    class Meta:
        ordering = ["created_at"]
