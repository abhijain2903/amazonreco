from django.db import models

from catalog.models import CATEGORIES, Sku
from core.models import Base

PROMO_TYPES = [("price_discount", "Price discount"), ("deal", "Deal (Lightning / Best Deal)"), ("coupon", "Coupon"),
               ("prime_day", "Prime Day / event"), ("price_protection", "Price protection"), ("other", "Other")]

STORED_STAGES = ["draft", "submitted", "rejected", "approved", "dn_validated", "claimed", "cn_shortfall", "closed"]
STAGE_LABELS = {"draft": "Draft", "submitted": "Submitted", "rejected": "Rejected by Amazon", "approved": "Approved",
                "live": "Live", "waiting_dn": "Waiting for DN", "dn_overdue": "DN overdue",
                "dn_received": "DN to validate", "dn_validated": "DN validated", "claimed": "Claimed",
                "cn_shortfall": "CN shortfall", "closed": "Closed"}
STAGE_TONES = {"draft": "", "submitted": "info", "rejected": "bad", "approved": "info", "live": "pri",
               "waiting_dn": "", "dn_overdue": "bad", "dn_received": "warn", "dn_validated": "info",
               "claimed": "info", "cn_shortfall": "bad", "closed": "ok"}


class Promotion(Base):
    mecl_ref = models.CharField(max_length=24, unique=True)
    agreement_no = models.CharField(max_length=20, null=True, blank=True, unique=True,
                                    help_text="Amazon agreement number (rule R8: unique)")
    name = models.CharField(max_length=160)
    category = models.CharField(max_length=8, choices=CATEGORIES)
    promo_type = models.CharField(max_length=20, choices=PROMO_TYPES, default="price_discount")
    start = models.DateTimeField()
    end = models.DateTimeField()
    dn_due = models.DateTimeField()
    owner_name = models.CharField(max_length=120)
    stage = models.CharField(max_length=14, default="draft", db_index=True,
                             choices=[(s, STAGE_LABELS[s]) for s in STORED_STAGES])

    class Meta:
        ordering = ["-start"]

    def __str__(self):
        return self.mecl_ref


class PromoLine(Base):
    promotion = models.ForeignKey(Promotion, on_delete=models.CASCADE, related_name="lines")
    sku = models.ForeignKey(Sku, on_delete=models.PROTECT)
    support_h = models.BigIntegerField(help_text="Support per unit, halalas")
    expected_units = models.IntegerField()
    sold_units = models.IntegerField(null=True, blank=True, help_text="From the Amazon sales report")

    class Meta:
        ordering = ["created_at"]
