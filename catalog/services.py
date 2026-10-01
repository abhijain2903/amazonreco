"""Master-data commands. Imports never create master data; admins add it here."""
import re

from django.db import transaction
from django.utils import timezone

from core.services import CommandError, audit, require

from .models import CATEGORY_NAMES, FulfilmentCentre, Price, Sku


@transaction.atomic
def add_fc(user, code, name, city=""):
    require(user, "settings")
    code, name, city = (code or "").strip().upper(), (name or "").strip(), (city or "").strip()
    if not re.fullmatch(r"[A-Z0-9][A-Z0-9-]{1,19}", code):
        raise CommandError("Enter the Amazon FC code as it appears in Vendor Central, e.g. RUH-FC1.")
    if not name:
        raise CommandError("Give the fulfilment centre a name.")
    if FulfilmentCentre.objects.filter(code=code).exists():
        raise CommandError(f"FC {code} is already in the FC master.")
    fc = FulfilmentCentre.objects.create(code=code, name=name, city=city)
    audit("fc", code, f"Fulfilment centre {code} added ({name}{', ' + city if city else ''})", user, action="create",
          after={"code": code, "name": name, "city": city})
    return fc


@transaction.atomic
def upsert_sku(user, sku_code, model_no, asin, category, ean="", description="", case_pack=1):
    """Add or edit one SKU from Settings (the same fields as upload U1)."""
    require(user, "settings")
    sku_code, model_no, asin = (sku_code or "").strip(), (model_no or "").strip(), (asin or "").strip().upper()
    if not (sku_code and model_no and asin):
        raise CommandError("SKU code, model number and ASIN are required.")
    if category not in CATEGORY_NAMES:
        raise CommandError("Pick a category.")
    try:
        case_pack = max(1, int(case_pack or 1))
    except ValueError:
        raise CommandError("Case pack is a whole number of units.")
    s = Sku.objects.filter(sku_code=sku_code).first()
    for field, val in (("model_no", model_no), ("asin", asin)):
        clash = Sku.objects.filter(**{field: val}).exclude(sku_code=sku_code).first()
        if clash:
            raise CommandError(f"{val} already belongs to SKU {clash.sku_code}.")
    vals = dict(model_no=model_no, asin=asin, category=category, ean=(ean or "").strip(), description=(description or "").strip()[:120],
                case_pack=case_pack)
    before = {k: getattr(s, k) for k in vals} if s else None
    if s:
        for k, v in vals.items():
            setattr(s, k, v)
        s.bump()
        s.save()
    else:
        s = Sku.objects.create(sku_code=sku_code, **vals)
    audit("sku", sku_code, f"SKU {'updated' if before else 'added'} in Settings: {model_no} / {asin}", user, action="master_data",
          before=before, after=vals)
    return s, before is None


@transaction.atomic
def set_price(user, sku_code, cost_sar, valid_from):
    """A new agreed price from a date. The open price closes the day before, so R1 can still check older POs against
    the price that was valid on their order date. POs waiting for confirmation are re-checked."""
    from datetime import date, timedelta

    from core.services import to_h
    from orders.services import refresh_open_pos
    require(user, "settings")
    s = Sku.objects.filter(sku_code=(sku_code or "").strip()).first()
    if not s:
        raise CommandError(f"SKU {sku_code} is not in the SKU master.")
    try:
        cost_h, vf = to_h(cost_sar), date.fromisoformat(valid_from)
    except (ValueError, ArithmeticError):
        raise CommandError("Enter the agreed cost in SAR and the date it applies from.")
    if cost_h <= 0:
        raise CommandError("The agreed cost must be above 0.")
    Price.objects.filter(sku=s, valid_to__isnull=True, valid_from__lt=vf).update(valid_to=vf - timedelta(days=1))
    Price.objects.filter(sku=s, valid_from=vf).delete()
    Price.objects.create(sku=s, cost_h=cost_h, valid_from=vf)
    before = s.cost_h
    if vf <= timezone.localdate():
        s.cost_h = cost_h
        s.save(update_fields=["cost_h", "updated_at"])
    audit("sku", s.sku_code, f"Agreed cost set to SAR {cost_h / 100:,.2f} from {vf:%d %b %Y}", user, action="price",
          before={"cost_h": before}, after={"cost_h": cost_h, "valid_from": vf.isoformat()})
    refresh_open_pos()
    return s
