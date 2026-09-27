"""Upload pipeline: store -> parse -> map -> validate (preview) -> commit."""
import csv
import hashlib
import io
from datetime import date, datetime

from django.conf import settings
from django.core.files.base import ContentFile
from django.db import transaction

from core.services import CommandError, actor_name, audit, require
from integrations.connectors import count_in
from rules.services import get_cfg

from . import types
from .models import SavedMapping, UploadBatch, UploadRow

CONNECTOR_FOR = {"U4": "amazon_vc", "U6": "amazon_vc", "U8": "amazon_vc", "U3": "sap", "U5": "sap", "U1": "sap", "U9": "finance"}


def _cell(v):
    if v is None:
        return ""
    if isinstance(v, (datetime, date)):
        return v.strftime("%Y-%m-%d")
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v).strip()


def parse(filename, data: bytes):
    name = filename.lower()
    if name.endswith((".xlsx", ".xlsm")):
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        ws = wb.worksheets[0]
        rows = [[_cell(c) for c in r] for r in ws.iter_rows(values_only=True)]
    elif name.endswith((".csv", ".txt")):
        text = data.decode("utf-8-sig", errors="replace")
        delim = ";" if text.split("\n", 1)[0].count(";") > text.split("\n", 1)[0].count(",") else ","
        rows = [[c.strip() for c in r] for r in csv.reader(io.StringIO(text), delimiter=delim)]
    else:
        raise CommandError("Use a .csv or .xlsx file.")
    rows = [r for r in rows if any(c != "" for c in r)]
    if len(rows) < 2:
        raise CommandError("The file has no data rows under the header.")
    return rows


def signature(headers):
    return hashlib.sha1("|".join(types.norm_h(h) for h in headers).encode()).hexdigest()


def auto_map(tid, headers):
    saved = SavedMapping.objects.filter(upload_type=tid, header_signature=signature(headers)).first()
    if saved:
        return {f: {"idx": i, "how": "saved"} for f, i in saved.mapping.items()}
    m = {}
    nh = [types.norm_h(h) for h in headers]
    for f, _, syn in types.TYPES[tid]["cols"]:
        cands = {types.norm_h(x) for x in [f, *syn]}
        idx = next((i for i, h in enumerate(nh) if h in cands), -1)
        m[f] = {"idx": idx, "how": "auto" if idx >= 0 else "missing"}
    return m


def missing_required(batch):
    return [f for f, req, _ in types.TYPES[batch.upload_type]["cols"] if req and batch.mapping.get(f, {}).get("idx", -1) < 0]


@transaction.atomic
def create_batch(user, tid, filename, data: bytes):
    require(user, "upload")
    if tid not in types.TYPES:
        raise CommandError("Unknown upload type.")
    if len(data) > settings.HUB_MAX_UPLOAD_BYTES:
        raise CommandError("The file is larger than 25 MB.")
    rows = parse(filename, data)
    checksum = hashlib.sha256(data).hexdigest()
    b = UploadBatch(upload_type=tid, filename=filename, checksum=checksum, headers=rows[0], raw_rows=rows[1:],
                    rows_total=len(rows) - 1, uploaded_by=user if getattr(user, "pk", None) else None,
                    uploaded_by_name=actor_name(user))
    b.mapping = auto_map(tid, b.headers)
    b.file.save(filename, ContentFile(data), save=False)
    b.save()
    return b


def duplicate_of(batch):
    return UploadBatch.objects.filter(checksum=batch.checksum, status="committed").exclude(pk=batch.pk).first()


def set_mapping(batch, field, idx):
    batch.mapping[field] = {"idx": int(idx), "how": "manual"}
    batch.save(update_fields=["mapping", "updated_at"])


def row_dicts(batch):
    cols = [c[0] for c in types.TYPES[batch.upload_type]["cols"]]
    for r in batch.raw_rows:
        yield {f: (str(r[batch.mapping[f]["idx"]]).strip() if 0 <= batch.mapping[f]["idx"] < len(r) else "") for f in cols}


@transaction.atomic
def build_preview(user, batch):
    require(user, "upload")
    if missing_required(batch):
        raise CommandError("Map every required column first.")
    cfg = get_cfg()
    batch.rows.all().delete()
    objs, err, warn = [], 0, 0
    ctx = {}
    for i, o in enumerate(row_dicts(batch)):
        e, w = types.validate(batch.upload_type, o, ctx, cfg)
        err += bool(e)
        warn += bool(w) and not e
        objs.append(UploadRow(batch=batch, row_no=i + 2, data=o, errors=e, warnings=w))
    UploadRow.objects.bulk_create(objs)
    batch.error_count, batch.warning_count, batch.status = err, warn, "preview"
    batch.save()
    SavedMapping.objects.update_or_create(upload_type=batch.upload_type, header_signature=signature(batch.headers),
                                          defaults={"mapping": {f: m["idx"] for f, m in batch.mapping.items()}})
    return batch


@transaction.atomic
def commit(user, batch_id):
    require(user, "upload")
    batch = UploadBatch.objects.select_for_update().get(pk=batch_id)
    if batch.status != "preview":
        raise CommandError("This upload was already imported.")
    ok = [r.data for r in batch.rows.all() if not r.errors]
    if not ok:
        raise CommandError("There are no valid rows to import.")
    created, updated, lines = types.apply(batch.upload_type, ok, user)
    batch.created_count, batch.updated_count, batch.summary, batch.status = created, updated, lines, "committed"
    batch.save()
    if batch.upload_type in CONNECTOR_FOR:
        count_in(CONNECTOR_FOR[batch.upload_type], created + updated)
    audit("upload", batch.pk, f"{types.TYPES[batch.upload_type]['name']}: {batch.filename} imported "
          f"({created} created, {updated} updated, {batch.error_count} rows skipped)", user, action="commit")
    return batch


def sample(tid):
    name, rows = types.sample_rows(tid)
    buf = io.StringIO()
    csv.writer(buf).writerows(rows)
    return name, buf.getvalue().encode()
