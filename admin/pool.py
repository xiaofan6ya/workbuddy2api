"""账号池运行态与流量治理原语（纯内存、线程安全、不碰数据库）。

为什么单独成模块
----------------
原实现把「防撞号窗口」放在 `accounts.last_picked_at` 上，**每次选号都要 commit 一次
数据库**；并发一高，选号本身就变成瓶颈，且用户/后台的任何写事务都会与之争锁。
本模块把这类「只在一瞬间有意义」的状态挪进进程内存，数据库只保留需要跨重启的
结果状态（冷却截止、连续失败计数、禁用）。

职责边界
--------
本模块负责：
  * 在途租约（单账号并发闸门，占满的号不参与选号）
  * 会话粘性路由（同一会话固定同一账号，TTL 滚动续期）
  * 防撞号窗口（同一账号在极短时间内不重复被选中）
  * WAF IP 级 fail-fast 闸门（多账号同时 403 = 出口 IP 被拦，继续轮转只会加重风控）
  * 轮转退避（指数 + 抖动）
  * 三因子加权随机选号（余额 / 快过期 / 闲置）
  * 会话键与轮级键提取

数据库（`accounts` 表）负责：冷却截止时间、熔断截止、连续失败计数、禁用状态。

借鉴来源（E:\\反代理\\workbuddy\\workbuddy2api-master）
-----------------------------------------------------
  internal/pool/pool.go       Acquire / Release 在途租约（CAS 计数 + 上限拒绝）
  internal/pool/pick.go       top5 短名单 + 三因子加权抽签 + 等权重洗牌 + 防撞号
  internal/server/backoff.go  500ms·2^n 封顶 8s 再 ±25% 抖动
  internal/server/wafip.go    60s 滑窗内不同账号命中 WAF 403 达阈值 → IP 级 fail-fast
  internal/session/session.go 粘性路由：快路径命中 / 失效重分配 / TTL / 绑定跟随成功号
  internal/session/ids.go     会话键与对话轮级键的提取口径
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import random
import threading
import time
from datetime import datetime, timezone
from typing import Any, Iterable

# ---------------------------------------------------------------------------
# 轮转退避：指数 + 抖动
# ---------------------------------------------------------------------------
#: 轮转退避基数（毫秒）。对齐官方 CLI 的 500ms 形态。
ROTATE_BACKOFF_BASE_MS = 500.0
#: 轮转退避封顶（毫秒）。轮转上限默认 3 次，实际等待序列 500ms / 1s。
ROTATE_BACKOFF_CAP_MS = 8000.0
#: 抖动比例（±25%）。目的是打散多请求同相位重试 —— WAF 频控按密度判罚，
#: 齐步走的退避会以固定周期再次聚团。
JITTER_FRACTION = 0.25

#: 防撞号窗口（秒）：同一账号在该窗口内不重复被选中。
MIN_PICK_GAP_S = 0.1
#: 短名单大小：先按权重取前 N 名，再在名单内加权抽签（避免单号垄断流量）。
TOP_N = 5

#: 权重参数（对齐 master 的 weightOf）。
CREDITS_WEIGHT = 10.0        # 余额占比权重
EXPIRING_WEIGHT = 8.0        # 快过期积分占比权重
IDLE_WEIGHT_PER_HOUR = 0.5   # 闲置补偿：每小时未使用 +0.5
IDLE_WEIGHT_MAX = 5.0        # 闲置补偿封顶
#: 等权重判定容差：权重公式微调后不再位级相等，精确相等比较会让洗牌静默失效。
WEIGHT_EPSILON = 1e-9

#: WAF IP 级判定的滑动窗与激活时长（秒）。
WAF_IP_WINDOW_S = 60.0
#: 判定阈值：窗内「不同账号」命中 WAF 403 达到该数即判 IP 级拦截。
#: 取 2 —— 单号反复 403 归账号级软冷却管；两个不同号在 60s 内接连被拦已是 IP 级证据。
WAF_IP_THRESHOLD = 2


def jitter_ms(d: float, rand: random.Random | None = None) -> float:
    """给时长施加 ±JITTER_FRACTION 的均匀抖动；d<=0 原样返回（零等待不抖动）。"""
    if d <= 0:
        return 0.0
    r = rand or random
    factor = 1.0 + (r.random() * 2 - 1) * JITTER_FRACTION
    return max(0.0, d * factor)


def backoff_after_ms(n: int, rand: random.Random | None = None) -> float:
    """返回第 n 次轮转（0 基）前应等待的退避毫秒：base·2^n 封顶，再抖动。"""
    d = ROTATE_BACKOFF_BASE_MS
    if d <= 0:
        return 0.0
    for _ in range(max(0, n)):
        if d >= ROTATE_BACKOFF_CAP_MS:
            break
        d *= 2
    if d > ROTATE_BACKOFF_CAP_MS:
        d = ROTATE_BACKOFF_CAP_MS
    return jitter_ms(d, rand)


async def rotate_backoff_async(n: int) -> bool:
    """第 n 次轮转前等待退避。客户端断开时 asyncio.sleep 抛 CancelledError，
    由调用方的流式生成器自然终止（等价于 master 的 sleepCtx 返回 false）。"""
    d = backoff_after_ms(n) / 1000.0
    if d > 0:
        await asyncio.sleep(d)
    return True


def _now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class RetryBudget:
    """Bound new attempts across models, without imposing a stream lifetime cap."""

    def __init__(self, attempts: int, window_seconds: float) -> None:
        self.limit = max(1, attempts)
        self.window = max(1.0, window_seconds)
        self.started = time.monotonic()
        self.used = 0

    def available(self) -> bool:
        return self.used < self.limit and time.monotonic() - self.started < self.window

    def consume(self) -> None:
        self.used += 1


def _as_utc(dt: datetime | None) -> datetime | None:
    """把可能带时区/不带的 datetime 统一成 naive UTC，便于比较。"""
    if dt is None:
        return None
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


# ---------------------------------------------------------------------------
# 在途租约
# ---------------------------------------------------------------------------
class InFlight:
    """单账号在途请求计数：占满的账号不参与选号。

    为什么需要：粘性路由会把同一会话的请求都导向同一账号，加权选号也会倾向高余额
    账号 —— 两者叠加会让个别账号同时扛住大量长连接。上游按账号限速时，这种单号
    过载会直接表现为成片的 429/5xx，然后这些号被冷却，流量又整体挤到下几个号，
    形成雪崩。在途上限把并发摊平到整个池子上。

    上限 <=0 表示不限：计数仍然累加（供观测），但永不拒绝。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counts: dict[str, int] = {}

    def acquire(self, uid: str, limit: int) -> bool:
        if not uid:
            return True
        with self._lock:
            cur = self._counts.get(uid, 0)
            if limit > 0 and cur >= limit:
                return False
            self._counts[uid] = cur + 1
            return True

    def release(self, uid: str) -> None:
        """释放一个名额；幂等减到 0（防重复释放扣成负数）。"""
        if not uid:
            return
        with self._lock:
            cur = self._counts.get(uid, 0)
            if cur <= 1:
                self._counts.pop(uid, None)
            else:
                self._counts[uid] = cur - 1

    def count(self, uid: str) -> int:
        with self._lock:
            return self._counts.get(uid, 0)

    def full(self, uid: str, limit: int) -> bool:
        if limit <= 0 or not uid:
            return False
        with self._lock:
            return self._counts.get(uid, 0) >= limit

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return dict(self._counts)


# ---------------------------------------------------------------------------
# 防撞号窗口
# ---------------------------------------------------------------------------
class RecentPick:
    """记录每个账号最近一次被选中的时刻，用于「N 毫秒内不重复选中」。

    放在内存而不是数据库：这个窗口只有 100ms 量级，落库既慢又没意义
    （原实现每次选号 commit 一次，高并发下选号本身成了瓶颈）。
    """

    def __init__(self, gap_s: float = MIN_PICK_GAP_S) -> None:
        self._lock = threading.Lock()
        self._last: dict[str, float] = {}
        self._gap = gap_s

    def mark(self, uid: str) -> None:
        if not uid:
            return
        with self._lock:
            self._last[uid] = time.monotonic()

    def fresh(self, uid: str) -> bool:
        """该账号距上次选中是否已超过防撞号窗口。"""
        if not uid or self._gap <= 0:
            return True
        with self._lock:
            t = self._last.get(uid)
        if t is None:
            return True
        return (time.monotonic() - t) >= self._gap

    def prune(self, keep: int = 512) -> None:
        """防止长期运行后字典无限增长（只保留最近的若干条）。"""
        with self._lock:
            if len(self._last) <= keep:
                return
            items = sorted(self._last.items(), key=lambda kv: kv[1], reverse=True)
            self._last = dict(items[:keep])


# ---------------------------------------------------------------------------
# WAF IP 级闸门
# ---------------------------------------------------------------------------
class WafIpGate:
    """WAF 403 的 IP 级 fail-fast 状态机。

    背景：WAF 403 拦的是**网关出口 IP** 而非账号 —— 实测 xx 个账号 1 秒内全 403。
    账号级软冷却在这种场景下不够：轮转会把一次客户端请求放大 MaxRotate 倍，
    同一出口 IP 继续打上游只会加重风控。

    判定：窗口内「不同账号」命中 WAF 403 达到阈值即判定 IP 级拦截，激活一个窗口。
    激活期内新命中不续期（保守：不做主动探测，窗口自然解除）。
    单号反复 403 永不触发 —— 只数不同账号。
    """

    def __init__(self, window_s: float = WAF_IP_WINDOW_S,
                 threshold: int = WAF_IP_THRESHOLD) -> None:
        self._lock = threading.Lock()
        self._hits: dict[str, float] = {}
        self._until = 0.0
        self._window = window_s
        self._threshold = threshold

    def note(self, uid: str) -> bool:
        """记一次某账号的 WAF 403；返回记账后 IP 级拦截是否激活。"""
        now = time.monotonic()
        with self._lock:
            if now < self._until:
                return True  # 激活期内：不续期、不记账，自然解除
            self._hits[uid or "-"] = now
            self._hits = {u: t for u, t in self._hits.items()
                          if now - t <= self._window}
            if len(self._hits) >= self._threshold:
                self._until = now + self._window
                self._hits.clear()
                return True
            return False

    def active(self) -> bool:
        with self._lock:
            return time.monotonic() < self._until

    def remaining(self) -> int:
        with self._lock:
            return max(0, int(self._until - time.monotonic()))

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()
            self._until = 0.0


# ---------------------------------------------------------------------------
# 会话粘性路由
# ---------------------------------------------------------------------------
class StickyRouter:
    """同一会话（conversationId / metadata 键）尽量绑定同一账号。

    为什么重要（两个收益）：
      1. **上游前缀缓存不碎** —— 换号等于换一份服务端上下文缓存，多轮对话每次都
         重新计费、也更慢；
      2. **拟人** —— 真实用户的一次会话属于同一台设备/同一个账号；一次会话在号池里
         逐轮跳号，是很容易被识别的批量特征。

    实现要点（对齐 master 的 session.Router）：
      * 命中走快路径（轻量锁），未命中/失效才进入重分配；
      * 分配只认「当前可用」的账号，账号被限流/占满会自动重分配；
      * TTL 滚动续期，空闲即过期释放（纯内存，重启后自然重建）；
      * 请求成功后把会话**重绑到实际成功的账号**，让多轮收敛到稳定号。
    """

    def __init__(self, ttl_s: float = 1800.0, gc_interval_s: float = 300.0,
                 enabled: bool = True) -> None:
        self._lock = threading.Lock()
        self._bind: dict[str, tuple[str, float]] = {}  # key -> (uid, last_active)
        self._ttl = ttl_s
        self._gc = gc_interval_s
        self._enabled = enabled
        self._stop = False
        self._thread: threading.Thread | None = None

    # -- 生命周期 ---------------------------------------------------------
    def start_gc(self) -> None:
        """启动后台 GC（幂等）。进程退出时线程是 daemon，无需显式停止。"""
        if not self._enabled or self._thread is not None:
            return
        t = threading.Thread(target=self._gc_loop, daemon=True, name="wb-sticky-gc")
        self._thread = t
        t.start()

    def _gc_loop(self) -> None:
        while not self._stop:
            time.sleep(max(5.0, self._gc))
            try:
                self.gc_once()
            except Exception:
                pass

    def gc_once(self) -> int:
        now = time.monotonic()
        with self._lock:
            dead = [k for k, (_, t) in self._bind.items() if now - t > self._ttl]
            for k in dead:
                self._bind.pop(k, None)
            return len(dead)

    # -- 读写 -------------------------------------------------------------
    def resolve(self, key: str, available: set[str]) -> str | None:
        """返回该会话当前绑定的账号 uid；无效/已不可用则返回 None（调用方重分配）。"""
        if not self._enabled or not key:
            return None
        now = time.monotonic()
        with self._lock:
            ent = self._bind.get(key)
            if ent is None:
                return None
            uid, last = ent
            if now - last > self._ttl:
                self._bind.pop(key, None)
                return None
            if available and uid not in available:
                # 绑定号已冷却/占满/被禁用 → 解绑重分配
                self._bind.pop(key, None)
                return None
            self._bind[key] = (uid, now)  # 滚动续期
            return uid

    def bind(self, key: str, uid: str) -> None:
        """显式绑定（幂等覆盖）。供「粘性跟随最终成功号」使用。"""
        if not self._enabled or not key or not uid:
            return
        with self._lock:
            self._bind[key] = (uid, time.monotonic())

    def unbind(self, key: str) -> bool:
        """解除绑定（请求失败时调用，让该会话下次重新分配）。"""
        if not key:
            return False
        with self._lock:
            return self._bind.pop(key, None) is not None

    def count(self) -> int:
        with self._lock:
            return len(self._bind)

    def snapshot(self) -> dict[str, str]:
        with self._lock:
            return {k: v[0] for k, v in self._bind.items()}


# ---------------------------------------------------------------------------
# 三因子加权随机选号
# ---------------------------------------------------------------------------
def _weight(credits: int, expiring: int, max_credits: int,
            last_used: datetime | None, now: datetime) -> float:
    """单个候选的三因子权重（credits 比例 ×10 + 快过期占比 ×8 + 闲置补偿）。"""
    w = 1.0
    # 1. 余额占比。max_credits>0 才计入：全员 0 余额时该项无区分度。
    if max_credits > 0:
        w += float(max(0, credits)) / float(max_credits) * CREDITS_WEIGHT
    # 2. 快过期积分加成：官方赠送的奖励积分按批过期，不用就作废，
    #    所以「快过期占比」是一个独立的强权重项（与余额多少正交）。
    if credits > 0 and expiring > 0:
        w += float(min(expiring, credits)) / float(credits) * EXPIRING_WEIGHT
    # 3. 闲置补偿：久未使用的号补位，避免高分号永久垄断流量。
    lu = _as_utc(last_used)
    if lu is None:
        w += IDLE_WEIGHT_MAX  # 从未使用 → 满分
    else:
        hours = max(0.0, (now - lu).total_seconds() / 3600.0)
        w += min(hours * IDLE_WEIGHT_PER_HOUR, IDLE_WEIGHT_MAX)
    return w


# ---------------------------------------------------------------------------
# 到期紧迫度：所有策略的**首要**排序依据
# ---------------------------------------------------------------------------
#: 「快到期的先用完」的紧迫度分级。数值越小越先被选中。
#:
#: 为什么把它做成独立于策略的**首要**键，而不是只塞进 weighted 的权重里：
#: 权重是概率倾斜 —— 一个 7 天内到期的号只是「更容易」被选中，而不是
#: 「一定先被选中」。只要池子里还有余额更高或更闲置的号，抽签就可能
#: 一直不选它，直到那批额度作废。用户的原话是「还是优先把快到期的账号
#: 积分先用完才行」—— 这要求的是**严格优先**，不是倾向。
#:
#: 分级定义（与 admin/credits.urgency_rank 保持同一口径）：
#:   0 = 有 7 天内到期的剩余额度（再不烧就作废）
#:   1 = 有过期时间但还早（自然形成「离到期近的先走」）
#:   2 = 全部长期有效（没有作废风险，只用它兜底）
EXPIRY_RANK_SOON = 0
EXPIRY_RANK_DATED = 1
EXPIRY_RANK_EVERGREEN = 2

#: 判定「快过期」的窗口（天）。与 admin.credits.EXPIRING_SOON_DAYS 同值，
#: 官方套餐页按 7 天把额度标成橙色。这里不 import credits 是为了让 pool
#: 保持「纯内存、零依赖」的定位（它被高频路径调用，不该牵出 ORM/网络层）。
EXPIRING_SOON_DAYS = 7


def expiry_key(account: Any, now: datetime | None = None) -> tuple:
    """账号的到期紧迫度排序键：**越小越先被使用**。

    读取 `credits_expiring_soon` / `credits_soonest_expire_at` 两个列
    （由 `refresh_credits` 定时任务写入，见 admin/credits.py）。

    缺数据时的兜底：老库升级后这两个列可能还是空的。此时
    `credits_expiring_soon` 为 0 且 `credits_soonest_expire_at` 为 None，
    会被判成「长期有效」（rank 2）—— 排在最后。这是**刻意**的选择：
    把未知当成「没有作废风险」，最坏只是晚一点用它，
    而把未知当成「快过期了」会让调度去烧一个可能根本不紧急的号，
    反而耽误真正快过期的额度。
    """
    now = now or _now_utc()
    expiring_soon = int(getattr(account, "credits_expiring_soon", 0) or 0)
    if expiring_soon > 0:
        soonest = _as_utc(getattr(account, "credits_soonest_expire_at", None))
        ts = soonest.timestamp() if soonest is not None else float("inf")
        return (EXPIRY_RANK_SOON, ts)

    soonest = _as_utc(getattr(account, "credits_soonest_expire_at", None))
    if soonest is not None:
        return (EXPIRY_RANK_DATED, soonest.timestamp())
    return (EXPIRY_RANK_EVERGREEN, float("inf"))


def sort_by_expiry(cands: list[Any], *, now: datetime | None = None,
                   secondary=None) -> list[Any]:
    """按「到期紧迫度优先」排序候选账号，同级内用 `secondary` 决出先后。

    `secondary` 是 `key(account) -> tuple`；缺省时同级内按余额降序
    （余额多先走，让单个号的额度更可能被用干净）。
    """
    now = now or _now_utc()
    if secondary is None:
        def secondary(a):
            return (-int(getattr(a, "balance_remain", 0) or 0),)
    return sorted(cands, key=lambda a: (expiry_key(a, now), secondary(a)))


def weighted_pick(cands: list[dict], *, rand: random.Random | None = None,
                  now: datetime | None = None) -> dict | None:
    """三因子加权随机选号：先取 Top-N 短名单，再在名单内加权抽签。

    每个候选是 dict，需含：
        uid / credits / credits_expiring / last_used_at
        可选 expiry_rank（最小到期紧迫度键）—— 见 `pick_with_expiry_priority`

    为什么是「Top-N + 抽签」而不是直接按权重排序取第一：纯排序会让余额最高的号
    承担几乎全部流量（热点），且余额一旦回落就骤停；抽签是**概率倾斜**而非硬排序，
    能把流量摊开，同时保持「余额多/快过期/闲置久」的倾向。

    Top-N 截断的等权重陷阱：权重全等时按 uid 字典序截断，会让排序靠后的账号
    永远进不了短名单（实测出现过某号占 79/100 的惊群）。这里在检出并列时对
    候选做一次洗牌（用独立随机源，不影响注入源的确定性语义）。
    """
    if not cands:
        return None
    now = now or _now_utc()
    max_credits = 0
    for c in cands:
        max_credits = max(max_credits, int(c.get("credits") or 0))

    weighted = [
        (c, _weight(int(c.get("credits") or 0), int(c.get("credits_expiring") or 0),
                    max_credits, c.get("last_used_at"), now))
        for c in cands
    ]
    weighted.sort(key=lambda p: p[1], reverse=True)

    if len(weighted) > TOP_N:
        # 等权重洗牌：仅在存在并列时做，避免字典序截断造成饿死。
        eq = any(abs(w - weighted[0][1]) < WEIGHT_EPSILON for _, w in weighted[1:])
        if eq:
            shuf = random.Random(time.monotonic_ns())
            shuf.shuffle(weighted)
            weighted.sort(key=lambda p: p[1], reverse=True)
        weighted = weighted[:TOP_N]

    # 短名单内加权抽签
    total = 0.0
    for _, w in weighted:
        total += max(0.0, w)
    if total <= 0:
        return weighted[0][0]
    r = (rand or random).random() * total
    acc = 0.0
    for c, w in weighted:
        acc += max(0.0, w)
        if r < acc:
            return c
    return weighted[-1][0]


def pick_with_expiry_priority(cands: list[dict], *,
                              rand: random.Random | None = None,
                              now: datetime | None = None) -> dict | None:
    """先按到期紧迫度严格分级，再在**同级内**做三因子加权抽签。

    这是 `weighted` 策略的正确形态。原实现只在权重里给快过期积分加 8 分，
    属于概率倾斜：池子里只要还有余额更高/更闲置的号，抽签就可能一直不选
    那个快过期的号，直到额度作废。这里把「紧迫度」提成硬性分组 ——
    只要存在 7 天内到期的号，就在这批号里抽签，绝不会退回到其他号，
    因此「先用快过期的」从倾向变成了保证。

    每个候选 dict 需额外含 `expiry_rank`（`expiry_key(...)` 的结果）。
    缺省时全部判为「长期有效」，退化成原来的纯权重行为（向后兼容）。
    """
    if not cands:
        return None
    now = now or _now_utc()
    best_rank = min(_rank_of(c) for c in cands)
    tier = [c for c in cands if _rank_of(c) == best_rank]
    return weighted_pick(tier, rand=rand, now=now)


def _rank_of(cand: dict) -> tuple:
    """候选的紧迫度键；缺省视为「长期有效」（排在最后）。"""
    key = cand.get("expiry_rank")
    if isinstance(key, tuple) and key:
        return key
    return (EXPIRY_RANK_EVERGREEN, float("inf"))


# ---------------------------------------------------------------------------
# 会话键提取
# ---------------------------------------------------------------------------
def _text_of(content: Any) -> str:
    """把消息 content 归约成可签名的文本（纯文本 / parts 数组 / 其它）。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                t = item.get("text")
                if isinstance(t, str):
                    parts.append(t)
                elif item.get("type") == "image_url":
                    # 图片轮也要能签名，否则首图会话出现粘性盲区
                    parts.append("[image]")
        return "".join(parts)
    return ""


def extract_session_key(body: dict) -> str:
    """从请求体提取会话级粘性键；找不到返回空串（绝不失败）。

    优先级（对齐 master session.ExtractKey）：
      1. metadata.conversation_id
      2. metadata.conversationId
      3. conversation_id
      4. conversationId
      5. prompt_cache_key（pi-ai 系客户端把会话 ID 放在这个 OpenAI 前缀缓存字段里）

    `metadata.user_id` **有意不作为粘性键**：它的粒度太粗 —— 一个用户的所有并行
    对话会被钉到同一个账号上，远粗于上游「对话级」的缓存边界，既浪费粘性收益
    又把单号压垮。
    """
    if not isinstance(body, dict):
        return ""
    meta = body.get("metadata")
    if isinstance(meta, dict):
        for k in ("conversation_id", "conversationId"):
            v = meta.get(k)
            if isinstance(v, str) and v:
                return v
    for k in ("conversation_id", "conversationId"):
        v = body.get(k)
        if isinstance(v, str) and v:
            return v
    pck = body.get("prompt_cache_key")
    if isinstance(pck, str) and pck:
        return pck
    return ""


def fallback_session_key(body: dict) -> str:
    """无会话标识的客户端（OpenAI 兼容协议）派生一个会话级稳定键。

    OpenAI 协议的请求体里既没有 conversationId 也没有 metadata，`extract_session_key`
    恒返回空串 —— 于是同一段连续对话在号池里逐请求换号，上游前缀缓存被打散、
    费用上升，多轮上下文也更慢。这里用**首条 user 消息文本的 sha256** 兜底：
    会话内历史不断追加，但首条 user 消息恒定 → 同会话恒同键；用户开新会话
    （首条消息不同）→ 自然换键。

    抑制条件：请求体带 `metadata.user_id` 或顶层 `user_id` 时**恒返回空串**。
    这类客户端已显式声明了用户维度，若再借首条 prompt 获得粘性，会把该用户的
    所有并行对话钉到同一账号 —— 正是上面要避免的过粗粒度。
    """
    if not isinstance(body, dict):
        return ""
    if _has_user_id(body):
        return ""
    msgs = body.get("messages")
    if not isinstance(msgs, list):
        return ""
    first = ""
    for m in msgs:
        if isinstance(m, dict) and m.get("role") == "user":
            first = _text_of(m.get("content")).strip()
            break
    if not first:
        return ""
    digest = hashlib.sha256(first.encode("utf-8", "replace")).hexdigest()
    return "d-" + digest[:16]


def turn_key(body: dict) -> str:
    """派生「对话轮级」键：最后一条 user 消息的序号 + 文本摘要。

    用于向上游声明「本轮请求」的聚合身份：一次用户发送内的所有上游调用
    （tool call 多轮 / 换号重试 / 降级重发）body 里最后一条 user 消息恒定 → 同键；
    用户发下一条消息 → 换键。取**最后一条**而不是第一条，才对齐「对话轮」语义
    （取第一条会把整段会话的所有轮并进同一个键）。
    """
    if not isinstance(body, dict):
        return ""
    msgs = body.get("messages")
    if not isinstance(msgs, list):
        return ""
    for i in range(len(msgs) - 1, -1, -1):
        m = msgs[i]
        if not isinstance(m, dict) or m.get("role") != "user":
            continue
        sig = _text_of(m.get("content"))
        if not sig:
            return ""  # 该消息无可签名内容：本轮不建立聚合键，不往前找
        digest = hashlib.sha256(sig.encode("utf-8", "replace")).hexdigest()
        return f"u{i}:{digest[:16]}"
    return ""


def session_key_for(body: dict) -> str:
    """会话级键：显式会话标识优先，其次首条 user 文本兜底。"""
    return extract_session_key(body) or fallback_session_key(body)


def _has_user_id(body: dict) -> bool:
    """body 是否携带用户维度标识（metadata.user_id 或顶层 user_id）。"""
    meta = body.get("metadata")
    if isinstance(meta, dict):
        v = meta.get("user_id")
        if isinstance(v, str) and v:
            return True
    v = body.get("user_id")
    return isinstance(v, str) and bool(v)


# ---------------------------------------------------------------------------
# 进程级单例
# ---------------------------------------------------------------------------
class PoolRuntime:
    """把上面几个运行态原语收拢成一个单例，供路由层统一取用。"""

    def __init__(self) -> None:
        self.inflight = InFlight()
        self.recent = RecentPick()
        self.waf = WafIpGate()
        self.sticky = StickyRouter()

    def configure(self, *, max_in_flight: int, sticky_enabled: bool,
                  sticky_ttl_s: float, sticky_gc_s: float) -> None:
        """按配置装配（启动时调用一次）。"""
        self.max_in_flight = max_in_flight
        self.sticky = StickyRouter(ttl_s=sticky_ttl_s, gc_interval_s=sticky_gc_s,
                                   enabled=sticky_enabled)
        if sticky_enabled:
            self.sticky.start_gc()

    max_in_flight: int = 0

    def available(self, accounts: Iterable[Any]) -> list[Any]:
        """过滤掉在途占满的账号（在途上限 <=0 时不过滤）。"""
        limit = getattr(self, "max_in_flight", 0) or 0
        if limit <= 0:
            return list(accounts)
        return [a for a in accounts if not self.inflight.full(getattr(a, "uid", "") or "", limit)]

    def acquire(self, uid: str) -> bool:
        return self.inflight.acquire(uid or "", getattr(self, "max_in_flight", 0) or 0)

    def release(self, uid: str) -> None:
        self.inflight.release(uid or "")

    def status(self) -> dict:
        return {
            "inflight": self.inflight.snapshot(),
            "sticky_bindings": self.sticky.count(),
            "waf_ip_blocked": self.waf.active(),
            "waf_ip_remaining_s": self.waf.remaining(),
        }


POOL = PoolRuntime()


def _json_probe(body_bytes: bytes) -> dict:
    """把原始请求体解析成 dict；失败返回空 dict（会话键提取绝不抛异常）。"""
    if not body_bytes:
        return {}
    try:
        obj = json.loads(body_bytes)
    except Exception:
        return {}
    return obj if isinstance(obj, dict) else {}
