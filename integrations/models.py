from django.db import models
from django.utils import timezone

from core.models import Base

MODES = [("off", "Off"), ("file", "File upload"), ("manual", "Manual entry"), ("api", "API"),
         ("edi", "EDI"), ("odata", "OData"), ("idoc", "IDoc"), ("bapi", "BAPI / RFC"), ("smtp", "SMTP relay"),
         ("m365", "Microsoft 365"), ("entra", "Microsoft Entra ID"), ("saml", "SAML 2.0")]


class Connector(Base):
    key = models.CharField(max_length=20, unique=True)
    name = models.CharField(max_length=60)
    short = models.CharField(max_length=4)
    mode = models.CharField(max_length=10, choices=MODES, default="off")
    description = models.CharField(max_length=200)
    last_sync = models.DateTimeField(null=True, blank=True)
    records_today = models.IntegerField(default=0)
    settings = models.JSONField(default=dict, blank=True, help_text="Non-secret settings only")

    class Meta:
        ordering = ["created_at"]

    @property
    def status_label(self):
        if (self.key, self.mode) == ("amazon_vc", "api"):
            from .spapi import Client
            return "Live · SP-API" if Client.configured() else "API · credentials missing"
        return {"file": "File mode", "manual": "Manual entry", "off": "Not connected"}.get(self.mode, "Configured · not live")

    @property
    def tone(self):
        if (self.key, self.mode) == ("amazon_vc", "api"):
            from .spapi import Client
            return "ok" if Client.configured() else "bad"
        return {"file": "info", "manual": "", "off": ""}.get(self.mode, "warn")


class SyncRun(Base):
    connector = models.ForeignKey(Connector, on_delete=models.CASCADE, related_name="runs")
    kind = models.CharField(max_length=30)
    ok = models.BooleanField(default=True)
    message = models.CharField(max_length=300)
    records_in = models.IntegerField(default=0)
    records_out = models.IntegerField(default=0)
    finished_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-finished_at"]


class AmazonTransaction(Base):
    """One document sent to Amazon through the Selling Partner API (acknowledgement, ASN, invoice, credit note).
    Amazon answers each submission with a transaction ID and processes it later; the hub polls its status."""

    KINDS = [("po_ack", "PO acknowledgement"), ("asn", "ASN"), ("invoice", "Invoice"), ("credit_note", "Credit note"),
             ("pickup", "Pickup request")]
    STATUSES = [("queued", "Queued"), ("processing", "With Amazon"), ("success", "Accepted"), ("failure", "Refused by Amazon"),
                ("error", "Not sent")]
    kind = models.CharField(max_length=12, choices=KINDS)
    po_no = models.CharField(max_length=20, db_index=True)
    ref = models.CharField(max_length=30, help_text="PO, ASN, invoice or credit memo number")
    transaction_id = models.CharField(max_length=64, blank=True, db_index=True)
    status = models.CharField(max_length=10, choices=STATUSES, default="queued", db_index=True)
    errors = models.JSONField(default=list, blank=True)
    payload = models.JSONField(default=dict, blank=True)
    attempts = models.PositiveIntegerField(default=0)
    sent_at = models.DateTimeField(null=True, blank=True)
    checked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    @property
    def error_text(self):
        return "; ".join(f"{e.get('code', '')}: {e.get('message', '')}".strip(": ") for e in self.errors)[:300]


class AmazonReport(Base):
    """One run of an Amazon report (Reports API): requested, processed by Amazon, then downloaded and loaded."""

    STATUSES = [("requested", "Requested"), ("processing", "Amazon is preparing it"), ("loaded", "Loaded"),
                ("no_data", "No data for the period"), ("failed", "Failed")]
    report_type = models.CharField(max_length=60)
    report_id = models.CharField(max_length=40, blank=True, db_index=True)
    status = models.CharField(max_length=12, choices=STATUSES, default="requested", db_index=True)
    data_start = models.DateTimeField(null=True, blank=True)
    data_end = models.DateTimeField(null=True, blank=True)
    options = models.JSONField(default=dict, blank=True)
    rows = models.IntegerField(default=0)
    message = models.CharField(max_length=300, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]


class AmazonPromoPerformance(Base):
    """What Amazon reports for a promotion or coupon it ran: units sold at the promotional price and what ME funded.
    Linked to the hub's promotion by the funding agreement (promotions) or by ASIN and dates (coupons)."""

    KINDS = [("promotion", "Promotion"), ("coupon", "Coupon")]
    kind = models.CharField(max_length=10, choices=KINDS)
    amazon_id = models.CharField(max_length=60)
    name = models.CharField(max_length=200, blank=True)
    vendor_code = models.CharField(max_length=12, blank=True)
    agreement_id = models.CharField(max_length=30, blank=True, db_index=True)
    start = models.DateTimeField(null=True, blank=True)
    end = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=30, blank=True)
    units = models.IntegerField(default=0)
    spent_h = models.BigIntegerField(null=True, blank=True, help_text="What ME funded, per Amazon, halalas")
    revenue_h = models.BigIntegerField(null=True, blank=True)
    by_asin = models.JSONField(default=dict, blank=True, help_text="{asin: {units, spent_h}}")
    promotion = models.ForeignKey("promotions.Promotion", null=True, blank=True, on_delete=models.SET_NULL, related_name="amazon_reports")

    class Meta:
        ordering = ["-start"]
        constraints = [models.UniqueConstraint(fields=["kind", "amazon_id"], name="uniq_amazon_promo_perf")]


class AmazonCharge(Base):
    """An invoice Amazon issued to ME (Finances Invoices API, type OTHER): co-op / promotion funding, price protection,
    damage allowance, shortage or return charges. Linked to POs, agreements, shortages and returns by Amazon's own
    references, so the deduction it causes in a remittance can be put against the right invoice."""

    invoice_no = models.CharField(max_length=40, unique=True)
    identifier = models.CharField(max_length=80, blank=True)
    status = models.CharField(max_length=20, blank=True)
    kind = models.CharField(max_length=30, blank=True, help_text="Amazon's service charge type, e.g. PRICE_PROTECTION")
    description = models.CharField(max_length=200, blank=True)
    issue_date = models.DateTimeField(null=True, blank=True)
    amount_h = models.BigIntegerField(default=0)
    vendor_code = models.CharField(max_length=12, blank=True)
    po_nos = models.JSONField(default=list, blank=True)
    agreement_ids = models.JSONField(default=list, blank=True)
    shortage_invoice_ids = models.JSONField(default=list, blank=True)
    return_ids = models.JSONField(default=list, blank=True)
    debit_note_no = models.CharField(max_length=40, blank=True, help_text="The hub debit note created from it, if any")

    class Meta:
        ordering = ["-issue_date"]

    @property
    def status_label(self):
        return self.status.replace("_", " ").capitalize()

    @property
    def kind_label(self):
        return {"PRICE_PROTECTION": "Price protection", "DAMAGE_ALLOWANCE": "Damage allowance",
                "FBA_INVENTORY_STORAGE_FEE": "Storage fee", "COOP": "Co-op / promotion"}.get(self.kind, "Amazon charge")


class AmazonRemittance(Base):
    """A payment Amazon made (Finance Remittance API), loaded once into the hub's payments."""

    unique_payment_id = models.CharField(max_length=40, unique=True)
    payment_no = models.CharField(max_length=40)
    payment_date = models.DateTimeField(null=True, blank=True)
    amount_h = models.BigIntegerField(default=0)
    status = models.CharField(max_length=12, blank=True)
    lines = models.IntegerField(default=0)
    vendor_code = models.CharField(max_length=12, blank=True)
    loaded = models.BooleanField(default=False)
    message = models.CharField(max_length=300, blank=True)

    class Meta:
        ordering = ["-payment_date"]
