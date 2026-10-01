"""Reconciliation matching: similarity features, the seeded real-world cases, accepting/rejecting suggestions, and the
Claude layer against a local stand-in for the Messages API (no network, no key)."""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from debitnotes.models import DebitNote
from debitnotes.services import evaluate
from matching import ai, features as f, matchers
from matching import services as ms
from matching.models import MatchSettings, MatchSuggestion
from payments.models import Payment

from .conftest import toast

pytestmark = pytest.mark.django_db


# ---------- features ----------
def test_reference_similarity():
    assert f.ref_similarity("MEI-2026-04312", "MEI-2026-04312")[0] == 1
    assert f.ref_similarity("mei/2026/04312", "MEI-2026-04312") == (1.0, "same reference, different formatting")
    s, why = f.ref_similarity("71060476", "71060467")
    assert s == 0.92 and "swapped" in why and "76 / 67" in why
    assert f.ref_similarity("MEI-26-04312", "MEI-2026-04312")[0] == 0.88          # trimmed year, last digits agree
    assert f.ref_similarity("MEI-2026-04312", "MEI-2026-04313")[0] >= 0.86        # one character
    assert f.ref_similarity("MULTIPLE INVOICES", "MEI-2026-04312")[0] < 0.5


def test_amount_and_combinations():
    assert f.amount_score(10000, 10050, 100)[0] == 1
    assert f.amount_score(9000, 10000, 100)[0] < 1 and "less" in f.amount_score(9000, 10000, 100)[1]
    combos = f.subset_sum(700, [("a", 300), ("b", 400), ("c", 250), ("d", 450)], tol_h=0)
    assert ["b", "a"] in combos and ["d", "c"] in combos
    assert f.subset_sum(701, [("a", 300), ("b", 400)], tol_h=0) == []


# ---------- the seeded cases ----------
def pay(ref=None, reason=None):
    q = Payment.objects.all()
    q = q.filter(invoice_ref=ref) if ref else q.filter(reason__startswith=reason)
    return q.get()


def typo_payment():
    return Payment.objects.get(status="unmatched", invoice_ref__startswith="MEI/")


def test_mistyped_reference_is_found():
    p = typo_payment()
    top = matchers.payment_invoice(p)[0]
    assert top["score"] >= 90 and any("swapped" in r for r in top["reasons"])
    assert f.transposition(f.norm(p.invoice_ref), f.norm(top["targets"][0]))


def test_combined_payment_is_split_across_invoices():
    from billing.models import Invoice
    p = pay("MULTIPLE INVOICES")
    top = matchers.payment_invoice(p)[0]
    assert len(top["targets"]) == 2
    assert sum(Invoice.objects.get(invoice_no=n).total_h for n in top["targets"]) == p.paid_h


def test_promo_deduction_points_to_its_debit_note():
    p = pay(reason="Promotional allowance")
    top = matchers.payment_debit_note(p)[0]
    dn = DebitNote.objects.get(dn_no=top["targets"][0])
    assert dn.agreement_no in p.reason and top["score"] >= 90
    assert matchers.deduction(p)["action"] == "link_dn"


def test_unlinked_debit_note_finds_its_promotion():
    dn = next(d for d in DebitNote.objects.filter(validated=False) if evaluate(d)["status"] == "unlinked")
    top = matchers.debit_note_promotion(dn)[0]
    assert top["score"] >= 90 and any("swapped" in r for r in top["reasons"])


def test_badly_wrong_amount_is_not_suggested():
    p = typo_payment()
    shown = ms.refresh("pay_inv", p)
    assert all("more than the total" not in " ".join(s.reasons) or s.score < 50 for s in shown)


# ---------- accepting and rejecting ----------
def accept(client, s):
    return client.post(f"/matching/{s.pk}/accept/", {"version": s.version})


def test_accept_single_match(as_user):
    p = typo_payment()
    s = ms.refresh("pay_inv", p)[0]
    r = accept(as_user("priya"), s)
    assert toast(r)["tone"] == "ok"
    p.refresh_from_db()
    assert p.status == "matched" and p.invoice.invoice_no == s.targets[0] and p.po.stage == "paid"
    s.refresh_from_db()
    assert s.status == "accepted" and s.decided_by_name
    assert not MatchSuggestion.objects.filter(source=p.payment_no, status="pending").exists()
    from core.models import AuditEvent
    assert AuditEvent.objects.filter(entity="payment", entity_id=p.payment_no, action="suggestion_accepted").exists()


def test_accept_combined_payment(as_user):
    p = pay("MULTIPLE INVOICES")
    s = ms.refresh("pay_inv", p)[0]
    accept(as_user("priya"), s)
    parts = list(Payment.objects.filter(payment_no__startswith=p.payment_no))
    assert len(parts) == 2 and all(x.status == "matched" and x.po.stage == "paid" for x in parts)
    assert sum(x.paid_h for x in parts) == p.paid_h
    assert sorted(x.invoice.invoice_no for x in parts) == sorted(s.targets)


def test_accept_debit_note_link_for_deduction(as_user):
    p = pay(reason="Promotional allowance")
    s = ms.refresh("pay_dn", p)[0]
    accept(as_user("priya"), s)
    p.refresh_from_db()
    assert p.status == "accepted" and f"linked to {s.targets[0]}" in p.reason
    other = Payment.objects.filter(status="short").exclude(pk=p.pk).first()   # the same DN cannot be used twice
    r = as_user("priya").post(f"/pay/{other.payment_no}/link-dn/", {"dn_no": s.targets[0]})
    assert toast(r)["tone"] == "bad" and "already linked" in toast(r)["msg"]
    r = as_user("priya").post(f"/pay/{other.payment_no}/link-dn/", {"dn_no": "VCDN-0000000"})
    assert toast(r)["tone"] == "bad" and "not a validated debit note" in toast(r)["msg"]


def test_accept_promotion_for_unlinked_dn(as_user):
    dn = next(d for d in DebitNote.objects.filter(validated=False) if evaluate(d)["status"] == "unlinked")
    s = ms.refresh("dn_promo", dn)[0]
    r = accept(as_user("faisal"), s)
    assert toast(r)["tone"] == "ok"
    dn.refresh_from_db()
    assert evaluate(dn)["status"] != "unlinked"


def test_rejected_pair_is_never_proposed_again(as_user):
    p = typo_payment()
    s = ms.refresh("pay_inv", p)[0]
    as_user("priya").post(f"/matching/{s.pk}/reject/", {"version": s.version})
    assert s.targets not in [x.targets for x in ms.refresh("pay_inv", p)]


def test_stale_and_forbidden_decisions_refused(as_user):
    p = typo_payment()
    s = ms.refresh("pay_inv", p)[0]
    assert toast(as_user("omar").post(f"/matching/{s.pk}/accept/", {"version": s.version}))["tone"] == "bad"   # Credit control
    assert toast(as_user("priya").post(f"/matching/{s.pk}/accept/", {"version": s.version - 1}))["tone"] == "bad"
    p.refresh_from_db()
    assert p.status == "unmatched"


def test_run_auto_match_only_takes_confident_single_matches(as_user):
    typo, hinted = typo_payment().payment_no, Payment.objects.get(status="unmatched", hint__gt="").payment_no
    combined = pay("MULTIPLE INVOICES").payment_no
    r = as_user("priya").post("/pay/automatch/")
    assert toast(r)["msg"] == "2 payments matched"
    assert Payment.objects.get(payment_no=typo).status == "matched"          # score 96, far ahead of the next
    assert Payment.objects.get(payment_no=hinted).status == "matched"        # invoice number in the remittance advice
    assert Payment.objects.get(payment_no=combined).status == "unmatched"    # several invoices: a person confirms
    inv = [p.invoice_id for p in Payment.objects.filter(payment_no__in=[typo, hinted])]
    assert len(set(inv)) == 2                                                # no invoice matched twice


def test_automatic_matching_when_switched_on():
    cfg = MatchSettings.get()
    cfg.auto_apply, cfg.auto_threshold = True, 95
    cfg.save()
    p = typo_payment()
    rows = ms.refresh("pay_inv", p)
    p.refresh_from_db()
    assert p.status == "matched" and rows[0].status == "auto"


# ---------- screens ----------
def test_drawers_show_suggestions(as_user):
    c = as_user("priya")
    r = c.get(f"/records/payment/{typo_payment().payment_no}/")
    assert b"Suggested match" in r.content and b"swapped" in r.content and b"Accept" in r.content
    p = pay(reason="Promotional allowance")
    r = c.get(f"/records/po/{p.po.po_no}/?tab=invoice")
    assert b"Deduction on" in r.content and b"link to the debit note" in r.content and b"Suggested debit note" in r.content
    r = c.get(f"/pay/{pay(reason='Shortage').payment_no}/dispute/")
    assert b"Proof of delivery attached" in r.content and b"Drafted by" in r.content
    assert b"Ask AI" not in r.content      # AI not connected in tests


def test_matching_settings(as_user):
    assert toast(as_user("faisal").get("/settings/?tab=matching"))["tone"] == "bad"
    r = as_user("admin").get("/settings/?tab=matching")
    assert b"Suggestions come from the matching rules only" in r.content
    assert toast(as_user("admin").post("/matching/settings/", {"field": "show_threshold", "value": "65"}))["tone"] == "ok"
    assert MatchSettings.get().show_threshold == 65
    assert toast(as_user("admin").post("/matching/settings/", {"field": "auto_threshold", "value": "50"}))["tone"] == "bad"
    assert toast(as_user("faisal").post("/matching/settings/", {"field": "auto_apply", "value": "on"}))["tone"] == "bad"


def test_did_you_mean_on_imports():
    from claims.models import Claim
    c = Claim.objects.filter(status="sent").first()
    typo = c.claim_no[:-2] + c.claim_no[-1] + c.claim_no[-2] if c.claim_no[-1] != c.claim_no[-2] else c.claim_no + "9"
    assert matchers.closest_claim(typo) == c.claim_no
    from catalog.models import Sku
    s = Sku.objects.first()
    assert s.model_no in (matchers.closest_sku(s.model_no.replace("-", "")) or "")


# ---------- Claude layer against a local stand-in for the Messages API ----------
class FakeClaude:
    """Answers POST /v1/messages with a canned structured reply and records what it was sent."""

    def __init__(self, answer, stop_reason="end_turn", status=200):
        self.answer, self.stop_reason, self.status, self.requests = answer, stop_reason, status, []
        outer = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.requests.append(dict(path=self.path, headers=dict(self.headers), body=body))
                out = json.dumps({"id": "msg_test", "type": "message", "role": "assistant", "model": body["model"],
                                  "content": [{"type": "text", "text": json.dumps(outer.answer)}], "stop_reason": outer.stop_reason,
                                  "stop_sequence": None, "usage": {"input_tokens": 10, "output_tokens": 5}}).encode()
                if outer.status != 200:
                    out = json.dumps({"type": "error", "error": {"type": "authentication_error", "message": "invalid x-api-key"}}).encode()
                self.send_response(outer.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

            def log_message(self, *a):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def close(self):
        self.server.shutdown()


@pytest.fixture
def claude(settings, monkeypatch):
    def start(answer, stop_reason="end_turn"):
        fake = FakeClaude(answer, stop_reason)
        settings.AI_PROVIDER, settings.AI_MODEL = "anthropic", "claude-opus-5"
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        monkeypatch.setenv("ANTHROPIC_BASE_URL", fake.url)
        monkeypatch.setattr(ai, "_client", None)
        started.append(fake)
        return fake
    started = []
    yield start
    for s in started:
        s.close()
    ai._client = None


def test_claude_breaks_a_close_call(claude, as_user):
    p = Payment.objects.get(status="unmatched", hint__gt="")
    cands = matchers.payment_invoice(p)
    assert len(cands) > 1
    fake = claude({"choice": 1, "confidence": 70, "rationale": "Amazon trims the year; the amount only fits this invoice."})
    MatchSettings.objects.all().delete()
    ms.AI_CLOSE_CALL = 100   # treat every list as a close call for this test
    try:
        rows = ms.refresh("pay_inv", p, use_ai=True)
    finally:
        ms.AI_CLOSE_CALL = 10
    assert rows[1].method == "ai" and rows[1].ai_model == "claude-opus-5" and "trims the year" in rows[1].ai_rationale
    req = fake.requests[0]
    assert req["body"]["model"] == "claude-opus-5"
    assert req["body"]["output_config"]["format"]["type"] == "json_schema" and req["body"]["output_config"]["effort"] == "low"
    assert req["body"]["fallbacks"] == "default" and "server-side-fallback-2026-07-01" in req["headers"].get("anthropic-beta", "")
    sent = json.dumps(req["body"])
    assert p.invoice_ref in sent and "@" not in sent              # the record, and no e-mail addresses or people


def test_claude_reads_a_deduction(claude, as_user):
    p = pay(reason="Shortage")
    claude({"type": "shortage", "action": "dispute", "confidence": 88, "rationale": "ASN and invoice agree; delivery was on time.",
            "dispute_note": "ASN quantities were delivered in full on the booked slot. Please reverse SAR 2,300."})
    r = as_user("priya").post(f"/matching/review/deduction/{p.payment_no}/", {"ai": "1"})
    assert toast(r)["msg"].startswith("AI (claude-opus-5)")
    s = MatchSuggestion.objects.get(kind="deduction", source=p.payment_no, status="pending")
    assert s.method == "ai" and s.extra["action"] == "dispute" and "reverse SAR 2,300" in s.extra["note"]
    assert b"Drafted by AI (claude-opus-5)" in as_user("priya").get(f"/pay/{p.payment_no}/dispute/").content


def test_refusal_or_outage_keeps_the_rules_result(claude, settings, monkeypatch):
    p = pay(reason="Shortage")
    claude({"type": "other"}, stop_reason="refusal")
    assert ms.refresh_deduction(p, use_ai=True).method == "rules"
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://127.0.0.1:9")   # nothing listening
    monkeypatch.setattr(ai, "_client", None)
    import anthropic
    monkeypatch.setattr(ai, "_get_client", lambda conn=None: anthropic.Anthropic(max_retries=0, timeout=2))
    assert ms.refresh_deduction(p, use_ai=True).method == "rules"


def test_ask_claude_needs_ai_switched_on(as_user):
    r = as_user("priya").post(f"/matching/review/pay_inv/{typo_payment().payment_no}/", {"ai": "1"})
    assert toast(r)["tone"] == "bad" and "not switched on" in toast(r)["msg"]


# ---------- connecting Claude from Settings → Matching ----------
KEY = "sk-ant-api03-" + "x" * 40 + "AbCd"


def connect(as_user, **data):
    return as_user("admin").post("/matching/settings/ai/", data)


def test_admin_connects_claude_with_a_key(as_user):
    r = connect(as_user, provider="anthropic", api_key=KEY)
    assert toast(r)["tone"] == "ok"
    cfg = MatchSettings.get()
    assert cfg.ai_provider == "anthropic" and cfg.api_key_hint == "AbCd" and KEY not in cfg.api_key_enc   # encrypted at rest
    assert ai.connection()["key"] == KEY and ai.available()
    page = as_user("admin").get("/settings/?tab=matching").content
    assert KEY.encode() not in page and b"AbCd" in page                                                  # write-only
    from core.models import AuditEvent
    logged = " ".join(e.text + json.dumps(e.after or {}) + json.dumps(e.before or {}) for e in AuditEvent.objects.filter(entity="settings"))
    assert "configure_ai" in AuditEvent.objects.filter(entity="settings").values_list("action", flat=True) and KEY not in logged


def test_key_rules(as_user):
    assert toast(as_user("faisal").post("/matching/settings/ai/", {"provider": "anthropic", "api_key": KEY}))["tone"] == "bad"
    assert toast(connect(as_user, provider="anthropic", api_key="hello"))["tone"] == "bad"      # not a Claude key
    assert toast(connect(as_user, provider="anthropic"))["tone"] == "bad"                       # no key saved yet
    connect(as_user, provider="anthropic", api_key=KEY)
    connect(as_user, provider="anthropic")                                                      # empty field keeps the key
    assert ai.connection()["key"] == KEY
    assert toast(as_user("faisal").post("/matching/settings/ai/remove-key/"))["tone"] == "bad"
    as_user("admin").post("/matching/settings/ai/remove-key/")
    assert not ai.available() and MatchSettings.get().api_key_enc == "" and MatchSettings.get().ai_provider == "off"


def test_server_setting_wins(settings, as_user):
    connect(as_user, provider="anthropic", api_key=KEY)
    settings.AI_PROVIDER = "bedrock"
    c = ai.connection()
    assert c["source"] == "server" and c["provider"] == "bedrock" and c["key"] is None and c["model"] == "anthropic.claude-opus-5"
    assert b"Set on the server" in as_user("admin").get("/settings/?tab=matching").content


def test_saved_key_unreadable_after_server_key_change(settings, as_user):
    connect(as_user, provider="anthropic", api_key=KEY)
    settings.SECRETS_KEY = "rotated-server-key"
    c = ai.connection()
    assert not c["ready"] and "enter it again" in c["problem"] and not ai.available()


@pytest.fixture
def fake_claude(monkeypatch):
    made = []

    def start(answer=None, status=200):
        fake = FakeClaude(answer or {"choice": 0, "confidence": 80, "rationale": "ok"}, status=status)
        monkeypatch.setenv("ANTHROPIC_BASE_URL", fake.url)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.setattr(ai, "_client", None)
        made.append(fake)
        return fake
    yield start
    for f in made:
        f.close()
    ai._client = None


def test_test_connection_uses_the_saved_key(fake_claude, as_user):
    fake = fake_claude()
    connect(as_user, provider="anthropic", api_key=KEY)
    r = as_user("admin").post("/matching/settings/ai/test/")
    assert toast(r)["tone"] == "ok" and "Connected to Claude" in toast(r)["msg"]
    headers = {k.lower(): v for k, v in fake.requests[-1]["headers"].items()}
    assert headers["x-api-key"] == KEY
    p = typo_payment()                                   # real suggestion calls use the same key
    ms.AI_CLOSE_CALL = 100
    try:
        ms.refresh("pay_inv", p, use_ai=True)
    finally:
        ms.AI_CLOSE_CALL = 10
    assert {k.lower(): v for k, v in fake.requests[-1]["headers"].items()}["x-api-key"] == KEY


def test_test_connection_explains_a_rejected_key(fake_claude, as_user):
    fake_claude(status=401)
    connect(as_user, provider="anthropic", api_key=KEY)
    t = toast(as_user("admin").post("/matching/settings/ai/test/"))
    assert t["tone"] == "bad" and "rejected" in t["msg"]


# ---------- OpenAI as the provider ----------
OPENAI_KEY = "sk-proj-" + "y" * 40 + "WxYz"


class FakeOpenAI:
    """Answers POST /chat/completions like the OpenAI API and records what it was sent."""

    def __init__(self, answer, finish="stop", refusal=None, status=200):
        self.requests = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.requests.append(dict(path=self.path, headers={k.lower(): v for k, v in self.headers.items()}, body=body))
                msg = {"role": "assistant", "content": None if refusal else json.dumps(answer), "refusal": refusal}
                out = {"id": "chatcmpl-test", "object": "chat.completion", "created": 1, "model": body["model"],
                       "choices": [{"index": 0, "message": msg, "finish_reason": finish}],
                       "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}
                if status != 200:
                    out = {"error": {"message": "Incorrect API key provided", "type": "invalid_request_error", "code": "invalid_api_key"}}
                data = json.dumps(out).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *a):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}/v1"


@pytest.fixture
def fake_openai(monkeypatch):
    made = []

    def start(answer=None, **kw):
        fake = FakeOpenAI(answer or {"choice": 0, "confidence": 80, "rationale": "Same amount and the year is trimmed."}, **kw)
        monkeypatch.setenv("OPENAI_BASE_URL", fake.url)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.setattr(ai, "_client", None)
        made.append(fake)
        return fake
    yield start
    for f in made:
        f.server.shutdown()
    ai._client = None


def test_openai_needs_its_own_key_and_a_model(as_user):
    assert toast(connect(as_user, provider="openai", api_key=OPENAI_KEY))["tone"] == "bad"            # no model
    assert toast(connect(as_user, provider="openai", api_key=KEY, model="gpt-x"))["tone"] == "bad"   # a Claude key
    connect(as_user, provider="anthropic", api_key=KEY)
    t = toast(connect(as_user, provider="openai", model="gpt-x"))                                       # Claude key not reused
    assert t["tone"] == "bad" and "OpenAI API key" in t["msg"]
    assert toast(connect(as_user, provider="openai", api_key=OPENAI_KEY, model="gpt-x"))["tone"] == "ok"
    c = ai.connection()
    assert c["provider"] == "openai" and c["model"] == "gpt-x" and c["key"] == OPENAI_KEY and c["hint"] == "WxYz"
    connect(as_user, provider="anthropic")                                                              # back to Claude: needs its key again
    assert ai.connection()["provider"] == "openai"
    page = as_user("admin").get("/settings/?tab=matching").content
    assert OPENAI_KEY.encode() not in page and b"Connected to OpenAI" in page


def test_openai_connection_test_and_suggestions(fake_openai, as_user):
    fake = fake_openai()
    connect(as_user, provider="openai", api_key=OPENAI_KEY, model="gpt-x")
    t = toast(as_user("admin").post("/matching/settings/ai/test/"))
    assert t["tone"] == "ok" and "OpenAI" in t["msg"]
    assert fake.requests[-1]["headers"]["authorization"] == f"Bearer {OPENAI_KEY}"
    p = Payment.objects.get(status="unmatched", hint__gt="")
    ms.AI_CLOSE_CALL = 100
    try:
        rows = ms.refresh("pay_inv", p, use_ai=True)
    finally:
        ms.AI_CLOSE_CALL = 10
    assert rows[0].method == "ai" and rows[0].ai_model == "gpt-x" and "trimmed" in rows[0].ai_rationale
    body = fake.requests[-1]["body"]
    assert body["model"] == "gpt-x" and body["messages"][0]["role"] == "system"
    rf = body["response_format"]
    assert rf["type"] == "json_schema" and rf["json_schema"]["strict"] is True and "choice" in rf["json_schema"]["schema"]["properties"]


def test_openai_refusal_and_rejected_key(fake_openai, as_user):
    p = pay(reason="Shortage")
    fake_openai(refusal="I can't help with that.")
    connect(as_user, provider="openai", api_key=OPENAI_KEY, model="gpt-x")
    assert ms.refresh_deduction(p, use_ai=True).method == "rules"
    fake_openai(status=401)
    t = toast(as_user("admin").post("/matching/settings/ai/test/"))
    assert t["tone"] == "bad" and "rejected" in t["msg"] and "OpenAI dashboard" in t["msg"]
