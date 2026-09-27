from django.db import models
from django.utils import timezone

from core.models import Base

CATEGORIES = [("PA", "Personal audio"), ("DI", "Digital imaging"), ("TV", "Television"),
              ("HAV", "Home audio & video"), ("Bundle", "Bundles")]
CATEGORY_NAMES = dict(CATEGORIES)


class FulfilmentCentre(Base):
    code = models.CharField(max_length=20, unique=True)
    name = models.CharField(max_length=80)
    city = models.CharField(max_length=60)

    class Meta:
        ordering = ["code"]

    def __str__(self):
        return self.code


class Sku(Base):
    """A product line. Current agreed Amazon cost and SAP free stock are kept on the row
    for fast checks; full price history lives in Price."""

    sku_code = models.CharField(max_length=20, unique=True)
    model_no = models.CharField(max_length=40, unique=True)
    asin = models.CharField(max_length=12, unique=True)
    ean = models.CharField(max_length=20, blank=True)
    description = models.CharField(max_length=120, blank=True)
    category = models.CharField(max_length=8, choices=CATEGORIES)
    cost_h = models.BigIntegerField(default=0, help_text="Current agreed Amazon cost, halalas")
    free_stock = models.IntegerField(default=0)
    stock_as_of = models.DateTimeField(default=timezone.now)
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ["sku_code"]

    def __str__(self):
        return f"{self.model_no} ({self.sku_code})"


class Price(Base):
    sku = models.ForeignKey(Sku, on_delete=models.CASCADE, related_name="prices")
    cost_h = models.BigIntegerField()
    valid_from = models.DateField()
    valid_to = models.DateField(null=True, blank=True)

    class Meta:
        ordering = ["-valid_from"]


def resolve_sku(value):
    """Find a SKU by ME code, ASIN or model number."""
    if not value:
        return None
    v = str(value).strip()
    return (Sku.objects.filter(sku_code=v).first() or Sku.objects.filter(asin=v.upper()).first()
            or Sku.objects.filter(model_no__iexact=v).first())
