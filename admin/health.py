"""Gateway observations, independent of official daily quota accounting."""
from collections import Counter
from datetime import datetime, timedelta
import math

from sqlalchemy import func

from admin.config import settings
from admin.models import Account, AccountModelCooldown, ModelConfig, UsageLog
from admin.pool import POOL


def _percentiles(values):
    values = sorted(v for v in values if v is not None and v >= 0)
    return {f"p{p}": values[max(0, math.ceil(len(values) * p / 100) - 1)] if values else None
            for p in (50, 95)}


def model_health(db, hours=24):
    from admin.routers.proxy import _looks_login_dead

    now = datetime.utcnow()
    since = now - timedelta(hours=hours)
    accounts = db.query(Account).all()
    configs = {c.model_id: c for c in db.query(ModelConfig).filter(
        ModelConfig.level == "system", ModelConfig.enabled == 1).all()}
    limits = db.query(AccountModelCooldown).filter(AccountModelCooldown.until > now).all()
    grouped = db.query(UsageLog.model, UsageLog.error_kind, func.count(UsageLog.id)).filter(
        UsageLog.created_at >= since).group_by(UsageLog.model, UsageLog.error_kind).all()
    counts = {}
    for model, kind, count in grouped:
        if not model:
            continue
        counts.setdefault(model, Counter())["success" if kind == "" else kind or "unknown"] += count
    result = []
    waf = POOL.waf.remaining()
    healthy = [a for a in accounts if a.status == "active" and not _looks_login_dead(a, now)
               and all(getattr(a, field) is None or getattr(a, field) <= now
                       for field in ("cool_until", "breaker_until", "degrade_until"))]
    for model in sorted(set(configs) | set(counts) | {r.model for r in limits}):
        config = configs.get(model)
        free = config is not None and config.credit_multiplier == 0
        cooling = [r for r in limits if r.model == model]
        blocked = {r.account_id for r in cooling}
        eligible = [a for a in healthy if a.id not in blocked and (free or (a.balance_remain or 0) > 0)]
        available = [a for a in eligible if not POOL.inflight.full(a.uid or "", settings.MAX_IN_FLIGHT)]
        distribution = counts.get(model, Counter())
        total = sum(distribution.values())
        ok = distribution.get("success", 0) + distribution.get("", 0)
        # Percentiles describe the latest bounded sample; counts use the whole window.
        samples = db.query(UsageLog.ttfb_ms, UsageLog.latency_ms).filter(
            UsageLog.model == model, UsageLog.created_at >= since,
            UsageLog.error_kind.in_(("", "success"))).order_by(
                UsageLog.created_at.desc(), UsageLog.id.desc()).limit(2000).all()
        result.append({"model": model, "enabled": config is not None, "free": free,
                       "eligible_accounts": len(eligible),
                       "available_accounts": len(available) if not waf and config else 0,
                       "limited_accounts": len(blocked),
                       "earliest_model_recovery": min((r.until for r in cooling), default=None),
                       "limit_reasons": dict(Counter(r.kind for r in cooling)),
                       "requests": total, "successes": ok, "failures": total - ok,
                       "success_rate": round(ok / total, 4) if total else None,
                       "failure_rate": round((total - ok) / total, 4) if total else None,
                       "failure_reasons": {k: v for k, v in distribution.items() if k not in ("", "success")},
                       "ttfb_ms": _percentiles([r[0] for r in samples]),
                       "latency_ms": _percentiles([r[1] for r in samples]),
                       "latency_sample_count": len(samples)})
    for item in result:
        reset = item["earliest_model_recovery"]
        item["earliest_model_recovery"] = reset.isoformat() + "Z" if reset else None
    return {"items": result, "usage_scope": "gateway_only", "range_hours": hours,
            "observed_at": now.isoformat() + "Z", "waf_remaining_seconds": waf,
            "latency_sample_limit": 2000, "daily_quota_remaining": None,
            "runtime_scope": "process_local"}
