"""Who can do what. Mirrors the permission matrix in the Prototype Spec (section 10)."""
from .models import ROLE_TITLES

PERMS = {
    "upload": ["PIC", "Planning", "Logistics", "Product", "Finance", "Admin"],
    "confirm": ["PIC", "Admin"],
    "book": ["PIC", "Planning", "Admin"],
    "release": ["Credit", "Admin"],
    "ship": ["PIC", "Logistics", "Admin"],
    "invoice": ["PIC", "Finance", "Admin"],
    "dispute": ["PIC", "Finance", "Admin"],
    "promo": ["PIC", "Product", "Admin"],
    "dn": ["PIC", "Finance", "Admin"],
    "cn": ["Product", "Finance", "Admin"],
    "override": ["PIC", "Finance", "Manager", "Admin"],
    "settings": ["Admin"],
}
PERM_LABELS = {
    "upload": "Upload data", "confirm": "Confirm PO", "book": "Book in SAP", "release": "Release order",
    "ship": "ASN, slot, dispatch", "invoice": "Submit invoice", "dispute": "Disputes", "promo": "Create promo",
    "dn": "Validate DN", "cn": "Record CN, close claim", "override": "Override a failed check",
    "settings": "Settings, integrations",
}


def can(user, perm):
    if user is None or not getattr(user, "is_authenticated", False):
        return False
    if user.is_superuser:
        return True
    return any(r in PERMS[perm] for r in (user.roles or []))


def who_can(perm):
    # Admin can do everything, so it is only named when it is the only role (Settings, Integrations).
    return " or ".join(ROLE_TITLES[r] for r in PERMS[perm] if r != "Admin") or ROLE_TITLES["Admin"]


def caps(user):
    return {p: can(user, p) for p in PERMS}
