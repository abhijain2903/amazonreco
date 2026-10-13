"""Amazon Selling Partner API client for vendors (retail procurement).

Covers the four APIs ME uses: Vendor Orders, Vendor Shipments, Vendor Invoices and Vendor Transaction Status
(models: github.com/amzn/selling-partner-api-models). Calls carry a Login with Amazon access token, obtained from the
app's refresh token; request signing (SigV4) is no longer required by Amazon.

The HTTP transport is a plain function so tests replace it; nothing here touches the database.
"""
import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request

from django.conf import settings

log = logging.getLogger("hub.spapi")

MAX_WINDOW_DAYS = 7          # getPurchaseOrders / getPurchaseOrdersStatus: date range of 7 days at most
RETRY_STATUS = (429, 500, 502, 503, 504)


class SpApiError(Exception):
    def __init__(self, message, status=None, errors=None):
        super().__init__(message)
        self.status = status
        self.errors = errors or []


def urllib_transport(method, url, headers, body=None, timeout=30):
    """(status, headers, text). Never raises for HTTP errors: the caller decides what an error status means."""
    data = body.encode() if isinstance(body, str) else body
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, dict(r.headers), r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers or {}), e.read().decode(errors="replace")


class Client:
    def __init__(self, transport=None, sleep=time.sleep):
        self.transport = transport or urllib_transport
        self.sleep = sleep
        self.endpoint = settings.SPAPI_ENDPOINT.rstrip("/")
        self._token, self._expires = None, 0

    @staticmethod
    def configured():
        return all([settings.SPAPI_CLIENT_ID, settings.SPAPI_CLIENT_SECRET, settings.SPAPI_REFRESH_TOKEN])

    @staticmethod
    def missing():
        names = {"HUB_SPAPI_CLIENT_ID": settings.SPAPI_CLIENT_ID, "HUB_SPAPI_CLIENT_SECRET": settings.SPAPI_CLIENT_SECRET,
                 "HUB_SPAPI_REFRESH_TOKEN": settings.SPAPI_REFRESH_TOKEN}
        return [k for k, v in names.items() if not v]

    # ---------- auth ----------
    def token(self):
        if self._token and time.time() < self._expires - 60:
            return self._token
        if not self.configured():
            raise SpApiError("Amazon SP-API credentials are not set on the server: " + ", ".join(self.missing()))
        body = urllib.parse.urlencode({"grant_type": "refresh_token", "refresh_token": settings.SPAPI_REFRESH_TOKEN,
                                       "client_id": settings.SPAPI_CLIENT_ID, "client_secret": settings.SPAPI_CLIENT_SECRET})
        status, _, text = self.transport("POST", settings.SPAPI_TOKEN_URL,
                                         {"Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"}, body)
        data = _json(text)
        if status != 200 or "access_token" not in data:
            raise SpApiError(f"Amazon login failed ({status}): {data.get('error_description') or data.get('error') or text[:200]}", status)
        self._token, self._expires = data["access_token"], time.time() + int(data.get("expires_in", 3600))
        return self._token

    # ---------- calls ----------
    def call(self, method, path, params=None, body=None):
        url = self.endpoint + path
        if params:
            url += "?" + urllib.parse.urlencode({k: _param(v) for k, v in params.items() if v not in (None, "")})
        payload = json.dumps(body) if body is not None else None
        for attempt in range(5):
            headers = {"x-amz-access-token": self.token(), "Accept": "application/json", "User-Agent": "ME-Vendor-Hub/1.0 (Language=Python)"}
            if payload is not None:
                headers["Content-Type"] = "application/json"
            status, rh, text = self.transport(method, url, headers, payload)
            if status in RETRY_STATUS and attempt < 4:
                wait = min(30, 2 ** attempt)
                log.warning("SP-API %s %s answered %s, retrying in %ss", method, path, status, wait)
                self.sleep(wait)
                continue
            if status == 403 and attempt == 0:
                self._token = None           # an expired token: log in again once
                continue
            data = _json(text)
            if status >= 400:
                errors = data.get("errors") or []
                msg = "; ".join(f"{e.get('code')}: {e.get('message')}" for e in errors) or text[:200]
                raise SpApiError(f"Amazon answered {status}: {msg}", status, errors)
            return data
        raise SpApiError(f"Amazon kept answering {status} for {path}", status)

    def _pages(self, path, params, key):
        params = dict(params)
        while True:
            data = self.call("GET", path, params)
            payload = data.get("payload") or {}
            yield from payload.get(key) or []
            nxt = (payload.get("pagination") or {}).get("nextToken")
            if not nxt:
                return
            params = {"nextToken": nxt}

    # ---------- Vendor Orders ----------
    def purchase_orders(self, **params):
        """getPurchaseOrders, every page. Use createdAfter/Before or changedAfter/Before (7 days at most)."""
        return self._pages("/vendor/orders/v1/purchaseOrders", {"includeDetails": "true", "limit": 100, **params}, "orders")

    def purchase_order(self, po_no):
        return (self.call("GET", f"/vendor/orders/v1/purchaseOrders/{urllib.parse.quote(po_no)}").get("payload")) or {}

    def purchase_order_statuses(self, **params):
        """getPurchaseOrdersStatus, every page: confirmation and receiving status per line."""
        return self._pages("/vendor/orders/v1/purchaseOrdersStatus", {"limit": 100, **params}, "ordersStatus")

    def submit_acknowledgements(self, acks):
        return _txid(self.call("POST", "/vendor/orders/v1/acknowledgements", body={"acknowledgements": acks}))

    # ---------- Vendor Shipments ----------
    def submit_shipment_confirmations(self, confirmations):
        return _txid(self.call("POST", "/vendor/shipping/v1/shipmentConfirmations", body={"shipmentConfirmations": confirmations}))

    # ---------- Vendor Invoices ----------
    def submit_invoices(self, invoices):
        return _txid(self.call("POST", "/vendor/payments/v1/invoices", body={"invoices": invoices}))

    # ---------- Vendor Transaction Status ----------
    def transaction(self, transaction_id):
        return (self.call("GET", f"/vendor/transactions/v1/transactions/{urllib.parse.quote(transaction_id)}").get("payload") or {}).get(
            "transactionStatus") or {}


def _param(v):
    if hasattr(v, "isoformat"):
        return v.isoformat().replace("+00:00", "Z")
    return v


def _json(text):
    try:
        return json.loads(text) if text else {}
    except ValueError:
        return {}


def _txid(data):
    t = (data.get("payload") or {}).get("transactionId")
    if not t:
        raise SpApiError("Amazon did not return a transaction ID.")
    return t
