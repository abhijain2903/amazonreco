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
        return {"file": "File mode", "manual": "Manual entry", "off": "Not connected"}.get(self.mode, "Configured · not live")


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
