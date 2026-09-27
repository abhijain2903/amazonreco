from django.db import models

from catalog.models import Sku
from core.models import Base
from orders.models import PurchaseOrder


class SapBilling(Base):
    po = models.OneToOneField(PurchaseOrder, on_delete=models.CASCADE, related_name="sap_billing")
    billing_no = models.CharField(max_length=20)
    received_at = models.DateTimeField()


class SapBillingLine(Base):
    billing = models.ForeignKey(SapBilling, on_delete=models.CASCADE, related_name="lines")
    sku = models.ForeignKey(Sku, on_delete=models.PROTECT)
    qty = models.IntegerField()
    price_h = models.BigIntegerField()


class Invoice(Base):
    """Invoice sent to Amazon. SAP stays the legal tax invoice of record (ZATCA)."""

    po = models.OneToOneField(PurchaseOrder, on_delete=models.CASCADE, related_name="invoice")
    invoice_no = models.CharField(max_length=30, unique=True)
    invoice_date = models.DateTimeField()
    net_h = models.BigIntegerField()
    vat_h = models.BigIntegerField()
    total_h = models.BigIntegerField()
    sap_billing_no = models.CharField(max_length=20)

    class Meta:
        ordering = ["-invoice_date"]


class InvoiceLine(Base):
    invoice = models.ForeignKey(Invoice, on_delete=models.CASCADE, related_name="lines")
    sku = models.ForeignKey(Sku, on_delete=models.PROTECT)
    qty = models.IntegerField()
    price_h = models.BigIntegerField()
    net_h = models.BigIntegerField()
