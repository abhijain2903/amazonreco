"""Role access: every command is refused, on the server, to each role the permission matrix leaves out, and allowed
for a role it includes. Buttons being disabled is not enough — these tests post directly, as a user could."""
import pytest

from debitnotes.models import DebitNote
from debitnotes.services import evaluate
from fulfilment.services import delivery_of
from identity.permissions import PERMS
from orders.models import PurchaseOrder
from payments.models import Dispute, Payment
from promotions.models import Promotion
from promotions.services import stage_of

from .conftest import toast

pytestmark = pytest.mark.django_db

USERS = {"PIC": "faisal", "Planning": "noura", "Credit": "omar", "Logistics": "khalid", "Product": "reem", "Finance": "priya",
         "Manager": "tariq"}


def po(stage, test=None):
    return next(p for p in PurchaseOrder.objects.filter(stage=stage) if test is None or test(p)).po_no


def promo(st):
    return next(p for p in Promotion.objects.all() if stage_of(p) == st).mecl_ref


def dn(status):
    return next(d for d in DebitNote.objects.filter(validated=False) if evaluate(d)["status"] == status).dn_no


def short():
    return Payment.objects.filter(status="short").first().payment_no


def claim(status):
    from claims.models import Claim
    return Claim.objects.filter(status=status).first().claim_no


def blocked(p):
    from billing.services import invoice_blocked
    return invoice_blocked(p)


def suggestion():
    from matching import services as ms
    p = Payment.objects.filter(status="unmatched").first()
    return str(ms.refresh("pay_inv", p)[0].pk)


def connector():
    from integrations.connectors import ensure_connectors
    from integrations.models import Connector
    ensure_connectors()
    return Connector.objects.first().key


# (perm, url, form data) — every command endpoint in the hub, with a record in the right state from the example data
ACTIONS = {
    "po.lines": ("confirm", lambda: f"/pos/{po('new')}/lines/", {}),
    "po.accept_green": ("confirm", lambda: f"/pos/{po('new')}/accept-green/", {}),
    "po.confirm": ("confirm", lambda: f"/pos/{po('new')}/confirm/", {}),
    "po.book": ("book", lambda: f"/pos/{po('confirmed')}/book/", {}),
    "po.release": ("release", lambda: f"/pos/{po('booked')}/release/", {}),
    "po.hold": ("release", lambda: f"/pos/{po('booked')}/hold/", {"reason": "Over credit limit"}),
    "po.sync_delivery": ("ship", lambda: f"/pos/{po('released', lambda p: not delivery_of(p))}/sync-delivery/", {}),
    "po.asn": ("ship", lambda: f"/pos/{po('released', delivery_of)}/asn/", {}),
    "po.slot": ("ship", lambda: f"/pos/{po('asn')}/slot/", {"slot_id": "CC1", "date": "2030-01-01", "window": "08:00–12:00"}),
    "po.deliver": ("ship", lambda: f"/pos/{po('slot')}/deliver/", {}),
    "po.fix_billing": ("invoice", lambda: f"/pos/{po('delivered', blocked)}/fix-billing/", {}),
    "po.invoice": ("invoice", lambda: f"/pos/{po('delivered', lambda p: not blocked(p))}/invoice/", {}),
    "pay.automatch": ("dispute", lambda: "/pay/automatch/", {}),
    "pay.match": ("dispute", lambda: f"/pay/{Payment.objects.filter(status='unmatched').first().payment_no}/match/", {"invoice_no": "x"}),
    "pay.dispute": ("dispute", lambda: f"/pay/{short()}/dispute/", {"type": "shortage", "amount": "1"}),
    "pay.accept": ("dispute", lambda: f"/pay/{short()}/accept/", {"reason": "x"}),
    "pay.link_dn": ("dispute", lambda: f"/pay/{short()}/link-dn/", {"dn_no": "x"}),
    "pay.dispute_status": ("dispute", lambda: f"/pay/disputes/{Dispute.objects.filter(status='submitted').first().case_no}/won/", {}),
    "promo.wizard": ("promo", lambda: "/promos/new/", {"act": "next", "name": "x"}),
    "promo.submit": ("promo", lambda: f"/promos/{promo('draft')}/submit/", {}),
    "promo.approve": ("promo", lambda: f"/promos/{promo('submitted')}/approve/", {"agreement": "79999999"}),
    "promo.reject": ("promo", lambda: f"/promos/{promo('submitted')}/reject/", {}),
    "promo.sold": ("promo", lambda: f"/promos/{promo('live')}/sold/", {}),
    "promo.chase": ("promo", lambda: f"/promos/{promo('dn_overdue')}/chase/", {}),
    "promo.claim": ("promo", lambda: f"/promos/{promo('dn_validated')}/claim/", {}),
    "dn.approve": ("dn", lambda: f"/dns/{dn('to_validate')}/approve/", {}),
    "dn.override": ("override", lambda: f"/dns/{dn('mismatch')}/override/", {"reason": "x"}),
    "dn.dispute": ("dn", lambda: f"/dns/{dn('mismatch')}/dispute/", {}),
    "dn.link": ("dn", lambda: f"/dns/{dn('unlinked')}/link/", {"ref": "x"}),
    "claim.cn": ("cn", lambda: f"/claims/{claim('sent')}/cn/", {"cn_no": "CN-1", "amount": "1"}),
    "claim.chase": ("cn", lambda: f"/claims/{claim('shortfall')}/chase/", {}),
    "claim.write_off": ("cn", lambda: f"/claims/{claim('shortfall')}/write-off/", {"reason": "x"}),
    "upload.U4": ("upload", lambda: "/uploads/new/", {"type": "U4", "sample": "1"}),
    "upload.U7_promotions": ("promo", lambda: "/uploads/new/", {"type": "U7", "sample": "1"}),
    "upload.U9_credit_notes": ("cn", lambda: "/uploads/new/", {"type": "U9", "sample": "1"}),
    "integrations.save": ("settings", lambda: f"/integrations/{connector()}/save/", {"mode": "file"}),
    "integrations.test": ("settings", lambda: f"/integrations/{connector()}/test/", {}),
    "integrations.sync": ("settings", lambda: f"/integrations/{connector()}/sync/", {}),
    "settings.rule": ("settings", lambda: "/settings/rules/R12/", {"field": "abs", "value": "1"}),
    "settings.fc_add": ("settings", lambda: "/settings/fcs/add/", {"code": "TST-FC1", "name": "Test"}),
    "settings.holiday": ("settings", lambda: "/settings/holidays/add/", {"day": "2030-01-01", "name": "Test"}),
    "settings.sku": ("settings", lambda: "/settings/skus/save/", {"sku_code": "T1", "model_no": "T1", "asin": "B0T1", "category": "PA"}),
    "settings.price": ("settings", lambda: f"/settings/prices/save/", {"sku_code": "x", "cost": "1", "valid_from": "2030-01-01"}),
    "matching.accept": ("dispute", lambda: f"/matching/{suggestion()}/accept/", {}),
    "matching.review": ("dn", lambda: f"/matching/review/dn_promo/{dn('unlinked')}/", {}),
    "matching.settings": ("settings", lambda: "/matching/settings/", {"field": "show_threshold", "value": "50"}),
    "matching.ai": ("settings", lambda: "/matching/settings/ai/", {"provider": "off"}),
}


def refused(r):
    t = toast(r)
    return t.get("tone") == "bad" and t.get("msg", "").startswith("This needs the")


@pytest.mark.parametrize("name", list(ACTIONS))
def test_action_follows_the_permission_matrix(name, as_user):
    perm, url, data = ACTIONS[name]
    allowed = [r for r in USERS if r in PERMS[perm]]
    for role, username in USERS.items():
        if role in allowed:
            continue
        r = as_user(username).post(url(), data)
        assert refused(r), f"{name}: {role} ({username}) was not refused — status {r.status_code}, toast {toast(r)}"
    if allowed:                                    # and a role the matrix includes is let through (no role refusal)
        r = as_user(USERS[allowed[0]]).post(url(), data)
        assert not refused(r), f"{name}: {allowed[0]} was refused although the matrix allows it"


@pytest.mark.parametrize("role,home", [("PIC", "/action/"), ("Planning", "/pos/"), ("Credit", "/pos/"), ("Logistics", "/ship/"),
                                       ("Product", "/promos/"), ("Finance", "/pay/"), ("Manager", "/")])
def test_each_role_lands_on_its_own_start_page(role, home):
    from django.test import Client
    from identity.models import User
    c = Client()
    c.force_login(User.objects.get(username=USERS[role]))
    assert c.get("/login/").url == home


def test_action_center_shows_each_role_its_own_work(as_user):
    """"My role" lists only the role's own items; Manager (and Admin) oversee everything."""
    from core.actions import action_items
    from identity.models import User
    everyone = action_items(None, mine=False)
    for role, username in USERS.items():
        mine = action_items(User.objects.get(username=username), mine=True)
        if role == "Manager":
            assert len(mine) == len(everyone)
        else:
            assert mine and all(role in i["roles"] for i in mine) and len(mine) < len(everyone), role
