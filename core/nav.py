"""Sidebar menu and badge counts."""
from debitnotes.models import DebitNote
from debitnotes.services import evaluate
from orders.models import PurchaseOrder
from payments.models import Payment

from .actions import action_items

NAV = [
    ("Overview", [("dashboard", "/", "Dashboard", "home"), ("action", "/action/", "Action Center", "inbox")]),
    ("Sell-in", [("pos", "/pos/", "Purchase Orders", "po"), ("ship", "/ship/", "Shipments & Invoices", "truck"),
                 ("pay", "/pay/", "Payments & Disputes", "wallet")]),
    ("Sell-out", [("promos", "/promos/", "Promotions", "tag"), ("dns", "/dns/", "Debit Notes", "receipt"),
                  ("claims", "/claims/", "Claims & Credit Notes", "claim")]),
    ("Data", [("uploads", "/uploads/", "Uploads", "upload"), ("integrations", "/integrations/", "Integrations", "plug")]),
    ("Admin", [("settings", "/settings/", "Settings", "sliders")]),
]
TITLES = {k: label for _, items in NAV for k, _, label, _ in items}


def section_for(path):
    for _, items in NAV:
        for key, url, _, _ in items:
            if url != "/" and path.startswith(url):
                return key
    return "dashboard"


def badges(user):
    mine = action_items(user, mine=True)
    dns = [evaluate(d)["status"] for d in DebitNote.objects.filter(validated=False).prefetch_related("lines__sku")]
    new = PurchaseOrder.objects.filter(stage="new")
    from django.utils import timezone
    return {
        "action": (len(mine), any(i["sev"] == "bad" for i in mine)),
        "pos": (new.count(), new.filter(confirm_by__lt=timezone.now()).exists()),
        "pay": (Payment.objects.filter(status__in=["short", "unmatched"]).count(), Payment.objects.filter(status="short").exists()),
        "dns": (sum(s in ("to_validate", "mismatch", "unlinked") for s in dns), "mismatch" in dns),
    }


def build(user, path):
    b = badges(user)
    cur = section_for(path)
    return [{"group": g, "items": [{"key": k, "url": u, "label": l, "icon": i, "on": k == cur,
                                     "badge": b.get(k, (0, False))[0], "hot": b.get(k, (0, False))[1] and k != cur}
                                    for k, u, l, i in items]} for g, items in NAV]
