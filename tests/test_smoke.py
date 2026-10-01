"""Every page, drawer tab and dialog renders for an admin and for the PIC (no template or query errors)."""
import pytest

pytestmark = pytest.mark.django_db

PAGES = ["/", "/action/", "/pos/", "/pos/?view=board", "/ship/", "/pay/", "/pay/?tab=match", "/pay/?tab=matched", "/pay/?tab=disputes",
         "/promos/", "/promos/?view=board", "/promos/?view=timeline", "/promos/?tab=after", "/dns/", "/dns/?tab=mismatch",
         "/dns/?tab=unlinked", "/dns/?tab=validated", "/dns/?tab=disputed", "/claims/", "/claims/?tab=sent", "/claims/?tab=shortfall",
         "/claims/?tab=closed", "/uploads/", "/integrations/", "/settings/", "/api/v1/purchase-orders", "/api/v1/action-items",
         "/search/?q=MECL", "/notifications/", "/promos/new/", "/uploads/new/"]
PAGES += [f"/settings/?tab={t}" for t in ["prices", "rules", "cats", "fcs", "users", "notify", "numbering"]]
PAGES += [f"/ship/?tab={t}" for t in ["asn", "slot", "transit", "invoice", "submitted"]]
PAGES += [f"/pos/?tab={t}" for t in ["new", "book", "release", "ship", "done", "all"]]
PAGES += [f"/uploads/new/?type=U{i}" for i in range(1, 10)] + [f"/uploads/template/U{i}/" for i in range(1, 10)]


def _records():
    from catalog.models import Sku
    from claims.models import Claim
    from debitnotes.models import DebitNote
    from integrations.models import Connector
    from orders.models import PurchaseOrder
    from payments.models import Dispute, Payment
    from promotions.models import Promotion
    urls = [f"/records/po/{p.po_no}/?tab={t}" for p in PurchaseOrder.objects.all()
            for t in ["lines", "shipment", "invoice", "checks", "timeline", "notes", "docs"]]
    urls += [f"/records/promo/{p.mecl_ref}/?tab={t}" for p in Promotion.objects.all() for t in ["models", "dn", "claim", "timeline", "notes", "docs"]]
    urls += [u for d in DebitNote.objects.all() for u in (f"/records/dn/{d.dn_no}/", f"/dns/{d.dn_no}/override/")]
    urls += [f"/records/payment/{p.payment_no}/" for p in Payment.objects.all()]
    urls += [f"/records/dispute/{d.case_no}/" for d in Dispute.objects.all()]
    urls += [f"/records/conn/{c.key}/?mode={m}" for c in Connector.objects.all() for m in ["file", "api", "odata", "off"]]
    urls += [f"/claims/{c.claim_no}/cn/" for c in Claim.objects.all()]
    urls.append(f"/records/sku/{Sku.objects.first().sku_code}/")
    return urls


@pytest.mark.parametrize("username", ["admin", "faisal"])
def test_everything_renders(username, as_user):
    c = as_user(username)
    bad = []
    for url in PAGES + _records():
        # Drawers and dialogs are HTMX partials; without the header a record URL redirects to its list page.
        r = c.get(url, HTTP_HX_REQUEST="true") if url.startswith("/records/") else c.get(url)
        if r.status_code not in (200, 204, 302):
            bad.append((url, r.status_code))
    assert not bad, bad
