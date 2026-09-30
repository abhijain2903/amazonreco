from django.db import models

from catalog.models import Sku
from core.models import Base
from orders.models import PurchaseOrder


class SapDelivery(Base):
    po = models.OneToOneField(PurchaseOrder, on_delete=models.CASCADE, related_name="sap_delivery")
    delivery_no = models.CharField(max_length=20, unique=True)
    cartons = models.IntegerField(default=1)
    ship_date = models.DateTimeField()


class SapDeliveryLine(Base):
    delivery = models.ForeignKey(SapDelivery, on_delete=models.CASCADE, related_name="lines")
    sku = models.ForeignKey(Sku, on_delete=models.PROTECT)
    qty = models.IntegerField()
    asn_qty = models.IntegerField(null=True, blank=True, help_text="Draft ASN quantity edited by the user")

    class Meta:
        ordering = ["created_at"]  # creation order: stable across databases (ids are random UUIDs)


class Shipment(Base):
    """The ASN sent to Amazon, plus its Carrier Central delivery slot."""

    po = models.OneToOneField(PurchaseOrder, on_delete=models.CASCADE, related_name="shipment")
    asn_no = models.CharField(max_length=20, unique=True)
    sap_delivery_no = models.CharField(max_length=20)
    cartons = models.IntegerField()
    ship_date = models.DateTimeField()
    submitted_at = models.DateTimeField()
    slot_id = models.CharField(max_length=30, blank=True)
    slot_start = models.DateTimeField(null=True, blank=True)
    slot_window = models.CharField(max_length=20, blank=True)


class ShipmentLine(Base):
    shipment = models.ForeignKey(Shipment, on_delete=models.CASCADE, related_name="lines")
    sku = models.ForeignKey(Sku, on_delete=models.PROTECT)
    qty = models.IntegerField()

    class Meta:
        ordering = ["created_at"]  # creation order: stable across databases (ids are random UUIDs)
