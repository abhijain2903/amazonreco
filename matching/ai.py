"""Claude for the parts of matching that rules cannot do well: reading Amazon's free-text deduction reasons and
choosing between close candidates. Every call is optional: with no provider, a refusal, a timeout or an API error
the function returns None and the rules-based suggestion stands.

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
        "type": {"type": "string", "enum": ["shortage", "price", "promo", "damage", "other"]},
        "action": {"type": "string", "enum": ["dispute", "accept", "link_dn", "review"]},
        "confidence": {"type": "integer", "description": "0-100"},
        "rationale": {"type": "string"},
        "dispute_note": {"type": "string", "description": "Note to Amazon, 2-3 sentences, citing the evidence given; empty if not disputing"},
    },
    "required": ["type", "action", "confidence", "rationale", "dispute_note"],
    "additionalProperties": False,
}

_client = None


def available():
    return settings.AI_PROVIDER in ("anthropic", "bedrock") and MatchSettings.get().ai_enabled


def model_name():
    return settings.AI_MODEL


def _get_client():
    global _client
    if _client is None:
        import anthropic
        if settings.AI_PROVIDER == "bedrock":
            _client = anthropic.AnthropicBedrockMantle(aws_region=settings.AI_REGION, timeout=60.0, max_retries=2)
        else:
            _client = anthropic.Anthropic(timeout=60.0, max_retries=2)
    return _client


def _ask(task, payload, schema):
    """One structured call. Returns (dict, model) or (None, "")."""
    if not available():
        return None, ""
    import anthropic
    kwargs = dict(model=settings.AI_MODEL, max_tokens=4000, system=SYSTEM,
                  messages=[{"role": "user", "content": f"{task}\n\n{json.dumps(payload, sort_keys=True, default=str)}"}],
                  output_config={"effort": "low", "format": {"type": "json_schema", "schema": schema}})
    try:
        client = _get_client()
        if settings.AI_PROVIDER == "anthropic":
            # Server-side fallback: if the model declines, the API retries on another model in the same call.
            resp = client.beta.messages.create(**kwargs, betas=["server-side-fallback-2026-07-01"], fallbacks="default")
        else:
            resp = client.messages.create(**kwargs)
    except anthropic.APIConnectionError as e:
        log.warning("AI call failed (connection): %s", e)
        return None, ""
    except anthropic.RateLimitError as e:
        log.warning("AI call rate-limited: %s", e)
        return None, ""
    except anthropic.APIStatusError as e:
        log.warning("AI call failed (%s): %s", e.status_code, e.message)
        return None, ""
    if resp.stop_reason != "end_turn":
        log.warning("AI call stopped with %s", resp.stop_reason)
        return None, ""
    text = next((b.text for b in resp.content if b.type == "text"), "")
    try:
        return json.loads(text), resp.model
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
        "the evidence; accept only when the evidence shows Amazon is right; review when unclear) and draft the note.",
        {"payment": record, "evidence": evidence}, DEDUCTION_SCHEMA)
    return dict(out, model=model) if out else None
