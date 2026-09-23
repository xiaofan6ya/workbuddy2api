"""代理网关：带 API Key 的 /v1/chat/completions 与 /v1/models。

流程：校验 Key → 配额拦截（超额提示『积分已耗尽』）→ 从可用账号中挑选 →
用该账号凭据转发到后端 → 流式返回 → 按用量回扣 Key 额度。
"""
import hashlib
import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone

import httpx
from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy import or_
from sqlalchemy.orm import Session

from admin import backend, pool
from admin import client_profile as cprofile
from admin.config import settings
from admin.db import SessionLocal, get_db
from admin.models import Account, AccountModelCooldown, ApiKey, ModelConfig, UsageLog
from admin.routers.models import _is_model_allowed
from admin.security import check_quota, get_key_row
from admin.pool import POOL

HTTP_LIMITS = backend.HTTP_LIMITS


def _stream_timeout() -> httpx.Timeout:
    """流式请求的超时：不设总时长上限，只分别约束「连接 / 静默 / 上传 / 取连接」。

    先澄清一个容易搞错的点：`httpx.AsyncClient(timeout=300)` **不是**总时长 5 分钟，
    它把 connect/read/write/pool 四项**各**设为 300s。所以原代码并不会在流式
    响应中途掐断活跃的流（read 是「两次读到数据之间」的间隔上限，有数据就重置）。

    但它确实有两个真实缺陷，两个都在「该快的时候不快」这一侧：

    1. **connect=300**：一个 TCP 连不上的账号会先让我们干等最多 5 分钟，才轮到
       换号逻辑。这才是用户感知到的「卡住不动」的主因 —— 而号池里恰恰总有
       若干连不上的号（被墙/被限速/节点故障）。连接超时必须短，让它快速轮转。

    2. **pool=300**：连接池打满时，新请求在池外排队最多 5 分钟。池满本身就是
       过载信号，应当快速失败并把压力交回上层（退避/换号），而不是无限排队
       —— 排队只会把延迟一层层叠起来，最后整池一起超时。

    另外还有一处**要主动避免的坑**，依据来自官方客户端自己的源码：

      D:\\WorkBuddy\\resources\\app.asar.unpacked\\...\\ardot-mcp-app\\
      _workbuddy-runtime\\mcp-app-bootstrap.cjs

    官方 MCP 运行时在 Node 18+ 下把 `http.Server.requestTimeout` 锁到 0（三重
    防御补丁），因为 Node 默认的 300000ms 是**总时长**，会在流仍然活跃的情况下
    到点强行掐断长连接 SSE，客户端只看到 undici `TypeError: terminated` /
    "SSE stream disconnected"。官方把它当必须修的 bug，修法就是「总时长 = 0」。

    教训迁移到我们这边：**永远不要给流式响应加总时长上限**。所以这里显式拆开
    四项，只让 read 承担「静默多久算死」的职责，不给整个响应设 deadline。

    这里的 read 实际就等价于参考实现 internal/upstream/idle.go 的空闲监控
    （有数据就续期、静默即取消），而且不需要额外起监控任务。因此**非流式端点
    也用这一套**：上游一律以 `stream: true` 返回 SSE，我们在内部聚合，
    用静默上限而不是总时长，长推理才不会被自己掐断。
    """
    return httpx.Timeout(
        connect=settings.STREAM_CONNECT_TIMEOUT,
        read=settings.STREAM_IDLE_TIMEOUT,
        write=settings.STREAM_WRITE_TIMEOUT,
        pool=settings.STREAM_POOL_TIMEOUT,
    )

_logger = logging.getLogger("proxy")

# Responses API 适配器（converter 同款）；缺失时 /v1/responses 优雅降级为 501
try:
    from responses_adapter import responses_request_to_chat, ResponsesStreamConverter
    from responses_projection import project_responses_chat_body
    _RESPONSES_AVAILABLE = True
except Exception:  # pragma: no cover - 降级分支
    _RESPONSES_AVAILABLE = False
    responses_request_to_chat = None
    ResponsesStreamConverter = None
    project_responses_chat_body = None

# Anthropic Messages 适配器（与 converter 同款，复用同一套双向转换）；
# 缺失时 /v1/messages 优雅降级为 501，不影响其余端点。
try:
    from anthropic_adapter import AnthropicStreamConverter, anthropic_request_to_chat
    _ANTHROPIC_AVAILABLE = True
except Exception:  # pragma: no cover - 降级分支
    _ANTHROPIC_AVAILABLE = False
    anthropic_request_to_chat = None
    AnthropicStreamConverter = None

# harness 脱敏（与 converter 的 /gw 端点同款）。Claude Code 的 system prompt / tools
# 里含 "DoS / exploit / credential" 这类合规声明词，会被上游内容审核误判并整条拒绝
# （典型报错 400 code=11128 "Illegal API invocation from an unapproved channel"）。
try:
    from desensitize import desensitize_body
    _DESENSITIZE_AVAILABLE = True
except Exception:  # pragma: no cover - 降级分支
    _DESENSITIZE_AVAILABLE = False
    desensitize_body = None

router = APIRouter(tags=["proxy"])


def _client_ip(request: Request) -> str:
    """提取发起请求的真实客户端 IP。

    经反向代理部署时，上游往往会带上 X-Forwarded-For / X-Real-IP；
    取 XFF 首个（最原始客户端），否则 X-Real-IP，最后退回直连 socket 地址。
    这是「记录原客户端用户真实 IP」的关键，便于风控对账与上游用途日志对齐。
    """
    xff = request.headers.get("X-Forwarded-For")
    if xff:
        return xff.split(",")[0].strip()
    real = request.headers.get("X-Real-IP")
    if real:
        return real.strip()
    return request.client.host if request.client else ""


def _upstream_extra_headers(request: Request, purpose: str = "conversation",
                            body: dict | None = None,
                            account: "Account | None" = None) -> dict:
    """构造需要透传给上游的风控/审计/指纹头。

    分五组，各有明确依据（对齐参考实现与官方客户端实测形状）：

    1. **客户端 IP**（X-Forwarded-For / X-Real-IP / X-Client-IP）
       让上游请求用量里的「客户端」列显示真实来源，而不是反代服务器 IP。
       若 ADMIN_UPSTREAM_CLIENT_HEADER 指定了自定义 header 名，则额外发送该头。

    2. **用量归属**（X-Agent-Purpose / X-IDE-Name / X-IDE-Type / X-Product / X-IDE-Version）
       上游按这组头把请求归到「使用端」列。缺失时该列为空 —— 一个 client 为空的
       请求在用量统计里就是明显的网关特征。真实桌面端发 WorkBuddy，所以默认带。

    3. **官方客户端风控闸门**（X-CodeBuddy-Request / Accept-Language / X-Requested-With）
       X-CodeBuddy-Request: 1 是官方客户端所有 API 请求必带的闸门头（源自 master
       headers.go:164 的 D1 结论）。Accept-Language 按账号域下发（CN → zh-CN），
       缺失会被上游按语言异常误判。

    4. **账号级稳定设备标识**（X-Machine-ID / X-Session-ID）
       由 uid 稳定派生（跨重启恒定、账号间互异）。语义是「每个账号一台固定虚拟
       设备」：每次随机 = 频繁换设备 = 明显异常；所有账号共用一个常量 =
       多号同设备 = 最容易被批量识别的特征。两者都错，所以必须按 uid 派生。

    5. **会话头族**（X-Conversation-ID / X-Conversation-Request-ID / X-Request-ID / B3）
       官方客户端一次 user send 内的所有 tool call / 重试 / 换号复用同一个
       conversationRequestID，后台据此把一次对话聚合成一条而非碎片成 N 条。
       **换号重试必须复用同一个 ID** —— 否则上游看到的是 N 个不同会话在秒级并发，
       这正是「一重试就暴露」的典型形态。
    """
    ip = _client_ip(request)
    # 归属头 / 风控闸门头 / 版本号统一来自「客户端参数档案」
    # （后台可配置、可同步到线上，见 admin/client_profile.py）。
    # 这里用 risk_headers() 一次取全，避免「UA 报 A、X-IDE-Version 报 B」的错位。
    h: dict[str, str] = {
        "X-Agent-Purpose": purpose or "conversation",
    }
    h.update(cprofile.risk_headers())
    # 档案里的 ide_name 允许被 ADMIN_UPSTREAM_CLIENT_NAME 覆盖（历史行为保持一致）
    override = settings.UPSTREAM_CLIENT_NAME.strip()
    if override:
        h["X-IDE-Name"] = override
        h["X-IDE-Type"] = override
        h["X-Product"] = override
    if ip:
        h.update({
            "X-Forwarded-For": ip,
            "X-Real-IP": ip,
            "X-Client-IP": ip,
        })
        custom = settings.UPSTREAM_CLIENT_HEADER.strip()
        if custom:
            h[custom] = ip

    # 账号级稳定设备标识（需知道 uid）
    uid = (account.uid or "") if account is not None else ""
    if uid:
        h["X-Machine-ID"] = backend.stable_device_id(uid, "machine")
        h["X-Session-ID"] = backend.stable_device_id(uid, "session")

    # 会话头族：换号重试时由调用方**复用同一份**（见 _session_headers）
    if body is not None:
        h.update(_session_headers(body, uid))
    return h


def _session_headers(body: dict, uid: str) -> dict:
    """构造会话头族（对话 ID / 对话轮级聚合主键 / 消息级 ID / 链路追踪）。

    调用方在**轮转循环外**生成一次、循环内复用，保证换号重试时上游看到的是
    同一次对话轮，而不是 N 个并发会话。

    链路头族（依据官方客户端 CLI 的 `injectOtelSpanHeaders`，见逆向报告
    `workbuddy-auth-protocol-report.md`）—— 官方在**每次模型请求**上都会注入
    这一整套，缺一个就是可识别的「非官方客户端」特征：

        traceparent  00-<traceId32>-<spanId16>-<01|00>
        b3           <traceId32>-<spanId16>-<1|0>
        X-B3-TraceId / X-B3-SpanId / X-B3-Sampled
        X-Trace-ID   <traceId32>

    注意 `X-B3-ParentSpanId` **故意不发**：官方只在存在父 span 时才带
    （`ec && setHeader(...)`）；网关发出的都是**根请求**，带了反而异常。
    """
    h: dict[str, str] = {}
    conv_id = pool.extract_session_key(body)
    if conv_id:
        # 透传客户端原值优先；客户端没给就不伪造（避免误导上游建错会话）
        h["X-Conversation-ID"] = conv_id
    rk = pool.turn_key(body) or conv_id or ""
    if rk:
        digest = hashlib.sha256(rk.encode("utf-8", "replace")).hexdigest()
        h["X-Conversation-Request-ID"] = digest[:32]
        h["X-Root-Request-ID"] = digest[:32]
        trace = digest[:32]
    else:
        trace = hashlib.sha256(f"{time.time_ns()}:{uid}".encode()).hexdigest()[:32]
    # 消息级 ID：每条独立（32 hex，与官方 X-Request-ID 形态一致）
    mid = hashlib.sha256(f"{time.time_ns()}:{uid}:msg".encode()).hexdigest()[:32]
    h["X-Conversation-Message-ID"] = mid
    h["X-Request-ID"] = mid
    # span id 为 16 hex（8 字节），trace id 为 32 hex（16 字节）—— OTEL 规范。
    span = mid[:16]
    h["X-B3-TraceId"] = trace
    h["X-B3-SpanId"] = span
    h["X-B3-Sampled"] = "1"
    h["X-Trace-ID"] = trace
    # 单头 b3 与 W3C traceparent：与上面的 X-B3-* 保持**同一份** trace/span，
    # 否则三个头互相矛盾，比不发更容易被识别。
    h["b3"] = f"{trace}-{span}-1"
    h["traceparent"] = f"00-{trace}-{span}-01"
    # 官方模型请求恒带 X-Agent-Intent，默认 "craft"（无 meta 时的兜底值）。
    h["X-Agent-Intent"] = "craft"
    return h



def _record_usage(key_id: int, account_id: int, model: str, credits: float | None,
                  updated_auth_json: str | None, *,
                  client_ip: str = "", use_case: str = "",
                  prompt_tokens: int | None = None,
                  completion_tokens: int | None = None, total_tokens: int | None = None,
                  cached_tokens: int | None = None,
                  seq: int = 0, ttfb_ms: int | None = None,
                  latency_ms: int | None = None, error_kind: str = "") -> int | None:
    """流式响应结束后独立开一个 DB 会话写入用量/额度。

    关键点：请求作用域的 db 会话在端点返回 StreamingResponse 时已被依赖 teardown 关闭，
    不能在流式生成器里复用它做 commit（会抛 ResourceClosedError 且被流式 except 静默吞掉）。
    这里用全新的 SessionLocal 落库，并把错误显式记录到日志，绝不再静默丢失。

    credits 语义：
    - None 表示上游未返回真实积分，此时按模型倍率估算；
    - 0.0 表示上游明确返回 0 或请求失败，不再估算。

    若本次使用了估算值，会启动后台线程在 60 秒后调用上游用量接口回写真实积分。
    """
    log_id = None
    try:
        db = SessionLocal()
        try:
            # 上游 usage 没给 credits 时，用本地模型配置的 credit_multiplier 估算。
            # credit_multiplier 在 model_configs 里保存的是「每千 token 积分」，
            # 因此估算公式为 total_tokens * multiplier / 1000；
            # 当 total_tokens 缺失时用 completion_tokens 兜底。
            estimated = False
            original_credits = credits
            if credits is None and (total_tokens or completion_tokens):
                mc = db.query(ModelConfig).filter(ModelConfig.model_id == (model or ""),
                                                   ModelConfig.enabled == 1).first()
                mult = mc.credit_multiplier if mc else 0
                if mult:
                    toks = total_tokens if total_tokens else completion_tokens
                    credits = float(toks) * mult / 1000.0
                    estimated = True
            if credits is None:
                credits = 0.0
            _logger.info("记录用量 model=%s raw_credits=%s est=%s pt=%s ct=%s tt=%s cached=%s client_ip=%s use_case=%s",
                         model, original_credits, credits, prompt_tokens, completion_tokens, total_tokens,
                         cached_tokens, client_ip, use_case)
            key = db.query(ApiKey).filter(ApiKey.id == key_id).first()
            if key is not None:
                key.credit_used = float(key.credit_used or 0) + credits
            acc = db.query(Account).filter(Account.id == account_id).first()
            if acc is not None:
                acc.balance_remain = max(0, int(acc.balance_remain or 0) - int(credits))
                acc.last_used_at = datetime.utcnow()
                if updated_auth_json:
                    acc.auth_json = updated_auth_json
            log = UsageLog(
                api_key_id=key_id, account_id=account_id, model=model, credits=credits,
                prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
                total_tokens=total_tokens, cached_tokens=cached_tokens,
                client_ip=client_ip or "", use_case=use_case or "",
                seq=seq, ttfb_ms=ttfb_ms, latency_ms=latency_ms, error_kind=error_kind or "",
            )
            db.add(log)
            db.commit()
            log_id = log.id
            if estimated and account_id and updated_auth_json:
                threading.Thread(
                    target=_fetch_real_credits,
                    args=(log_id, account_id, updated_auth_json, model, credits, log.created_at),
                    daemon=True,
                ).start()
        finally:
            db.close()
    except Exception as e:  # 记账失败不应影响已返回的响应，但必须留痕便于排查
        _logger.exception("记录用量失败 key=%s acc=%s model=%s credits=%s: %s",
                          key_id, account_id, model, credits, e)
    return log_id


def _fetch_real_credits(log_id: int, account_id: int, auth_json: str, model: str,
                        estimated_credits: float, created_at: datetime) -> None:
    """延迟查询上游真实用量接口，回写 UsageLog.credits 并校正额度。

    上游 /billing/meter/get-user-request-usage 有分钟级延迟，通常在请求完成后 30~90s
    才能查到。这里等待 60s 后按 [created_at-5min, created_at+5min] + model 匹配最近一条。
    """
    try:
        time.sleep(60)
        sess = AccountSession(auth_json)
        try:
            start = (created_at - timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
            end = (created_at + timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
            result = sess.fetch_request_usage(start, end, page_num=1, page_size=50)
            data = (result.get("data") or {}).get("data") or []
            client_name = (settings.UPSTREAM_CLIENT_NAME or "WorkBuddy").strip() or "WorkBuddy"
            candidates = [
                r for r in data
                if r.get("model") == model and client_name in (r.get("client") or "")
            ]
            if not candidates:
                _logger.info("真实积分回写未找到匹配 log=%s model=%s", log_id, model)
                return
            # 取 requestTime 最接近 created_at 的一条
            def _ts(item):
                try:
                    return datetime.strptime(item.get("requestTime", ""), "%Y-%m-%d %H:%M:%S")
                except Exception:
                    return datetime.min
            best = min(candidates, key=lambda r: abs((_ts(r) - created_at).total_seconds()))
            real = float(best.get("credit") or 0)
            _logger.info("真实积分匹配 log=%s model=%s real=%s est=%s requestTime=%s",
                         log_id, model, real, estimated_credits, best.get("requestTime"))
            db = SessionLocal()
            try:
                log = db.query(UsageLog).filter(UsageLog.id == log_id).first()
                if log is None:
                    return
                delta = real - log.credits
                if abs(delta) < 0.0001:
                    return
                log.credits = real
                key = db.query(ApiKey).filter(ApiKey.id == log.api_key_id).first()
                if key is not None:
                    key.credit_used = max(0.0, float(key.credit_used or 0) + delta)
                acc = db.query(Account).filter(Account.id == log.account_id).first()
                if acc is not None:
                    acc.balance_remain = max(0, int(acc.balance_remain or 0) - int(delta))
                db.commit()
                _logger.info("真实积分回写完成 log=%s real=%s delta=%s", log_id, real, delta)
            finally:
                db.close()
        finally:
            sess.close()
    except Exception as e:
        _logger.exception("真实积分回写失败 log=%s: %s", log_id, e)

# ---------------------------------------------------------------------------
# 请求级表格日志：每个 /v1/* chat 请求出口打印一行。
# seq 进程级递增；模型截断 11 字符；uid 只显示前 8 位。
# ---------------------------------------------------------------------------
import itertools

_CHAT_SEQ = itertools.count(1)


def _log_chat_row(ttfb_ms, latency_ms, model, mode, uid, status, toks, error_kind=""):
    """向 stdout 打印一行表格日志，便于排查慢请求与风控。"""
    seq = next(_CHAT_SEQ)
    now = datetime.now().strftime("%H:%M:%S")
    model = (model or "-")[:11]
    tok_field = "-" if toks is None else str(toks)
    tps = "-"
    if toks is not None and latency_ms and latency_ms > 0:
        tps = f"{toks * 1000 / latency_ms:.1f}"
    ttfb = "-" if ttfb_ms is None or ttfb_ms <= 0 else f"{ttfb_ms}ms"
    uid_prefix = (uid or "-")[:8]
    latency = f"{latency_ms}ms" if latency_ms is not None else "-"
    print(f"| #{seq:03d} | {now} | {model:11s} | {mode:6s} | {status:3d} | uid={uid_prefix} | TTFB={ttfb:>5} | tok={tok_field:>5} | {tps:>6}t/s | total={latency:>7} | {error_kind}", flush=True)
    return seq


# ---------------------------------------------------------------------------
# 上游错误分类 + 账号状态机（ pool/upstream）：
#   - 网络层错误不累计 errCount
#   - 404 短冷却不累计 errCount（防雪崩）
#   - HTTP 5xx 累计 errCount，阈值 5 触发 10m 冷却
#   - 余额不足 / session 死亡 / 429 分别处理
# ---------------------------------------------------------------------------

_HARD_CREDIT_MARKERS = [
    "insufficient credit", "no credit", "credit exhausted", "out of credit",
    "quota exceeded", "quota exhaust", "payment required", "credit not enough",
    "not enough credit",
    "积分不足", "额度不足", "余额不足", "积分用完", "额度用尽", "没有积分",
]
_SESSION_DEAD_MARKERS = ["Offline user session not found", "12153", "session not found", "invalid session"]

#: 账号级授权/配额故障：不是余额问题，也不是限流，而是「这个号当前不允许调」。
#:   * 11140 "request illegal" —— 账号级授权风控；
#:   * 14017 "trial not activated" —— 试用未激活/注册未完成；
#:   * 14015 "license expired" —— 授权到期；
#:   * 14016 "enterprise not activated" —— 企业未开通。
#: 都应当**换号并冷却**，而不是无限重试同一个号；也不能误判成 429 软限流
#: （14017 常带 429 状态码），否则会把一个长期不可用的号当「等一会就好」处理。
#:
#: 注：14015/14016 **不在**上游 `isQuotaExhaustedError` 集合里，语义是「授权态问题」
#: 而非「额度用完」，所以走 account_fault（30 分钟冷却）而不是 hard_credit（次日 04:00）。
_ACCOUNT_FAULT_CODES = ("11140", "14017", "14015", "14016")
_ACCOUNT_FAULT_MARKERS = ("request illegal from an unapproved channel",
                          "trial version is not yet activated",
                          "trial not activated",
                          "license expired",
                          "enterprise not activated")

# ---------------------------------------------------------------------------
# 上游限流错误码族（6000–6008）
#
# 依据：官方客户端 CLI bundle 里的权威枚举 `ServerErrorCode`（我们原先**只知道 6004**，
# 整族其余 7 个码全部漏判 —— 而它们恰恰就是「限速限流」本身）：
#
#     6000 CraftRateLimit   限流基值（未细化）
#     6001 CraftRateTPSLimit  每秒 token 数
#     6002 CraftRateTPMLimit  每分钟 token 数
#     6003 CraftRateTPHLimit  每小时 token 数
#     6004 CraftRateTPDLimit  每天 token 数   ← 原先唯一处理的
#     6005 CraftRateRPSLimit  每秒请求数
#     6006 CraftRateRPMLimit  每分钟请求数
#     6007 CraftRateRPHLimit  每小时请求数
#     6008 CraftRateRPDLimit  每天请求数
#
# 客户端的判定（bundle 内 `isTransientRateLimitBusinessCode` / `isCraftDailyQuotaBusinessCode`）：
#     e_ = {6004, 6008}                  → 日额度，**不重试**（换号也没用，当天用完了）
#     6000–6008 且 ∉ e_                 → 瞬时限流，**重试**（等一会/换号就能过）
#
# 对本网关的含义（比客户端更细，因为我们是号池）：
#   * 秒/分/时级（6000–6003、6005–6007）是**账号级**额度 → 换号立刻可用 → soft_rate；
#   * 日级（6004、6008）是**该账号×该模型**的日额度 → 换号可用、同号换模型也可用
#     → model_rate（只冷却这一对）。
#
# 为什么必须按码判定而不是只看 429：上游会把限流包在 **HTTP 200** 或 **400** 里返回。
# 此时旧逻辑会落到最后 `status >= 400 → "client"`（**不可重试**）或 `"transport"`，
# 于是「限流了却不换号」—— 正是用户反馈的「接口直接报错，没有切换其它账号」。
_ACCOUNT_RATE_CODES = frozenset({"6000", "6001", "6002", "6003",
                                "6005", "6006", "6007"})
#: 该账号对该模型的**日**额度：只冷这一对，换模型/换号都可解。
_MODEL_RATE_CODES = frozenset({"6004", "6008"})
#: 全族并集，仅用于自检与文档（分类走上面两个子集）。
_RATE_LIMIT_CODES = _ACCOUNT_RATE_CODES | _MODEL_RATE_CODES

#: 额度彻底耗尽（客户端 `isQuotaExhaustedError` 的精确集合）。
#: 这组是**持久**失败：当天的额度真的用完了，等几分钟不会恢复，
#: 换号也未必有额度 —— 按次日 04:00 冷却，避免每次请求都白转一轮。
#:   14001 UsageLimitExceeded            个人用量超限
#:   14012 UsageLimitExceededEnterprise  企业用量超限
#:   14013 UsageLimitExceededTencent     腾讯侧用量超限
#:   14014 UsageLimitEnterpriseExhausted 企业额度用尽
#:   14018 UsageLimitUserExhausted       个人额度用尽
#: （14002 ConversationChatTooMany / 10105 ConversationLimitExceeded 是**会话数**限制，
#:   不是额度，语义不同，故不并入这里。）
_QUOTA_EXHAUSTED_CODES = frozenset({"14001", "14012", "14013", "14014", "14018"})

#: 「纯请求速率」限流：code 14003。
#:
#: 官方枚举里它叫 `RateLimitError`（`codebuddy.js` @2842 附近的 ServerErrorCode），
#: `classifyErrorDetail` 给它的归类是 `{category:"quota", subcategory:"quota_request_limit"}`
#: —— 注意与 `quota_balance_exhausted`（14001/14002/14012/… 余额耗尽）是**不同**
#: 的 subcategory：14003 是「发得太快」，不是「额度没了」。
#: 上游文案：`{"code":14003,"msg":"too many requests","displayMsg":{"zh":"请求过于频繁，请稍后重试。"}}`；
#: 官方 UI 把它渲染成「当前模型请求繁忙，请切换模型或稍后重试」。
#:
#: 因此它是**瞬时**错误：分类仍是 soft_rate（可重试、换号可解），但冷却时长必须
#: 走 REQUEST_RATE_SECONDS（秒级），不能走 SOFT_RATE_SECONDS（600 秒）。
_REQUEST_RATE_CODES = frozenset({"14003"})

#: 「只是被临时限流」的冷却类型 —— 到期前也允许作为**最后一档**兜底候选。
#:
#: 为什么需要兜底：这些状态本来就是「等一会就好」。当**整池**都落在这种状态时，
#: `_select_account` 直接返回 None 会让一次瞬时抖动升级成硬停摆 ——
#: 所有请求拿到 503「无可用账号」，而实际上没有一个号被禁用、也没有一个号额度耗尽。
#: （2026-09-23 线上事故即为此：上游 14003 在 76 秒内命中全部 16 个号，
#: 每个号被软冷却 600 秒，期间请求全部 503。）
#:
#: 明确**不包含** hard_credit / account_fault / session_dead / waf：
#: 那些是持久失败或终态（额度耗尽、需重新登录、出口 IP 被拦），
#: 兜底重试只会白烧一次上游调用，还可能加重风控。
_TRANSIENT_COOL_KINDS = frozenset({"soft_rate", "not_found", "upstream_internal"})

#: 模型级限流：code 6004/6008「该模型的使用量超限」。只冷却**这一个模型**，
#: 该账号对其他模型仍可用 —— 否则「切个模型就能继续用」的号会被整体摘出池子。
_MODEL_RATE_CODE = "6004"
#: 该后端无此模型：code 11102。这是确定性答复，重试无意义，只能换模型/换号。
_MODEL_BLOCK_CODE = "11102"
_MODEL_BLOCK_MARKERS = ("service info not found",)
#: WAF 403 的判据：403 且**没有业务信封**（无 code 字段 / 空体 / HTML 拦截页）。
#: 带业务信封的 403（11140 等）已在上面各层捕获，不会走到这里。
_ENVELOPE_CODE_RE = re.compile(r'"code"\s*:\s*"?(-?\d+)"?')

#: 上游把「内部错误」包在 **HTTP 200** 响应体里返回时，HTTP 层完全看不出来。
#: 判据来自真实故障：桌面端 10000 的底层是 JSON-RPC -32603 "Internal error"，
#: 以及 ENOSPC 这类磁盘写满的运维级瞬时故障。这类失败**必须换号重试**，
#: 否则用户看到的就是「接口直接报错了，但没有切换其它账号」。
_UPSTREAM_INTERNAL_MARKERS = (
    "-32603", "internal error", "internal server error",
    '"category":"internal"', '"category": "internal"',
    "enospc", "no space left on device",
)

#: 可重试（换号 / 换模型）的错误分类白名单。
#: 集中定义一处，避免 5 个轮转循环各自内联一份而逐渐漂移 ——
#: 任何一处漏掉一个分类，那条路径就会「报错但不重试」。
_RETRYABLE_KINDS = frozenset({
    "hard_credit", "session_dead", "soft_rate", "model_rate", "model_block",
    "account_fault", "not_found", "server", "waf", "upstream_internal",
})

#: 模型级错误里，「该账号对该模型限流」换号可解（6004，账号各自的额度）；
#: 「该后端无此模型」（11102）带的是**后端**语义，不是全局事实 ——
#: 实测号池里的 15 个账号分属 3 个不同 domain（workbuddy.cn ×10 /
#: codebuddy.cn ×3 / copilot.tencent.com ×2），所以换到**其它 domain** 的账号时，
#: 同一个模型仍可能是可用的。
#:
#: ⚠️ 已知取舍（此处**故意**未改）：仍按「换模型」处理。理由是若改成换号，
#: 一旦某模型在上游全局不存在，就会把每个模型最多 MAX_ROTATE(3) 个账号写进
#: 6h 的 model_block 冷却（`MODEL_BLOCK_SECONDS`），15 个账号的池子很容易被
#: 一次请求打穿 —— 代价远大于收益。而且该分支在真实流量中从未触发过
#: （`account_model_cool` 表为空，6004/11102 都没发生过）。
#: 要放开应当做「只换到**不同 domain** 的账号」的定向轮转，而不是无脑 continue。
_MODEL_SWITCH_KINDS = frozenset({"model_block"})

#: 提交前探测缓冲上限：超过它就直接转发，避免为了等错误而无限期憋住正常流。
_INBAND_PROBE_MAX = 64 * 1024


def _json_code(body: str) -> str | None:
    """从响应体里取业务 code 字段（容错 JSON 空格）。取不到返回 None。"""
    if not body:
        return None
    m = _ENVELOPE_CODE_RE.search(body)
    return m.group(1) if m else None


def _looks_like_inband_error(body: str) -> bool:
    """响应体（可能是 SSE 文本）里是否含「上游错误」信封。

    上游有两类必须识别的失败，**HTTP 状态码都是 200**：

    1. JSON-RPC 信封：``{"code":-32603,"message":"Internal error",...}``
       —— 客户端报的 Error Code 10000 底层就是它；
    2. error 字段/事件：``{"error":{...}}`` 或 SSE ``event: error``。

    判定**基于解析后的结构**（error 字段 / 负 code / message 文本），
    而不是对原始字节做 substring —— 否则模型正常回答里恰好出现
    "internal error" 这几个字就会被误判成失败并触发换号。
    """
    if not body:
        return False

    def _err_text(obj) -> str:
        """从候选错误对象里取出可匹配的文案。"""
        if isinstance(obj, str):
            return obj
        if not isinstance(obj, dict):
            return ""
        parts = []
        for k in ("message", "msg", "detail", "details", "reason", "error_description"):
            v = obj.get(k)
            if isinstance(v, str):
                parts.append(v)
        # data 里还可能再嵌一层 {"data":{"details":"ENOSPC ..."}}
        d = obj.get("data")
        if isinstance(d, dict):
            parts.append(_err_text(d))
        err = obj.get("error")
        if isinstance(err, (dict, str)):
            parts.append(_err_text(err))
        return " ".join(parts)

    def _is_error_obj(obj) -> bool:
        if not isinstance(obj, dict):
            return False
        # 显式 error 字段
        if isinstance(obj.get("error"), (dict, str)):
            err = obj["error"]
            if err not in (None, "", {}, []):
                return True
        # JSON-RPC 负 code（-32603 等）；正数是业务码，不算
        c = obj.get("code")
        if isinstance(c, int) and c < 0:
            return True
        if isinstance(c, str) and c.startswith("-") and c[1:].isdigit():
            return True
        # status/category 明确 internal
        for k in ("category", "type", "status"):
            v = obj.get(k)
            if isinstance(v, str) and "internal" in v.lower():
                return True
        return False

    def _markers_hit(text: str) -> bool:
        low = (text or "").lower()
        return bool(low) and any(m in low for m in _UPSTREAM_INTERNAL_MARKERS)

    seen_any = False
    for candidate in _iter_json_objects(body):
        seen_any = True
        if _is_error_obj(candidate):
            return True
        if _markers_hit(_err_text(candidate)):
            return True
    # 有 data: 行但一行都没解析成功（截断/非 JSON）时，
    # 只剩「整段就是错误信封」这一种可能，做一次保守的尾部匹配。
    if not seen_any and body.lstrip().startswith("{"):
        return _markers_hit(body)
    return False


def _iter_json_objects(body: str):
    """依次产出 body 里可解析的 JSON 对象（裸 JSON 或 SSE data: 行）。"""
    stripped = body.lstrip()
    if stripped.startswith("{"):
        try:
            obj = json.loads(stripped)
        except Exception:
            obj = None
        if isinstance(obj, dict):
            yield obj
    for line in body.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if payload in ("", "[DONE]"):
            continue
        try:
            obj = json.loads(payload)
        except Exception:
            continue
        if isinstance(obj, dict):
            yield obj


def _sse_has_error_event(body: str) -> bool:
    """SSE 里是否出现 ``event: error`` 行（与 data 信封独立的一种信号）。"""
    for line in body.splitlines():
        s = line.strip()
        if s.startswith("event:") and "error" in s.lower():
            return True
    return False


#: 各种协议里「真实正文增量」的字段名。只有看到其中之一，才认为这条流是健康的。
_CONTENT_FIELDS = ("content", "reasoning_content", "text", "thinking", "tool_calls")


def _sse_has_content(body: str) -> bool:
    """SSE 里是否已经出现**真实正文增量**。

    流式模式下只要把字节提交给客户端，就再也无法换号重试了。所以提交前
    必须确认「这确实是正文，而不是一个 200 包着的错误信封」。
    注意 ``delta.role`` 之类只有元信息的帧不算正文。
    """
    for line in body.splitlines():
        s = line.strip()
        if not s.startswith("data:"):
            continue
        payload = s[5:].strip()
        if payload in ("", "[DONE]"):
            continue
        try:
            obj = json.loads(payload)
        except Exception:
            continue
        if not isinstance(obj, dict):
            continue
        # Anthropic 形态：content_block_delta {"delta":{"type":"text_delta","text":"..."}}
        # OpenAI chat：choices[].delta.content
        choices = obj.get("choices")
        if isinstance(choices, list):
            for ch in choices:
                if not isinstance(ch, dict):
                    continue
                for holder in (ch.get("delta"), ch.get("message")):
                    if isinstance(holder, dict):
                        for f in _CONTENT_FIELDS:
                            v = holder.get(f)
                            if v:
                                return True
                # Responses API：output 数组里的文本
                if ch.get("text"):
                    return True
        # Responses / Anthropic 顶层 delta
        for holder in (obj.get("delta"), obj.get("content_block")):
            if isinstance(holder, dict):
                for f in _CONTENT_FIELDS:
                    if holder.get(f):
                        return True
                if holder.get("type") == "text_delta" and holder.get("text"):
                    return True
        if isinstance(obj.get("delta"), str) and obj["delta"]:
            return True
        # 非流式的完整消息（聚合模式会用到）
        if obj.get("content") or obj.get("output_text"):
            return True
    return False


_EXHAUSTION_HINTS = {
    "hard_credit": "账号积分已用完（次日 04:00 重置）",
    "soft_rate": "上游限流中，请稍后重试",
    "model_rate": "该模型在多个账号上都已达额度上限",
    "model_block": "上游没有这个模型（该后端不提供）",
    "account_fault": "账号授权异常（需重新登录/激活）",
    "session_dead": "账号登录态失效（需重新登录）",
    "upstream_internal": "上游服务器内部故障（如磁盘写满），非本网关问题",
    "server": "上游服务异常",
    "waf": "网关出口 IP 被上游 WAF 拦截",
    "not_found": "上游接口路径不存在",
    "transport": "与上游的网络连接异常",
    "no_account": "号池里没有可用账号（余额或状态均不满足）",
}


def _exhaustion_message(kind: str, last_msg: str, tried_accounts: int,
                        tried_models: int) -> str:
    """把「全部失败」翻译成**可操作**的中文说明。

    只在提示里带错误分类与次数，不把上游原文整段抛给客户端 ——
    原文可能含内部细节，且对排障者没有额外价值（完整信息在网关日志里）。
    """
    hint = _EXHAUSTION_HINTS.get(kind or "", "未知错误")
    tried = f"已尝试 {tried_accounts} 个账号 / {tried_models} 个候选模型"
    base = f"{hint}；{tried}"
    if kind == "waf" and last_msg:
        return f"{base}，已暂停轮转；请稍后重试"
    return f"{base}。请稍后重试，或联系管理员查看网关日志。"


def _classify_error(status: int, body: str) -> str:
    """按 HTTP 状态码 + 业务 code + body 关键词返回错误分类。

    分层顺序**很重要**，每层都要先于「更宽泛」的层判定：
      1. 11102 模型不存在 —— 语义最具体（确定性答复），且只在 400/404 判定。
         429+11102 属于限流语义，必须留给第 5 层。
      2. session 死亡（12153）—— 终态，需要人工重登。它可能与 "rate limit"
         文案混排，若被 429 先判会误当软冷却，让死号留在池里反复被选中。
      3. 账号级授权故障（11140/14017）—— 常带 429 状态码，必须先于限流判定。
      4. 6004 模型级限流 —— 只冷却该模型，不能污染账号级冷却。
      5. **余额/额度耗尽的明确标记** —— 必须先于通用 429。这类错误是**持久**
         失败（余额到次日 04:00 才会重置），若误判成「限流、等一会就好」，
         这个号会在整天的每一次请求里都被选中并失败，白烧一次轮转 + 增加
         所有用户的延迟。宁可保守地把它按日冷却（误判代价：一个健康号当天
         少用；反向误判代价：每次请求都多一次必败重试）。
       6. 429 / 限流文案。
       7. 404 / 5xx / WAF 403 / 其它 4xx。
       8. **HTTP 200 但体内是错误信封**（JSON-RPC -32603 / ENOSPC / error 字段）
          —— 放在最后，具体业务码优先；上游确实会把内部错误塞进 200 响应，
          这类失败必须换号重试，否则用户看到的就是「报错了但没切换账号」。
    """
    if not status and not body:
        return "transport"
    body_l = (body or "").lower()
    code = _json_code(body)

    # 1. 该后端无此模型（确定性，重试无意义）
    if status in (400, 404) and code == _MODEL_BLOCK_CODE:
        return "model_block"
    for m in _MODEL_BLOCK_MARKERS:
        if status in (400, 404) and m in body_l:
            return "model_block"

    # 2. session 死亡（终态，连续计数后才禁用）
    for m in _SESSION_DEAD_MARKERS:
        if m in body:
            return "session_dead"

    # 3. 账号级授权/配额故障（可能带 429，必须先判）
    if code in _ACCOUNT_FAULT_CODES:
        return "account_fault"
    if status == 403:
        for m in _ACCOUNT_FAULT_MARKERS:
            if m in body_l:
                return "account_fault"

    # 4. 上游限流码族 6000–6008（客户端权威枚举 ServerErrorCode）。
    #     必须在「通用 429」和「其余 4xx」之前 —— 上游会把限流包在 200/400 里返回，
    #     落到后面就会被判成 client（不可重试）或 transport，于是「限流却不换号」。
    #     日级（6004/6008）只冷该模型；秒/分/时级（其余）是账号级，换号即可。
    if code in _MODEL_RATE_CODES:
        return "model_rate"
    if code in _ACCOUNT_RATE_CODES:
        return "soft_rate"
    # 4b. 额度彻底耗尽（14001/14012/14013/14014/14018）：持久失败，
    #     按次日 04:00 硬冷却。必须先于通用 429 —— 这些码常带 429 状态码。
    if code in _QUOTA_EXHAUSTED_CODES:
        return "hard_credit"

    # 5. 余额/额度不足（硬冷却到次日 04:00）—— 必须在通用限流之前
    if status in (402, 412):
        return "hard_credit"
    for m in _HARD_CREDIT_MARKERS:
        if m.lower() in body_l or m in body:
            return "hard_credit"

    # 6. 通用限流
    if status == 429:
        return "soft_rate"
    for m in ("rate limit", "rate-limit", "too many requests",
              "限流", "请求过于频繁", "频率过高"):
        if m in body_l or m in body:
            return "soft_rate"

    # 7. 其余
    if status == 404:
        return "not_found"
    if status >= 500:
        return "server"
    # 408 请求超时 / 425 Too Early：瞬时，换号重试即可，绝不能算客户端错误。
    if status in (408, 425):
        return "server"
    # 401 上游鉴权失效：通常是该账号 token 过期/被吊销。
    # 换号能立刻恢复服务，因此归入可重试，而不是把 401 透传给客户端。
    if status == 401:
        return "session_dead"
    if status == 403:
        # 走到这里说明 403 但**没有业务信封** —— 典型是 APISIX/WAF 拦截页或空体。
        # 账号级软冷却 + fail-fast（多号同时命中则判定出口 IP 被拦）。
        return "waf"
    if status >= 400:
        return "client"
    # 8. HTTP 200 但体内是上游错误信封（JSON-RPC -32603 / ENOSPC / error 字段）。
    #    放在最后：具体业务码（6004/11102/12153…）优先级更高，只有它们都不匹配
    #    时才按「上游内部故障」处理。真实故障：客户端 10000 的底层就是这个。
    if status == 200 and body and _looks_like_inband_error(body):
        return "upstream_internal"
    if status == 200 and body and _sse_has_error_event(body):
        return "upstream_internal"
    return "transport"  # 网络层/无响应状态




def _next_day_4am(now: datetime) -> datetime:
    """返回 now 所属日期的次日 04:00（本地时区）。

    为什么是次日 04:00：上游的日额度在凌晨重置，04:00 是重置完成后的安全时点
    （与参考实现的 CoolHard 口径一致）。余额耗尽的号在此之前调了必 402，
    继续轮换只会浪费轮转次数并刷噪音日志。
    """
    return now.replace(hour=4, minute=0, second=0, microsecond=0) + timedelta(days=1)


def _cooling_active(acc: Account, now: datetime) -> bool:
    """账号当前是否处于任一冷却/熔断/降权中（或门，生效的是最晚截止者）。"""
    for attr in ("cool_until", "breaker_until", "degrade_until"):
        until = getattr(acc, attr, None)
        if until is not None and until > now:
            return True
    return False


def _bump_breaker(db: Session, acc: Account, now: datetime, msg: str) -> None:
    """累计一次熔断失败；达阈值则按指数退避置 breaker_until。

    只对「反复失败」（5xx 等）计数：带权威恢复时刻的错误（429 有重置时间、
    402 有次日签到）各有自己的处置，再并入连续失败会让用户正常重试越堆越厚。
    退避随命中次数翻倍并封顶，且**已在熔断期内不延长**（防重试把冷却堆成 2h）。
    """
    if acc.breaker_until is not None and acc.breaker_until > now:
        return  # 熔断期内不翻倍、不延长
    acc.breaker_fails = (acc.breaker_fails or 0) + 1
    acc.last_err_msg = (msg or f"upstream {acc.breaker_fails}")[:255]
    if acc.breaker_fails >= settings.BREAKER_THRESHOLD:
        base = max(1, settings.BREAKER_COOLDOWN_SECONDS)
        cap = max(base, settings.BREAKER_COOLDOWN_MAX_SECONDS)
        d = base * (2 ** min(acc.breaker_fails - settings.BREAKER_THRESHOLD, 6))
        acc.breaker_until = now + timedelta(seconds=min(d, cap))
        acc.breaker_fails = 0  # 计数清零：下轮重新累计，避免一路翻到封顶


def _apply_account_policy(db: Session, acc: Account, kind: str, status: int,
                          msg: str, model: str = "",
                          reset_at: datetime | None = None) -> None:
    """根据错误分类更新账号状态：冷却 / 熔断 / 降权 / 禁用 / 模型级冷却。

    分层原则（每条都有明确的恢复依据）：
      * hard_credit  → 次日 04:00（上游日额度重置后）
      * soft_rate    → 秒级有界退避，连续触发按 2 倍指数放大并封顶
      * model_rate   → **只冷却该模型**（6004），账号对别的模型照常可用
      * model_block  → 该 (账号, 模型) 负缓存（11102），重试无意义
      * account_fault→ 账号级授权故障（11140/14017），冷却并换号，不无限重试
      * session_dead → 连续计数达阈值才禁用（避免临时 12153 误杀）
      * waf          → 短冷却 + IP 级 fail-fast（见 pool.POOL.waf）
      * server/5xx   → 熔断，指数退避
      * not_found    → 短冷却，不累计 errCount（防雪崩）
      * transport / client → 只记时间 + 连败计数（不叠加权威惩罚）

    每次**成功**都会清空连续失败计数（见 `_note_success`），所以偶发抖动不会
    累积成熔断 —— 只有真正持续坏的账号才会被摘出去。
    """
    now = datetime.utcnow()
    if kind == "hard_credit":
        acc.cool_until = _next_day_4am(now)
        acc.cool_kind = "hard_credit"
        acc.err_count = 0
        acc.breaker_fails = 0
        acc.last_err_at = now
        acc.last_err_msg = (msg or "余额不足")[:255]
    elif kind == "soft_rate":
        # 有上游重置时间就精确对齐（绝不指数放大：那样会把全池推到封顶），
        # 没有才做有界指数退避。
        #
        # 基数按**码族**分档：14003 是「请求过于频繁」（纯速率），上游自己就说
        # 「请稍后重试」，给秒级即可；6000–6008 那类配额型限流才用 600 秒起。
        # 共用一个 600 秒基数会把几次并发请求放大成整池停摆（见线上故障复盘）。
        if _json_code(msg) in _REQUEST_RATE_CODES:
            base = max(1, settings.REQUEST_RATE_SECONDS)
            cap = max(base, settings.REQUEST_RATE_MAX_SECONDS)
        else:
            base = max(1, settings.SOFT_RATE_SECONDS)
            cap = max(base, settings.SOFT_RATE_MAX_SECONDS)
        if reset_at is not None:
            until = min(reset_at, now + timedelta(seconds=cap))
        else:
            streak = (acc.err_count or 0) + 1
            until = now + timedelta(seconds=min(base * (2 ** min(streak - 1, 8)), cap))
        # 已在软冷却中不延长（防兜底探测把冷却越堆越厚）
        if acc.cool_kind != "soft_rate" or not acc.cool_until or acc.cool_until <= now:
            acc.cool_until = until
        acc.cool_kind = "soft_rate"
        acc.err_count = (acc.err_count or 0) + 1
        acc.breaker_fails = 0
        acc.last_err_at = now
        acc.last_err_msg = (msg or "429 rate limit")[:255]
    elif kind == "model_rate":
        # 只冷却触发调用的那个模型：账号级 until 不动，切模型立即可用。
        _set_model_cooldown(db, acc, model, "model_rate",
                            (msg or "6004 model rate limit")[:255],
                            reset_at=reset_at,
                            fallback_seconds=settings.MODEL_SOFT_RATE_SECONDS)
        acc.last_err_at = now
        acc.last_err_msg = (msg or "6004 model rate limit")[:255]
    elif kind == "model_block":
        # 该后端无此模型：负缓存 (账号, 模型)，重试无意义。
        _set_model_cooldown(db, acc, model, "model_block",
                            (msg or "11102 model not available")[:255],
                            reset_at=None,
                            fallback_seconds=settings.MODEL_BLOCK_SECONDS)
        acc.last_err_at = now
        acc.last_err_msg = (msg or "11102 model not available")[:255]
    elif kind == "account_fault":
        # 账号级授权/配额故障：换号 + 冷却。冷却时长取 30 分钟。
        acc.cool_until = now + timedelta(minutes=30)
        acc.cool_kind = "account_fault"
        acc.err_count = 0
        acc.breaker_fails = 0
        acc.last_err_at = now
        acc.last_err_msg = (msg or "account fault")[:255]
    elif kind == "session_dead":
        # 连续计数：12153 在真实环境会被临时性触发（上游抖动 / 并发刷新 token），
        # 一次就禁用等于误杀一个健康号。达阈值才禁用。
        acc.session_dead_fails = (acc.session_dead_fails or 0) + 1
        acc.cool_kind = "session_dead"
        acc.last_err_at = now
        acc.last_err_msg = (msg or "session dead")[:255]
        if acc.session_dead_fails >= max(1, settings.SESSION_DEAD_THRESHOLD):
            acc.status = "disabled"
            acc.cool_until = None
            acc.breaker_until = None
            acc.degrade_until = None
    elif kind == "waf":
        # WAF 403：短冷却 + IP 级判定。多账号同时命中说明是**出口 IP** 被拦，
        # 此时继续轮转只会把一次请求放大成 N 次（加重风控）。
        acc.cool_until = now + timedelta(seconds=60)
        acc.cool_kind = "waf"
        acc.err_count = 0
        acc.last_err_at = now
        acc.last_err_msg = (msg or "waf 403")[:255]
        POOL.waf.note(acc.uid or "")
    elif kind == "not_found":
        # 404 短冷却不累计 errCount（防雪崩）
        acc.cool_until = now + timedelta(seconds=60)
        acc.cool_kind = "not_found"
        acc.last_err_at = now
        acc.last_err_msg = (msg or "upstream 404")[:255]
    elif kind == "upstream_internal":
        # HTTP 200 但体内是上游内部错误（-32603 Internal error / ENOSPC 写盘失败）。
        # 这是**瞬时运维级故障**：上游那台机器磁盘满了，换一个账号（很可能是
        # 另一台后端）就能立刻恢复。因此：
        #   * 做秒级短冷却，把刚刚那个后端从选号里摘出去一会儿；
        #   * 累计连败计数（是「不知道原因的持续失败」，符合降权目标形态）；
        #   * **不动** err_count/session_dead_fails —— 不是账号的错，不该熔断。
        acc.cool_until = now + timedelta(seconds=max(15, settings.SOFT_RATE_SECONDS // 20))
        acc.cool_kind = "upstream_internal"
        acc.consecutive_fails = (acc.consecutive_fails or 0) + 1
        acc.last_err_at = now
        acc.last_err_msg = (msg or "upstream internal error")[:255]
    elif kind == "server" or status >= 500:
        # HTTP 5xx：熔断（指数退避），不再用「累计 5 次固定 10 分钟」——
        # 固定时长对持续坏的号太短（10 分钟后又被选中再失败），对偶发又太长。
        _bump_breaker(db, acc, now, msg or f"upstream {status}")
        acc.last_err_at = now
    elif kind == "transport":
        # 网络层抖动：不累计 errCount（不是账号的错），但要计入连败降权 ——
        # 「不知道原因的持续失败」正是降权的目标形态。
        acc.consecutive_fails = (acc.consecutive_fails or 0) + 1
        acc.last_err_at = now
        acc.last_err_msg = (msg or "transport error")[:255]
    else:
        # 其他 4xx 只换号，不累计 errCount（可能只是这次参数有问题）
        acc.consecutive_fails = (acc.consecutive_fails or 0) + 1
        acc.last_err_at = now
        acc.last_err_msg = (msg or f"upstream {status}")[:255]
    try:
        db.commit()
    except Exception:
        db.rollback()


def _note_success(db: Session, acc: Account) -> None:
    """请求成功：清空所有连续失败计数与熔断/降权截止。

    这是「偶发抖动不会累积成熔断」的关键 —— 没有这个清零，任何长期运行的池子
    最终都会因为零星失败把健康号一个个摘出去。
    """
    changed = False
    if acc.breaker_fails:
        acc.breaker_fails = 0
        changed = True
    if acc.err_count:
        acc.err_count = 0
        changed = True
    if acc.consecutive_fails:
        acc.consecutive_fails = 0
        changed = True
    if acc.degrade_until is not None:
        acc.degrade_until = None
        changed = True
    if acc.breaker_until is not None and acc.breaker_until <= datetime.utcnow():
        acc.breaker_until = None
        changed = True
    if acc.session_dead_fails:
        acc.session_dead_fails = 0
        changed = True
    if changed:
        try:
            db.commit()
        except Exception:
            db.rollback()


#: 连败降权时长（秒）与封顶。达阈后临时出池，避免「一直失败但一直没到熔断阈值」的号
#: 持续被选中、持续把失败传给用户。
DEGRADE_SECONDS = 300
DEGRADE_SECONDS_MAX = 7200


def _maybe_degrade(db: Session, acc: Account) -> None:
    """连败降权：连续失败达阈值则临时出池（不延长已在降权期内的截止）。"""
    now = datetime.utcnow()
    if acc.degrade_until is not None and acc.degrade_until > now:
        return
    if (acc.consecutive_fails or 0) < settings.BREAKER_THRESHOLD:
        return
    acc.degrade_until = now + timedelta(
        seconds=min(DEGRADE_SECONDS, DEGRADE_SECONDS_MAX))
    acc.consecutive_fails = 0
    try:
        db.commit()
    except Exception:
        db.rollback()


def _set_model_cooldown(db: Session, acc: Account, model: str, kind: str,
                        reason: str, *, reset_at: datetime | None,
                        fallback_seconds: int) -> None:
    """写入/更新 (账号, 模型) 级冷却。

    与账号级冷却**完全独立**：6004 限流后账号对其他模型仍可用，
    这样「切个模型就能继续」的号不会被整体白扔。
    """
    if not model:
        return
    now = datetime.utcnow()
    row = (db.query(AccountModelCooldown)
           .filter(AccountModelCooldown.account_id == acc.id,
                   AccountModelCooldown.model == model)
           .first())
    if kind == "model_rate" and reset_at is not None:
        until = min(reset_at, now + timedelta(seconds=settings.SOFT_RATE_MAX_SECONDS))
    elif kind == "model_block":
        # 负缓存按命中次数指数退避：官方确定「该后端无此模型」，重试无意义；
        # 半开到期后允许再试一次，再命中就翻倍，封顶 24h。
        hits = (row.hits or 0) + 1 if row else 1
        ttl = min(fallback_seconds * (2 ** min(hits - 1, 4)), 86400)
        until = now + timedelta(seconds=ttl)
    else:
        until = now + timedelta(seconds=max(1, fallback_seconds))
    if row is None:
        row = AccountModelCooldown(account_id=acc.id, model=model)
        db.add(row)
    row.until = until
    row.kind = kind
    row.reason = reason
    row.hits = (row.hits or 0) + 1


def _model_cooled(db: Session, acc: Account, model: str, now: datetime) -> bool:
    """该账号对该模型是否处于模型级冷却中。"""
    if not model:
        return False
    row = (db.query(AccountModelCooldown)
           .filter(AccountModelCooldown.account_id == acc.id,
                   AccountModelCooldown.model == model)
           .first())
    return bool(row and row.until and row.until > now)


def _parse_reset_at(msg: str) -> datetime | None:
    """从上游限流文案里解析重置墙钟时间。

    上游 429 的文案形如「将在 2026-09-14 15:12:00 重置」，也有相对时间形态。
    解析失败返回 None，调用方回落到有界退避 —— 绝不猜一个时间。
    """
    if not msg:
        return None
    m = re.search(r"(20\d{2})[-/年](\d{1,2})[-/月](\d{1,2})[日\sT]*(\d{1,2}):(\d{2})(?::(\d{2}))?", msg)
    if not m:
        return None
    try:
        y, mo, d, h, mi = (int(m.group(i)) for i in range(1, 6))
        sec = int(m.group(6) or 0)
        # 上游文案固定按 UTC+8 解释
        from datetime import timezone as _tz
        local = datetime(y, mo, d, h, mi, sec, tzinfo=_tz(timedelta(hours=8)))
        return local.astimezone(_tz.utc).replace(tzinfo=None)
    except Exception:
        return None



def _reset_at_from_headers(headers) -> datetime | None:
    """从上游**响应头**解析限流重置时间（官方客户端同款口径）。

    为什么必须读头而不是只读文案：429 的响应体文案是**人类可读**的、会随上游版本
    变化，而 `Retry-After` / `x-ratelimit-reset` 是**机器可读**的契约字段。
    官方客户端（CLI bundle `parseRetryAfterMs` / `parseRateLimitResetMs`）读的就是这两个：

        retry-after                            → 整数**秒**（相对量）
        anthropic-ratelimit-unified-reset      → epoch 秒 或 HTTP 日期
        x-ratelimit-reset                      → 同上

    与官方一致的取舍：
      * `Retry-After` **只认整数秒**，日期形态按官方行为忽略（`httpx` 已帮我们把
        相对秒规整进这个头，所以这里再解析一次整数即可）；
      * reset 头先试纯数字（epoch 秒），失败再试 HTTP 日期；
      * 解出来的时间若已过去（<= now）则视为无效，继续看下一个头 —— 返回过去的时间
        会让冷却立即失效，等于没冷却。

    返回 UTC naive（与库内 `datetime.utcnow()` 口径一致），失败返回 None。
    """
    if not headers:
        return None
    try:
        get = headers.get
    except AttributeError:
        return None

    now = datetime.utcnow()

    # 1) Retry-After：整数秒的相对量（最权威，优先）
    raw = get("retry-after")
    if raw:
        try:
            secs = int(str(raw).strip())
            if secs > 0:
                return now + timedelta(seconds=secs)
        except (TypeError, ValueError):
            pass  # 日期形态：按官方行为忽略

    # 2) reset 头：epoch 秒 或 HTTP 日期
    for name in ("anthropic-ratelimit-unified-reset", "x-ratelimit-reset"):
        raw = get(name)
        if not raw:
            continue
        text = str(raw).strip()
        if not text:
            continue
        when: datetime | None = None
        if text.isdigit():
            try:
                when = datetime.utcfromtimestamp(int(text))
            except (OverflowError, OSError, ValueError):
                when = None
        if when is None:
            # HTTP 日期（RFC 7231）→ datetime
            try:
                from email.utils import parsedate_to_datetime
                when = parsedate_to_datetime(text).astimezone(
                    timezone.utc).replace(tzinfo=None)
            except Exception:
                when = None
        if when is not None and when > now:
            return when
    return None


def _account_session_safe(db: Session, acc: Account) -> backend.AccountSession | None:
    """创建 AccountSession 并调用 get_headers()（可能触发 token 刷新）。

    若刷新失败（session 死亡等），按策略禁用/冷却该账号并返回 None。
    """
    sess = backend.AccountSession(acc.auth_json)
    try:
        sess.get_headers()  # 内部会触发 token 刷新并写临时文件
        return sess
    except Exception as e:
        msg = str(e)
        kind = _classify_error(0, msg)
        if kind == "transport":
            kind = "session_dead"  # token 刷新失败通常等于 session 失效
        _apply_account_policy(db, acc, kind, 0, msg)
        try:
            sess.close()
        except Exception:
            pass
        return None


_CREDIT_RE = re.compile(r"x\s*([0-9]+(?:\.[0-9]+)?)")


def _transient_cool_fallback(db: Session, exclude_ids: set | None,
                             min_balance: int, now: datetime,
                             limit: int = 5) -> list:
    """整池都在临时冷却时的兜底候选（冷却最早到期的前 `limit` 个）。

    只在 `_select_account` 的严格过滤结果为空时调用。语义是「退一档，
    而不是判定无号可用」：

      * 只放宽 `cool_until` —— `breaker_until` / `degrade_until` 仍然生效，
        因为它们代表**连续真实失败**，不是单次限流可以解释的；
      * 只接受 `_TRANSIENT_COOL_KINDS`，持久状态（额度耗尽 / 需重登 / IP 被拦）
        一律不参与，避免对着一个死号反复重试；
      * 不在这里做在途/防撞号/模型级过滤 —— 调用方会继续走 `_select_account`
        剩下的同一套下游逻辑，所以并发请求会被防撞号窗口摊到不同号上，
        不会出现「一堆请求同时砸同一个兜底号」。

    返回按 `cool_until` 升序的候选（最早到期的排最前，最可能已恢复）。
    """
    q = (db.query(Account)
           .filter(Account.status == "active",
                   Account.cool_kind.in_(tuple(_TRANSIENT_COOL_KINDS))))
    if min_balance > 0:
        q = q.filter(Account.balance_remain > 0)
    if exclude_ids:
        q = q.filter(~Account.id.in_(exclude_ids))
    q = q.filter(or_(Account.breaker_until.is_(None), Account.breaker_until <= now))
    q = q.filter(or_(Account.degrade_until.is_(None), Account.degrade_until <= now))
    return q.order_by(Account.cool_until.asc()).limit(limit).all()


def _no_account_reason(db: Session) -> str:
    """`_select_account` 选不出号时，给出一句**如实**的原因。

    原实现固定回「全部禁用或额度耗尽」。这句话在「整池只是被上游临时限流」
    的场景下是**错的** —— 它会把排障直接引向「换账号 / 充值」，
    而真实原因只是等几十秒。线上事故里 16 个号全是 active 且余额 1000+，
    却被这句话误导成额度问题。
    """
    now = datetime.utcnow()
    accs = db.query(Account).all()
    if not accs:
        return "号池为空（还没有添加任何账号）"
    active = [a for a in accs if (a.status or "") == "active"]
    if not active:
        return f"全部 {len(accs)} 个账号都已禁用"
    funded = [a for a in active if int(a.balance_remain or 0) > 0]
    if not funded:
        return f"{len(active)} 个启用中的账号余额都为 0（积分耗尽）"
    transient = [a for a in funded
                 if a.cool_kind in _TRANSIENT_COOL_KINDS
                 and a.cool_until is not None and a.cool_until > now]
    if len(transient) == len(funded):
        soon = min(a.cool_until for a in transient)
        left = max(0, int((soon - now).total_seconds()))
        return (f"全部 {len(funded)} 个账号正被上游临时限流"
                f"（最近一个约 {left} 秒后恢复，无需人工处理）")
    blocked = len(funded) - len(transient)
    return (f"{len(funded)} 个账号有余额，但当前都不可选"
            f"（其中 {blocked} 个处于熔断/降权或需人工处理的异常状态）")


def _select_account(db: Session, exclude_ids: set | None = None,
                    min_balance: int = 1, mark_picked: bool = True,
                    model: str = "", sticky_key: str = "",
                    require_realm: str = "") -> Account | None:
    """从健康账号池中挑选一个账号。

    选号 = 会话粘性（命中即定）→ 健康过滤 → 加权/择优（软均衡）三层串联。

    健康条件：active、有余额、不在**任一**冷却/熔断/降权期内、不在 exclude_ids 中、
    该账号对该 model 没有处于模型级冷却（6004/11102）、在途未占满。

    选号策略（`ADMIN_ACCOUNT_SELECT`）：
      * `remain`   余额最多优先（原行为，确定性）
      * `lru`      最久未用优先（确定性）
      * `weighted` 三因子加权随机（余额占比 ×10 + 快过期积分占比 ×8 + 闲置补偿），
                   Top-5 短名单内抽签 —— 概率倾斜而非硬排序，流量摊得更开

    防撞号：100ms 内刚被选中的账号跳过（内存窗口，不再每次 commit 数据库）。
    若全部刚被用过则兜底放行，绝不因为防撞号而选不出号。
    """
    now = datetime.utcnow()
    q = db.query(Account).filter(Account.status == "active")
    if min_balance > 0:
        q = q.filter(Account.balance_remain > 0)
    if exclude_ids:
        q = q.filter(~Account.id.in_(exclude_ids))
    # 冷却 == 或门：cool_until / breaker_until / degrade_until 任一未到期即不可选。
    # 生效的一定是最晚到期者 —— 三者各管一类原因，互不覆盖（原实现用单个
    # cool_until 会被后写的短冷却覆盖掉先写的长冷却，等于提前放行一个坏号）。
    q = q.filter(or_(Account.cool_until.is_(None), Account.cool_until <= now))
    q = q.filter(or_(Account.breaker_until.is_(None), Account.breaker_until <= now))
    q = q.filter(or_(Account.degrade_until.is_(None), Account.degrade_until <= now))

    rows = q.all()
    if not rows:
        # 整池都进了**临时**冷却，而不是「没有健康账号」。
        # 这里不直接返回 None（那会变成对客户端的 503 硬失败），
        # 而是退一档：把冷却最早到期、且阻塞原因只是临时限流的号拿来做候选。
        # 14003 这类「请求过于频繁」很可能已经自然恢复；即便没恢复，
        # 试一次也远好于给用户一个必然失败的 503。
        rows = _transient_cool_fallback(db, exclude_ids, min_balance, now)
        if rows:
            _logger.warning(
                "号池全部处于临时冷却中，启用兜底候选 %d 个（最早的 %s 到期）",
                len(rows), rows[0].cool_until)
    if not rows:
        return None

    # 模型级冷却过滤（6004 / 11102）：只排除「该账号×该模型」，
    # 账号对其他模型仍然可用 —— 这正是模型级冷却独立存在的意义。
    if model:
        rows = [a for a in rows if not _model_cooled(db, a, model, now)]
        if not rows:
            return None

    # 在途占满的号不参与选号（把并发摊平到整个池子，避免单号过载→成片 429）
    rows = POOL.available(rows)
    if not rows:
        return None

    # 真实客户端 IP / realm 维度：本系统只保留 CN 域，require_realm 预留扩展位。
    if require_realm:
        rows = [a for a in rows if (a.domain or "").find(require_realm) >= 0] or rows

    # -- 第一层：会话粘性（命中即定，优先于一切均衡策略）------------------
    if sticky_key:
        bound = POOL.sticky.resolve(sticky_key, {a.uid or "" for a in rows})
        if bound:
            hit = next((a for a in rows if (a.uid or "") == bound), None)
            if hit is not None:
                if mark_picked:
                    POOL.recent.mark(hit.uid or "")
                    hit.last_picked_at = now
                return hit

    # -- 第二层：防撞号窗口 ----------------------------------------------
    fresh = [a for a in rows if POOL.recent.fresh(a.uid or "")]
    pool_rows = fresh or rows  # 全被刚用过时兜底放行
    POOL.recent.prune()

    # -- 第三层：按策略选择 ----------------------------------------------
    acc: Account | None = None
    if settings.ACCOUNT_SELECT == "weighted":
        canon = [
            {"uid": a.uid or "", "credits": int(a.balance_remain or 0),
             "credits_expiring": int(a.credits_expiring or 0),
             "last_used_at": a.last_used_at, "acc": a}
            for a in pool_rows
        ]
        picked = pool.weighted_pick(canon, now=now)
        acc = picked["acc"] if picked else None
    elif settings.ACCOUNT_SELECT == "lru":
        # 最久未用优先：从未用过的（None）排最前，让新号/闲置号先被使用。
        pool_rows.sort(key=lambda a: a.last_used_at or datetime.min)
        acc = pool_rows[0]
    else:
        # remain（默认）：余额最多优先。
        # 在 Python 侧排序是因为上面已做过内存过滤（在途占满 / 模型级冷却），
        # 再回数据库 order_by 会丢掉这些过滤结果。reverse=True 配正数 key。
        pool_rows.sort(key=lambda a: int(a.balance_remain or 0), reverse=True)
        acc = pool_rows[0]

    if acc is not None and mark_picked:
        POOL.recent.mark(acc.uid or "")
        acc.last_picked_at = now
        try:
            db.commit()  # 仅落「最近使用」观测；选号本身不再依赖这次写
        except Exception:
            db.rollback()
    return acc



def _key_group_models(db: Session, key: "ApiKey | None") -> set[str] | None:
    """返回该 Key 允许的模型集合；None 表示不限制（可用全部启用模型）。

    绑定分组后只允许组内模型。分组被删除 → group_id 由删除接口置 0，
    这里读到 0 即视为不限制（自动降级，不锁死 Key）。
    组内模型全部被禁用时返回空集合，此时拒绝一切模型调用 —— 这是「配了分组但
    组内模型都不可用」的明确信号，不应静默放开。
    """
    if key is None:
        return None
    gid = int(getattr(key, "group_id", 0) or 0)
    if not gid:
        return None
    from admin.routers.groups import effective_group_models
    return effective_group_models(db, gid)


def _model_out_of_group_error(model: str) -> JSONResponse:
    """模型不在 Key 绑定分组内时的统一拒绝响应。"""
    return JSONResponse(
        status_code=403,
        content={"error": {
            "message": f"模型 '{model}' 不在该 API Key 绑定的分组内",
            "type": "model_not_in_group",
            "code": "model_not_in_group",
        }},
    )


def _pick_best_model(db: Session, requested_model: str,
                     allowed: set[str] | None = None) -> str | None:
    """根据请求模型和可用配置，选出最优实际使用的模型 ID。

    策略：
      - 用户指定了具体模型 → 校验白名单后直接用（或返回 None 表示被拒）
      - 用户传 "auto" 或空 → 优先选免费模型（credit_multiplier=0），没有免费的才选付费的
      - 未配置任何模型规则时放行全部（向后兼容），返回原始 model
      - allowed 非 None 时（Key 绑定了分组），只在 allowed 集合内选择
    """
    from admin.routers.models import _is_model_allowed, _get_free_models, _get_enabled_models
    from admin.models import ModelConfig

    # 检查是否有任何配置记录（无配置=向后兼容，放行全部）
    has_any_config = db.query(ModelConfig).first() is not None

    # 具体模型：有配置时校验白名单，无配置直接放行
    if requested_model and requested_model != "auto":
        if allowed is not None:
            # 绑定了分组：只认组内模型，优先于全局白名单判定，
            # 组内没有就是没权限（比白名单更严格，语义更明确）
            return requested_model if requested_model in allowed else None
        if not has_any_config:
            return requested_model  # 无配置，放行
        if _is_model_allowed(db, requested_model):
            return requested_model
        return None  # 被白名单拒绝

    # auto 模式：有配置时免费优先，无配置也从后端取模型列表自选（绝不透传 auto）
    if allowed is not None:
        # 分组内：免费优先，否则任意组内模型
        free_in_group = _get_free_models(db) & allowed
        if free_in_group:
            return sorted(free_in_group)[0]
        if allowed:
            return sorted(allowed)[0]
        return None  # 分组内无可用模型（都被禁用）

    if not has_any_config:
        # 无本地配置时：尝试从后端拉一次模型列表来选免费模型
        try:
            acc_tmp = _select_account(db)
            if acc_tmp:
                with backend.AccountSession(acc_tmp.auth_json) as sess:
                    raw_models = sess.fetch_models()
                # 选第一个 credits 为 0 或含 "free"/"x0.00" 的模型
                for rm in raw_models:
                    mid = rm.get("id", "")
                    if mid and mid.lower() != "auto":
                        cred = str(rm.get("credits") or "")
                        if not cred or "x0.00" in cred or "free" in cred.lower():
                            return mid
                # 没有免费模型就返回第一个非 auto
                for rm in raw_models:
                    mid = rm.get("id", "")
                    if mid and mid.lower() != "auto":
                        return mid
        except Exception:
            pass
        return "deepseek-v4-flash"  # 兜底：无配置且后端不可达时用默认模型

    free_models = _get_free_models(db)
    if free_models:
        return list(free_models)[0]  # 取第一个免费模型

    # 无免费模型：取任意一个启用的
    enabled = _get_enabled_models(db)
    if enabled:
        return list(enabled)[0]

    return None  # 有配置但全禁用


def _candidate_models(db: Session, tried: set, allowed: set[str] | None = None) -> list:
    """按 免费→付费 顺序返回可用模型候选（排除已尝试的），用于 429/5xx 自动切换。

    allowed 非 None 时（Key 绑定了分组）只在组内挑选，避免自动切换时
    悄悄把请求切到分组外的模型上。
    """
    from admin.routers.models import _get_enabled_models, _get_free_models

    free = _get_free_models(db) - tried
    paid = (_get_enabled_models(db) - free) - tried
    if allowed is not None:
        free &= allowed
        paid &= allowed
    return list(free) + list(paid)


def _aggregate_chat_sse(sse_text: str) -> dict:
    """把上游 chat SSE 聚合成一个非流式 `chat.completion` 对象。

    上游 `/v2/chat/completions` 只支持流式，所以 `stream: false` 的客户端
    必须在网关内部聚合。**这个函数是必需的，不是优化**：若直接把 SSE 原样
    返回给 `stream: false` 的调用方，OpenAI SDK 会拿到 `text/event-stream`
    去做 `json.loads`，直接抛 JSONDecodeError —— 非流式调用 100% 失败。

    合并规则与 `converter._collect_stream` 保持一致（两边必须同源，否则
    直连网关与后台网关对同一请求会给出不同的响应形状）：

      * `delta.content` 拼接为 `message.content`
      * `delta.reasoning_content` 拼接为 `message.reasoning_content`
        （思维链不能丢，见 converter.py 的同类注释）
      * `delta.tool_calls` 按 `index` 聚合、`arguments` 分片拼接
      * `usage` 取最后一个出现的事件
      * 兼容上游偶发回 `message` 而非 `delta` 的形态
    """
    content_parts: list[str] = []
    reasoning_parts: list[str] = []
    tool_calls: dict[int, dict] = {}
    model: str | None = None
    finish_reason: str | None = None
    usage: dict | None = None
    saw_content_delta = False
    created = None
    resp_id = ""

    for line in sse_text.splitlines():
        line = line.strip()
        if not line or not line.startswith("data:"):
            continue
        payload = line[len("data:"):].strip()
        if payload in ("", "[DONE]"):
            continue
        try:
            chunk = json.loads(payload)
        except Exception:
            continue
        if not isinstance(chunk, dict):
            continue
        model = chunk.get("model") or model
        resp_id = chunk.get("id") or resp_id
        created = chunk.get("created") or created
        if isinstance(chunk.get("usage"), dict):
            usage = chunk["usage"]
        for choice in (chunk.get("choices") or []):
            if not isinstance(choice, dict):
                continue
            if choice.get("finish_reason"):
                finish_reason = choice["finish_reason"]
            delta = choice.get("delta") or {}
            if delta.get("content"):
                content_parts.append(delta["content"])
                saw_content_delta = True
            if delta.get("reasoning_content"):
                reasoning_parts.append(delta["reasoning_content"])
            msg = choice.get("message") or {}
            if not saw_content_delta and msg.get("content"):
                content_parts.append(msg["content"])
            if not reasoning_parts and msg.get("reasoning_content"):
                reasoning_parts.append(msg["reasoning_content"])
            for tc in (delta.get("tool_calls") or msg.get("tool_calls") or []):
                if not isinstance(tc, dict):
                    continue
                idx = tc.get("index", 0)
                slot = tool_calls.setdefault(
                    idx, {"id": None, "name": None, "arguments": ""})
                if tc.get("id"):
                    slot["id"] = tc["id"]
                fn = tc.get("function") or {}
                if fn.get("name"):
                    slot["name"] = fn["name"]
                if fn.get("arguments"):
                    slot["arguments"] += fn["arguments"]

    tcs = None
    if tool_calls:
        tcs = [
            {"id": v["id"], "type": "function",
             "function": {"name": v["name"], "arguments": v["arguments"]}}
            for _, v in sorted(tool_calls.items())
        ]
        finish_reason = finish_reason or "tool_calls"

    message: dict = {"role": "assistant",
                     "content": "".join(content_parts) or None}
    if reasoning_parts:
        message["reasoning_content"] = "".join(reasoning_parts)
    if tcs:
        message["tool_calls"] = tcs

    out: dict = {
        "id": resp_id or f"chatcmpl-{os.urandom(12).hex()}",
        "object": "chat.completion",
        "created": created or int(time.time()),
        "model": model or "",
        "choices": [{"index": 0, "message": message,
                     "finish_reason": finish_reason or "stop",
                     "logprobs": None}],
    }
    if usage is not None:
        out["usage"] = usage
    return out


def _parse_usage(sse_text: str) -> dict:
    """从 chat SSE 文本里找最后一个带 usage 的事件，解析 credits 与 token 明细。

    返回 {"credits", "prompt_tokens", "completion_tokens", "total_tokens", "cached_tokens"}。
    上游通常只在最后一个事件回传 usage（配合 stream_options.include_usage=True）。

    积分字段优先级：usage.credits > usage.credit > usage.cost，均缺失时返回 credits=None，
    由调用方按模型倍率估算（倍率单位为「每千 token」）。
    """
    credits = None
    prompt_tokens = completion_tokens = total_tokens = cached_tokens = None
    for line in sse_text.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[len("data:"):].strip()
        if payload in ("", "[DONE]"):
            continue
        try:
            obj = json.loads(payload)
        except Exception:
            continue
        usage = obj.get("usage")
        if not isinstance(usage, dict):
            continue
        # 桌面端源码读取 usage.credit / usage.credits；旧协议有 usage.cost，一并兼容。
        cred = usage.get("credits")
        if cred is None:
            cred = usage.get("credit")
        if cred is None and isinstance(usage.get("cost"), (int, float)):
            cred = usage.get("cost")
        if cred is not None:
            cred_str = str(cred).strip()
            # 兼容 "x 100" / "x100" 以及纯数字 "100" / "100.5"
            m = _CREDIT_RE.search(cred_str)
            if m:
                credits = float(m.group(1))
            else:
                try:
                    credits = float(cred_str)
                except ValueError:
                    pass
            _logger.debug("parse_usage credit raw=%r parsed=%s", cred, credits)
        if usage.get("prompt_tokens") is not None:
            prompt_tokens = usage["prompt_tokens"]
        if usage.get("completion_tokens") is not None:
            completion_tokens = usage["completion_tokens"]
        if usage.get("total_tokens") is not None:
            total_tokens = usage["total_tokens"]
        # 缓存命中 token：OpenAI 标准在 prompt_tokens_details.cached_tokens
        cached = None
        ptd = usage.get("prompt_tokens_details")
        if isinstance(ptd, dict):
            cached = ptd.get("cached_tokens")
        if cached is None and usage.get("cached_tokens") is not None:
            cached = usage["cached_tokens"]
        if cached is not None:
            cached_tokens = cached
    return {
        "credits": credits,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
        "cached_tokens": cached_tokens,
    }


def _estimate_credits(sse_text: str, model: str) -> float:
    """兼容旧调用：仅返回 credits 估算（token 明细用 _parse_usage）。"""
    u = _parse_usage(sse_text)
    if u["credits"] is not None:
        return u["credits"]
    toks = u["total_tokens"] or u["completion_tokens"]
    if toks:
        return float(toks) * settings.COST_PER_TOKEN / 1000.0
    return 0.0


# Claude Code 等 Anthropic 客户端发来的是 claude-* 模型名，上游不认。
# 按 opus / sonnet / haiku 三个档次映射到本后台白名单里的模型，
# 档次目标来自 .env（ADMIN_ANTHROPIC_MODEL_*），默认 auto。
_ANTHROPIC_MODEL_TIERS = (
    ("opus", settings.ANTHROPIC_MODEL_OPUS),
    ("sonnet", settings.ANTHROPIC_MODEL_SONNET),
    ("haiku", settings.ANTHROPIC_MODEL_HAIKU),
)


def _map_anthropic_model(db: Session, model: str, key: "ApiKey | None" = None) -> str:
    """把 Anthropic 的模型名翻译成本后台白名单里的模型名。

    按顺序判定：
      1. 空 / auto                 → auto（由号池按免费优先自选）
      2. 已在白名单里（如 glm-5.2） → 原样，允许直接点名上游模型
      3. 含 opus / sonnet / haiku   → 取 .env 配置的对应档次模型
      4. 其余（claude-* 等）        → auto

    传了 key（且绑定了分组）时，判定 2 的「在白名单里」改为「在该分组里」：
    否则客户端点一个分组外的模型名会被原样放行，绕过分组限制。
    """
    allowed = _key_group_models(db, key)
    m = (model or "").strip()
    if not m or m == "auto":
        return "auto"
    if allowed is not None:
        if m in allowed:
            return m
    elif _pick_best_model(db, m):
        return m
    low = m.lower()
    for tier, target in _ANTHROPIC_MODEL_TIERS:
        if tier in low and target:
            # 档次映射目标也必须落在分组内，否则退化为 auto 由组内自选
            if allowed is None or target in allowed:
                return target
    return "auto"


@router.post("/v1/chat/completions")
async def chat_completions(
    request: Request,
    db: Session = Depends(get_db),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    authorization: str | None = Header(default=None),
):
    api_key = x_api_key
    if not api_key and authorization and authorization.startswith("Bearer "):
        api_key = authorization[7:].strip()
    if not api_key:
        return JSONResponse(status_code=401, content={"error": {"message": "缺少 API Key", "type": "auth_error"}})

    key = get_key_row(db, api_key)
    if not key:
        return JSONResponse(status_code=401, content={"error": {"message": "无效 API Key", "type": "auth_error"}})
    try:
        check_quota(key)
    except Exception as e:
        return JSONResponse(status_code=e.status_code, content=e.detail)

    acc = _select_account(db)
    if not acc:
        # 文案必须如实：常见情形是「整池被临时限流」而非「禁用/额度耗尽」，
        # 后者会把排障引向换号/充值，而真实原因只是等几十秒。
        return JSONResponse(status_code=503,
                            content={"error": {"message": f"无可用账号：{_no_account_reason(db)}",
                                               "type": "no_account"}})

    try:
        payload = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": {"message": "bad json", "type": "invalid_request"}})

    model = payload.get("model", "auto")

    # 模型白名单检查 + 免费优先选择（绑定了分组的 Key 只在组内选择）
    allowed = _key_group_models(db, key)
    resolved_model = _pick_best_model(db, model, allowed)
    if resolved_model is None:
        if allowed is not None and model not in ("auto", ""):
            return _model_out_of_group_error(model)
        return JSONResponse(
            status_code=400,
            content={"error": {"message": f"模型 '{model}' 不存在或已被禁用", "type": "model_not_found"}},
        )

    # 候选模型顺序：auto 模式按 免费→付费 排列，支持上游 429/5xx 自动切换下一个
    if model in ("auto", ""):
        order = [resolved_model] + _candidate_models(db, {resolved_model}, allowed)
        order = order[:8]  # 最多尝试 8 个，避免全局限流时反复重试
    else:
        order = [resolved_model]  # 具体模型：不静默切换，失败即报错

    body = dict(payload)
    body["stream"] = True
    # 客户端是否要流式。OpenAI SDK 默认 stream=false，所以**不能**假定为 True。
    # 上游只支持流式，因此客户端要非流式时我们先向自己要 SSE、再内部聚合。
    client_wants_stream = bool(payload.get("stream", False))

    # 始终要求上游回传 usage（token 与缓存命中），保证调用方一定能拿到用量自行记录
    opts = dict(body.get("stream_options") or {})
    opts["include_usage"] = True
    body["stream_options"] = opts

    url = f"{settings.BACKEND}/v2/chat/completions"

    # 会话粘性键与会话头族在**轮转循环外**算一次，循环内所有尝试复用同一份：
    # 换号重试若换了会话 ID，上游看到的是 N 个并发会话而不是一次对话的一次重试。
    sticky_key = pool.session_key_for(body)
    session_hdr = _session_headers(body, "")

    async def _stream(aggregate: bool = False):
        """带账号轮换 + 在途租约 + 退避 + 错误状态机 + 表格日志的流式代理。

        ``aggregate=False``（默认）：逐块 yield SSE，直接转发给客户端。
        ``aggregate=True``：同样的轮换/重试/记账逻辑，但把上游 SSE 收集起来，
        最后 yield **一个** 非流式 `chat.completion` JSON —— 供 `stream: false`
        的调用方使用。两条路径共用这套重试逻辑（含租约、粘性、退避、分类处置），
        避免为「要不要流式」复制一份号池状态机（那份复制迟早会漂移）。
        """
        db2 = SessionLocal()
        held_uid = ""  # 当前持有的在途租约
        try:
            request_start = time.perf_counter()
            ttfb_at = None
            status_out = 503
            seq = None
            mode = "stream"
            final_model = resolved_model or model
            final_acc_id = 0
            final_uid = "-"
            total_toks = None
            collected = []
            delivered = False
            last_err_kind = ""
            last_err_msg = ""
            rotate_idx = 0  # 轮转序号（退避按它指数增长）
            tried_accounts = 0   # 供「全部失败」提示说明尝试规模
            tried_models_n = 0

            async with httpx.AsyncClient(timeout=_stream_timeout(), limits=backend.HTTP_LIMITS) as client:
                for m in order:
                    body["model"] = m
                    tried_models_n += 1
                    tried_ids: set = set()
                    for attempt in range(max(1, settings.MAX_ROTATE)):
                        # WAF IP 级 fail-fast：多账号接连 403 说明是**出口 IP** 被拦，
                        # 继续轮转只会把一次请求放大成 N 次并加重风控，立即终止。
                        if POOL.waf.active():
                            last_err_kind = "waf"
                            last_err_msg = (f"出口 IP 被 WAF 拦截，"
                                            f"剩余 {POOL.waf.remaining()}s")
                            break
                        acc_i = _select_account(db2, exclude_ids=tried_ids, min_balance=1,
                                                model=m, sticky_key=sticky_key)
                        if not acc_i:
                            break
                        tried_ids.add(acc_i.id)
                        tried_accounts += 1
                        # 在途租约：占满上限的号选号阶段已被过滤，
                        # 这里再 acquire 一次以闭合并发窗口（选号与占用之间有间隙）。
                        if not POOL.acquire(acc_i.uid or ""):
                            continue
                        held_uid = acc_i.uid or ""
                        sess_i = _account_session_safe(db2, acc_i)
                        if sess_i is None:
                            POOL.release(held_uid)
                            held_uid = ""
                            continue
                        # 会话头族 + 账号级设备标识按本次账号重算（设备标识是账号维度）
                        extra = _upstream_extra_headers(request)
                        extra.update(session_hdr)
                        if acc_i.uid:
                            extra["X-Machine-ID"] = backend.stable_device_id(acc_i.uid, "machine")
                            extra["X-Session-ID"] = backend.stable_device_id(acc_i.uid, "session")
                        headers_i = sess_i.get_headers(extra=extra)
                        try:
                            async with client.stream("POST", url, headers=headers_i, json=body) as r:
                                if r.status_code >= 400:
                                    detail = await r.aread()
                                    text = detail[:500].decode(errors="ignore") if isinstance(detail, bytes) else str(detail)[:500]
                                    kind = _classify_error(r.status_code, text)
                                    _apply_account_policy(db2, acc_i, kind, r.status_code, text,
                                                          model=m, reset_at=_reset_at_from_headers(getattr(r, "headers", None)) or _parse_reset_at(text))
                                    _maybe_degrade(db2, acc_i)
                                    sess_i.close()
                                    POOL.release(held_uid)
                                    held_uid = ""
                                    # 可重试：余额不足 / session 死亡 / 限流 / 模型级限流 /
                                    # 账号故障 / 上游 5xx / 404 / WAF / 上游内部错误
                                    # 都换号或换模型，绝不把中断感传递给客户端。
                                    if kind in _RETRYABLE_KINDS:
                                        last_err_kind = kind
                                        last_err_msg = text
                                        # 11102「该后端无此模型」是确定性答复：换号无意义，
                                        # 只能换模型，故跳出账号循环、由外层取 order 下一项。
                                        # 6004「该模型额度超限」是**账号级**的（每个号各自
                                        # 计数），换号就能继续 —— 这里绝不能 break，
                                        # 否则「限流时直接报错、不切换其它账号」正是用户踩的坑。
                                        if kind in _MODEL_SWITCH_KINDS:
                                            break
                                        await pool.rotate_backoff_async(rotate_idx)
                                        rotate_idx += 1
                                        continue
                                    # 不可重试的客户端错误（400/403 等）才原样返回
                                    if not delivered:
                                        if aggregate:
                                            # 聚合模式必须产出 JSON：上游原文可能不是
                                            # JSON，直接 json.loads 会失败并被误报成
                                            # 「网关聚合失败」，掩盖真实原因。
                                            try:
                                                up = json.loads(text)
                                            except Exception:
                                                up = None
                                            yield json.dumps(
                                                up if isinstance(up, dict) else
                                                {"error": {"message": text,
                                                           "type": "upstream_error",
                                                           "code": r.status_code}},
                                                ensure_ascii=False)
                                        else:
                                            yield text
                                    return
                                # HTTP 200 —— 但 200 **不等于**成功：上游会把内部错误
                                # （-32603 Internal error / ENOSPC 写盘失败）塞进 200 的
                                # 响应体里。一旦我们把字节转发给客户端，就再也无法换号了，
                                # 所以先缓冲一小段，确认是**正文**再提交。
                                final_model = m
                                final_acc_id = acc_i.id
                                final_uid = acc_i.uid or "-"
                                probe = ""
                                committed = False
                                async for chunk in r.aiter_text():
                                    if ttfb_at is None:
                                        ttfb_at = time.perf_counter()
                                    collected.append(chunk)
                                    if committed:
                                        if not aggregate:
                                            yield chunk
                                        continue
                                    probe += chunk
                                    # 看到真实正文增量就提交（此后不再换号）；
                                    # 或缓冲到上限仍未见到正文，也不再憋着。
                                    if _sse_has_content(probe) or len(probe) >= _INBAND_PROBE_MAX:
                                        committed = True
                                        delivered = True
                                        if not aggregate:
                                            yield probe
                                        probe = ""
                            # 流式完成：先判定这是不是「200 包着的错误」
                            text = "".join(collected)
                            stream_is_error = bool(text) and (
                                _looks_like_inband_error(text) or _sse_has_error_event(text)
                            )
                            # 聚合模式全程缓冲、尚未向客户端产出任何字节，可整条重试。
                            # 流式模式只在「还没提交过字节」时才可重试。
                            can_retry = aggregate or not committed
                            if stream_is_error:
                                kind = _classify_error(200, text)
                                if can_retry:
                                    _apply_account_policy(db2, acc_i, kind, 200, text[:500],
                                                          model=m, reset_at=_reset_at_from_headers(getattr(r, "headers", None)) or _parse_reset_at(text))
                                    _maybe_degrade(db2, acc_i)
                                    sess_i.close()
                                    POOL.release(held_uid)
                                    held_uid = ""
                                    last_err_kind = kind
                                    last_err_msg = text[:500]
                                    _logger.warning(
                                        "上游 200 但响应体是错误，换号重试 kind=%s acc=%s model=%s body=%.200s",
                                        kind, final_uid, m, text)
                                    # 累积内容清空：下一轮账号从零收集，
                                    # 否则最终会把两个号的内容拼在一起。
                                    collected.clear()
                                    delivered = False
                                    if kind in _RETRYABLE_KINDS:
                                        # 11102 换模型（跳出账号循环，外层取下个模型）；
                                        # 其余（含 -32603 / ENOSPC）换号。
                                        if kind in _MODEL_SWITCH_KINDS:
                                            break
                                        await pool.rotate_backoff_async(rotate_idx)
                                        rotate_idx += 1
                                        continue
                                    # 不可重试：如实把错误交给客户端
                                    if aggregate:
                                        yield json.dumps(
                                            {"error": {"message": text[:500],
                                                       "type": "upstream_error"}},
                                            ensure_ascii=False)
                                    else:
                                        yield text
                                    return
                                # 已经把字节发给客户端了，收不回来 —— 但绝不能记成成功：
                                # 如实标记错误，让用量/日志与真实结果一致。
                                latency_ms = int((time.perf_counter() - request_start) * 1000)
                                seq = _log_chat_row(None, latency_ms, final_model, mode,
                                                    final_uid, 200, None, error_kind=kind)
                                _record_usage(key.id, final_acc_id, final_model, 0.0, None,
                                              client_ip=_client_ip(request),
                                              use_case="chat-completion", seq=seq,
                                              latency_ms=latency_ms, error_kind=kind)
                                sess_i.close()
                                POOL.release(held_uid)
                                held_uid = ""
                                _apply_account_policy(db2, acc_i, kind, 200, text[:500], model=m)
                                return
                            # 流式模式下若始终没识别到正文（如仅角色帧就结束），
                            # 把缓冲原样补发给客户端，绝不静默吞掉内容。
                            if not committed and not aggregate and probe:
                                ttfb_at = ttfb_at or time.perf_counter()
                                delivered = True
                                yield probe
                                probe = ""
                            # 确认是健康响应后，才清空连败计数：
                            # 「200 但体内是错误」不该被记成一次成功。
                            acc_i.last_used_at = datetime.utcnow()
                            _note_success(db2, acc_i)
                            usage = _parse_usage(text)
                            total_toks = usage["total_tokens"] or usage["completion_tokens"]
                            status_out = 200
                            latency_ms = int((time.perf_counter() - request_start) * 1000)
                            ttfb_ms = int((ttfb_at - request_start) * 1000) if ttfb_at else None
                            seq = _log_chat_row(ttfb_ms, latency_ms, final_model, mode, final_uid,
                                                status_out, total_toks, error_kind="success")
                            # 粘性跟随最终成功号：让多轮对话收敛到「对该会话持续成功」
                            # 的那个账号，而不是每轮重新抽签。
                            if sticky_key and held_uid:
                                POOL.sticky.bind(sticky_key, held_uid)
                            updated = sess_i.updated_json()
                            sess_i.close()
                            POOL.release(held_uid)
                            held_uid = ""
                            _record_usage(key.id, final_acc_id, final_model, usage["credits"], updated,
                                          client_ip=_client_ip(request), use_case="chat-completion",
                                          prompt_tokens=usage["prompt_tokens"],
                                          completion_tokens=usage["completion_tokens"],
                                          total_tokens=usage["total_tokens"],
                                          cached_tokens=usage["cached_tokens"],
                                          seq=seq, ttfb_ms=ttfb_ms, latency_ms=latency_ms,
                                          error_kind="success")
                            if aggregate:
                                # 聚合模式下客户端要的是一个 JSON 对象，
                                # 而不是 text/event-stream。这里 yield **字符串**
                                # （与流式分支的 chunk 同为 str），调用方统一
                                # json.loads 一次即可，不必按类型分支。
                                yield json.dumps(_aggregate_chat_sse(text),
                                                 ensure_ascii=False)
                            return
                        except Exception as e:
                            if delivered:
                                # 已经往客户端发过字节：流式模式下无法「收回」，
                                # 只能静默结束（客户端看到截断的流，符合 SSE 语义）。
                                # 但聚合模式下我们还没产出任何东西，必须**如实报错**，
                                # 否则调用方会拿到空响应却不知道上游中途断了。
                                if aggregate:
                                    yield json.dumps(
                                        {"error": {
                                            "message": f"上游流中断：{e}",
                                            "type": "upstream_error"}},
                                        ensure_ascii=False)
                                sess_i.close()
                                POOL.release(held_uid)
                                held_uid = ""
                                return
                            kind = _classify_error(0, str(e))
                            _apply_account_policy(db2, acc_i, kind, 0, str(e), model=m)
                            _maybe_degrade(db2, acc_i)
                            last_err_kind = kind
                            last_err_msg = str(e)
                            sess_i.close()
                            POOL.release(held_uid)
                            held_uid = ""
                            # 网络/传输错误也换号重试（含退避）
                            await pool.rotate_backoff_async(rotate_idx)
                            rotate_idx += 1
                            continue
            # 全部账号/模型均失败
            latency_ms = int((time.perf_counter() - request_start) * 1000)
            err_kind = last_err_kind or ("no_account" if not last_err_msg else "transport")
            seq = _log_chat_row(None, latency_ms, final_model, mode, "-", status_out, None, error_kind=err_kind)
            _record_usage(key.id, 0, final_model, 0.0, None,
                          client_ip=_client_ip(request), use_case="chat-completion", seq=seq, latency_ms=latency_ms,
                          error_kind=err_kind)
            err_msg = _exhaustion_message(err_kind, last_err_msg, tried_accounts, tried_models_n)
            err_obj = {"error": {"message": err_msg, "type": "no_model_available"}}
            if aggregate:
                # 聚合模式：统一产出 JSON（调用方不是 SSE 客户端，
                # 给它 data: 行只会让它 json.loads 失败）。
                yield json.dumps(err_obj, ensure_ascii=False)
            else:
                yield f"data: {json.dumps(err_obj, ensure_ascii=False)}\n\n"
        finally:
            # 兜底释放租约：流被客户端中断（CancelledError）时也必须归还名额，
            # 否则那个账号的在途计数永远减不回去，最终被永久排除在选号之外。
            if held_uid:
                POOL.release(held_uid)
            db2.close()


    if client_wants_stream:
        return StreamingResponse(_stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    # 非流式：复用同一套号池状态机，但把上游 SSE 聚合成单个 chat.completion。
    # 上游只支持流式，所以「非流式」在本网关里意味着「内部聚合」而非
    # 「向上游要非流式」—— 这也正是 admin 侧长期把 SSE 原样吐给
    # stream:false 调用方的原因，属于必须修的协议违约。
    agg_out = ""
    async for chunk in _stream(aggregate=True):
        agg_out = chunk
    if not agg_out:
        return JSONResponse(
            status_code=503,
            content={"error": {"message": _exhaustion_message("no_account", "", 0, len(order)),
                               "type": "no_model_available"}})
    try:
        payload_out = json.loads(agg_out)
    except Exception:
        return JSONResponse(status_code=502,
                            content={"error": {"message": "网关聚合响应失败", "type": "upstream_error"}})
    if isinstance(payload_out, dict) and payload_out.get("error"):
        # 错误必须带上**正确的状态码**：把上游 400（参数错）报成 503（服务不可用）
        # 会让调用方去重试一个永远不会成功的请求，也会掩盖真实故障。
        err = payload_out["error"] or {}
        code = err.get("code")
        status = code if isinstance(code, int) and 400 <= code < 600 else 503
        return JSONResponse(status_code=status, content=payload_out)
    return JSONResponse(content=payload_out)


@router.post("/v1/responses")
async def responses_proxy(
    request: Request,
    db: Session = Depends(get_db),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    authorization: str | None = Header(default=None),
):
    """OpenAI Responses API 兼容端点（带 API Key 配额 / 用量记账 / 账号级熔断重试）。

    与 /v1/chat/completions 同一套托管逻辑：校验 Key → 配额 → 候选模型 →
    自动挑选健康账号；遇到余额不足 / session 死亡 / 限流 / 5xx 时自动换号，
    绝不把上游中断感传递给客户端。
    """
    if not _RESPONSES_AVAILABLE:
        return JSONResponse(status_code=501, content={"error": {"message": "Responses 适配器未加载", "type": "not_supported"}})

    api_key = x_api_key
    if not api_key and authorization and authorization.startswith("Bearer "):
        api_key = authorization[7:].strip()
    if not api_key:
        return JSONResponse(status_code=401, content={"error": {"message": "缺少 API Key", "type": "auth_error"}})
    key = get_key_row(db, api_key)
    if not key:
        return JSONResponse(status_code=401, content={"error": {"message": "无效 API Key", "type": "auth_error"}})
    try:
        check_quota(key)
    except Exception as e:
        return JSONResponse(status_code=e.status_code, content=e.detail)

    try:
        payload = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": {"message": "bad json", "type": "invalid_request"}})

    try:
        chat_body = responses_request_to_chat(payload)
    except Exception as e:
        return JSONResponse(status_code=400,
                            content={"error": {"message": f"请求转换失败：{e}", "type": "invalid_request"}})

    chat_body, _stats = project_responses_chat_body(chat_body)
    chat_body.setdefault("model", "auto")
    chat_body["stream"] = True
    opts = dict(chat_body.get("stream_options") or {})
    opts["include_usage"] = True
    chat_body["stream_options"] = opts

    requested = payload.get("model", "auto")
    allowed = _key_group_models(db, key)
    resolved = _pick_best_model(db, requested, allowed)
    if resolved is None:
        if allowed is not None and requested not in ("auto", ""):
            return _model_out_of_group_error(requested)
        return JSONResponse(status_code=400,
                            content={"error": {"message": f"模型 '{requested}' 不存在或已被禁用", "type": "model_not_found"}})

    order = [resolved]
    if requested in ("auto", ""):
        order = [resolved] + _candidate_models(db, {resolved}, allowed)
        order = order[:8]

    client_wants_stream = bool(payload.get("stream", True))
    model_name = payload.get("model", "auto")
    url = f"{settings.BACKEND}/v2/chat/completions"

    # 会话粘性键与会话头族：在轮转循环外算一次，循环内所有尝试复用同一份
    # （换号重试若换了会话 ID，上游看到的是 N 个并发会话而非一次对话的一次重试）。
    sticky_key = pool.session_key_for(chat_body)
    session_hdr = _session_headers(chat_body, "")

    if not client_wants_stream:
        # 非流式：内部重试，成功后聚合为单一 Response 对象
        db2 = SessionLocal()
        held_uid = ""
        try:
            rotate_idx = 0
            last_err_kind = ""
            last_err_msg = ""
            for m in order:
                body = dict(chat_body)
                body["model"] = m
                tried_ids: set = set()
                for _ in range(max(1, settings.MAX_ROTATE)):
                    if POOL.waf.active():
                        break
                    acc_i = _select_account(db2, exclude_ids=tried_ids, min_balance=1,
                                            model=m, sticky_key=sticky_key)
                    if not acc_i:
                        break
                    tried_ids.add(acc_i.id)
                    if not POOL.acquire(acc_i.uid or ""):
                        continue
                    held_uid = acc_i.uid or ""
                    sess_i = _account_session_safe(db2, acc_i)
                    if sess_i is None:
                        POOL.release(held_uid)
                        held_uid = ""
                        continue
                    extra_i = _upstream_extra_headers(request)
                    extra_i.update(session_hdr)
                    if acc_i.uid:
                        extra_i["X-Machine-ID"] = backend.stable_device_id(acc_i.uid, "machine")
                        extra_i["X-Session-ID"] = backend.stable_device_id(acc_i.uid, "session")
                    headers_i = sess_i.get_headers(extra=extra_i)
                    try:
                        async with httpx.AsyncClient(timeout=_stream_timeout(), limits=backend.HTTP_LIMITS) as client:
                            r = await client.post(url, headers=headers_i, json=body)
                            if r.status_code >= 400:
                                text = r.text[:500]
                                kind = _classify_error(r.status_code, text)
                                _apply_account_policy(db2, acc_i, kind, r.status_code, text,
                                                      model=m, reset_at=_reset_at_from_headers(getattr(r, "headers", None)) or _parse_reset_at(text))
                                _maybe_degrade(db2, acc_i)
                                sess_i.close()
                                POOL.release(held_uid)
                                held_uid = ""
                                if kind in _RETRYABLE_KINDS:
                                    # 11102 换模型（外层取 order 下一项）；
                                    # 6004 是账号级额度，换号即可 —— 不能 break。
                                    if kind in _MODEL_SWITCH_KINDS:
                                        break
                                    await pool.rotate_backoff_async(rotate_idx)
                                    rotate_idx += 1
                                    continue
                                return JSONResponse(status_code=r.status_code,
                                                    content={"error": {"message": text, "code": r.status_code}})
                            # HTTP 200 但体内是错误信封（-32603 / ENOSPC）：同样要换号。
                            if _looks_like_inband_error(r.text) or _sse_has_error_event(r.text):
                                kind = _classify_error(200, r.text)
                                _apply_account_policy(db2, acc_i, kind, 200, r.text[:500], model=m)
                                _maybe_degrade(db2, acc_i)
                                sess_i.close()
                                POOL.release(held_uid)
                                held_uid = ""
                                last_err_kind = kind
                                last_err_msg = r.text[:500]
                                _logger.warning("上游 200 但响应体是错误（responses）kind=%s acc=%s body=%.200s",
                                                kind, acc_i.uid or "-", r.text)
                                if kind in _RETRYABLE_KINDS:
                                    if kind in _MODEL_SWITCH_KINDS:
                                        break
                                    await pool.rotate_backoff_async(rotate_idx)
                                    rotate_idx += 1
                                    continue
                                return JSONResponse(status_code=502,
                                                    content={"error": {"message": r.text[:500],
                                                                       "type": "upstream_error"}})
                            converter = ResponsesStreamConverter(model=model_name)
                            for line in r.text.splitlines():
                                if not line.strip():
                                    continue
                                converter.feed_line(line)
                            converter.finish()
                            obj = converter.get_nonstream_response()
                            cost_info = _parse_usage(r.text)
                            acc_i.last_used_at = datetime.utcnow()
                            _note_success(db2, acc_i)  # 成功清零连败计数
                            updated = sess_i.updated_json()
                            total_toks = cost_info["total_tokens"] or cost_info["completion_tokens"]
                            seq = _log_chat_row(None, None, m, "resp", acc_i.uid or "-", 200, total_toks, error_kind="success")
                            sess_i.close()
                            if sticky_key and held_uid:
                                POOL.sticky.bind(sticky_key, held_uid)
                            POOL.release(held_uid)
                            held_uid = ""
                            _record_usage(key.id, acc_i.id, m, cost_info["credits"], updated,
                                          client_ip=_client_ip(request), use_case="responses",
                                          prompt_tokens=cost_info["prompt_tokens"],
                                          completion_tokens=cost_info["completion_tokens"],
                                          total_tokens=cost_info["total_tokens"],
                                          cached_tokens=cost_info["cached_tokens"],
                                          seq=seq, error_kind="success")
                            return JSONResponse(content=obj)
                    except Exception as e:
                        kind = _classify_error(0, str(e))
                        _apply_account_policy(db2, acc_i, kind, 0, str(e), model=m)
                        _maybe_degrade(db2, acc_i)
                        sess_i.close()
                        POOL.release(held_uid)
                        held_uid = ""
                        await pool.rotate_backoff_async(rotate_idx)
                        rotate_idx += 1
                        continue
            seq = _log_chat_row(None, None, resolved, "resp", "-", 503, None, error_kind="no_account")
            _record_usage(key.id, 0, resolved, 0.0, None,
                          client_ip=_client_ip(request), use_case="responses", seq=seq, error_kind="no_account")
            if POOL.waf.active():
                return JSONResponse(status_code=503, content={"error": {
                    "message": f"上游 WAF 拦截了网关出口 IP，已暂停轮转，请 {POOL.waf.remaining()}s 后重试",
                    "type": "waf_blocked"}})
            return JSONResponse(
                status_code=503,
                content={"error": {"message": _exhaustion_message(last_err_kind, last_err_msg, 0, len(order) if order else 0),
                                   "type": "no_model_available"}})
        finally:
            if held_uid:
                POOL.release(held_uid)
            db2.close()


    async def _stream():
        db2 = SessionLocal()
        held_uid = ""
        try:
            request_start = time.perf_counter()
            ttfb_at = None
            seq = None
            final_model = resolved
            final_acc_id = 0
            final_uid = "-"
            total_toks = None
            raw_lines: list[str] = []
            delivered = False
            last_err_kind = ""
            last_err_msg = ""
            rotate_idx = 0

            async with httpx.AsyncClient(timeout=_stream_timeout(), limits=backend.HTTP_LIMITS) as client:
                for m in order:
                    body = dict(chat_body)
                    body["model"] = m
                    tried_ids: set = set()
                    for _ in range(max(1, settings.MAX_ROTATE)):
                        if POOL.waf.active():
                            last_err_kind = "waf"
                            last_err_msg = f"出口 IP 被 WAF 拦截，剩余 {POOL.waf.remaining()}s"
                            break
                        acc_i = _select_account(db2, exclude_ids=tried_ids, min_balance=1,
                                                model=m, sticky_key=sticky_key)
                        if not acc_i:
                            break
                        tried_ids.add(acc_i.id)
                        if not POOL.acquire(acc_i.uid or ""):
                            continue
                        held_uid = acc_i.uid or ""
                        sess_i = _account_session_safe(db2, acc_i)
                        if sess_i is None:
                            POOL.release(held_uid)
                            held_uid = ""
                            continue
                        extra_i = _upstream_extra_headers(request)
                        extra_i.update(session_hdr)
                        if acc_i.uid:
                            extra_i["X-Machine-ID"] = backend.stable_device_id(acc_i.uid, "machine")
                            extra_i["X-Session-ID"] = backend.stable_device_id(acc_i.uid, "session")
                        headers_i = sess_i.get_headers(extra=extra_i)
                        converter = ResponsesStreamConverter(model=model_name)
                        try:
                            async with client.stream("POST", url, headers=headers_i, json=body) as r:
                                if r.status_code >= 400:
                                    detail = await r.aread()
                                    text = detail[:500].decode(errors="ignore") if isinstance(detail, bytes) else str(detail)[:500]
                                    kind = _classify_error(r.status_code, text)
                                    _apply_account_policy(db2, acc_i, kind, r.status_code, text,
                                                          model=m, reset_at=_reset_at_from_headers(getattr(r, "headers", None)) or _parse_reset_at(text))
                                    _maybe_degrade(db2, acc_i)
                                    sess_i.close()
                                    POOL.release(held_uid)
                                    held_uid = ""
                                    if kind in _RETRYABLE_KINDS:
                                        last_err_kind = kind
                                        last_err_msg = text
                                        # 11102 换模型；6004 是账号级额度，换号即可。
                                        if kind in _MODEL_SWITCH_KINDS:
                                            break
                                        await pool.rotate_backoff_async(rotate_idx)
                                        rotate_idx += 1
                                        continue
                                    yield f"data: {json.dumps({'type': 'error', 'error': {'message': text, 'code': r.status_code}}, ensure_ascii=False)}\n\n"
                                    return
                                final_model = m
                                final_acc_id = acc_i.id
                                final_uid = acc_i.uid or "-"
                                async for line in r.aiter_lines():
                                    if not line.strip():
                                        continue
                                    if ttfb_at is None:
                                        ttfb_at = time.perf_counter()
                                    events = converter.feed_line(line)
                                    if events:
                                        delivered = True
                                        yield events
                                    raw_lines.append(line)
                            # HTTP 200 但体内是错误信封（-32603 / ENOSPC）：
                            # 尚未向客户端产出任何事件时可安全换号重试。
                            _raw_text = "\n".join(raw_lines)
                            if (_looks_like_inband_error(_raw_text) or _sse_has_error_event(_raw_text)):
                                kind = _classify_error(200, _raw_text)
                                if not delivered:
                                    _apply_account_policy(db2, acc_i, kind, 200, _raw_text[:500], model=m)
                                    _maybe_degrade(db2, acc_i)
                                    sess_i.close()
                                    POOL.release(held_uid)
                                    held_uid = ""
                                    last_err_kind = kind
                                    last_err_msg = _raw_text[:500]
                                    _logger.warning("上游 200 但响应体是错误（resp-stream）kind=%s acc=%s body=%.200s",
                                                    kind, final_uid, _raw_text)
                                    raw_lines.clear()
                                    if kind in _RETRYABLE_KINDS:
                                        if kind in _MODEL_SWITCH_KINDS:
                                            break
                                        await pool.rotate_backoff_async(rotate_idx)
                                        rotate_idx += 1
                                        continue
                                    yield f"data: {json.dumps({'type': 'error', 'error': {'message': _raw_text[:500]}}, ensure_ascii=False)}\n\n"
                                    return
                                # 已经产出过事件：收不回来，但绝不能记成成功。
                                latency_ms = int((time.perf_counter() - request_start) * 1000)
                                seq = _log_chat_row(None, latency_ms, final_model, "resp", final_uid,
                                                    200, None, error_kind=kind)
                                _record_usage(key.id, final_acc_id, final_model, 0.0, None,
                                              client_ip=_client_ip(request), use_case="responses",
                                              seq=seq, latency_ms=latency_ms, error_kind=kind)
                                sess_i.close()
                                POOL.release(held_uid)
                                held_uid = ""
                                _apply_account_policy(db2, acc_i, kind, 200, _raw_text[:500], model=m)
                                return
                            finish = converter.finish()
                            if finish:
                                delivered = True
                                yield finish
                            text = "\n".join(raw_lines)
                            # 健康响应才清空连败计数并更新「最近使用」。
                            acc_i.last_used_at = datetime.utcnow()
                            _note_success(db2, acc_i)
                            usage = _parse_usage(text)
                            total_toks = usage["total_tokens"] or usage["completion_tokens"]
                            latency_ms = int((time.perf_counter() - request_start) * 1000)
                            ttfb_ms = int((ttfb_at - request_start) * 1000) if ttfb_at else None
                            seq = _log_chat_row(ttfb_ms, latency_ms, final_model, "resp", final_uid, 200, total_toks, error_kind="success")
                            updated = sess_i.updated_json()
                            sess_i.close()
                            if sticky_key and held_uid:
                                POOL.sticky.bind(sticky_key, held_uid)
                            POOL.release(held_uid)
                            held_uid = ""
                            _record_usage(key.id, final_acc_id, final_model, usage["credits"], updated,
                                          client_ip=_client_ip(request), use_case="responses",
                                          prompt_tokens=usage["prompt_tokens"],
                                          completion_tokens=usage["completion_tokens"],
                                          total_tokens=usage["total_tokens"],
                                          cached_tokens=usage["cached_tokens"],
                                          seq=seq, ttfb_ms=ttfb_ms, latency_ms=latency_ms,
                                          error_kind="success")
                            return
                        except Exception as e:
                            if delivered:
                                sess_i.close()
                                POOL.release(held_uid)
                                held_uid = ""
                                return
                            kind = _classify_error(0, str(e))
                            _apply_account_policy(db2, acc_i, kind, 0, str(e), model=m)
                            _maybe_degrade(db2, acc_i)
                            last_err_kind = kind
                            last_err_msg = str(e)
                            sess_i.close()
                            POOL.release(held_uid)
                            held_uid = ""
                            await pool.rotate_backoff_async(rotate_idx)
                            rotate_idx += 1
                            continue
            latency_ms = int((time.perf_counter() - request_start) * 1000)
            err_kind = last_err_kind or "no_account"
            seq = _log_chat_row(None, latency_ms, final_model, "resp", "-", 503, None, error_kind=err_kind)
            _record_usage(key.id, 0, final_model, 0.0, None,
                          client_ip=_client_ip(request), use_case="responses", seq=seq, latency_ms=latency_ms,
                          error_kind=err_kind)
            if err_kind == "waf":
                msg = _exhaustion_message("waf", last_err_msg, 0, len(order) if order else 0)
            else:
                msg = _exhaustion_message(err_kind, last_err_msg, 0, len(order) if order else 0)
            yield f"data: {json.dumps({'type': 'error', 'error': {'message': msg, 'code': 503}}, ensure_ascii=False)}\n\n"
        finally:
            # 兜底释放租约：客户端中断流时也必须归还名额，否则该账号的在途计数
            # 永远减不回去，最终被永久排除在选号之外。
            if held_uid:
                POOL.release(held_uid)
            db2.close()

    return StreamingResponse(_stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.post("/v1/messages")
async def anthropic_messages(
    request: Request,
    db: Session = Depends(get_db),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    authorization: str | None = Header(default=None),
):
    """Anthropic Messages API 兼容端点（带 API Key 配额 / 用量记账 / 号池熔断重试）。

    与 /v1/chat/completions 共用完全相同的托管逻辑，差别只在两头：
    入口把 Anthropic 请求转成 Chat 格式，出口把 Chat SSE 转回 Anthropic 事件流。
    这样 Claude Code 这类只会说 Anthropic 协议的客户端，就能直接吃后台号池的
    配额与用量记账，而不必再走 /gw 那条「桌面端单账号、无配额」的旁路。

    模型名：claude-opus-* / claude-sonnet-* / claude-haiku-* 按档次映射到白名单
    里的模型（见 _map_anthropic_model），也允许直接传 glm-5.2 这类上游模型名。
    """
    if not _ANTHROPIC_AVAILABLE:
        return JSONResponse(status_code=501,
                            content={"error": {"message": "Anthropic 适配器未加载", "type": "not_supported"}})

    api_key = x_api_key
    if not api_key and authorization and authorization.startswith("Bearer "):
        api_key = authorization[7:].strip()
    if not api_key:
        return JSONResponse(status_code=401, content={"error": {"message": "缺少 API Key", "type": "auth_error"}})
    key = get_key_row(db, api_key)
    if not key:
        return JSONResponse(status_code=401, content={"error": {"message": "无效 API Key", "type": "auth_error"}})
    try:
        check_quota(key)
    except Exception as e:
        return JSONResponse(status_code=e.status_code, content=e.detail)

    try:
        payload = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": {"message": "bad json", "type": "invalid_request"}})

    if not payload.get("messages"):
        return JSONResponse(status_code=400,
                            content={"error": {"message": "messages is required", "type": "invalid_request"}})

    try:
        chat_body = anthropic_request_to_chat(payload)
    except Exception as e:
        return JSONResponse(status_code=400,
                            content={"error": {"message": f"请求转换失败：{e}", "type": "invalid_request"}})

    # Claude Code 的 system prompt / tools 是固定 harness 模板，内含 "DoS attacks /
    # exploit development / credential testing" 一类合规声明词 —— 这些是「拒绝作恶」
    # 声明，却会被上游内容审核当成敏感内容，整条请求被拒（就是那个 11128
    # "unapproved channel"）。这里做与 converter /gw 端点完全相同的 harness 压缩 +
    # 零宽空格脱敏：模型读到的仍是原词，后端的关键词匹配失效。少了这一步，
    # Claude Code 的真实请求基本发不出去（简单 hello 测试却会通过，很容易误判）。
    if _DESENSITIZE_AVAILABLE and settings.ANTHROPIC_DESENSITIZE:
        try:
            _raw_len = len(json.dumps(chat_body, ensure_ascii=False))
            chat_body = desensitize_body(
                chat_body,
                roles=("system", "developer"),
                desensitize_harness_user=True,
                desensitize_tools=True,
                compact_harness=not settings.ANTHROPIC_NO_COMPACT,
                strip_tool_metadata=True,
            )
            _logger.info("harness 脱敏 %d -> %d 字节（compact=%s）",
                         _raw_len, len(json.dumps(chat_body, ensure_ascii=False)),
                         not settings.ANTHROPIC_NO_COMPACT)
        except Exception as e:
            _logger.warning("harness 脱敏失败，按原样发送：%s", e)

    requested = _map_anthropic_model(db, payload.get("model", "auto"), key)
    allowed = _key_group_models(db, key)
    resolved = _pick_best_model(db, requested, allowed)
    if resolved is None:
        if allowed is not None and requested not in ("auto", ""):
            return _model_out_of_group_error(requested)
        return JSONResponse(status_code=400,
                            content={"error": {"message": f"模型 '{requested}' 不存在或已被禁用", "type": "model_not_found"}})

    order = [resolved]
    if requested in ("auto", ""):
        order = ([resolved] + _candidate_models(db, {resolved}, allowed))[:8]

    # 上游一律按流式拉取：Anthropic 的方向就是「消费 Chat SSE 再转事件流」。
    # 客户端若要非流式，我们在内部聚合完再一次性返回。
    chat_body["stream"] = True
    opts = dict(chat_body.get("stream_options") or {})
    opts["include_usage"] = True
    chat_body["stream_options"] = opts

    # Anthropic Messages API 的 stream 默认是 false
    client_wants_stream = bool(payload.get("stream", False))
    model_name = payload.get("model", "auto")
    url = f"{settings.BACKEND}/v2/chat/completions"

    # 会话粘性键与会话头族：轮转循环外算一次，循环内所有尝试复用同一份。
    # 这里用**原始 payload** 而非转换后的 chat_body：Anthropic 协议的
    # `metadata.user_id` 只在原 payload 里，用它才能正确触发「已声明用户维度
    # 则不借首条 prompt 建立粘性」的抑制规则。
    sticky_key = pool.session_key_for(payload)
    session_hdr = _session_headers(payload if isinstance(payload, dict) else {}, "")

    if not client_wants_stream:
        db2 = SessionLocal()
        held_uid = ""
        try:
            rotate_idx = 0
            last_err_kind = ""
            last_err_msg = ""
            for m in order:
                body = dict(chat_body)
                body["model"] = m
                tried_ids: set = set()
                for _ in range(max(1, settings.MAX_ROTATE)):
                    if POOL.waf.active():
                        break
                    acc_i = _select_account(db2, exclude_ids=tried_ids, min_balance=1,
                                            model=m, sticky_key=sticky_key)
                    if not acc_i:
                        break
                    tried_ids.add(acc_i.id)
                    if not POOL.acquire(acc_i.uid or ""):
                        continue
                    held_uid = acc_i.uid or ""
                    sess_i = _account_session_safe(db2, acc_i)
                    if sess_i is None:
                        POOL.release(held_uid)
                        held_uid = ""
                        continue
                    extra_i = _upstream_extra_headers(request)
                    extra_i.update(session_hdr)
                    if acc_i.uid:
                        extra_i["X-Machine-ID"] = backend.stable_device_id(acc_i.uid, "machine")
                        extra_i["X-Session-ID"] = backend.stable_device_id(acc_i.uid, "session")
                    headers_i = sess_i.get_headers(extra=extra_i)
                    try:
                        async with httpx.AsyncClient(timeout=_stream_timeout(), limits=backend.HTTP_LIMITS) as client:
                            r = await client.post(url, headers=headers_i, json=body)
                            if r.status_code >= 400:
                                text = r.text[:500]
                                kind = _classify_error(r.status_code, text)
                                _apply_account_policy(db2, acc_i, kind, r.status_code, text,
                                                      model=m, reset_at=_reset_at_from_headers(getattr(r, "headers", None)) or _parse_reset_at(text))
                                _maybe_degrade(db2, acc_i)
                                sess_i.close()
                                POOL.release(held_uid)
                                held_uid = ""
                                last_err_kind = kind
                                last_err_msg = text
                                if kind in _RETRYABLE_KINDS:
                                    if kind in _MODEL_SWITCH_KINDS:
                                        break
                                    await pool.rotate_backoff_async(rotate_idx)
                                    rotate_idx += 1
                                    continue
                                return JSONResponse(status_code=r.status_code,
                                                    content={"error": {"message": text, "type": "upstream_error"}})
                            # HTTP 200 但体内是错误信封（-32603 / ENOSPC）：同样换号。
                            if _looks_like_inband_error(r.text) or _sse_has_error_event(r.text):
                                kind = _classify_error(200, r.text)
                                _apply_account_policy(db2, acc_i, kind, 200, r.text[:500], model=m)
                                _maybe_degrade(db2, acc_i)
                                sess_i.close()
                                POOL.release(held_uid)
                                held_uid = ""
                                last_err_kind = kind
                                last_err_msg = r.text[:500]
                                _logger.warning("上游 200 但响应体是错误（anthropic）kind=%s acc=%s body=%.200s",
                                                kind, acc_i.uid or "-", r.text)
                                if kind in _RETRYABLE_KINDS:
                                    if kind in _MODEL_SWITCH_KINDS:
                                        break
                                    await pool.rotate_backoff_async(rotate_idx)
                                    rotate_idx += 1
                                    continue
                                return JSONResponse(status_code=502,
                                                    content={"error": {"message": r.text[:500],
                                                                       "type": "upstream_error"}})
                            conv = AnthropicStreamConverter(model=model_name)
                            raw_lines: list[str] = []
                            for line in r.text.splitlines():
                                if not line.strip():
                                    continue
                                raw_lines.append(line)
                                conv.feed_line(line)
                            msg_obj = conv.build_message()
                            cost_info = _parse_usage("\n".join(raw_lines))
                            acc_i.last_used_at = datetime.utcnow()
                            _note_success(db2, acc_i)  # 成功清零连败计数
                            updated = sess_i.updated_json()
                            total_toks = cost_info["total_tokens"] or cost_info["completion_tokens"]
                            seq = _log_chat_row(None, None, m, "anthropic", acc_i.uid or "-", 200,
                                                total_toks, error_kind="success")
                            sess_i.close()
                            if sticky_key and held_uid:
                                POOL.sticky.bind(sticky_key, held_uid)
                            POOL.release(held_uid)
                            held_uid = ""
                            _record_usage(key.id, acc_i.id, m, cost_info["credits"], updated,
                                          client_ip=_client_ip(request), use_case="anthropic",
                                          prompt_tokens=cost_info["prompt_tokens"],
                                          completion_tokens=cost_info["completion_tokens"],
                                          total_tokens=cost_info["total_tokens"],
                                          cached_tokens=cost_info["cached_tokens"],
                                          seq=seq, error_kind="success")
                            return JSONResponse(content=msg_obj)
                    except Exception as e:
                        kind = _classify_error(0, str(e))
                        _apply_account_policy(db2, acc_i, kind, 0, str(e), model=m)
                        _maybe_degrade(db2, acc_i)
                        sess_i.close()
                        POOL.release(held_uid)
                        held_uid = ""
                        await pool.rotate_backoff_async(rotate_idx)
                        rotate_idx += 1
                        continue
            seq = _log_chat_row(None, None, resolved, "anthropic", "-", 503, None, error_kind="no_account")
            _record_usage(key.id, 0, resolved, 0.0, None,
                          client_ip=_client_ip(request), use_case="anthropic",
                          seq=seq, error_kind="no_account")
            if POOL.waf.active():
                return JSONResponse(status_code=503, content={"error": {
                    "message": f"上游 WAF 拦截了网关出口 IP，已暂停轮转，请 {POOL.waf.remaining()}s 后重试",
                    "type": "waf_blocked"}})
            return JSONResponse(
                status_code=503,
                content={"error": {"message": _exhaustion_message(last_err_kind, last_err_msg, 0, len(order) if order else 0),
                                   "type": "no_model_available"}})
        finally:
            if held_uid:
                POOL.release(held_uid)
            db2.close()


    async def _stream():
        db2 = SessionLocal()
        held_uid = ""
        try:
            request_start = time.perf_counter()
            ttfb_at = None
            seq = None
            final_model = resolved
            final_acc_id = 0
            final_uid = "-"
            total_toks = None
            raw_lines: list[str] = []
            delivered = False
            last_err_kind = ""
            last_err_msg = ""
            rotate_idx = 0

            async with httpx.AsyncClient(timeout=_stream_timeout(), limits=backend.HTTP_LIMITS) as client:
                for m in order:
                    body = dict(chat_body)
                    body["model"] = m
                    tried_ids: set = set()
                    for _ in range(max(1, settings.MAX_ROTATE)):
                        if POOL.waf.active():
                            last_err_kind = "waf"
                            last_err_msg = f"出口 IP 被 WAF 拦截，剩余 {POOL.waf.remaining()}s"
                            break
                        acc_i = _select_account(db2, exclude_ids=tried_ids, min_balance=1,
                                                model=m, sticky_key=sticky_key)
                        if not acc_i:
                            break
                        tried_ids.add(acc_i.id)
                        if not POOL.acquire(acc_i.uid or ""):
                            continue
                        held_uid = acc_i.uid or ""
                        sess_i = _account_session_safe(db2, acc_i)
                        if sess_i is None:
                            POOL.release(held_uid)
                            held_uid = ""
                            continue
                        extra_i = _upstream_extra_headers(request)
                        extra_i.update(session_hdr)
                        if acc_i.uid:
                            extra_i["X-Machine-ID"] = backend.stable_device_id(acc_i.uid, "machine")
                            extra_i["X-Session-ID"] = backend.stable_device_id(acc_i.uid, "session")
                        headers_i = sess_i.get_headers(extra=extra_i)
                        conv = AnthropicStreamConverter(model=model_name)
                        try:
                            async with client.stream("POST", url, headers=headers_i, json=body) as r:
                                if r.status_code >= 400:
                                    detail = await r.aread()
                                    text = detail[:500].decode(errors="ignore") if isinstance(detail, bytes) else str(detail)[:500]
                                    kind = _classify_error(r.status_code, text)
                                    _apply_account_policy(db2, acc_i, kind, r.status_code, text,
                                                          model=m, reset_at=_reset_at_from_headers(getattr(r, "headers", None)) or _parse_reset_at(text))
                                    _maybe_degrade(db2, acc_i)
                                    sess_i.close()
                                    POOL.release(held_uid)
                                    held_uid = ""
                                    if kind in _RETRYABLE_KINDS:
                                        last_err_kind = kind
                                        last_err_msg = text
                                        # 11102 换模型；6004 是账号级额度，换号即可。
                                        if kind in _MODEL_SWITCH_KINDS:
                                            break
                                        await pool.rotate_backoff_async(rotate_idx)
                                        rotate_idx += 1
                                        continue
                                    # 不可重试的客户端错误才原样透出
                                    yield conv.error_event(text)
                                    return
                                final_model = m
                                final_acc_id = acc_i.id
                                final_uid = acc_i.uid or "-"
                                async for line in r.aiter_lines():
                                    if not line.strip():
                                        continue
                                    if ttfb_at is None:
                                        ttfb_at = time.perf_counter()
                                    raw_lines.append(line)
                                    events = conv.feed_line(line)
                                    if events:
                                        delivered = True
                                        yield events
                            # HTTP 200 但体内是错误信封（-32603 / ENOSPC）：
                            # 尚未产出任何事件时可安全换号重试。
                            _raw_text = "\n".join(raw_lines)
                            if (_looks_like_inband_error(_raw_text) or _sse_has_error_event(_raw_text)):
                                kind = _classify_error(200, _raw_text)
                                if not delivered:
                                    _apply_account_policy(db2, acc_i, kind, 200, _raw_text[:500], model=m)
                                    _maybe_degrade(db2, acc_i)
                                    sess_i.close()
                                    POOL.release(held_uid)
                                    held_uid = ""
                                    last_err_kind = kind
                                    last_err_msg = _raw_text[:500]
                                    _logger.warning("上游 200 但响应体是错误（anthropic-stream）kind=%s acc=%s body=%.200s",
                                                    kind, final_uid, _raw_text)
                                    raw_lines.clear()
                                    if kind in _RETRYABLE_KINDS:
                                        if kind in _MODEL_SWITCH_KINDS:
                                            break
                                        await pool.rotate_backoff_async(rotate_idx)
                                        rotate_idx += 1
                                        continue
                                    yield conv.error_event(_raw_text[:500])
                                    return
                                # 已产出过事件：收不回来，但绝不记成成功。
                                latency_ms = int((time.perf_counter() - request_start) * 1000)
                                seq = _log_chat_row(None, latency_ms, final_model, "anthropic",
                                                    final_uid, 200, None, error_kind=kind)
                                _record_usage(key.id, final_acc_id, final_model, 0.0, None,
                                              client_ip=_client_ip(request), use_case="anthropic",
                                              seq=seq, latency_ms=latency_ms, error_kind=kind)
                                sess_i.close()
                                POOL.release(held_uid)
                                held_uid = ""
                                _apply_account_policy(db2, acc_i, kind, 200, _raw_text[:500], model=m)
                                return
                            tail = conv.finish()
                            if tail:
                                yield tail
                            text = "\n".join(raw_lines)
                            # 健康响应才清空连败计数并更新「最近使用」。
                            acc_i.last_used_at = datetime.utcnow()
                            _note_success(db2, acc_i)
                            usage = _parse_usage(text)
                            total_toks = usage["total_tokens"] or usage["completion_tokens"]
                            latency_ms = int((time.perf_counter() - request_start) * 1000)
                            ttfb_ms = int((ttfb_at - request_start) * 1000) if ttfb_at else None
                            seq = _log_chat_row(ttfb_ms, latency_ms, final_model, "anthropic", final_uid, 200,
                                                total_toks, error_kind="success")
                            updated = sess_i.updated_json()
                            sess_i.close()
                            if sticky_key and held_uid:
                                POOL.sticky.bind(sticky_key, held_uid)
                            POOL.release(held_uid)
                            held_uid = ""
                            _record_usage(key.id, final_acc_id, final_model, usage["credits"], updated,
                                          client_ip=_client_ip(request), use_case="anthropic",
                                          prompt_tokens=usage["prompt_tokens"],
                                          completion_tokens=usage["completion_tokens"],
                                          total_tokens=usage["total_tokens"],
                                          cached_tokens=usage["cached_tokens"],
                                          seq=seq, ttfb_ms=ttfb_ms, latency_ms=latency_ms,
                                          error_kind="success")
                            return
                        except Exception as e:
                            if delivered:
                                sess_i.close()
                                POOL.release(held_uid)
                                held_uid = ""
                                return
                            kind = _classify_error(0, str(e))
                            _apply_account_policy(db2, acc_i, kind, 0, str(e), model=m)
                            _maybe_degrade(db2, acc_i)
                            last_err_kind = kind
                            last_err_msg = str(e)
                            sess_i.close()
                            POOL.release(held_uid)
                            held_uid = ""
                            await pool.rotate_backoff_async(rotate_idx)
                            rotate_idx += 1
                            continue
            latency_ms = int((time.perf_counter() - request_start) * 1000)
            err_kind = last_err_kind or "no_account"
            seq = _log_chat_row(None, latency_ms, final_model, "anthropic", "-", 503, None, error_kind=err_kind)
            _record_usage(key.id, 0, final_model, 0.0, None,
                          client_ip=_client_ip(request), use_case="anthropic",
                          seq=seq, latency_ms=latency_ms, error_kind=err_kind)
            if err_kind == "waf":
                msg = _exhaustion_message("waf", last_err_msg, 0, len(order) if order else 0)
            else:
                msg = _exhaustion_message(err_kind, last_err_msg, 0, len(order) if order else 0)
            yield AnthropicStreamConverter(model=model_name).error_event(msg, "overloaded_error")
        finally:
            if held_uid:
                POOL.release(held_uid)
            db2.close()

    return StreamingResponse(_stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.post("/v1/messages/count_tokens")
async def anthropic_count_tokens(
    request: Request,
    db: Session = Depends(get_db),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    authorization: str | None = Header(default=None),
):
    """Anthropic token 计数端点（本地粗估，不请求上游、不消耗配额）。

    Claude Code 发消息前会调它做上下文预算。上游没有等价接口，这里按
    「字符数 / 3」估算并刻意偏向高估 —— 宁可让客户端早点压缩上下文，
    也不要低估，否则真正请求时可能超出上游上限直接失败。
    """
    api_key = x_api_key
    if not api_key and authorization and authorization.startswith("Bearer "):
        api_key = authorization[7:].strip()
    if not api_key:
        return JSONResponse(status_code=401, content={"error": {"message": "缺少 API Key", "type": "auth_error"}})
    if not get_key_row(db, api_key):
        return JSONResponse(status_code=401, content={"error": {"message": "无效 API Key", "type": "auth_error"}})

    try:
        payload = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": {"message": "bad json", "type": "invalid_request"}})

    text = "".join((
        json.dumps(payload.get("system") or "", ensure_ascii=False),
        json.dumps(payload.get("messages") or [], ensure_ascii=False),
        json.dumps(payload.get("tools") or [], ensure_ascii=False),
    ))
    return {"input_tokens": max(1, len(text) // 3)}


@router.get("/v1/models")
async def models(
    db: Session = Depends(get_db),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    authorization: str | None = Header(default=None),
):
    api_key = x_api_key
    if not api_key and authorization and authorization.startswith("Bearer "):
        api_key = authorization[7:].strip()
    if not api_key:
        return JSONResponse(status_code=401, content={"error": {"message": "缺少 API Key", "type": "auth_error"}})
    key = get_key_row(db, api_key)
    if not key:
        return JSONResponse(status_code=401, content={"error": {"message": "无效 API Key", "type": "auth_error"}})
    try:
        check_quota(key)
    except Exception as e:
        return JSONResponse(status_code=e.status_code, content=e.detail)

    acc = _select_account(db)
    if not acc:
        return JSONResponse(status_code=503,
                            content={"error": {"message": f"无可用账号：{_no_account_reason(db)}",
                                               "type": "no_account"}})
    # 绑定了分组的 Key 只应看到组内模型，否则客户端会照着完整列表点模型然后被 403
    allowed = _key_group_models(db, key)
    try:
        with backend.AccountSession(acc.auth_json) as sess:
            models_raw = sess.fetch_models()
            acc.auth_json = sess.updated_json()
        acc.last_used_at = datetime.utcnow()
        db.commit()
        data = [{
            "id": m.get("id"),
            "object": "model",
            "owned_by": "codebuddy",
            "name": m.get("name") or m.get("id"),
            "credit_multiplier": backend.CredentialManager._parse_credit_multiplier(m.get("credits"))
            if hasattr(backend.CredentialManager, "_parse_credit_multiplier") else None,
        } for m in models_raw
            if m.get("id")
            and m.get("id", "").lower() != "auto"
            and (m.get("id") in allowed if allowed is not None else _is_model_allowed(db, m.get("id")))]
        return {"object": "list", "data": data, "source": "backend"}
    except Exception as e:
        return JSONResponse(status_code=502, content={"error": {"message": f"获取模型失败：{e}", "type": "upstream"}})
