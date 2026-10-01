from django.db import models

from catalog.models import FulfilmentCentre, Sku
from core.models import Base

RTV_REASONS = [("defective", "Defective / customer return"), ("overstock", "Overstock (returnable under the agreement)"),
               ("recall", "Recall"), ("wrong_item", "Wrong item received"), ("other", "Other")]
RTV_STATUS = [("requested", "To authorise"), ("refused", "Refused"), ("authorised", "Authorised, on its way"),
              ("received", "Received"), ("credited", "Credited"), ("disputed", "Disputed")]
CONDITIONS = [("good", "Good, resaleable"), ("damaged", "Damaged"), ("missing", "Missing")]


class ReturnAuth(Base):
    """Amazon returning stock to ME (RTV): Amazon requests it, ME authorises, the goods come back, and Amazon deducts
    their cost from a payment. The deduction is checked against what actually arrived."""

    rtv_no = models.CharField(max_length=30, unique=True, help_text="Amazon's return / authorisation number")
    vendor_code = models.CharField(max_length=12, blank=True)
    fc = models.ForeignKey(FulfilmentCentre, null=True, blank=True, on_delete=models.SET_NULL)
    reason = models.CharField(max_length=12, choices=RTV_REASONS, default="defective")
    status = models.CharField(max_length=12, choices=RTV_STATUS, default="requested", db_index=True)
    requested_at = models.DateTimeField()
    authorised_at = models.DateTimeField(null=True, blank=True)
    received_at = models.DateTimeField(null=True, blank=True)
    note = models.CharField(max_length=200, blank=True)
    payment_no = models.CharField(max_length=30, blank=True, help_text="The payment whose deduction covers this return")
    dispute_no = models.CharField(max_length=20, blank=True)

    class Meta:
        ordering = ["-requested_at"]

    def __str__(self):
        return self.rtv_no

    @property
    def amount_h(self):
        """What Amazon will deduct: requested units at cost."""
        return sum(l.qty * l.unit_cost_h for l in self.lines.all())

    @property
    def received_h(self):
        """What came back resaleable or damaged-but-received: the most ME should accept."""
        return sum((l.qty_received or 0) * l.unit_cost_h for l in self.lines.all() if l.condition != "missing")


class ReturnLine(Base):
    rtv = models.ForeignKey(ReturnAuth, on_delete=models.CASCADE, related_name="lines")
    sku = models.ForeignKey(Sku, on_delete=models.PROTECT)
    qty = models.IntegerField()
    unit_cost_h = models.BigIntegerField()
    qty_received = models.IntegerField(null=True, blank=True)
    condition = models.CharField(max_length=8, choices=CONDITIONS, blank=True)

    class Meta:
        ordering = ["created_at"]
