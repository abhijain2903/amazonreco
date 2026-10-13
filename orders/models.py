from django.db import models

from catalog.models import FulfilmentCentre, Sku
from core.models import Base

STAGES = ["new", "confirmed", "booked", "released", "asn", "slot", "delivered", "invoiced", "backorder", "paid"]
STAGE_LABELS = {"new": "To confirm", "confirmed": "Confirmed", "booked": "Booked in SAP", "released": "Released",
                "asn": "ASN sent", "slot": "Slot booked", "delivered": "Delivered", "invoiced": "Invoiced", "backorder": "Backorder open",
                "paid": "Paid", "rejected": "Rejected", "cancelled": "Cancelled by Amazon"}
STAGE_TONES = {"new": "info", "confirmed": "info", "booked": "info", "released": "info", "asn": "info",
               "slot": "info", "delivered": "info", "invoiced": "pri", "backorder": "warn", "paid": "ok", "rejected": "bad", "cancelled": "bad"}
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
    rfpo = models.CharField(max_length=30, blank=True, help_text="ME's release / booking reference (RFPO)")
    sap_order_no = models.CharField(max_length=20, blank=True)
    sf_order_id = models.CharField(max_length=20, blank=True)
    source = models.CharField(max_length=10, default="file", help_text="How the PO arrived: file (upload) or api (SP-API)")
    po_type = models.CharField(max_length=24, blank=True, help_text="Amazon's PO type: RegularOrder, RushOrder, NewProductIntroduction …")
    amazon_changed_at = models.DateTimeField(null=True, blank=True, help_text="Amazon's last change to the PO already applied here")

    class Meta:
        ordering = ["-order_date"]

    def __str__(self):
        return self.po_no

    # Several deliveries / shipments / invoices per PO (split shipments, backorders): these give the latest one.
    def _latest(self, rel):
        return getattr(self, rel).order_by("-seq", "-created_at").first() if self.pk else None

    @property
    def sap_delivery(self):
        return self._latest("sap_deliveries")

    @property
    def shipment(self):
        return self._latest("shipments")

    @property
    def sap_billing(self):
        return self._latest("sap_billings")

    @property
    def invoice(self):
        return self._latest("invoices")

    @property
    def stage_index(self):
        return STAGES.index(self.stage) if self.stage in STAGES else -1

    @property
    def stage_label(self):
        return STAGE_LABELS.get(self.stage, self.stage)


# Amazon accepts only three reasons for a rejected quantity (SP-API OrderItemAcknowledgement.rejectionReason). The hub's
# own reasons are kept for ME's records; this is what Amazon is told. A price difference has no Amazon reason: it is
# settled with the buyer, and a rejection for it goes to Amazon as "temporarily unavailable".
AMAZON_REJECT = {"Out of stock": "TemporarilyUnavailable", "Limited stock": "TemporarilyUnavailable",
                 "Discontinued": "ObsoleteProduct", "Not our product / wrong ASIN": "InvalidProductIdentifier"}
AMAZON_REJECT_LABELS = {"TemporarilyUnavailable": "Temporarily unavailable", "ObsoleteProduct": "Obsolete product",
                        "InvalidProductIdentifier": "Invalid product identifier"}


class PoLine(Base):
    DECISIONS = [("accept", "Accept"), ("partial", "Partial"), ("backorder", "Backorder rest"), ("reject", "Reject")]
    po = models.ForeignKey(PurchaseOrder, on_delete=models.CASCADE, related_name="lines")
    position = models.PositiveIntegerField(default=0)
    sku = models.ForeignKey(Sku, on_delete=models.PROTECT)
    asin = models.CharField(max_length=12)
    qty_ordered = models.IntegerField()
    cost_h = models.BigIntegerField()
    decision = models.CharField(max_length=10, choices=DECISIONS, default="accept")
    qty_confirmed = models.IntegerField(default=0, help_text="Units confirmed to ship now")
    qty_backorder = models.IntegerField(default=0, help_text="Units acknowledged as backordered, shipped later")
    backorder_eta = models.DateField(null=True, blank=True)
    reason = models.CharField(max_length=120, blank=True)
    touched = models.BooleanField(default=False, help_text="User changed the suggested decision")
    item_seq = models.CharField(max_length=10, blank=True, help_text="Amazon's itemSequenceNumber for the line")
    vendor_sku = models.CharField(max_length=40, blank=True, help_text="vendorProductIdentifier as Amazon sent it")
    backorder_allowed = models.BooleanField(default=True, help_text="Amazon accepts a backordered quantity on this line")
    amazon_uom = models.CharField(max_length=6, default="Eaches", help_text="Unit Amazon ordered in: Eaches or Cases")
    unit_size = models.PositiveIntegerField(default=1, help_text="Units per case when Amazon ordered in cases")
    qty_received = models.IntegerField(null=True, blank=True, help_text="Units Amazon has received so far (SP-API)")
    received_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["position"]

    @property
    def amazon_reject_code(self):
        return AMAZON_REJECT.get(self.reason, "TemporarilyUnavailable")

    @property
    def amazon_reject_label(self):
        return AMAZON_REJECT_LABELS[self.amazon_reject_code]

    @property
    def committed(self):
        """Everything ME agreed to ship on this line: now plus backorder."""
        return self.qty_confirmed + self.qty_backorder
