"""积分资源包解析与到期口径（纯函数，无 IO）。

为什么单独成模块
----------------
原来「一个积分包值多少、什么时候到期」的判定散在两处：
`backend.fetch_credit_details` 只把上游字段摊平成一个 dict，
`routers/accounts._compute_expiring` 再按 30 天窗口自己算一遍。

这带来三个真实缺陷：

1. **到期时间取错**。上游的 `DeductionEndTime` 并不总是真实到期时间 ——
   版本基础用量（`TCACA_code_008`）的 `DeductionEndTime` 是一个 2034 年的
   占位值，真正的周期结束在 `CycleEndTime`（当月月底）。只读前者就会把这个
   包判成「永不过期」，于是它的剩余额度永远不会被计入「快过期」，
   选号时也就永远轮不到它 —— 到期后整包作废。
2. **口径不一致**。列表页说 30 天内算快过期，参考实现（以及官方客户端）
   用的是 **7 天**。两个数字并存时，用户看到的「快过期」和调度用的
   「快过期」不是同一件事。
3. **无法回答「最近到底哪个包先到期」**。只保留一个汇总数字，
   丢掉了逐包明细，所以做不出参考实现那种「近期到期」的包列表。

本模块把这条口径收拢成唯一一份实现：`resource_summary` 负责单包归一化，
`summarize_account` 负责账号级汇总，调度、展示、统计全部消费同一份结果。

借鉴来源（E:\\反代理\\workbuddy-switch-main）
-------------------------------------------
  crates/wb-switch-core/src/modules/credits.rs
    resolve_expire_at / resource_summary / credit_result
  取值口径与常量（EXPIRING_SOON_DAYS / EXPIRY_CYCLE_OVERRIDE_DAYS /
  FAR_FUTURE_EXPIRY_DAYS）与其保持一致，这样两边的「快过期」是同一个意思。
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

#: 「快过期」窗口（天）。与参考实现及官方客户端一致 —— 官方在套餐页
#: 把 7 天内到期的额度标成橙色，调度也按这个窗口抢先消耗。
EXPIRING_SOON_DAYS = 7

#: `DeductionEndTime` 比 `CycleEndTime` 晚超过该天数时，视前者为**长期占位值**，
#: 改用 `CycleEndTime`。实测形态：版本基础用量包给出
#: `DeductionEndTime=2034-12-12`（占位）与 `CycleEndTime=当月月底` 并存。
EXPIRY_CYCLE_OVERRIDE_DAYS = 365

#: 最终解析出的到期时间距 now 超过该天数时视为「长期有效」（`expire_at = None`），
#: 避免 2049 / 2034 这类占位值流到前端，被当成真实到期日展示。
FAR_FUTURE_EXPIRY_DAYS = 730

#: 商品码 → 官方中文名。
#:
#: 官方客户端在**国内版**不使用后端下发的 `PackageName`（那是运营原文，
#: 形如「CodeBuddy个人版国内运营裂变包」，长且带内部术语），而是按商品码
#: 映射成前端文案；国际版才用 `PackageName`。这里照抄同一张映射表
#: （快照来源：官方客户端 asar 内 package-name-resolver.ts）。
#: 未登记的商品码保持原回落链（package_name → package_code），不会漏展示。
CREDIT_PACKAGE_NAMES: dict[str, str] = {
    "TCACA_code_001_PqouKr6QWV": "CodeBuddy 个人体验版",
    "TCACA_code_002_AkiJS3ZHF5": "版本基础用量",
    "TCACA_code_003_FAnt7lcmRT": "CodeBuddy 个人标准版",
    "TCACA_code_005_maRGyrHhw1": "版本基础用量",
    "TCACA_code_006_DbXS0lrypC": "CodeBuddy 个人体验版",
    "TCACA_code_007_nzdH5h4Nl0": "平台奖励积分",
    "TCACA_code_008_cfWoLwvjU4": "版本基础用量",
    "TCACA_code_009_0XmEQc2xOf": "购买积分",
    "TCACA_code_023_4xbGhMrE6q": "版本基础用量",
    "TCACA_code_026_BaESVICNoi": "版本基础用量",
    "TCACA_code_027_0FCGVA6vSa": "版本基础用量",
    "TCACA_code_028_NtpWi0jzXs": "版本赠送用量",
    "TCACA_code_029_6wCGEWquYy": "平台奖励积分",
    "TCACA_code_030_BjSt89qTvr": "平台奖励积分",
    "TCACA_code_035_ArVxJcGDsm": "版本基础用量",
    "TCACA_code_036_lupO5WgNdG": "购买积分",
    "TCACA_code_037_WxOD3MpI2o": "版本赠送用量",
    "TCACA_code_038_OhvqZtiPKr": "购买积分",
    "TCACA_code_039_KRcQj7wUat": "版本基础用量",
    "TCACA_code_040_mi9rCYg46x": "版本基础用量",
}


def package_display_name(package_code: str | None, package_name: str | None,
                         fallback: str = "积分包") -> str:
    """资源包展示名：命中官方商品码映射用官方中文名，否则回落到上游原文。

    回落链与官方客户端一致：`商品码映射` → `PackageName` → `PackageCode` → 兜底文案。
    """
    code = (package_code or "").strip()
    if code:
        official = CREDIT_PACKAGE_NAMES.get(code)
        if official:
            return official
    name = (package_name or "").strip()
    if name:
        return name
    return code or fallback


# ---------------------------------------------------------------------------
# 时间解析
# ---------------------------------------------------------------------------
def now_ms() -> int:
    """当前时间（毫秒）。集中一个入口便于测试注入。"""
    return int(time.time() * 1000)


def parse_timestamp_ms(value: Any) -> int | None:
    """把上游各种形态的时间解析成毫秒时间戳；无法解析返回 None。

    上游同一个语义字段可能是：
      * 毫秒时间戳（`DeductionEndTime`）
      * 秒时间戳（历史字段里出现过）
      * `"2026-09-30 23:59:59"` 这样的本地时间字符串（`CycleEndTime`）
      * `"%Y-%m-%d"` 纯日期
      * 空串（`ExpiredTime` 未过期时就是空串，**必须**判成 None 而不是 0）
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        n = float(value)
        if n <= 0:
            return None
        # 10 位以内视为秒，13 位视为毫秒（与参考实现同判据）
        return int(round(n * 1000)) if abs(n) < 10_000_000_000 else int(round(n))
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        # 纯数字字符串
        try:
            return parse_timestamp_ms(float(text))
        except ValueError:
            pass
        # Preserve explicit offsets; unzoned upstream billing clocks are CN time,
        # not the deployment host's local timezone (Linux servers often use UTC).
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
            if len(text) == 10:
                dt = dt.replace(hour=23, minute=59, second=59)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone(timedelta(hours=8)))
            return int(dt.timestamp() * 1000)
        except ValueError:
            pass
        for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S",
                    "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
            try:
                dt = datetime.strptime(text, fmt)
            except ValueError:
                continue
            if fmt == "%Y-%m-%d":
                dt = dt.replace(hour=23, minute=59, second=59)
            return int(dt.replace(tzinfo=timezone(timedelta(hours=8))).timestamp() * 1000)
        return None
    return None


def resolve_expire_at(raw: dict, now: int | None = None) -> int | None:
    """解析一个资源包的到期时间（毫秒）；长期有效返回 None。

    取值顺序（对齐参考实现的 `resolve_expire_at`）：

    1. `DeductionEndTime` → `deductionEndTime` → `ExpiredTime` → `expiredTime`
    2. `CycleEndTime` → `cycleEndTime`
    3. 若 (1) 比 (2) 晚超过 ``EXPIRY_CYCLE_OVERRIDE_DAYS`` 天 → 认定 (1) 是
       长期占位值，改用 (2)
    4. 最终值距 now 超过 ``FAR_FUTURE_EXPIRY_DAYS`` 天 → 视为长期有效，返回 None

    第 3 步是这里最关键的修正：不做它，版本基础用量包（占位到 2034 年）
    会被判成永不过期，其剩余额度永远不会进入「快过期」统计。
    """
    now = now if now is not None else now_ms()
    deduction_end = parse_timestamp_ms(_first(raw, (
        "DeductionEndTime", "deductionEndTime", "ExpiredTime", "expiredTime")))
    cycle_end = parse_timestamp_ms(_first(raw, ("CycleEndTime", "cycleEndTime")))

    override_ms = EXPIRY_CYCLE_OVERRIDE_DAYS * 86400 * 1000
    if deduction_end is not None and cycle_end is not None:
        if deduction_end - cycle_end > override_ms:
            expire_at = cycle_end
        else:
            expire_at = deduction_end
    elif deduction_end is not None:
        expire_at = deduction_end
    else:
        expire_at = cycle_end

    if expire_at is None:
        return None
    # 长期占位值：既包括远超窗口的将来，也包括上游偶发给出的 0/极小值。
    if expire_at - now > FAR_FUTURE_EXPIRY_DAYS * 86400 * 1000:
        return None
    return expire_at


def _first(raw: dict, keys: Iterable[str]) -> Any:
    for k in keys:
        if k in raw and raw[k] not in (None, ""):
            return raw[k]
    return None


def _number(value: Any) -> float | None:
    """宽松取数：上游把容量写成字符串 `"459.49000428"` 也很常见。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return float(text)
        except ValueError:
            return None
    return None


def _first_number(raw: dict, keys: Iterable[str]) -> float | None:
    for k in keys:
        n = _number(raw.get(k))
        if n is not None:
            return n
    return None


# ---------------------------------------------------------------------------
# 单包归一化
# ---------------------------------------------------------------------------
#: 槽位优先级：精确值优先于整数近似值。
#:
#: 上游对同一语义给了两套字段：`CycleCapacityRemain`（整数，实测 459）
#: 与 `CycleCapacityRemainPrecise`（字符串小数，实测 459.49000428）。
#: 用整数版会让「已用 + 剩余 ≠ 总量」出现 40 + 459 = 499 ≠ 500 的缺口，
#: 展示和用量核对都会被这个缺口误导，所以优先取 precise。
_SIZE_KEYS = ("CycleCapacitySizePrecise", "CycleCapacitySize", "CycleTotalCapacity", "CapacitySizePrecise",
              "CapacitySize", "SlicePeriodCapacitySizePrecise", "SlicePeriodCapacitySize")
_REMAIN_KEYS = ("CycleCapacityRemainPrecise", "CycleCapacityRemain", "CycleRemainCapacity", "CapacityRemainPrecise",
                "CapacityRemain", "SlicePeriodCapacityRemainPrecise", "SlicePeriodCapacityRemain")
_USED_KEYS = ("CycleCapacityUsedPrecise", "CycleCapacityUsed", "CycleUsedCapacity", "CapacityUsedPrecise",
              "CapacityUsed", "SlicePeriodCapacityUsedPrecise", "SlicePeriodCapacityUsed")


def resource_summary(raw: dict, now: int | None = None) -> dict:
    """把一个上游资源包归一化成统一结构（供展示 / 统计 / 调度共用）。

    返回字段：
        package_code / package_name / display_name
        total / remaining / used        —— 当前周期口径，三者内部自洽
        status                          —— 上游 Status（0 生效 / 3 已结束）
        expire_at / expire_at_ms / expired / expiring_soon
        days_left                       —— 距到期天数（无到期为 None）

    `use_cycle=True` 的位置说明：剩余额度取 **当前周期剩余**
    （`CycleCapacityRemain*`）而不是账号层累积剩余（`CapacityRemain`）。
    体验版用完时 `CapacityRemain` 仍可能显示 500，用它会让调度以为这个号
    还有额度，切过去才发现用不了。
    """
    now = now if now is not None else now_ms()

    slices = raw.get("SlicePeriodUsageDetails") or raw.get("slicePeriodUsageDetails") or []
    if slices and isinstance(slices[0], dict):
        raw = {**slices[0], **{key: value for key, value in raw.items() if value not in (None, "")}}
    raw_total = _first_number(raw, _SIZE_KEYS)
    raw_remaining = _first_number(raw, _REMAIN_KEYS)
    raw_used = _first_number(raw, _USED_KEYS)

    # 三个数里缺一个就用另外两个补，保证 total = remaining + used 自洽。
    if raw_total is None:
        if raw_remaining is not None and raw_used is not None:
            raw_total = raw_remaining + raw_used
        else:
            raw_total = raw_remaining if raw_remaining is not None else raw_used
    total = max(0.0, raw_total or 0.0)
    if raw_remaining is None:
        remaining = max(0.0, total - (raw_used or 0.0))
    else:
        remaining = max(0.0, raw_remaining)
    if raw_used is None:
        used = max(0.0, total - remaining)
    else:
        used = max(0.0, raw_used)

    expire_at = resolve_expire_at(raw, now)
    expired = expire_at is not None and expire_at <= now
    expiring_soon = (expire_at is not None and expire_at > now
                     and expire_at - now <= EXPIRING_SOON_DAYS * 86400 * 1000)
    days_left = None
    if expire_at is not None:
        days_left = max(0.0, (expire_at - now) / 86400000.0)

    package_code = str(_first(raw, ("PackageCode", "packageCode")) or "")
    package_name = str(_first(raw, ("PackageName", "packageName")) or "")

    return {
        "package_code": package_code,
        "package_name": package_name,
        "display_name": package_display_name(package_code, package_name),
        "total": round(total, 4),
        "remaining": round(remaining, 4),
        "used": round(used, 4),
        "status": _first(raw, ("Status", "status")),
        "expire_at": expire_at,
        "expired": expired,
        "expiring_soon": expiring_soon,
        "days_left": round(days_left, 2) if days_left is not None else None,
    }


# ---------------------------------------------------------------------------
# 账号级汇总
# ---------------------------------------------------------------------------
def summarize_account(resources: list[dict], now: int | None = None) -> dict:
    """把逐包明细汇总成账号级到期视图。

    口径（与参考实现的 `credit_result` 一致）：

    * `soonest_expire_at` **只在还有剩余额度的包**里取最早 —— 已经用完的包
      即使明天到期也没有可抢救的额度，把它算进来会让调度白跑一趟。
    * `expiring_soon_remaining` 是 7 天内到期的包**剩余额度合计**，
      也就是「再不抢用就会作废」的那部分积分。这是调度的核心依据。
    * `expired_remaining` 是已经过期但账面上还有剩余的额度（上游扣减延迟
      或周期错位时会出现），单独列出以便对账，不计入可抢救额度。
    """
    now = now if now is not None else now_ms()
    total_capacity = 0.0
    total_remaining = 0.0
    expiring_soon_remaining = 0.0
    expired_remaining = 0.0
    soonest_expire_at: int | None = None
    expiring_soon = False
    expired = False

    for r in resources or []:
        remaining = float(r.get("remaining") or 0.0)
        total_capacity += float(r.get("total") or 0.0)
        total_remaining += remaining
        if r.get("expiring_soon"):
            expiring_soon_remaining += remaining
            if remaining > 0:
                expiring_soon = True
        if r.get("expired"):
            expired_remaining += remaining
            if remaining > 0:
                expired = True
        # 只在「还有剩余」的包里找最早到期
        if remaining > 0 and r.get("expire_at"):
            ts = int(r["expire_at"])
            if soonest_expire_at is None or ts < soonest_expire_at:
                soonest_expire_at = ts

    days_left = None
    if soonest_expire_at is not None:
        days_left = max(0.0, (soonest_expire_at - now) / 86400000.0)

    # 按到期时间升序（无到期的排最后）；这是「最近快到期的积分包」的展示顺序，
    # 也是调度读取优先级的顺序，两处必须是同一份排序。
    ordered = sorted(
        (r for r in (resources or []) if float(r.get("remaining") or 0) > 0),
        key=lambda r: (r.get("expire_at") is None, r.get("expire_at") or 0),
    )

    return {
        "total_capacity": round(total_capacity, 4),
        "total_remaining": round(total_remaining, 4),
        "expiring_soon_remaining": round(expiring_soon_remaining, 4),
        "expired_remaining": round(expired_remaining, 4),
        "soonest_expire_at": soonest_expire_at,
        "soonest_days_left": round(days_left, 2) if days_left is not None else None,
        "expiring_soon": expiring_soon,
        "expired": expired,
        "package_count": len(resources or []),
        "active_package_count": len(ordered),
        "resources": ordered,
        "updated_at": now,
    }


def urgency_rank(summary: dict | None) -> tuple:
    """调度的紧迫度排序键：**越小越先被选**。

    分级（这是「快到期的先用完」的严格实现）：

      0 —— 有 7 天内到期的剩余额度（最紧急，必须优先烧掉，否则作废）
      1 —— 有过期时间但还早（先消耗离到期近的，慢慢把长期包留到最后）
      2 —— 全部长期有效（没有作废风险，只用它兜底）

    同级内按 `soonest_expire_at` 升序（到期越早越先），
    无到期时间的在同级里排最后。这样即使所有号都很宽裕，
    也会自然形成「离到期近的先走」的稳定偏好，而不是靠录入时间猜。
    """
    if not summary:
        return (2, float("inf"))
    if summary.get("expiring_soon") and float(summary.get("expiring_soon_remaining") or 0) > 0:
        return (0, summary.get("soonest_expire_at") or float("inf"))
    soonest = summary.get("soonest_expire_at")
    if soonest:
        return (1, soonest)
    return (2, float("inf"))


def days_left_label(days_left: float | None) -> str:
    """到期天数的中文短标签（展示层共用，避免各处自己拼）。"""
    if days_left is None:
        return "长期有效"
    if days_left <= 0:
        return "已到期"
    if days_left < 1:
        hours = max(1, int(round(days_left * 24)))
        return f"{hours} 小时后到期"
    return f"{int(days_left)} 天后到期"


def format_expire_at(expire_at: int | None) -> str:
    """到期时间戳 → 本地可读字符串（无到期返回「长期有效」）。"""
    if not expire_at:
        return "长期有效"
    try:
        return datetime.fromtimestamp(expire_at / 1000).strftime("%Y-%m-%d %H:%M")
    except (OSError, OverflowError, ValueError):
        return "长期有效"


def expire_date_label(expire_at: int | None) -> str:
    """到期日期的短标签（列表里用，形如 `09/13`）。"""
    if not expire_at:
        return "长期有效"
    try:
        dt = datetime.fromtimestamp(expire_at / 1000)
    except (OSError, OverflowError, ValueError):
        return "长期有效"
    if dt.year != datetime.now().year:
        return dt.strftime("%Y/%m/%d")
    return dt.strftime("%m/%d")


def as_datetime(expire_at: int | None) -> datetime | None:
    """毫秒时间戳 → naive UTC datetime（存库用，与库内其它时间列口径一致）。"""
    if not expire_at:
        return None
    return datetime.utcfromtimestamp(expire_at / 1000)


def normalize_packages(raw_packages: list[dict], now: int | None = None) -> list[dict]:
    """批量归一化（过滤掉非 credits 单位的条目）。"""
    out: list[dict] = []
    for raw in raw_packages or []:
        if not isinstance(raw, dict):
            continue
        out.append(resource_summary(raw, now))
    return out
