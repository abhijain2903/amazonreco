"""Connector adapters. Every external system sits behind the same interface.

Modes: off, file (uploads / downloads), manual, or a live mode (api, edi, odata...).
Live adapters are stubs: each method documents what the real call does and raises
NotImplementedError until the connection details are agreed with ME IT.
"""
import logging
import random

from django.conf import settings
from django.utils import timezone

from core.services import next_number

from .models import Connector, SyncRun

log = logging.getLogger("hub.connectors")

DEFAULTS = [
    ("amazon_vc", "Amazon Vendor Central", "VC", "file",
     "POs, remittances and debit notes in; PO acknowledgements, ASNs and invoices out."),
    ("carrier", "Amazon Carrier Central", "CC", "manual", "Delivery slot bookings. Slot IDs are entered in the hub."),
    ("sap", "SAP", "SAP", "file", "Stock, deliveries and billing in; sales orders out."),
    ("salesforce", "Salesforce", "SF", "off", "Confirmed orders are logged as Salesforce orders."),
    ("finance", "Finance (SAP FI)", "FI", "file", "Payments, deductions and credit notes for booking."),
    ("email", "Email alerts", "@", "off", "Deadline and mismatch alerts, daily digest, claim files."),
    ("sso", "Single sign-on", "SSO", "off", "Staff sign in with their company account."),
]

FIELDS = {
    "amazon_vc": {"modes": ["file", "api", "edi"],
                  "api": ["Vendor code", "Marketplace", "Client ID", "Client secret", "Refresh token"],
                  "edi": ["EDI partner ID", "AS2 ID", "AS2 endpoint URL", "Certificate"],
                  "data": ["Purchase orders (in)", "Remittance / payments (in)", "Debit notes (in)",
                           "PO acknowledgement (out)", "ASN (out)", "Invoice (out)"]},
    "carrier": {"modes": ["manual"], "data": ["Delivery slot ID and time"]},
    "sap": {"modes": ["file", "odata", "idoc", "bapi"],
            "odata": ["System (S/4HANA or ECC)", "Host URL", "Client", "Username", "Password", "Sales org", "Plant"],
            "idoc": ["Logical system", "Partner profile", "Port"], "bapi": ["Host", "System number", "Client", "Username"],
            "data": ["Stock (in)", "Deliveries (in)", "Billing documents (in)", "Credit notes (in)", "Sales orders (out)"]},
    "salesforce": {"modes": ["off", "api"], "api": ["Instance URL", "Client ID", "Client secret", "Order object"],
                   "data": ["Confirmed orders (out)"]},
    "finance": {"modes": ["file", "api"], "api": ["Company code", "GL: Amazon receivable", "GL: deductions"],
                "data": ["Payments (out)", "Deductions (out)", "Credit notes (out)"]},
    "email": {"modes": ["off", "smtp", "m365"], "smtp": ["SMTP host", "Port", "From address"],
              "m365": ["Tenant ID", "Sender mailbox"], "data": ["Alerts", "Daily digest", "Claim files to product team"]},
    "sso": {"modes": ["off", "entra", "saml"], "entra": ["Tenant ID", "Client ID"], "saml": ["IdP metadata URL"],
            "data": ["User sign-in", "Role groups"]},
}
SECRET_WORDS = ("secret", "password", "token", "certificate")


def ensure_connectors():
    for key, name, short, mode, desc in DEFAULTS:
        Connector.objects.get_or_create(key=key, defaults={"name": name, "short": short, "mode": mode, "description": desc})


class Adapter:
    """Base adapter. `pull(kind, since)` brings records in; `push(kind, payload)` sends them out."""

    key = ""

    def __init__(self, connector: Connector):
        self.c = connector

    @property
    def live(self):
        return self.c.mode not in ("off", "file", "manual")

    def test_connection(self):
        if not self.live:
            return True, "Nothing to test in this mode."
        return True, "Test connection: simulated success. No real call was made."

    def pull(self, kind, since=None):
        if self.live:
            raise NotImplementedError(f"{self.c.name} live pull '{kind}' is not built yet.")
        return []  # File mode: records arrive through the Uploads page.

    def push(self, kind, payload):
        """File mode: the generated file is downloaded by the user and uploaded to the other system."""
        if self.live:
            raise NotImplementedError(f"{self.c.name} live push '{kind}' is not built yet.")
        SyncRun.objects.create(connector=self.c, kind=kind, message=f"{kind} file ready for manual upload", records_out=1)
        return {"mode": "file", "file": getattr(payload, "filename", "")}

    def sync(self):
        run = SyncRun.objects.create(connector=self.c, kind="sync", message="Sync run: simulated, 0 new records.")
        self.c.last_sync = timezone.now()
        self.c.save(update_fields=["last_sync", "updated_at"])
        return run


class AmazonVendorCentral(Adapter):
    """Live options: Selling Partner API for vendors (orders, shipments, invoices, transaction status;
    Saudi marketplace via the EU endpoint) or EDI 850/855/856/810/820 through an EDI provider."""

    key = "amazon_vc"


class SAP(Adapter):
    """Live options: OData (S/4HANA) or IDoc/BAPI (ECC) through SAP middleware."""

    key = "sap"

    def create_sales_order(self, po, sap_order_no=None):
        """Returns (SAP order #, Salesforce order id)."""
        sf = get_adapter("salesforce")
        sf_id = sf.create_order(po)
        if sap_order_no:
            return sap_order_no, sf_id
        if self.live:
            raise NotImplementedError("SAP live sales-order creation is not built yet.")
        if not settings.DEMO_SIMULATIONS:
            from core.services import CommandError
            raise CommandError("SAP is in file mode. Enter the SAP order number created in SAP.")
        return str(next_number("sap_order", 4500018420)), sf_id


class Salesforce(Adapter):
    key = "salesforce"

    def create_order(self, po):
        if self.c.mode == "off":
            return f"SF-{next_number('sf_order', 208440)}" if settings.DEMO_SIMULATIONS else ""
        raise NotImplementedError("Salesforce live order creation is not built yet.")


class Email(Adapter):
    key = "email"

    def send(self, to, subject, body):
        log.info("email (log only) to=%s subject=%s", to, subject)


REGISTRY = {"amazon_vc": AmazonVendorCentral, "sap": SAP, "salesforce": Salesforce, "email": Email}


def get_adapter(key) -> Adapter:
    c = Connector.objects.filter(key=key).first()
    if c is None:
        ensure_connectors()
        c = Connector.objects.get(key=key)
    return REGISTRY.get(key, Adapter)(c)


def count_in(key, n):
    Connector.objects.filter(key=key).update(last_sync=timezone.now())
    c = Connector.objects.filter(key=key).first()
    if c:
        c.records_today += n
        c.save(update_fields=["records_today"])
