"""Upload pipeline for every type, using the sample files built from the example data."""
import io

import openpyxl
import pytest

from uploads import services as svc
from uploads.models import UploadBatch
from uploads.types import TYPES

from .conftest import toast

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize("tid", list(TYPES))
def test_sample_file_imports(tid, as_user):
    c = as_user("admin")
    r = c.post("/uploads/new/", {"type": tid, "sample": "1"})
    assert r.status_code == 200 and b"Map columns" in r.content
    b = UploadBatch.objects.latest("created_at")
    assert not svc.missing_required(b), "sample columns should auto-map"
    r = c.post(f"/uploads/{b.pk}/preview/")
    assert r.status_code == 200 and b"Import" in r.content
    b.refresh_from_db()
    assert b.status == "preview"
    if b.rows.filter(errors=[]).exists():
        r = c.post(f"/uploads/{b.pk}/commit/")
        assert r.status_code == 200 and b"Import complete" in r.content, toast(r)
        b.refresh_from_db()
        assert b.status == "committed" and b.created_count + b.updated_count > 0


def test_xlsx_with_other_headers_maps_and_remembers(as_user):
    from catalog.models import Sku
    s = Sku.objects.first()
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Material", "Unrestricted", "Comment"])
    ws.append([s.sku_code, 77, "x"])
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    buf.name = "stock.xlsx"
    c = as_user("noura")
    r = c.post("/uploads/new/", {"type": "U3", "file": buf})
    assert b"All required columns matched" in r.content
    b = UploadBatch.objects.latest("created_at")
    c.post(f"/uploads/{b.pk}/preview/")
    c.post(f"/uploads/{b.pk}/commit/")
    s.refresh_from_db()
    assert s.free_stock == 77


def test_missing_column_blocks_preview(as_user):
    c = as_user("admin")
    f = io.BytesIO(b"foo,bar\n1,2\n")
    f.name = "bad.csv"
    r = c.post("/uploads/new/", {"type": "U3", "file": f})
    assert b"not found" in r.content
    b = UploadBatch.objects.latest("created_at")
    r = c.post(f"/uploads/{b.pk}/preview/")
    assert b"Map every required column" in r.content
    r = c.post(f"/uploads/{b.pk}/map/", {"map-sku_code": "0", "map-free_stock": "1"})
    assert b"All required columns matched" in r.content


def test_wrong_file_type_and_role(as_user):
    f = io.BytesIO(b"x")
    f.name = "notes.pdf"
    r = as_user("admin").post("/uploads/new/", {"type": "U3", "file": f})
    assert b"Use a .csv or .xlsx file" in r.content
    r = as_user("omar").post("/uploads/new/", {"type": "U3", "sample": "1"})   # Credit control cannot upload
    assert toast(r)["tone"] == "bad"
