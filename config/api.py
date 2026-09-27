"""JSON API (/api/v1/). OpenAPI docs at /api/v1/docs.

Same service layer as the screens, so every rule, permission and audit entry applies.
Auth: the browser session (staff) or a Bearer token for system integrations (HUB_API_TOKEN).
"""
import hmac
from datetime import datetime
from typing import List, Optional

from django.conf import settings
from django.contrib.auth import get_user_model
from ninja import File, NinjaAPI, Schema
from ninja.errors import HttpError
from ninja.files import UploadedFile
from ninja.security import HttpBearer, django_auth

from core.services import CommandError, Forbidden, StaleRecord


class ApiToken(HttpBearer):
    def authenticate(self, request, token):
        want = settings.HUB_API_TOKEN
        if want and hmac.compare_digest(token, want):
            user = get_user_model().objects.filter(username=settings.HUB_API_USER, is_active=True).first()
            if user:
                request.user = user
                return user
        return None


api = NinjaAPI(title="ME Vendor Hub API", version="1.0", auth=[django_auth, ApiToken()], urls_namespace="api-v1",
               description="Amazon order-to-cash and promotion-to-claim for Modern Electronics.")


@api.exception_handler(CommandError)
def command_error(request, exc):
    status = 403 if isinstance(exc, Forbidden) else 409 if isinstance(exc, StaleRecord) else 400
    return api.create_response(request, {"detail": str(exc)}, status=status)


# ---------- schemas ----------
class PoLineOut(Schema):
    id: str
    sku_code: str
    model_no: str
    asin: str
    qty_ordered: int
    qty_confirmed: int
    cost_sar: float
    agreed_sar: float
    decision: str
    reason: str
    check: str


class PoOut(Schema):
    po_no: str
    fc: str
    stage: str
    order_date: datetime
    confirm_by: datetime
    value_sar: float
    sap_order_no: Optional[str] = None
    version: int
    lines: Optional[List[PoLineOut]] = None


class ConfirmIn(Schema):
    version: Optional[int] = None


class PromoOut(Schema):
    mecl_ref: str
    name: str
    category: str
    start: datetime
    end: datetime
    dn_due: datetime
    agreement_no: Optional[str] = None
    stage: str
    support_sar: float


class DnOut(Schema):
    dn_no: str
    agreement_no: str
    dn_date: datetime
    status: str
    charged_sar: float
    expected_sar: Optional[float] = None
    variance_sar: Optional[float] = None
    mecl_ref: Optional[str] = None


class ActionOut(Schema):
    key: str
    severity: str
    title: str
    subtitle: str
    amount_sar: Optional[float] = None
    due: Optional[datetime] = None
    roles: List[str]
    url: str


class UploadOut(Schema):
    id: str
    upload_type: str
    filename: str
    status: str
    rows: int
    errors: int
    warnings: int
    missing_columns: List[str]


sar = lambda h: None if h is None else round(h / 100, 2)


def _po_out(po, with_lines=False):
    from orders.services import line_checks, po_lines, po_value_h
    lines = po_lines(po)
    out = dict(po_no=po.po_no, fc=po.fc.code, stage=po.stage, order_date=po.order_date, confirm_by=po.confirm_by,
               value_sar=sar(po_value_h(po, lines)), sap_order_no=po.sap_order_no, version=po.version)
    if with_lines:
        out["lines"] = []
        for l in lines:
            c = line_checks(l)
            out["lines"].append(dict(id=str(l.pk), sku_code=l.sku.sku_code, model_no=l.sku.model_no, asin=l.asin, qty_ordered=l.qty_ordered,
                                     qty_confirmed=l.qty_confirmed, cost_sar=sar(l.cost_h), agreed_sar=sar(c["agreed_h"]),
                                     decision=l.decision, reason=l.reason, check=c["tone"]))
    return out


# ---------- endpoints ----------
@api.get("/health", auth=None, tags=["system"])
def health(request):
    return {"ok": True}


@api.get("/purchase-orders", response=List[PoOut], tags=["orders"])
def list_pos(request, stage: Optional[str] = None, limit: int = 100):
    from orders.models import PurchaseOrder
    qs = PurchaseOrder.objects.select_related("fc").order_by("-order_date")
    if stage:
        qs = qs.filter(stage=stage)
    return [_po_out(p) for p in qs[:min(limit, 500)]]


@api.get("/purchase-orders/{po_no}", response=PoOut, tags=["orders"])
def get_po(request, po_no: str):
    from orders.services import get_po as svc_get
    return _po_out(svc_get(po_no), with_lines=True)


@api.post("/purchase-orders/{po_no}/confirm", response=PoOut, tags=["orders"])
def confirm(request, po_no: str, body: ConfirmIn):
    from orders.services import confirm_po
    po, _ = confirm_po(request.user, po_no, body.version)
    return _po_out(po, with_lines=True)


@api.get("/promotions", response=List[PromoOut], tags=["promotions"])
def list_promos(request, stage: Optional[str] = None):
    from promotions.models import Promotion
    from promotions.services import stage_of, support_h
    out = []
    for p in Promotion.objects.prefetch_related("lines"):
        st = stage_of(p)
        if stage and st != stage:
            continue
        out.append(dict(mecl_ref=p.mecl_ref, name=p.name, category=p.category, start=p.start, end=p.end, dn_due=p.dn_due,
                        agreement_no=p.agreement_no, stage=st, support_sar=sar(support_h(p))))
    return out


@api.get("/debit-notes", response=List[DnOut], tags=["promotions"])
def list_dns(request, status: Optional[str] = None):
    from debitnotes.models import DebitNote
    from debitnotes.services import evaluate
    out = []
    for d in DebitNote.objects.prefetch_related("lines__sku"):
        e = evaluate(d)
        if status and e["status"] != status:
            continue
        out.append(dict(dn_no=d.dn_no, agreement_no=d.agreement_no, dn_date=d.dn_date, status=e["status"], charged_sar=sar(e["charged_h"]),
                        expected_sar=sar(e["expected_h"]), variance_sar=sar(e["variance_h"]), mecl_ref=e["promo"].mecl_ref if e["promo"] else None))
    return out


@api.get("/action-items", response=List[ActionOut], tags=["work"])
def actions(request, mine: bool = True):
    from core.actions import action_items
    return [dict(key=i["key"], severity=i["sev"], title=i["title"], subtitle=i["sub"], amount_sar=sar(i.get("amt")), due=i.get("due"),
                 roles=i["roles"], url=i["cta"].get("url", "")) for i in action_items(request.user, mine)]


@api.post("/uploads/{upload_type}", response=UploadOut, tags=["uploads"])
def upload(request, upload_type: str, file: UploadedFile = File(...), preview: bool = True):
    """Create an upload batch (same pipeline as the Uploads screen). With preview=true the rows are validated;
    commit with POST /uploads/{id}/commit once there are no missing columns."""
    from uploads import services as us
    b = us.create_batch(request.user, upload_type, file.name, file.read())
    missing = us.missing_required(b)
    if preview and not missing:
        us.build_preview(request.user, b)
    return dict(id=str(b.pk), upload_type=b.upload_type, filename=b.filename, status=b.status, rows=b.rows_total,
                errors=b.error_count, warnings=b.warning_count, missing_columns=missing)


@api.post("/uploads/{batch_id}/commit", response=UploadOut, tags=["uploads"])
def upload_commit(request, batch_id: str):
    from uploads import services as us
    b = us.commit(request.user, batch_id)
    return dict(id=str(b.pk), upload_type=b.upload_type, filename=b.filename, status=b.status, rows=b.rows_total,
                errors=b.error_count, warnings=b.warning_count, missing_columns=[])

