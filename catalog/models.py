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
    rrp_h = models.BigIntegerField(null=True, blank=True, help_text="Recommended retail price incl. VAT, halalas")
    lifecycle = models.CharField(max_length=10, default="active", choices=[("new", "New"), ("active", "Active"), ("phase_out", "Phase-out"), ("eol", "EOL")])
    case_pack = models.PositiveIntegerField(default=1, help_text="Units per case; PO quantities should be whole cases")
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


def agreed_cost_h(sku, on_date):
    """The agreed cost per unit valid on a date (the price list keeps history: valid_from / valid_to).
    Falls back to the SKU's current cost when no price row covers that date."""
    from django.db.models import Q
    from django.utils import timezone
    d = timezone.localtime(on_date).date() if hasattr(on_date, "date") else on_date
    p = (Price.objects.filter(sku=sku, valid_from__lte=d).filter(Q(valid_to__isnull=True) | Q(valid_to__gte=d))
         .order_by("-valid_from").first())
    return p.cost_h if p else sku.cost_h


def resolve_sku(value):
    """Find a SKU by ME code, ASIN or model number."""
    if not value:
        return None
    v = str(value).strip()
    return (Sku.objects.filter(sku_code=v).first() or Sku.objects.filter(asin=v.upper()).first()
            or Sku.objects.filter(model_no__iexact=v).first())


class SellOut(models.Model):
    """Units Amazon sold to customers, net of returns (Vendor Central sales report). One row per SKU and day — a
    weekly or monthly file is stored on its last day."""

    sku = models.ForeignKey(Sku, on_delete=models.CASCADE, related_name="sellout")
    day = models.DateField(db_index=True)
    units = models.IntegerField()

    class Meta:
        constraints = [models.UniqueConstraint(fields=["sku", "day"], name="uniq_sellout_day")]


class AmazonStock(models.Model):
    """Amazon's stock on hand for a SKU on a date (Vendor Central inventory report)."""

    sku = models.ForeignKey(Sku, on_delete=models.CASCADE, related_name="amazon_stock")
    as_of = models.DateField(db_index=True)
    units = models.IntegerField()

    class Meta:
        constraints = [models.UniqueConstraint(fields=["sku", "as_of"], name="uniq_amazon_stock")]


class Forecast(models.Model):
    """Amazon's forecast for a SKU and month (Vendor Central forecasting report)."""

    sku = models.ForeignKey(Sku, on_delete=models.CASCADE, related_name="forecasts")
    month = models.DateField(help_text="First day of the month")
    sellout_units = models.IntegerField()
    sellin_units = models.IntegerField(null=True, blank=True, help_text="Empty: the tracker estimates it")

    class Meta:
        constraints = [models.UniqueConstraint(fields=["sku", "month"], name="uniq_forecast_month")]
