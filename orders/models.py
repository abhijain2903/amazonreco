from django.db import models

from catalog.models import FulfilmentCentre, Sku
from core.models import Base

STAGES = ["new", "confirmed", "booked", "released", "asn", "slot", "delivered", "invoiced", "paid"]
STAGE_LABELS = {"new": "To confirm", "confirmed": "Confirmed", "booked": "Booked in SAP", "released": "Released",
                "asn": "ASN sent", "slot": "Slot booked", "delivered": "Delivered", "invoiced": "Invoiced",
                "paid": "Paid", "rejected": "Rejected", "cancelled": "Cancelled by Amazon"}
STAGE_TONES = {"new": "info", "confirmed": "info", "booked": "info", "released": "info", "asn": "info",
               "slot": "info", "delivered": "info", "invoiced": "pri", "paid": "ok", "rejected": "bad", "cancelled": "bad"}
CLOSED = ["paid", "rejected", "cancelled"]


class PurchaseOrder(Base):
    po_no = models.CharField(max_length=20, unique=True)
    fc = models.ForeignKey(FulfilmentCentre, on_delete=models.PROTECT, related_name="pos")
    order_date = models.DateTimeField()
    confirm_by = models.DateTimeField()
    window_start = models.DateTimeField(null=True, blank=True)
    window_end = models.DateTimeField(null=True, blank=True)
    stage = models.CharField(max_length=12, default="new", db_index=True,
                             choices=[(s, STAGE_LABELS[s]) for s in STAGES + ["rejected", "cancelled"]])
    confirmed_at = models.DateTimeField(null=True, blank=True)
    booked_at = models.DateTimeField(null=True, blank=True)
    credit_hold = models.CharField(max_length=200, blank=True, help_text="Why credit control is holding the order")
    released_at = models.DateTimeField(null=True, blank=True)
    delivered_at = models.DateTimeField(null=True, blank=True)
    paid_at = models.DateTimeField(null=True, blank=True)
    vendor_code = models.CharField(max_length=12, blank=True, db_index=True, help_text="Amazon vendor code the PO was sent to")
    sap_order_no = models.CharField(max_length=20, blank=True)
    sf_order_id = models.CharField(max_length=20, blank=True)

    class Meta:
        ordering = ["-order_date"]

    def __str__(self):
        return self.po_no

    @property
    def stage_index(self):
        return STAGES.index(self.stage) if self.stage in STAGES else -1

    @property
    def stage_label(self):
        return STAGE_LABELS.get(self.stage, self.stage)


class PoLine(Base):
    DECISIONS = [("accept", "Accept"), ("partial", "Partial"), ("reject", "Reject")]
    po = models.ForeignKey(PurchaseOrder, on_delete=models.CASCADE, related_name="lines")
    position = models.PositiveIntegerField(default=0)
    sku = models.ForeignKey(Sku, on_delete=models.PROTECT)
    asin = models.CharField(max_length=12)
    qty_ordered = models.IntegerField()
    cost_h = models.BigIntegerField()
    decision = models.CharField(max_length=8, choices=DECISIONS, default="accept")
    qty_confirmed = models.IntegerField(default=0)
    reason = models.CharField(max_length=120, blank=True)
    touched = models.BooleanField(default=False, help_text="User changed the suggested decision")

    class Meta:
        ordering = ["position"]
