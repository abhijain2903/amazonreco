"""Master-data commands. Imports never create master data; admins add it here."""
import re

from django.db import transaction

from core.services import CommandError, audit, require

from .models import FulfilmentCentre


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
