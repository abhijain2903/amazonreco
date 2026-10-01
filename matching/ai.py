"""AI for the parts of matching that rules cannot do well: reading Amazon's free-text deduction reasons and
choosing between close candidates. Providers: Claude via the Anthropic API or Amazon Bedrock, or an OpenAI model.
Every call is optional: with no provider, a refusal, a timeout or an API error the function returns None and the
rules-based suggestion stands.

Only the fields needed for the decision are sent (references, amounts, dates, reason text) — no people or contacts.
"""
import json
import logging

from django.conf import settings

from .models import MatchSettings

log = logging.getLogger("hub.ai")

SYSTEM = (
    "You help the accounts team of Modern Electronics (ME), a vendor selling to Amazon.sa, reconcile Amazon's payments "
    "and debit notes with ME's invoices and promotion agreements. Amounts are SAR. You receive one record and the "
    "candidates a rules engine found, each with a score and reasons. Judge from the evidence given only; never invent "
    "references. Prefer 'none' over a weak guess: a person reviews every suggestion, and a wrong match costs more than "
    "no match. Keep rationales to one or two plain sentences a finance user can check."
)

CHOOSE_SCHEMA = {
    "type": "object",
    "properties": {
        "choice": {"type": "integer", "description": "Index of the best candidate, or -1 if none is convincing"},
        "confidence": {"type": "integer", "description": "0-100"},
        "rationale": {"type": "string"},
    },
    "required": ["choice", "confidence", "rationale"],
    "additionalProperties": False,
}

DEDUCTION_SCHEMA = {
    "type": "object",
    "properties": {
        "type": {"type": "string", "enum": ["shortage", "price", "promo", "damage", "chargeback", "returns", "coop", "other"]},
        "action": {"type": "string", "enum": ["dispute", "accept", "link_dn", "review"]},
        "confidence": {"type": "integer", "description": "0-100"},
        "rationale": {"type": "string"},
        "dispute_note": {"type": "string", "description": "Note to Amazon, 2-3 sentences, citing the evidence given; empty if not disputing"},
    },
    "required": ["type", "action", "confidence", "rationale", "dispute_note"],
    "additionalProperties": False,
}

PROVIDERS = ("anthropic", "openai", "bedrock")
LABELS = {"anthropic": "Claude (Anthropic API)", "openai": "OpenAI", "bedrock": "Claude on Amazon Bedrock", "off": "Not connected"}
DEFAULT_MODELS = {"anthropic": "claude-opus-5", "bedrock": "anthropic.claude-opus-5"}   # OpenAI: the admin names the model
ENV_KEYS = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY"}

_client = None
_client_for = None


def connection():
    """Which AI connection is in effect. The server's HUB_AI_PROVIDER wins; otherwise what an admin set in
    Settings → Matching. Returns provider, source, model, region, the key (never for Bedrock) and whether it's usable."""
    import os

    from .secrets import decrypt
    cfg = MatchSettings.get()
    if settings.AI_PROVIDER in PROVIDERS:
        provider, source = settings.AI_PROVIDER, "server"
        key = os.environ.get(ENV_KEYS[provider]) if provider in ENV_KEYS else None
        model = os.environ.get("HUB_AI_MODEL") or DEFAULT_MODELS.get(provider, "")
    else:
        provider, source = cfg.ai_provider, "settings"
        # A saved key is only ever sent to the provider it was entered for.
        key = decrypt(cfg.api_key_enc) if provider in ENV_KEYS and cfg.api_key_for == provider else None
        model = cfg.ai_model or DEFAULT_MODELS.get(provider, "")
    problem = ""
    if provider not in PROVIDERS:
        problem = "not connected"
    elif provider in ENV_KEYS and not key:
        problem = ("the saved API key can no longer be read (the server key changed); enter it again"
                   if source == "settings" and cfg.api_key_enc and cfg.api_key_for == provider else "missing its API key")
    elif not model:
        problem = "missing a model name"
    return dict(provider=provider, label=LABELS.get(provider, provider), source=source, model=model, region=settings.AI_REGION,
                key=key, hint=cfg.api_key_hint if source == "settings" and cfg.api_key_for == provider else "",
                ready=not problem, problem=problem)


def available():
    return MatchSettings.get().ai_enabled and connection()["ready"]


def model_name():
    return connection()["model"]


def _get_client(conn=None):
    """One client per connection; a new key, model host or provider builds a new client."""
    global _client, _client_for
    conn = conn or connection()
    ident = (conn["provider"], conn["region"], conn["key"])
    if _client is None or _client_for != ident:
        if conn["provider"] == "openai":
            import openai
            _client = openai.OpenAI(api_key=conn["key"], timeout=60.0, max_retries=2)
        else:
            import anthropic
            if conn["provider"] == "bedrock":
                _client = anthropic.AnthropicBedrockMantle(aws_region=conn["region"], timeout=60.0, max_retries=2)
            else:
                _client = anthropic.Anthropic(api_key=conn["key"], timeout=60.0, max_retries=2)
        _client_for = ident
    return _client


def _sdk_errors():
    """The SDK exception classes, both SDKs (they share the same names)."""
    import anthropic
    mods = [anthropic]
    try:
        import openai
        mods.append(openai)
    except ImportError:
        pass
    pick = lambda name: tuple(getattr(m, name) for m in mods)
    return {n: pick(n) for n in ("AuthenticationError", "PermissionDeniedError", "NotFoundError", "RateLimitError",
                                 "APIConnectionError", "APIStatusError")}


def _complete(conn, system, user, schema=None, small=False):
    """One request to whichever provider is connected. Returns (text, model, stopped_cleanly)."""
    client = _get_client(conn)
    if small:
        client = client.with_options(max_retries=0, timeout=30.0)
    if conn["provider"] == "openai":
        kw = dict(model=conn["model"], messages=([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": user}])
        if schema:
            kw["response_format"] = {"type": "json_schema", "json_schema": {"name": "answer", "schema": schema, "strict": True}}
        resp = client.chat.completions.create(**kw)
        ch = resp.choices[0]
        return ch.message.content or "", resp.model, ch.finish_reason == "stop" and not ch.message.refusal
    kw = dict(model=conn["model"], max_tokens=16 if small else 4000, messages=[{"role": "user", "content": user}])
    if system:
        kw["system"] = system
    if schema:
        kw["output_config"] = {"effort": "low", "format": {"type": "json_schema", "schema": schema}}
    if conn["provider"] == "anthropic" and not small:
        # Server-side fallback: if the model declines, the API retries on another model in the same call.
        resp = client.beta.messages.create(**kw, betas=["server-side-fallback-2026-07-01"], fallbacks="default")
    else:
        resp = client.messages.create(**kw)
    text = next((b.text for b in resp.content if b.type == "text"), "")
    return text, resp.model, resp.stop_reason == "end_turn" or (small and resp.stop_reason == "max_tokens")


def test_connection():
    """One tiny call to prove the connection works. Returns (ok, message for the admin)."""
    conn = connection()
    if not conn["ready"]:
        return False, f"{conn['label']} is {conn['problem']}."
    E = _sdk_errors()
    console = "the OpenAI dashboard" if conn["provider"] == "openai" else "the Claude Console" if conn["provider"] == "anthropic" else "Amazon Bedrock"
    try:
        _, model, _ = _complete(conn, "", "Reply with the single word OK.", small=True)
    except E["AuthenticationError"]:
        return False, f"The API key was rejected. Check it in {console} and enter it again."
    except E["PermissionDeniedError"]:
        return False, f"This key or account is not allowed to use {conn['model']}. Check access in {console}."
    except E["NotFoundError"]:
        return False, f"Model {conn['model']} is not available on this account or region. Check the model name."
    except E["RateLimitError"]:
        return False, f"Rate limited or out of credit. Check billing in {console}."
    except E["APIConnectionError"]:
        return False, "Could not reach the AI provider from the server. Check the server's internet access."
    except E["APIStatusError"] as e:
        return False, f"The AI provider returned an error ({e.status_code}): {e.message}"
    return True, f"Connected to {conn['label']} ({model})."


def _ask(task, payload, schema):
    """One structured call. Returns (dict, model) or (None, "")."""
    if not available():
        return None, ""
    conn = connection()
    E = _sdk_errors()
    try:
        text, model, clean = _complete(conn, SYSTEM, f"{task}\n\n{json.dumps(payload, sort_keys=True, default=str)}", schema)
    except E["APIConnectionError"] as e:
        log.warning("AI call failed (connection): %s", e)
        return None, ""
    except E["RateLimitError"] as e:
        log.warning("AI call rate-limited: %s", e)
        return None, ""
    except E["APIStatusError"] as e:
        log.warning("AI call failed (%s): %s", e.status_code, e.message)
        return None, ""
    if not clean:
        log.warning("AI call did not finish cleanly (refusal or length)")
        return None, ""
    try:
        return json.loads(text), model
    except ValueError:
        log.warning("AI returned invalid JSON")
        return None, ""


def choose(kind_label, record, candidates, history=()):
    """Pick the best of close candidates. candidates: [{"label", "score", "reasons"}]. history: recent decisions
    on the same kind of match, so the team's past choices inform this one."""
    out, model = _ask(
        f"Task: which candidate is the right {kind_label} match for this record? Answer with its index, or -1.",
        {"record": record, "candidates": [dict(index=i, **c) for i, c in enumerate(candidates)], "recent_decisions": list(history)},
        CHOOSE_SCHEMA)
    if not out or not -1 <= out.get("choice", -1) < len(candidates):
        return None
    return dict(out, model=model)


def classify_deduction(record, evidence):
    """Read Amazon's deduction reason with ME's evidence; recommend a dispute type, the next step and a note."""
    out, model = _ask(
        "Task: Amazon deducted money from this payment. Classify the deduction, recommend the next step "
        "(dispute when ME's evidence supports the full invoice; link_dn when it is a promotion debit note listed in "
        "the evidence; accept only when the evidence shows Amazon is right; review when unclear) and draft the note. "
        "For a chargeback, check it against the ASN timing, carton labels and appointment history in the evidence; for "
        "returns, compare with the returns received; cite the facts you rely on.",
        {"payment": record, "evidence": evidence}, DEDUCTION_SCHEMA)
    return dict(out, model=model) if out else None
