import time

from django.db import transaction

from core.services import audit, require

from . import engine
from .models import CheckResult, RuleConfig

_cache = {"cfg": None, "at": 0.0}
TTL = 5.0


def ensure_rules():
    for rid, (name, desc, params) in engine.DEFAULTS.items():
        RuleConfig.objects.get_or_create(rule_id=rid, defaults={"name": name, "description": desc, "params": params})


def get_cfg() -> engine.Cfg:
    """Current rule settings, cached for a few seconds per process."""
    if _cache["cfg"] is not None and time.monotonic() - _cache["at"] < TTL:
        return _cache["cfg"]
    rows = list(RuleConfig.objects.all())
    if not rows:
        return engine.Cfg.defaults()
    cfg = engine.Cfg({r.rule_id: {"enabled": r.enabled, **r.params} for r in rows},
                     version=sum(r.version for r in rows))
    _cache.update(cfg=cfg, at=time.monotonic())
    return cfg


def clear_cache():
    _cache.update(cfg=None, at=0.0)


def rule_rows():
    ensure_rules()
    order = list(engine.DEFAULTS)
    return sorted(RuleConfig.objects.all(), key=lambda r: order.index(r.rule_id))


@transaction.atomic
def update_rule(user, rule_id, enabled=None, **params):
    require(user, "settings")
    r = RuleConfig.objects.select_for_update().get(rule_id=rule_id)
    before = {"enabled": r.enabled, **r.params}
    if enabled is not None:
        r.enabled = enabled
    for k, v in params.items():
        if k in r.params:
            r.params[k] = float(v)
    r.bump()
    r.save()
    audit("rules", rule_id, f"{rule_id} {r.name} changed", user, action="update_rule", before=before,
          after={"enabled": r.enabled, **r.params})
    clear_cache()
    # Re-run checks on open records in the background.
    from core.tasks import rerun_open_checks
    rerun_open_checks.defer()
    return r


def record(entity, entity_id, checks, cfg):
    """Persist one run of check results for a record (replaces the previous run)."""
    CheckResult.objects.filter(entity=entity, entity_id=entity_id).delete()
    CheckResult.objects.bulk_create([
        CheckResult(entity=entity, entity_id=entity_id, rule_id=c["rule"], subject=c.get("subject", "")[:80],
                    passed=c.get("ok"), expected=str(c.get("exp", ""))[:60], actual=str(c.get("act", ""))[:60],
                    gap=str(c.get("gap", ""))[:60], config_version=cfg.version)
        for c in checks
    ])
