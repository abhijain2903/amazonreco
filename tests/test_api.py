import pytest
from django.test import Client, override_settings

pytestmark = pytest.mark.django_db


def test_api_requires_auth():
    assert Client().get("/api/v1/purchase-orders").status_code == 401
    assert Client().get("/api/v1/health").status_code == 200


@override_settings(HUB_API_TOKEN="t0ken", HUB_API_USER="admin")
def test_api_token_and_session():
    c = Client()
    r = c.get("/api/v1/purchase-orders?stage=new", HTTP_AUTHORIZATION="Bearer t0ken")
    assert r.status_code == 200 and r.json() and all(p["stage"] == "new" for p in r.json())
    po = r.json()[0]["po_no"]
    d = c.get(f"/api/v1/purchase-orders/{po}", HTTP_AUTHORIZATION="Bearer t0ken").json()
    assert d["lines"] and {"check", "agreed_sar"} <= set(d["lines"][0])
    assert c.get("/api/v1/purchase-orders", HTTP_AUTHORIZATION="Bearer wrong").status_code == 401


def test_api_command_error_is_400():
    from identity.models import User
    c = Client()
    c.force_login(User.objects.get(username="khalid"))
    from orders.models import PurchaseOrder
    po = PurchaseOrder.objects.filter(stage="new").first()
    r = c.post(f"/api/v1/purchase-orders/{po.po_no}/confirm", data={}, content_type="application/json",
               HTTP_X_CSRFTOKEN="x")
    assert r.status_code in (403, 400)


def test_openapi_schema():
    from identity.models import User
    c = Client()
    c.force_login(User.objects.get(username="admin"))
    s = c.get("/api/v1/openapi.json").json()
    assert "/api/v1/purchase-orders" in s["paths"]


@override_settings(DEMO_PASSWORD="let-me-in", DEV_LOGIN=True)
def test_demo_access_code_guards_dev_login():
    c = Client()
    r = c.post("/login/", {"user": "faisal", "code": "wrong"})
    assert r.status_code == 200 and b"access code is not right" in r.content
    r = c.post("/login/", {"user": "faisal", "code": "let-me-in"})
    assert r.status_code == 302
