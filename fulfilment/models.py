from django.db import models

from catalog.models import Sku
from core.models import Base
from orders.models import PurchaseOrder


class SapDelivery(Base):
    po = models.ForeignKey(PurchaseOrder, on_delete=models.CASCADE, related_name="sap_deliveries")
    seq = models.PositiveIntegerField(default=1, help_text="1st, 2nd … delivery for the PO")
    sales_order = models.CharField(max_length=20, blank=True, help_text="Sales order of this booking portion")
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

    po = models.ForeignKey(PurchaseOrder, on_delete=models.CASCADE, related_name="shipments")
    seq = models.PositiveIntegerField(default=1, help_text="1st, 2nd … shipment for the PO")
    sales_order = models.CharField(max_length=20, blank=True, help_text="Sales order of this booking portion")
    delivered_at = models.DateTimeField(null=True, blank=True)
    asn_no = models.CharField(max_length=20, unique=True)
    sap_delivery_no = models.CharField(max_length=20)
    cartons = models.IntegerField()
    ship_date = models.DateTimeField()
    submitted_at = models.DateTimeField()
    slot_id = models.CharField(max_length=30, blank=True)
    slot_start = models.DateTimeField(null=True, blank=True)
    slot_window = models.CharField(max_length=20, blank=True)
    freight = models.CharField(max_length=8, default="prepaid", choices=[("prepaid", "ME books the delivery (Carrier Central)"),
                                                                         ("collect", "Amazon collects (routing request)")])
    slot_outcome = models.CharField(max_length=10, blank=True, help_text="missed / refused: the last appointment failed")
    slot_note = models.CharField(max_length=200, blank=True)
    reschedules = models.PositiveIntegerField(default=0)


class ShipmentLine(Base):
    shipment = models.ForeignKey(Shipment, on_delete=models.CASCADE, related_name="lines")
    sku = models.ForeignKey(Sku, on_delete=models.PROTECT)
    qty = models.IntegerField()

    class Meta:
        ordering = ["created_at"]  # creation order: stable across databases (ids are random UUIDs)


class Carton(Base):
    """One carton on the ASN, with the SSCC printed on its label. Amazon receives against these."""

    shipment = models.ForeignKey(Shipment, on_delete=models.CASCADE, related_name="carton_list")
    seq = models.PositiveIntegerField()
    sscc = models.CharField(max_length=18, unique=True)
    sku = models.ForeignKey(Sku, on_delete=models.PROTECT)
    qty = models.IntegerField()

    class Meta:
        ordering = ["shipment", "seq"]
