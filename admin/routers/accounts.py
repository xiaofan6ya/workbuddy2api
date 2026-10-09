"""账号管理：列表 / 新增 / 批量上传 / 扫描本机 / 导入本机 / 刷新余额 / 启用禁用 / 删除 / 注入本机客户端。"""
import glob
import json
import os
import shutil
from datetime import datetime, timezone, timedelta
import time
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy import func

from admin import backend, jobrunner, wb_login
from admin.config import settings
from admin.db import SessionLocal, get_db
from admin.models import Account, AccountModelCooldown, ModelConfig, UsageLog
from admin.security import require_admin

router = APIRouter(prefix="/api/accounts", tags=["accounts"])


class AccountIn(BaseModel):
    name: Optional[str] = None  # 留空则自动取真实昵称 / uid
    auth_json: str


class AccountBatchIn(BaseModel):
    items: list[AccountIn]
    #: 是否对每条做**真实上游验证**（默认开）。关掉只做结构校验，
    #: 速度快但无法识别伪造签名的凭证 —— 只建议在导入自己刚导出的数据时用。
    verify: bool = True


class InjectIn(BaseModel):
    confirm: bool = False


class ImportLocalIn(BaseModel):
    files: list[str] = []  # 指定文件名；含 "all" 或 all=true 表示全部
    all: bool = False


class ExportIn(BaseModel):
    ids: list[int] = []      # 空 = 全部
    include_disabled: bool = False


def _uid_of(auth_json: str) -> str:
    """从 auth_json 里取 uid；取不到返回空串。"""
    try:
        return (backend.parse_auth_meta(auth_json) or {}).get("uid") or ""
    except Exception:
        return ""


def _reject_non_credential(auth_json: str) -> dict:
    """上传入口的**唯一**把关：不是客户端授权凭证就拒收。

    这里刻意复用 `wb_login.validate_credential` —— 与手机号登录入库那条路
    共用同一个校验器。两条路对「什么算合法凭证」必须完全一致，
    否则严格的那条就成了摆设（攻击者走宽松的那条即可）。

    为什么必须拦（而不是像原来那样只 `json.loads` 一下）：
    `json.loads` 只证明「是合法 JSON」，`{}`、`{"foo":1}`、
    `/v1/auth/accounts` 的账号列表、裸 accessToken 全都算「合法 JSON」，
    但它们都不是凭证 —— 收进来只会得到一条刷新余额必然失败的死记录，
    还会污染号池统计（看起来有 N 个号，实际可用的是少数）。

    抛 400 并把原因回给调用方，避免用户面对一句「格式错误」无从下手。
    返回校验得到的 meta（uid 等），供调用方复用。
    """
    check = wb_login.validate_credential(auth_json)
    if not check.ok:
        raise HTTPException(status_code=400, detail={
            "message": f"不是有效的客户端凭证：{check.reason}",
            "step": "validate",
        })
    return check.meta


def _verify_credential_live_or_400(auth_json: str):
    """**真实上游**验证：确认凭证可用、非伪造，并取回真实账号信息 + 余额。

    与 `_reject_non_credential`（纯结构）是两道独立关卡：
    结构校验挡不住「JWT 字段齐全但签名是编的」——那种凭证的
    accessToken/refreshToken/uid/expiresAt 全都能随手编。

    返回 `(profile, balance)`；`balance` 是验证阶段顺带拿到的真实余额，
    调用方可以直接写库，**不必再补一次完全一样的请求**。
    """
    probe = wb_login.probe_credential_live(auth_json)
    if not probe.ok:
        raise HTTPException(status_code=400, detail={
            "message": f"凭证未通过上游验证：{probe.message}",
            "step": "live_probe",
            "reason": probe.reason,
        })
    return (probe.profile or {}), (probe.balance or {})


def _post_login_automation(account_id: int, name: str) -> None:
    """登录/上传成功后，后台自动做成长任务 + 猫猫旅行 + 签到。

    直接复用登录路由里那套实现：**同一个行为不该有两份代码**，
    否则「手机号注册进来的号会自动做任务、上传进来的号不会」这种
    不一致会非常难发现。
    """
    try:
        from admin.routers.login import _schedule_post_login_automation
        _schedule_post_login_automation(account_id, name)
    except Exception:
        _logger.exception("提交登录后自动任务失败（不影响入库）")


def _existing_uid_index(db: Session) -> dict[str, Account]:
    """uid -> 已存在的账号记录（同 uid 有多条时保留 id 最小的那条）。

    历史遗留的重复记录不影响导入：无论哪条都会命中，从而跳过重复导入。
    """
    idx: dict[str, Account] = {}
    for a in db.query(Account).order_by(Account.id.asc()).all():
        if not a.uid:
            continue
        idx.setdefault(a.uid, a)   # 先到先得 = id 最小
    return idx


def _find_dup(db: Session, auth_json: str) -> Account | None:
    """按 uid 查这个凭据是否已在号池里。"""
    uid = _uid_of(auth_json)
    if not uid:
        return None
    return (db.query(Account)
            .filter(Account.uid == uid)
            .order_by(Account.id.asc())
            .first())


def _export_items(rows: list[Account]) -> list[str]:
    """把账号记录还原成 .info 原文列表。

    导出的是 auth_json 原文（与上传接受的格式完全一致），
    因此导出文件可以直接再传回来，不需要额外转换。
    """
    items: list[str] = []
    for a in rows:
        raw = (a.auth_json or "").strip()
        if not raw:
            continue
        try:
            json.loads(raw)
        except Exception:
            continue  # 跳过损坏记录，避免整个导出失败
        items.append(raw)
    return items


@router.post("/export")
def export_accounts(
    body: ExportIn,
    _: bool = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """导出账号登录态。

    返回 {items: [.info 原文...]}，与「批量上传」接受的格式一致，
    导出的内容可以直接原样再传回来（round-trip）。

    注意：导出内容含 token，等同账号凭据，请勿外传。
    """
    q = db.query(Account)
    if body.ids:
        q = q.filter(Account.id.in_(body.ids))
    elif not body.include_disabled:
        q = q.filter(Account.status == "active")
    rows = q.order_by(Account.id).all()

    items = _export_items(rows)
    return {
        "total": len(rows),
        "count": len(items),
        "skipped": len(rows) - len(items),
        "items": items,
        # 附带摘要供前端展示（不含凭据）
        "summary": [
            {"id": a.id, "name": a.name, "uid": a.uid, "status": a.status,
             "balance_total": a.balance_total, "balance_remain": a.balance_remain}
            for a in rows
        ],
    }


def _client_auth_dir() -> str:
    d = settings.CLIENT_AUTH_DIR or os.path.expandvars(
        r"%LOCALAPPDATA%\CodeBuddyExtension\Data\Public\auth"
    )
    return os.path.expandvars(d)


def _apply_meta(acc: Account, auth_json: str, profile: dict | None = None,
                balance: dict | None = None):
    """把凭证里的元信息落到账号记录上。

    `profile` 是**上游验证时返回的真实账号信息**。它优先于凭证里声明的值：
    凭证的 `account.*` 是提交者自己写的（可以由着性子改），而上游返回的
    是腾讯侧的事实 —— 用事实覆盖声明，避免「真令牌 + 假昵称/假 uid」入库。

    `balance` 是验证阶段顺带拿到的真实余额，直接写库，
    不用再补一次 `fetch_balance()`（同一个请求打两遍纯属浪费）。
    """
    meta = backend.parse_auth_meta(auth_json)
    prof = profile or {}
    acc.auth_json = auth_json
    uid = str(prof.get("uid") or meta.get("uid") or "")
    if uid:
        acc.uid = uid
    eid = str(prof.get("enterpriseId") or meta.get("enterprise_id") or "")
    if eid:
        acc.enterprise_id = eid
    if meta.get("domain"):
        acc.domain = meta["domain"]
    # 余额：拿不到就不动（绝不写 0 覆盖掉已知的真实值）
    if balance:
        acc.balance_total = int(balance.get("total") or 0)
        acc.balance_remain = int(balance.get("remain") or 0)
        acc.last_sync_at = datetime.utcnow()
    # 号池名称：上游真实昵称 → 上游手机号 → 凭证昵称 → uid
    # （昵称为 null/空串/"null" 一律视为缺失）
    def _clean(v) -> str:
        s = (v if isinstance(v, str) else "").strip()
        return "" if s.lower() in ("null", "none") else s

    if not acc.name:
        acc.name = (_clean(prof.get("nickname")) or _clean(prof.get("phoneNumber"))
                    or _clean(meta.get("nickname")) or uid or "未命名")


#: 「快过期积分」的判定窗口（天）。
#:
#: 官方套餐页与参考实现都以 **7 天** 为界（官方把 7 天内到期的额度标成橙色）。
#: 这里保留 30 天的 `EXPIRING_SOON_DAYS` 供**旧字段** `credits_expiring` 使用
#: （向后兼容，三因子加权的历史行为不变），而新的严格口径一律读
#: `admin.credits.EXPIRING_SOON_DAYS`（=7）。
EXPIRING_SOON_DAYS = 30


def _compute_expiring(normalized: list[dict]) -> int:
    """从**归一化**积分明细里算出「即将过期」的剩余额度合计（30 天旧口径）。

    .. deprecated:: 新代码请直接用 `credits.summarize_account` 的
       `expiring_soon_remaining`（7 天窗口）。本函数只用于维护历史字段
       `credits_expiring`，让三因子加权策略的行为与升级前保持一致。

    判定：有过期时间（`expire_at` 非空）且距今不超过 EXPIRING_SOON_DAYS 天，
    把这些包的剩余额度加起来。长期有效的包不计入。
    """
    now_ms = datetime.now(timezone.utc).timestamp() * 1000
    horizon_ms = EXPIRING_SOON_DAYS * 86400 * 1000
    total = 0
    for p in normalized or []:
        try:
            ts = int(p.get("expire_at") or 0)
        except Exception:
            continue
        if ts <= 0:
            continue
        left = ts - now_ms
        if 0 <= left <= horizon_ms:
            total += int(p.get("remaining") or 0)
    return total


def _sync_credit_snapshot(acc: Account, packages: list[dict]) -> dict:
    """把逐包积分明细归一化后写回账号记录，并返回账号级到期汇总。

    这是「快到期的先用完」这条调度的**唯一数据来源**：写回后，
    选号路径只需要读 `acc.credits_expiring_soon` / `acc.credits_soonest_expire_at`
    两个列，不必为每个候选账号发一次上游请求。

    为什么把明细也存下来（`credits_snapshot`）：参考实现能做出「最近快到期的
    积分包」列表并支持「查看全部积分包」，靠的就是手里有逐包数据。原先只存一个
    汇总数字，界面就只能显示一个总额，用户没法判断到底哪个包先作废。
    """
    from admin import credits as credit_rules

    collection = getattr(packages, "metadata", None)
    if collection and not collection["complete"]:
        try:
            previous = json.loads(acc.credits_snapshot or "{}")
        except (ValueError, TypeError):
            previous = {}
        if not isinstance(previous, dict):
            previous = {}
        previous = {**credit_rules.summarize_account(previous.get("resources") or []), **previous}
        previous["collection"] = {**collection, "snapshot_preserved": True}
        acc.credits_snapshot = json.dumps(previous, ensure_ascii=False)
        return previous
    now = credit_rules.now_ms()
    normalized = credit_rules.normalize_packages(packages, now)
    summary = credit_rules.summarize_account(normalized, now)

    acc.credits_expiring = _compute_expiring(normalized)      # 旧口径，保持兼容
    acc.credits_expiring_soon = int(summary["expiring_soon_remaining"])
    acc.credits_evergreen = int(
        sum(r["remaining"] for r in normalized
            if r["remaining"] > 0 and not r["expire_at"]))
    acc.credits_expired = int(summary["expired_remaining"])
    acc.credits_soonest_expire_at = credit_rules.as_datetime(summary["soonest_expire_at"])
    acc.credits_snapshot = json.dumps(
        {**summary, "collection": collection,
         "resources": [{k: v for k, v in r.items()} for r in normalized]},
        ensure_ascii=False)
    acc.credits_synced_at = datetime.utcnow()
    return summary


def _refresh_balance(acc: Account, with_expiry: bool = False) -> bool:
    """刷新账号余额。

    Args:
        with_expiry: 是否同时同步**积分包到期快照**（剩余额度按包拆分）。
            需要多打一次 `fetch_credit_details`（约 0.4s），
            所以只在整点定时任务与「刷新全部」里开 —— 高频路径（登录后收尾）不开，
            但**至少会保留上一次的值**，不会因为不刷新就被清零。
    """
    try:
        with backend.AccountSession(acc.auth_json) as sess:
            bal = sess.fetch_balance()
            acc.balance_total = int(bal.get("total", 0) or 0)
            acc.balance_remain = int(bal.get("remain", 0) or 0)
            if with_expiry:
                try:
                    _sync_credit_snapshot(acc, sess.fetch_credit_details())
                except Exception:
                    pass  # 拿不到就保留旧值（不写 0 覆盖）
            acc.auth_json = sess.updated_json()  # 回写可能刷新的 token
        acc.last_sync_at = datetime.utcnow()
        return True
    except Exception:
        return False


def _credit_fields(a: Account) -> dict:
    """账号记录里的积分到期字段 → 前端可直接用的结构。

    `credits_snapshot` 存的是完整汇总 JSON（含逐包明细）。解析失败时
    降级为「只有汇总数字、没有明细」，绝不让一条坏数据把整个列表打挂
    —— 列表是运维最常用的入口，宁可少显示也不要 500。
    """
    snap = {}
    raw = (a.credits_snapshot or "").strip()
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                snap = parsed
        except Exception:
            snap = {}

    from admin import credits as credit_rules

    soonest_ms = None
    if a.credits_soonest_expire_at:
        soonest_ms = int(a.credits_soonest_expire_at.replace(
            tzinfo=timezone.utc).timestamp() * 1000)
    resources = snap.get("resources") or []

    def _slim(r: dict) -> dict:
        """只带列表展示真正用到的字段。

        快照里的每个包有 ~10 个字段（含 package_name / days_left / status / used…），
        而列表每行只画「剩余额度 + 包名 + 到期日」三样。xx 个账号 × 全部包
        （实测可达数百条）会把响应从几十 KB 顶到近 200 KB，且随账号数线性增长 ——
        这正是「越用越卡」的来源。完整字段走单账号明细接口。
        """
        return {
            "package_code": r.get("package_code") or "",
            "display_name": r.get("display_name") or r.get("package_name") or "积分包",
            "remaining": round(float(r.get("remaining") or 0), 4),
            "total": round(float(r.get("total") or 0), 4),
            "expire_at": r.get("expire_at"),
            "expire_label": credit_rules.format_expire_at(r.get("expire_at")),
            "expire_date": credit_rules.expire_date_label(r.get("expire_at")),
            "expiring_soon": bool(r.get("expiring_soon")),
            "expired": bool(r.get("expired")),
        }

    return {
        "credits_expiring": int(a.credits_expiring or 0),          # 旧 30 天口径
        "credits_expiring_soon": int(a.credits_expiring_soon or 0),  # 严格 7 天口径
        "credits_evergreen": int(a.credits_evergreen or 0),
        "credits_expired": int(a.credits_expired or 0),
        "credits_soonest_expire_at": soonest_ms,
        "credits_soonest_days_left": snap.get("soonest_days_left"),
        "credits_synced_at": a.credits_synced_at.isoformat() if a.credits_synced_at else None,
        "credits_collection": snap.get("collection"),
        "credits_package_count": int(snap.get("package_count") or 0),
        "credits_active_package_count": int(snap.get("active_package_count") or 0),
        # 「最近快到期的积分包」：明细已按到期升序，取前 3 个供列表行直接渲染。
        # **不再返回全量 `credits_resources`** —— 前端从未用过它（见 index.html
        # 的 grep：只有 credits_next_expiring 被读取），纯属响应体积负担。
        "credits_next_expiring": [_slim(r) for r in resources[:3]],
    }


@router.get("")
def list_accounts(_: bool = Depends(require_admin), db: Session = Depends(get_db)):
    rows = db.query(Account).order_by(Account.id.desc()).all()
    now = datetime.utcnow()
    limits = _active_model_limits(db, now)
    items = [
        {
            "id": a.id,
            "name": a.name,
            "uid": a.uid,
            "enterprise_id": a.enterprise_id,
            "domain": a.domain,
            "status": a.status,
            "balance_total": a.balance_total,
            "balance_remain": a.balance_remain,
            "last_sync_at": a.last_sync_at.isoformat() if a.last_sync_at else None,
            "last_used_at": a.last_used_at.isoformat() if a.last_used_at else None,
            "created_at": a.created_at.isoformat() if a.created_at else None,
            "model_limits": limits.get(a.id, []),
            **_gateway_fields(a, now),
            **_credit_fields(a),
        }
        for a in rows
    ]
    # 汇总口径：**积分类合计一律只算 active 账号**。
    #
    # 原来这里把全部账号（含已禁用/登录失效的）一起加进 balance_remain /
    # credits_expiring_soon，导致两个问题：
    #   1. 与「积分到期」面板对不上 —— 那个面板只算 active，
    #      于是同一块屏上一个显示 xx、另一个显示 yy（两者相差 xx，
    #      正是 6 个禁用号里根本用不到的额度）；
    #   2. 数字只增不减：账号登录失效被禁用后，它的余额仍被算进「可用积分」，
    #      用户看到的是「号少了但积分没少」。
    #
    # 已禁用账号那部分**不隐藏、但单独给**（disabled_*），
    # 这样既不污染「可用」，又不至于让人以为数据丢了。
    active_items = [i for i in items if i["status"] == "active"]
    disabled_items = [i for i in items if i["status"] != "active"]
    summary = {
        "total": len(items),
        "active": len(active_items),
        "disabled": len(disabled_items),
        "available": sum(1 for i in active_items if i["balance_remain"] > 0),
        # —— 可用口径（只看 active），与 /api/credits/expiry 保持一致 ——
        "balance_total": sum(i["balance_total"] for i in active_items),
        "balance_remain": sum(i["balance_remain"] for i in active_items),
        "credits_expiring_soon": sum(i["credits_expiring_soon"] for i in active_items),
        "credits_evergreen": sum(i["credits_evergreen"] for i in active_items),
        "credits_expired": sum(i["credits_expired"] for i in active_items),
        "expiring_accounts": sum(
            1 for i in active_items if i["credits_expiring_soon"] > 0),
        "soonest_expire_at": min(
            (i["credits_soonest_expire_at"] for i in active_items
             if i["credits_soonest_expire_at"]),
            default=None),
        # —— 已禁用账号那部分：透明展示，但不计入上面的「可用」——
        "disabled_balance_remain": sum(i["balance_remain"] for i in disabled_items),
        "disabled_credits_expiring_soon": sum(
            i["credits_expiring_soon"] for i in disabled_items),
    }
    return {"items": items, "summary": summary}


def _gateway_fields(a: Account, now: datetime) -> dict:
    until = max((t for t in (a.cool_until, a.breaker_until, a.degrade_until)
                 if t and t > now), default=None)
    return {
        "gateway_state": "disabled" if a.status != "active" else "cooling" if until else "ready",
        "gateway_until": until.isoformat() + "Z" if until else None,
        "gateway_reason": a.cool_kind or "breaker" if until else "",
    }


def _active_model_limits(db: Session, now: datetime) -> dict:
    result = {}
    for row in db.query(AccountModelCooldown).filter(AccountModelCooldown.until > now).all():
        # Old daily entries may have been stored as model_rate; report accurately.
        from admin.routers.proxy import _json_code, _MODEL_RATE_CODES
        daily = row.kind == "model_daily" or _json_code(row.reason or "") in _MODEL_RATE_CODES
        result.setdefault(row.account_id, []).append({
            "model": row.model,
            "kind": "model_daily" if daily else row.kind,
            "until": row.until.isoformat() + "Z",
            "remaining_seconds": max(0, int((row.until - now).total_seconds())),
            "reset_source": "estimated" if (row.reason or "").startswith("[estimated]") else "upstream" if daily and row.kind == "model_daily" else "backoff",
            "hits": row.hits or 0,
        })
    return result


@router.get("/model-limits")
def model_limits(_: bool = Depends(require_admin), db: Session = Depends(get_db)):
    """Observed account/model limits + gateway usage; no upstream polling."""
    now = datetime.utcnow()
    rows = db.query(Account).all()
    limits = _active_model_limits(db, now)
    start = (now + timedelta(hours=8)).replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(hours=8)
    usage = {(account_id, model): {"requests_today": count, "tokens_today": int(tokens or 0)}
             for account_id, model, count, tokens in db.query(
                 UsageLog.account_id, UsageLog.model, func.count(UsageLog.id), func.sum(UsageLog.total_tokens))
             .filter(UsageLog.created_at >= start, UsageLog.error_kind == "success")
             .group_by(UsageLog.account_id, UsageLog.model).all()}
    items = []
    for account in rows:
        for entry in limits.get(account.id, []):
            items.append({**entry, "account_id": account.id,
                          "account_name": account.name or account.uid,
                          "account_status": account.status,
                          **usage.get((account.id, entry["model"]), {"requests_today": 0, "tokens_today": 0})})
    from admin.pool import POOL
    summary = []
    configs = {c.model_id: c for c in db.query(ModelConfig).filter(ModelConfig.enabled == 1, ModelConfig.level == "system").all()}
    for model in sorted(set(configs) | {e["model"] for e in items}):
        config = configs.get(model)
        free = config is not None and config.credit_multiplier == 0
        blocked = {e["account_id"] for e in items if e["model"] == model}
        available = [a for a in rows if a.status == "active" and a.id not in blocked
                     and _gateway_fields(a, now)["gateway_state"] == "ready"
                     and (free or (a.balance_remain or 0) > 0)
                     and not POOL.inflight.full(a.uid or "", settings.MAX_IN_FLIGHT)]
        summary.append({"model": model, "free": free,
                        "available_accounts": len(available), "limited_accounts": len(blocked),
                        "requests_today": sum(v["requests_today"] for (_, m), v in usage.items() if m == model),
                        "tokens_today": sum(v["tokens_today"] for (_, m), v in usage.items() if m == model)})
    return {"items": items, "models": summary, "observed_at": now.isoformat() + "Z",
            "waf_remaining_seconds": POOL.waf.remaining(),
            "usage_scope": "gateway_only", "daily_quota_total": None}


class ClearModelLimit(BaseModel):
    model: str


@router.post("/{acc_id}/model-limits/clear")
def clear_model_limit(acc_id: int, body: ClearModelLimit,
                      _: bool = Depends(require_admin), db: Session = Depends(get_db)):
    if not db.query(Account).filter(Account.id == acc_id).first():
        raise HTTPException(status_code=404, detail="账号不存在")
    count = db.query(AccountModelCooldown).filter(
        AccountModelCooldown.account_id == acc_id,
        AccountModelCooldown.model == body.model).delete(synchronize_session=False)
    db.commit()
    return {"ok": True, "cleared": count}


@router.post("")
def add_account(body: AccountIn, _: bool = Depends(require_admin), db: Session = Depends(get_db)):
    # 第一道：只收「auth 之后的授权 JSON」，非凭证一律 400
    _reject_non_credential(body.auth_json)
    # 第二道：**真调上游**确认凭证可用、非伪造，并取回真实账号信息 + 余额
    profile, balance = _verify_credential_live_or_400(body.auth_json)

    # 按 uid 去重：同一个人重复添加只会产生垃圾记录，
    # 而且会让批量任务对同一个号跑多次（白等、看起来像卡住）
    dup = _find_dup(db, body.auth_json)
    if dup:
        # 重复上传 = 用新凭证覆盖 + 刷新余额（原来直接 return，
        # 于是重传一份新凭证后列表还显示旧余额，看起来「没生效」）
        dup.auth_json = body.auth_json
        _apply_meta(dup, body.auth_json, profile=profile, balance=balance)
        db.commit()
        _post_login_automation(dup.id, dup.name or dup.uid)
        return {"id": dup.id, "name": dup.name, "ok": True,
                "duplicated": True,
                "message": f"该账号已存在（id={dup.id}），已用新凭证覆盖更新"}
    acc = Account(name=body.name) if body.name else Account()
    _apply_meta(acc, body.auth_json, profile=profile, balance=balance)
    db.add(acc)
    db.commit()
    db.refresh(acc)
    _refresh_balance(acc)
    db.commit()
    _post_login_automation(acc.id, acc.name or acc.uid)
    return {"id": acc.id, "name": acc.name, "ok": True, "duplicated": False,
            "verified": True, "balance_remain": acc.balance_remain}


@router.post("/batch")
def batch_add(body: AccountBatchIn, _: bool = Depends(require_admin), db: Session = Depends(get_db)):
    """批量上传：逐条做**结构校验 + 真实上游验证**，不合格的记进 errors 并跳过。

    `verify` 默认为 True。批量上传时如果每条都真调上游会慢（每条 1-2 次请求），
    但对「拒绝伪造凭证」这个要求来说这是必须的 —— 结构校验挡不住伪造签名。
    调用方若明确只想快速导入（例如刚从本机导出、已知有效），可显式传
    `verify=False` 跳过上游验证，但**结构校验永远执行**。
    """
    added = 0
    skipped = 0
    errors = []
    # 一次性取出已有 uid，避免每条都查库；
    # 同时把自己刚加进去的 uid 也塞进去，这样同一批里的重复项也能拦住
    seen = _existing_uid_index(db)
    for it in body.items:
        if not it.auth_json or not it.auth_json.strip():
            continue
        # 第一道：非凭证拒收（与单条上传、与手机号登录共用同一校验器）
        chk = wb_login.validate_credential(it.auth_json)
        if not chk.ok:
            errors.append(f"跳过一条：不是有效的客户端凭证（{chk.reason}）")
            continue

        # 第二道：真调上游确认可用、非伪造（可显式关闭）
        profile: dict = {}
        balance: dict = {}
        if body.verify:
            probe = wb_login.probe_credential_live(it.auth_json)
            if not probe.ok:
                errors.append(f"跳过一条：未通过上游验证（{probe.message}）")
                continue
            profile = probe.profile or {}
            balance = probe.balance or {}

        # uid 以上游返回的真实值为准（挡「真令牌 + 假 uid」）
        uid = str(profile.get("uid") or _uid_of(it.auth_json) or "")
        if uid and uid in seen:
            # 已存在 -> 覆盖新凭证并刷新余额（与单条上传同口径）
            old = seen[uid]
            old.auth_json = it.auth_json
            _apply_meta(old, it.auth_json, profile=profile, balance=balance)
            db.commit()
            skipped += 1
            continue
        acc = Account(name=it.name) if it.name else Account()
        _apply_meta(acc, it.auth_json, profile=profile, balance=balance)
        db.add(acc)
        db.commit()
        db.refresh(acc)
        if uid:
            seen[uid] = acc
        _post_login_automation(acc.id, acc.name or acc.uid)
        if _refresh_balance(acc):
            added += 1
        else:
            # 走到这里说明上游验证已过（或未开启），余额刷新失败通常是
            # 瞬时网络问题 —— 记为警告而不是拒绝，凭证本身是可信的。
            errors.append(f"账号 {acc.id} 余额刷新失败（凭据可能失效，请稍后刷新）")
        db.commit()
    return {"added": added, "skipped": skipped, "errors": errors}


@router.get("/duplicates")
def list_duplicates(_: bool = Depends(require_admin), db: Session = Depends(get_db)):
    """列出 uid 重复的账号组（每组保留一条，其余是可清理的冗余）。"""
    rows = db.query(Account).order_by(Account.id.asc()).all()
    groups: dict[str, list[Account]] = {}
    for a in rows:
        if a.uid:
            groups.setdefault(a.uid, []).append(a)

    out = []
    for uid, grp in groups.items():
        if len(grp) < 2:
            continue
        keep = grp[0]                      # id 最小 = 最早创建，通常历史记录最全
        out.append({
            "uid": uid,
            "keep": {"id": keep.id, "name": keep.name,
                     "created_at": _dt(keep.created_at)},
            "remove": [{"id": a.id, "name": a.name,
                        "created_at": _dt(a.created_at)} for a in grp[1:]],
        })
    return {
        "total": len(rows),
        "unique": len(groups),
        "groups": out,
        "redundant": sum(len(g["remove"]) for g in out),
    }


@router.post("/dedupe")
def dedupe_accounts(_: bool = Depends(require_admin), db: Session = Depends(get_db)):
    """清理重复账号：同一 uid 只保留 id 最小（最早创建）的那条。

    为什么保留最早的那条：使用记录（last_used_at）与既有关联数据都挂在
    老记录上，重复导入产生的新记录这些字段都是空的，删掉损失最小。
    """
    rows = db.query(Account).order_by(Account.id.asc()).all()
    groups: dict[str, list[Account]] = {}
    for a in rows:
        if a.uid:
            groups.setdefault(a.uid, []).append(a)

    removed: list[dict] = []
    for uid, grp in groups.items():
        for a in grp[1:]:
            removed.append({"id": a.id, "name": a.name, "uid": uid})
            db.delete(a)
    db.commit()
    return {"removed": len(removed), "kept": len(groups), "items": removed}


def _dt(v) -> str:
    return v.strftime("%Y-%m-%d %H:%M:%S") if v else ""


@router.get("/scan-local")
def scan_local(_: bool = Depends(require_admin)):
    """扫描本机 WorkBuddy/CodeBuddy 登录态目录，列出发现的账号（不读 token 内容到前端）。"""
    d = _client_auth_dir()
    if not os.path.isdir(d):
        return {"dir": d, "exists": False, "active_uid": None, "items": []}
    active_uid = None
    active_file = os.path.join(d, "workbuddy-desktop.info")
    if os.path.exists(active_file):
        try:
            active_uid = (json.load(open(active_file, encoding="utf-8")).get("account") or {}).get("uid")
        except Exception:
            pass
    items = []
    seen_uids = set()  # 按 uid 去重
    for f in sorted(glob.glob(os.path.join(d, "*.info"))):
        base = os.path.basename(f)
        if base.endswith(".bak") or ".bak-" in base:
            continue
        try:
            data = json.load(open(f, encoding="utf-8"))
        except Exception:
            continue
        meta = backend.parse_auth_meta(json.dumps(data))
        uid = meta.get("uid") or ""
        # 同 uid 只保留一份（优先保留无时间戳后缀的"实时登录"文件）
        if uid in seen_uids:
            continue
        seen_uids.add(uid)
        # 提取 token 到期时间
        auth = data.get("auth") or {}
        expires_at = auth.get("expiresAt") or 0
        expires_str = ""
        if expires_at:
            try:
                expires_str = datetime.fromtimestamp(expires_at / 1000).strftime("%Y-%m-%d %H:%M:%S")
            except (OSError, ValueError):
                expires_str = str(expires_at)
        # 提取昵称：过滤字面量 "null" 字符串
        raw_nick = meta.get("nickname") or ""
        nickname = raw_nick if raw_nick.strip().lower() not in ("null", "", "none") else ""
        items.append({
            "file": base,
            "uid": uid,
            "nickname": nickname,
            "domain": meta.get("domain") or "",
            "is_active": (uid == active_uid),
            "expires_at": expires_str,
            "expires_ts": expires_at,
        })
    return {"dir": d, "exists": True, "active_uid": active_uid, "items": items}


@router.post("/import-local")
def import_local(body: ImportLocalIn, _: bool = Depends(require_admin), db: Session = Depends(get_db)):
    """把本机登录态目录里的 .info 直接读入号池（服务端读取，不向前端暴露 token）。"""
    d = _client_auth_dir()
    if not os.path.isdir(d):
        raise HTTPException(status_code=500, detail=f"客户端登录目录不存在: {d}")
    names = list(body.files)
    if body.all or "all" in names:
        names = [
            os.path.basename(f) for f in glob.glob(os.path.join(d, "*.info"))
            if not (f.endswith(".bak") or ".bak-" in f)
        ]
    added = 0
    skipped = 0
    errors = []
    seen = _existing_uid_index(db)   # 已存在的 uid 跳过；同批内也去重
    for name in names:
        path = os.path.join(d, name)
        if not os.path.isfile(path):
            errors.append(f"文件不存在: {name}")
            continue
        try:
            auth = open(path, encoding="utf-8").read()
        except Exception:
            errors.append(f"{name}: 读取失败")
            continue
        # 目录里的 *.info 未必都是凭证（可能有半截文件 / 别的 JSON），
        # 与上传入口用同一个校验器把关，避免把垃圾读进号池。
        chk = wb_login.validate_credential(auth)
        if not chk.ok:
            errors.append(f"{name}: 不是有效的客户端凭证（{chk.reason}）")
            continue
        meta = backend.parse_auth_meta(auth)
        uid = meta.get("uid")
        if uid and uid in seen:
            skipped += 1
            continue  # 同 uid 多文件 / 已存在，只导入一次
        acc = Account()
        # 这是**本机客户端自己写的**登录态，可信度天然高于网页上传，
        # 故不做额外的上游验证；`_refresh_balance` 已经会真调一次上游，
        # 失败时记为警告（凭证本身来自本机，不该因瞬时网络问题被丢弃）。
        _apply_meta(acc, auth)
        db.add(acc)
        db.commit()
        db.refresh(acc)
        if uid:
            seen[uid] = acc
        if _refresh_balance(acc):
            added += 1
        else:
            errors.append(f"账号 {acc.id}({acc.name}) 余额刷新失败（稍后可手动刷新）")
        db.commit()
    return {"added": added, "skipped": skipped, "errors": errors}


@router.get("/{acc_id}/export")
def export_one_account(
    acc_id: int,
    _: bool = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """导出单个账号的登录态（.info 原文）。

    与批量导出格式一致，可直接用「批量上传」导回。
    """
    acc = db.query(Account).filter(Account.id == acc_id).first()
    if not acc:
        raise HTTPException(status_code=404, detail="账号不存在")
    raw = (acc.auth_json or "").strip()
    if not raw:
        raise HTTPException(status_code=400, detail="该账号没有可导出的凭据")
    try:
        json.loads(raw)
    except Exception:
        raise HTTPException(status_code=400, detail="凭据内容不是合法 JSON，无法导出")
    return {
        "id": acc.id,
        "name": acc.name,
        "uid": acc.uid,
        "item": raw,
    }


@router.post("/{acc_id}/inject")
def inject_to_client(acc_id: int, body: InjectIn, _: bool = Depends(require_admin), db: Session = Depends(get_db)):
    """把号池里某账号的 .info 注入到本机客户端的活动登录文件（先备份当前登录态）。"""
    acc = db.query(Account).filter(Account.id == acc_id).first()
    if not acc:
        raise HTTPException(status_code=404, detail="账号不存在")
    if not body.confirm:
        raise HTTPException(status_code=400, detail="需要 confirm=true 才执行注入")
    d = _client_auth_dir()
    if not os.path.isdir(d):
        raise HTTPException(status_code=500, detail=f"客户端登录目录不存在: {d}")
    target = os.path.join(d, "workbuddy-desktop.info")
    backup = None
    if os.path.exists(target):
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = target + f".bak-{ts}"
        shutil.copy2(target, backup)
    with open(target, "w", encoding="utf-8") as f:
        f.write(acc.auth_json)
    return {"ok": True, "target": target, "backup": backup, "account": acc.name, "uid": acc.uid}


@router.post("/{acc_id}/refresh")
def refresh_account(acc_id: int, with_expiry: bool = True,
                    _: bool = Depends(require_admin), db: Session = Depends(get_db)):
    """刷新单账号余额。

    `with_expiry` 默认 **True**：单个账号的刷新通常是「我想看看这个号现在
    到底还剩多少、哪个包要先到期」，只回一个总额等于没解决他的问题。
    多一次上游请求（约 0.4s），换取到期快照的实时性，这个交换是值得的。
    批量刷新走 `/credits/sync` 与定时任务，口径一致。
    """
    acc = db.query(Account).filter(Account.id == acc_id).first()
    if not acc:
        raise HTTPException(status_code=404, detail="账号不存在")
    ok = _refresh_balance(acc, with_expiry=with_expiry)
    db.commit()
    if not ok:
        raise HTTPException(status_code=502, detail="刷新失败：后端调用异常（凭据/限流）")
    return {
        "id": acc.id,
        "balance_total": acc.balance_total,
        "balance_remain": acc.balance_remain,
        **_credit_fields(acc),
    }


@router.get("/{acc_id}/credit-details")
def credit_details(acc_id: int, refresh: bool = False,
                   _: bool = Depends(require_admin), db: Session = Depends(get_db)):
    """获取账号积分明细（每个积分包的总量/剩余/到期时间）。

    默认**优先返回库内快照**：整点定时任务已经把逐包明细存下来了，
    点开详情时再打一次上游纯属浪费（每个号约 0.4s，用户要等）。
    `refresh=true`（界面上是「重新采集」）才真调上游并写回快照。

    返回的 `packages` 是**归一化后**的结构：到期时间已经过
    `resolve_expire_at` 处理（长期占位值不会被当成真实到期日），
    剩余额度取当前周期口径，并带上 `expiring_soon` / `days_left`
    供界面直接标色与排序。
    """
    acc = db.query(Account).filter(Account.id == acc_id).first()
    if not acc:
        raise HTTPException(status_code=404, detail="账号不存在")

    from admin import credits as credit_rules

    def _cached() -> list[dict]:
        raw = (acc.credits_snapshot or "").strip()
        if not raw:
            return []
        try:
            parsed = json.loads(raw)
        except Exception:
            return []
        return parsed.get("resources") or [] if isinstance(parsed, dict) else []

    packages: list[dict] = []
    source = "cache"
    error = ""
    if refresh or not _cached():
        try:
            with backend.AccountSession(acc.auth_json) as sess:
                raw_packages = sess.fetch_credit_details()
                # 回写可能刷新的 token
                acc.auth_json = sess.updated_json()
            _sync_credit_snapshot(acc, raw_packages)
            db.commit()
            packages = _cached()
            source = "live"
            collection = getattr(raw_packages, "metadata", None)
            if collection and not collection["complete"]:
                source = "cache" if packages else "none"
                error = "账单来源不完整，保留上次完整快照"
                if not packages:
                    raise HTTPException(status_code=502, detail=error)
        except Exception as e:
            error = str(e)
            packages = _cached()   # 上游失败也要能把上次的快照给出去
            source = "cache" if packages else "none"
            if not packages:
                raise HTTPException(status_code=502, detail=f"获取积分明细失败: {e}")
    else:
        packages = _cached()

    summary = credit_rules.summarize_account(packages)
    # 明细按到期升序返回 —— 与面板的「最近快到期的积分包」、以及 `packages`
    # 的展示顺序保持一致。用 `summary["resources"]` 而不是原始 `packages`：
    # 前者已排好序（且已排除没有剩余额度的包），后者是上游给出的顺序
    # （实测按创建时间，与到期时间无关，直接展示会出现「10/31 排在 10/12 前面」）。
    ordered = summary["resources"]
    try:
        snapshot_meta = json.loads(acc.credits_snapshot or "{}")
        collection_meta = snapshot_meta.get("collection") if isinstance(snapshot_meta, dict) else None
    except (ValueError, TypeError):
        collection_meta = None
    return {
        "id": acc_id,
        "account": acc.name,
        "source": source,                       # live | cache
        "error": error or None,
        "collection": collection_meta,
        # 旧字段保留：`packages` 维持升级前的扁平结构
        # （`name` / `total` / `remain` / `used` / `deduction_end` …）。
        # 把它改成新结构会让既有脚本与页面静默显示空白 —— 兼容成本极低，
        # 就不做这个破坏性变更。新代码请用下面的 `credits.resources`。
        "packages": [_legacy_package(r) for r in ordered],
        # 新增：账号级到期汇总 + 参考实现同口径的字段命名
        "credits": {
            "ok": True,
            "account_id": acc_id,
            "account_name": acc.name,
            "updated_at": (
                int(acc.credits_synced_at.replace(tzinfo=timezone.utc).timestamp() * 1000)
                if acc.credits_synced_at else None),
            "total_capacity": summary["total_capacity"],
            "total_remaining": summary["total_remaining"],
            "expiring_soon_remaining": summary["expiring_soon_remaining"],
            "expired_remaining": summary["expired_remaining"],
            "soonest_expire_at": summary["soonest_expire_at"],
            "soonest_days_left": summary["soonest_days_left"],
            "expiring_soon": summary["expiring_soon"],
            "expired": summary["expired"],
            "expiring_soon_days": credit_rules.EXPIRING_SOON_DAYS,
            #: 只含**仍有剩余**的包（已用完的不必占版面），按到期升序
            "resources": [
                {
                    "packageCode": r["package_code"],
                    "packageName": r["package_name"],
                    "displayName": r["display_name"],
                    "total": r["total"],
                    "remaining": r["remaining"],
                    "used": r["used"],
                    "status": r["status"],
                    "expireAt": r["expire_at"],
                    "expireLabel": credit_rules.format_expire_at(r["expire_at"]),
                    "expireDate": credit_rules.expire_date_label(r["expire_at"]),
                    "expired": r["expired"],
                    "expiringSoon": r["expiring_soon"],
                    "daysLeft": r["days_left"],
                }
                for r in ordered
            ],
            #: 全部包（含已用完），也按到期升序 —— 「积分明细」弹窗要看全量
            "all_resources": [
                {
                    "packageCode": r["package_code"],
                    "packageName": r["package_name"],
                    "displayName": r["display_name"],
                    "total": r["total"],
                    "remaining": r["remaining"],
                    "used": r["used"],
                    "status": r["status"],
                    "expireAt": r["expire_at"],
                    "expireLabel": credit_rules.format_expire_at(r["expire_at"]),
                    "expireDate": credit_rules.expire_date_label(r["expire_at"]),
                    "expired": r["expired"],
                    "expiringSoon": r["expiring_soon"],
                    "daysLeft": r["days_left"],
                }
                for r in sorted(
                    packages,
                    key=lambda x: (x.get("expire_at") is None,
                                   x.get("expire_at") or 0))
            ],
        },
    }


def _legacy_package(r: dict) -> dict:
    """归一化资源包 → 升级前的扁平结构（`packages` 字段的兼容层）。

    升级前 `fetch_credit_details` 就在这里把上游字段改名成
    `name` / `total` / `remain` / `deduction_end` 等；现在改名搬到
    `admin/credits.py`（口径判断的唯一点），但**对外结构保持不变** ——
    既有页面与脚本都在读它，改结构会让它们静默显示空白。
    """
    expire_at = r.get("expire_at")
    return {
        "name": r.get("display_name") or r.get("package_name") or "未命名",
        "total": r.get("total") or 0,
        "remain": r.get("remaining") or 0,
        "used": r.get("used") or 0,
        # 账号层累积剩余（CapacityRemain）：与「当前周期剩余」是两个口径，
        # 这里没有等价值就不假造，给 0 比给一个错数字诚实。
        "account_remain": 0,
        "account_used": 0,
        "cycle_start": "",
        "cycle_end": _ms_to_str(expire_at),
        "deduction_end_ts": expire_at or 0,
        "deduction_end": _ms_to_str(expire_at),
        "status": r.get("status"),
        "package_code": r.get("package_code") or "",
    }


def _ms_to_str(ts) -> str:
    if not ts:
        return ""
    try:
        return datetime.fromtimestamp(int(ts) / 1000).strftime("%Y-%m-%d %H:%M:%S")
    except (OSError, OverflowError, ValueError, TypeError):
        return ""


@router.post("/credits/sync")
def sync_credits(_: bool = Depends(require_admin), db: Session = Depends(get_db)):
    """立即重新采集**全部活跃账号**的积分到期快照。

    与整点定时任务 `refresh_credits` 走同一份实现（`_sync_credit_snapshot`），
    避免「手动刷新」和「自动刷新」出现两套口径这种经典问题。

    串行 + 每号间隔，与保活/批量任务的节流规则一致：批量并发请求上游
    本身就是很明显的机器特征。账号多时耗时会到几十秒，故前端应按后台任务
    的节奏处理（这里返回逐号结果，供前端展示进度）。
    """
    from admin.config import settings

    accounts = (db.query(Account).filter(Account.status == "active")
                .order_by(Account.id.asc()).all())
    ok = 0
    failed = 0
    detail: list[dict] = []
    for idx, a in enumerate(accounts):
        if idx and settings.KEEPALIVE_ACCOUNT_GAP > 0:
            time.sleep(settings.KEEPALIVE_ACCOUNT_GAP)
        try:
            with backend.AccountSession(a.auth_json) as sess:
                packages = sess.fetch_credit_details()
                a.auth_json = sess.updated_json()
            summary = _sync_credit_snapshot(a, packages)
            collection = getattr(packages, "metadata", None)
            if collection and not collection["complete"]:
                failed += 1
                detail.append({"id": a.id, "account": a.name, "ok": False,
                               "error": "账单来源不完整，保留旧快照", "collection": collection})
                db.commit()
                continue
            ok += 1
            detail.append({
                "id": a.id, "account": a.name, "ok": True,
                "expiring_soon": int(summary["expiring_soon_remaining"]),
                "soonest_days_left": summary["soonest_days_left"],
                "packages": summary["active_package_count"],
            })
        except Exception as e:
            failed += 1
            detail.append({"id": a.id, "account": a.name, "ok": False,
                           "error": str(e)[:160]})
        db.commit()

    expiring = sum(int(a.credits_expiring_soon or 0) for a in accounts)
    return {
        "ok": True, "total": len(accounts), "refreshed": ok, "failed": failed,
        "credits_expiring_soon": expiring,
        "detail": detail,
    }


@router.get("/{acc_id}/request-usage")
def request_usage(
    acc_id: int,
    start_time: str = "",
    end_time: str = "",
    page_num: int = 1,
    page_size: int = 10,
    _: bool = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """获取账号模型请求用量（对接 WorkBuddy 已有接口，不自建日志）。"""
    acc = db.query(Account).filter(Account.id == acc_id).first()
    if not acc:
        raise HTTPException(status_code=404, detail="账号不存在")
    # 默认今天
    if not start_time:
        start_time = datetime.now().strftime("%Y-%m-%d 00:00:00")
    if not end_time:
        end_time = datetime.now().strftime("%Y-%m-%d 23:59:59")
    try:
        with backend.AccountSession(acc.auth_json) as sess:
            data = sess.fetch_request_usage(start_time, end_time, page_num, page_size)
            acc.auth_json = sess.updated_json()
            db.commit()
        return data
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"获取请求用量失败: {e}")


@router.post("/{acc_id}/cat-travel")
def cat_travel(acc_id: int, _: bool = Depends(require_admin), db: Session = Depends(get_db)):
    """对单个账号执行一趟猫猫旅行：同意协议 → 首次领养(+300) → 派出 → 领奖。

    返回结构化分步结果，前端据此逐步提示「领养成功 +300」「领奖成功 +N」，
    无需再去积分明细核对。
    """
    acc = db.query(Account).filter(Account.id == acc_id).first()
    if not acc:
        raise HTTPException(status_code=404, detail="账号不存在")
    try:
        with backend.AccountSession(acc.auth_json) as sess:
            result = sess.run_cat_travel()
            acc.auth_json = sess.updated_json()  # 回写可能刷新的 token
            db.commit()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"猫猫旅行执行失败: {e}")
    # 领取到积分后顺带刷新余额，让前端列表立即反映最新值
    if result.get("credits"):
        _refresh_balance(acc)
        db.commit()
    return {
        "id": acc.id,
        "account": acc.name,
        "balance_remain": acc.balance_remain,
        **result,
    }


class CatTravelBatchIn(BaseModel):
    ids: list[int] = []  # 指定账号；为空则对全部启用账号执行


#: 猫猫旅行后台任务 key
CAT_JOB_KEY = "cat_travel"


@router.post("/cat-travel/batch-async")
def cat_travel_batch_async(
    body: CatTravelBatchIn,
    _: bool = Depends(require_admin),
):
    """异步批量猫猫旅行：立即返回 job_id，前端轮询进度。

    每个账号要串行做「同意协议 → 补门槛对话 → 领养 → 派猫 → 领奖」，
    同步等容易让 nginx 先超时（默认 60s），所以放到后台线程跑。
    重复点击复用同一个 job，不会叠起并发批量。
    """
    running = jobrunner.RUNNER.get(CAT_JOB_KEY)
    if running and running.status == "running":
        return {"reused": True, **running.snapshot()}

    db = SessionLocal()
    try:
        q = db.query(Account).filter(Account.status == "active")
        if body.ids:
            q = q.filter(Account.id.in_(body.ids))
        ids = [a.id for a in q.order_by(Account.id).all()]
    finally:
        db.close()

    def worker(job: jobrunner.Job) -> dict:
        db = SessionLocal()
        buckets = {"adopted": 0, "adopt_credits": 0,
                   "travel_claimed": 0, "travel_credits": 0,
                   "travel_none": 0, "traveling": 0,
                   "gate_blocked": 0, "error": 0}
        try:
            job.set_phase("执行中")
            for aid in ids:
                acc = db.query(Account).get(aid)
                if not acc:
                    continue
                try:
                    with backend.AccountSession(acc.auth_json) as sess:
                        res = sess.run_cat_travel()
                        acc.auth_json = sess.updated_json()
                        db.commit()
                except Exception as e:
                    item = {"id": aid, "account": acc.name, "ok": False,
                            "credits": 0, "summary": f"执行异常: {e}",
                            "steps": [], "outcome": "error"}
                    buckets["error"] += 1
                    job.add_item(item)
                    continue

                if res.get("credits"):
                    _refresh_balance(acc)
                    db.commit()
                outcome = res.get("outcome") or ("error" if not res.get("ok") else "travel_none")
                if outcome == "adopted":
                    buckets["adopted"] += 1
                    buckets["adopt_credits"] += res.get("credits") or 0
                elif outcome == "travel_claimed":
                    buckets["travel_claimed"] += 1
                    buckets["travel_credits"] += res.get("credits") or 0
                elif outcome in buckets:
                    buckets[outcome] += 1
                else:
                    buckets["travel_none"] += 1

                job.add_item({"id": aid, "account": acc.name,
                              "balance_remain": acc.balance_remain, **res})
                time.sleep(0.5)   # 账号之间留一点间隔，避免打太快
        finally:
            db.close()
        return {
            "total": len(ids),
            "credits": buckets["adopt_credits"] + buckets["travel_credits"],
            "succeeded": sum(1 for r in job.items if r.get("ok")),
            "buckets": buckets,
        }

    job = jobrunner.RUNNER.start(CAT_JOB_KEY, len(ids), worker, title="批量猫猫旅行")
    return job.snapshot()


@router.post("/cat-travel/batch")
def cat_travel_batch(
    body: CatTravelBatchIn,
    _: bool = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """批量执行猫猫旅行；逐个账号串行执行并返回每个账号的结果。

    按结果分类统计，避免把「首次领养 +300」和「旅行到站返现 +7」混在一起 ——
    两者量级差几十倍，合并成一个总数会让人误以为执行无效。
    """
    q = db.query(Account).filter(Account.status == "active")
    if body.ids:
        q = q.filter(Account.id.in_(body.ids))
    rows = q.order_by(Account.id).all()

    results = []
    total_credits = 0
    #: 分类计数：首次领养 / 旅行返现 / 无新增 / 门槛未达标 / 失败
    buckets = {"adopted": 0, "adopt_credits": 0,
               "travel_claimed": 0, "travel_credits": 0,
               "travel_none": 0, "traveling": 0,
               "gate_blocked": 0, "error": 0}
    for acc in rows:
        try:
            with backend.AccountSession(acc.auth_json) as sess:
                res = sess.run_cat_travel()
                acc.auth_json = sess.updated_json()
                db.commit()
        except Exception as e:
            results.append({"id": acc.id, "account": acc.name, "ok": False,
                            "credits": 0, "summary": f"执行异常: {e}", "steps": [],
                            "outcome": "error"})
            buckets["error"] += 1
            continue
        if res.get("credits"):
            total_credits += res["credits"]
            _refresh_balance(acc)
            db.commit()

        outcome = res.get("outcome") or ("error" if not res.get("ok") else "travel_none")
        if outcome == "adopted":
            buckets["adopted"] += 1
            buckets["adopt_credits"] += res.get("credits") or 0
        elif outcome == "travel_claimed":
            buckets["travel_claimed"] += 1
            buckets["travel_credits"] += res.get("credits") or 0
        elif outcome in buckets:
            buckets[outcome] += 1
        else:
            buckets["travel_none"] += 1

        results.append({
            "id": acc.id,
            "account": acc.name,
            "balance_remain": acc.balance_remain,
            **res,
        })
    return {
        "total": len(results),
        "credits": total_credits,
        "succeeded": sum(1 for r in results if r.get("ok")),
        "buckets": buckets,
        "results": results,
    }


@router.patch("/{acc_id}")
def patch_account(
    acc_id: int,
    body: dict,
    _: bool = Depends(require_admin),
    db: Session = Depends(get_db),
):
    acc = db.query(Account).filter(Account.id == acc_id).first()
    if not acc:
        raise HTTPException(status_code=404, detail="账号不存在")
    if "name" in body:
        acc.name = body["name"]
    if "status" in body and body["status"] in ("active", "disabled"):
        acc.status = body["status"]
    db.commit()
    return {"id": acc.id, "ok": True}


@router.delete("/{acc_id}")
def delete_account(acc_id: int, _: bool = Depends(require_admin), db: Session = Depends(get_db)):
    acc = db.query(Account).filter(Account.id == acc_id).first()
    if not acc:
        raise HTTPException(status_code=404, detail="账号不存在")
    db.query(AccountModelCooldown).filter(AccountModelCooldown.account_id == acc_id).delete(synchronize_session=False)
    db.delete(acc)
    db.commit()
    return {"id": acc_id, "ok": True}
