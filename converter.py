#!/usr/bin/env python3
"""
workbuddy2api — 把 CodeBuddy / WorkBuddy 的订阅暴露成标准 OpenAI 兼容 API。

原理（直连后端，原生 function calling）：
  - 读取本机已登录的 CodeBuddy 桌面端凭据（auth 文件里的 token / uid / enterpriseId）。
  - 直接转发到 CodeBuddy 后端 `https://copilot.tencent.com/v2/chat/completions`。
    该后端本身就是标准 OpenAI chat/completions 协议（含原生 tools / tool_calls / SSE 流式）。
  - 转换器只做两件事：①注入鉴权 header（Authorization / X-User-Id 等）
    ②在本地 /v1/* 与后端 /v2/* 之间做路径映射与透传（含 Anthropic / Chat / Responses 三种协议）。
  - token 过期时自动调 `/v2/plugin/auth/token/refresh` 刷新，并回写 auth 文件。

跨平台：自动定位 auth 目录（macOS / Windows / Linux）。
依赖：fastapi + uvicorn + httpx（pip install fastapi "uvicorn[standard]" httpx）。

用法：
  python3 converter.py                       # 默认 127.0.0.1:8787
  python3 converter.py --port 9000
  python3 converter.py --api-key mysecret    # 启用客户端鉴权
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import threading
import time
from pathlib import Path
from typing import Optional

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
import uvicorn

import upstream_compat

# 连接池：减少重复 TLS 握手； MaxIdleConnsPerHost=20 设计。
_HTTP_LIMITS = httpx.Limits(max_connections=100, max_keepalive_connections=20)


def _stream_timeout() -> httpx.Timeout:
    """流式/SSE 聚合请求的超时：只约束静默，不设响应总时长。

    为什么不用 `timeout=300`：它会展开成 connect/read/write/pool **各** 300s。
    read=300 本身是对的（上游在持续吐字就一直续期），但 connect=300 意味着
    一个连不上的账号要让我们干等最多 5 分钟才换号 —— 这才是「卡住不动」的主因。

    更要紧的是必须避免「给流式响应设总时长」这个坑。依据是官方客户端自己的
    源码（D:\\WorkBuddy\\resources\\app.asar.unpacked 内 ardot-mcp-app 的
    `_workbuddy-runtime/mcp-app-bootstrap.cjs`）：官方把 Node 18+ 默认的
    `http.Server.requestTimeout = 300000`（**总时长**）显式锁到 0，因为到点会
    在流仍活跃时强行掐断长连接 SSE，客户端只看到 undici `TypeError: terminated`。
    我们这边同理：绝不给流式响应设 deadline。
    """
    return httpx.Timeout(
        connect=float(os.getenv("ADMIN_STREAM_CONNECT_TIMEOUT", "15")),
        read=float(os.getenv("ADMIN_STREAM_IDLE_TIMEOUT", "180")),
        write=float(os.getenv("ADMIN_STREAM_WRITE_TIMEOUT", "60")),
        pool=float(os.getenv("ADMIN_STREAM_POOL_TIMEOUT", "20")),
    )


def _client_ip_headers(request: Request, purpose: str = "conversation") -> dict:
    """提取真实客户端 IP 与用途/产品头，透传给上游，避免请求用量里 client/agentPurpose 为空。

    真实 WorkBuddy 桌面端：
      - X-Agent-Purpose: "conversation" 用于普通对话
      - X-IDE-Name / X-IDE-Type: "WorkBuddy"（产品名，来自 clientInfoProvider 的
        platform / ideName，桌面端常量 WORKBUDDY_PLATFORM = "WorkBuddy"）
      - X-Product: **不是产品名，是部署形态**。官方全局
        ProductEndpointHttpInterceptor 里写的是
            headers["X-Product"] ||= configuration?.deploymentType ?? "SaaS"
        所以这里默认应当是 "SaaS"（= product.json 的 deploymentType），
        而不是 "WorkBuddy"。可用 ADMIN_UPSTREAM_PRODUCT 单独覆盖。
    """
    ip = None
    xff = request.headers.get("X-Forwarded-For")
    if xff:
        ip = xff.split(",")[0].strip()
    else:
        real = request.headers.get("X-Real-IP")
        if real:
            ip = real.strip()
        elif request.client:
            ip = request.client.host
    client_name = os.environ.get("ADMIN_UPSTREAM_CLIENT_NAME", "WorkBuddy").strip() or "WorkBuddy"
    product = (os.environ.get("ADMIN_UPSTREAM_PRODUCT", "SaaS").strip() or "SaaS")
    h = {
        "X-Agent-Purpose": purpose or "conversation",
        "X-IDE-Name": client_name,
        "X-IDE-Type": client_name,
        "X-Product": product,
    }
    if ip:
        h["X-Forwarded-For"] = ip
        h["X-Real-IP"] = ip
        h["X-Client-IP"] = ip
        custom = os.environ.get("ADMIN_UPSTREAM_CLIENT_HEADER", "").strip()
        if custom:
            h[custom] = ip
    return h

try:
    from desensitize import desensitize_body
except ImportError:  # 模块缺失时降级为不脱敏
    def desensitize_body(body, roles=("system",), desensitize_harness_user=False,
                         desensitize_tools=False, compact_harness=False,
                         strip_tool_metadata=False):
        return body

from responses_adapter import (
    responses_request_to_chat,
    ResponsesStreamConverter,
)
from responses_projection import project_responses_chat_body
from anthropic_adapter import (
    anthropic_request_to_chat,
    AnthropicStreamConverter,
)

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

BACKEND = "https://copilot.tencent.com"
DEFAULT_DOMAIN = "www.codebuddy.cn"
#: 出站 User-Agent：**必须**伪装成官方客户端。
#: 原来这里是 `codebuddy2openai/2.0` —— 一个自报家门的网关标识，上游「使用端」
#: 列会直接显示它，等于对风控举手。（官方无任何 UA 随机化，故这里保持确定性。）
#: 形状对齐官方桌面端三段式：`WorkBuddy/<客户端版本> WorkBuddy/<版本> CLI/<CLI版本>`。
#:
#: **取值走「客户端参数档案」**（`admin/client_profile.py`），优先级：
#:     环境变量 > （现场探测 / 后台保存）> 内置兜底
#:
#: 为什么不只读 wb_install：用户在后台改了版本号、或者线上实例根本没装客户端，
#: 都只能靠档案（可保存、可同步）才能报出正确版本。真实来源见 `wb_install`。
#: 覆盖方式：WORKBUDDY_DESKTOP_VERSION / WORKBUDDY_CLI_VERSION / WORKBUDDY_USER_AGENT。
from admin import client_profile as cprofile  # noqa: E402  客户端参数档案

# 模块导入期的常量：**不查库**（standalone converter 可能没配 MySQL）。
# 运行期发请求走 client_user_agent()，那里才读后台保存值。
_PROFILE_AT_IMPORT = cprofile.effective_local()
DESKTOP_VERSION = _PROFILE_AT_IMPORT["desktop_version"]
CLI_VERSION = _PROFILE_AT_IMPORT["cli_version"]
USER_AGENT = _PROFILE_AT_IMPORT["user_agent"]


def client_user_agent() -> str:
    """**实时的**出站 UA（后台改版本号后立即生效，无需重启）。

    `USER_AGENT` 常量在导入时算好，适合静态引用；发请求建议用本函数，
    它会问档案（内部 30 秒缓存，保存时立即失效，开销可忽略）。
    """
    return cprofile.ua()


def describe_client() -> str:
    """一行客户端诊断信息（启动日志 / 排障用）。

    UA 走档案（可能与探测值不同 —— 比如后台改过版本号），
    后半段 `WB.describe()` 给的是**安装包**的真实探测来源，两者对照着看
    最容易发现「线上在报兜底版本」这类问题。
    """
    ua = cprofile.ua()
    try:
        from wb_install import WB
        return f"UA={ua}｜{WB.describe()}"
    except Exception:
        return f"UA={ua}"

# ---------------------------------------------------------------------------
# 平台相关：定位 auth 目录
# ---------------------------------------------------------------------------

def auth_dirs() -> list[Path]:
    """返回按优先级排列的候选 auth 目录。

    优先级：
      1. 显式配置的 CODEBUDDY_AUTH_DIR（部署在服务器上时应始终配置它）
      2. 平台默认的桌面端数据目录
      3. 项目内 `auth/`（把凭据随项目一起部署时用）
      4. /opt/workbuddy2api/auth（历史部署路径）

    3/4 是兜底：服务器（尤其 Linux 上没装桌面端）常常既没有平台默认目录，
    也忘了配 CODEBUDDY_AUTH_DIR，结果 /gw 下所有端点 503。多列几个候选能让
    「凭据放对地方」就自动生效，而不是必须记得配环境变量。
    """
    env_dir = os.environ.get("CODEBUDDY_AUTH_DIR")
    if env_dir:
        return [Path(env_dir)]
    home = Path.home()
    plat = sys.platform
    if plat == "darwin":
        dirs = [home / "Library" / "Application Support" / "CodeBuddyExtension" / "Data" / "Public" / "auth"]
    elif plat == "win32":
        local = Path(os.environ.get("LOCALAPPDATA", home / "AppData" / "Local"))
        dirs = [local / "CodeBuddyExtension" / "Data" / "Public" / "auth"]
    else:
        xdg = Path(os.environ.get("XDG_DATA_HOME", home / ".local" / "share"))
        dirs = [xdg / "CodeBuddyExtension" / "Data" / "Public" / "auth"]
    dirs.append(Path(__file__).resolve().parent / "auth")
    dirs.append(Path("/opt/workbuddy2api/auth"))
    return dirs


def find_auth_file() -> Path | None:
    for d in auth_dirs():
        if not d.is_dir():
            continue
        # 优先用桌面端实时登录文件（无时间戳后缀），避免被历史备份按字典序抢走
        live = d / "workbuddy-desktop.info"
        if live.is_file():
            return live
        files = sorted(d.glob("*.info"))
        if files:
            return files[0]
    return None


# ---------------------------------------------------------------------------
# Auth 凭据管理（读 + 自动刷新 + 回写）
# ---------------------------------------------------------------------------

def _get_turing_device_token() -> str | None:
    """延迟取本机设备风控 Token；失败返回 None（不影响主流程）。

    放在模块级做懒加载：converter.py 既可作为 admin 的子模块被挂载，也可独立
    `python converter.py` 运行。admin 包不可用时（极少数情况）直接降级为不带该头。
    """
    try:
        from admin.turing_token import get_device_token
        return get_device_token()
    except Exception:
        return None


class CredentialManager:
    """从 auth 文件读取凭据；token 临近过期时自动刷新并回写。"""

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        self._cached: dict | None = None
        self._mtime: float = 0.0

    def _read_raw(self) -> dict:
        with open(self.path, "r", encoding="utf-8") as f:
            return json.load(f)

    def _load_if_stale(self):
        """若文件 mtime 变了（外部刷新过），重新加载缓存。"""
        try:
            mt = self.path.stat().st_mtime
        except OSError:
            return
        if self._cached is None or mt != self._mtime:
            self._cached = self._read_raw()
            self._mtime = mt

    def _session(self) -> dict:
        self._load_if_stale()
        if self._cached is None:
            raise RuntimeError(f"无法读取 auth 文件：{self.path}")
        return self._cached

    def _is_expired(self) -> bool:
        s = self._session()
        expires_at = (s.get("auth") or {}).get("expiresAt") or 0
        # 提前 60s 判定过期
        return time.time() * 1000 >= (expires_at - 60_000)

    def _refresh(self):
        """调后端刷新 token，写回 auth 文件与缓存。"""
        s = self._session()
        auth = s.get("auth") or {}
        headers = self._build_headers_from(auth, s.get("account") or {})
        headers["X-Refresh-Token"] = auth.get("refreshToken", "")
        headers["X-Auth-Refresh-Source"] = "plugin"
        url = f"{BACKEND}/v2/plugin/auth/token/refresh"
        try:
            with httpx.Client(timeout=15, limits=_HTTP_LIMITS) as c:
                r = c.post(url, headers=headers, json={})
            data = r.json()
        except Exception as e:
            raise RuntimeError(f"刷新 token 网络失败：{e}")
        if data.get("code") != 0 or not data.get("data"):
            raise RuntimeError(f"刷新 token 失败：{data.get('msg', data)}")
        new_auth = data["data"]
        # 继承部分字段
        new_auth["domain"] = new_auth.get("domain") or auth.get("domain")
        new_auth["lastRefreshTime"] = int(time.time() * 1000)
        # 计算 expiresAt（若后端没直接给）
        if not new_auth.get("expiresAt") and new_auth.get("expiresIn"):
            new_auth["expiresAt"] = int(time.time() * 1000) + new_auth["expiresIn"] * 1000
        if not new_auth.get("refreshExpiresAt") and new_auth.get("refreshExpiresIn"):
            new_auth["refreshExpiresAt"] = int(time.time() * 1000) + new_auth["refreshExpiresIn"] * 1000
        s["auth"] = new_auth
        # 原子写回
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(s, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)
        self._cached = s
        self._mtime = self.path.stat().st_mtime

    def _build_headers_from(self, auth: dict, account: dict) -> dict:
        domain = auth.get("domain") or DEFAULT_DOMAIN
        h = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": f"Bearer {auth.get('accessToken','')}",
            "X-User-Id": account.get("uid", ""),
            "X-Enterprise-Id": account.get("enterpriseId", ""),
            "X-Tenant-Id": account.get("enterpriseId", ""),
            "X-Domain": domain,
            "User-Agent": client_user_agent(),
        }
        # 风控设备头：与桌面端 Turing Shield 一致，缺失会被上游识别为异常客户端。
        # 取不到（桌面端未安装 / SDK 不支持）时优雅降级为不带该头，不影响主流程。
        tok = _get_turing_device_token()
        if tok:
            h["X-Device-Token"] = tok
        return h

    def get_headers(self, extra: dict | None = None) -> dict:
        """返回带最新 token 的后端请求 header；必要时先刷新。

        extra: 调用方（如 proxy.py）可注入的风控/审计头，例如真实客户端 IP、
               用途标识 X-Agent-Purpose 等。这些头会被 merge 到基础鉴权头之后，
               确保上游请求用量能正确显示 client 与 agentPurpose，降低被风控概率。
        """
        with self._lock:
            if self._is_expired():
                self._refresh()
            s = self._session()
            h = self._build_headers_from(s.get("auth") or {}, s.get("account") or {})
            if extra:
                h.update(extra)
            return h

    def summary(self) -> dict:
        s = self._session()
        auth = s.get("auth") or {}
        acct = s.get("account") or {}
        exp = auth.get("expiresAt", 0)
        return {
            "uid": acct.get("uid"),
            "nickname": acct.get("nickname"),
            "enterpriseName": acct.get("enterpriseName"),
            "token_expires_at": exp,
            "token_expired": self._is_expired(),
        }

    # -----------------------------------------------------------------------
    # 后端资源查询（模型列表、额度）
    # -----------------------------------------------------------------------

    def _request_backend(self, method: str, path: str, json_body: dict | None = None) -> dict:
        """向后端发一个同步请求，返回 {code, msg, requestId, data} 或抛异常。"""
        headers = self.get_headers()
        url = f"{BACKEND}{path}"
        try:
            with httpx.Client(timeout=15, limits=_HTTP_LIMITS) as c:
                if method.upper() == "GET":
                    r = c.get(url, headers=headers)
                else:
                    r = c.post(url, headers=headers, json=json_body or {})
        except Exception as e:
            raise RuntimeError(f"后端请求网络失败 {method} {path}: {e}")
        try:
            data = r.json()
        except Exception as e:
            raise RuntimeError(f"后端返回非 JSON {method} {path} HTTP {r.status_code}: {r.text[:200]}")
        if r.status_code != 200 or data.get("code") != 0:
            raise RuntimeError(f"后端请求失败 {method} {path}: HTTP {r.status_code} / {data.get('msg', data)}")
        return data

    def _request_backend_soft(self, method: str, path: str, json_body: dict | None = None) -> dict:
        """同 `_request_backend`，但后端返回业务 code!=0 时**不抛异常**，原样返回解析后的 dict。

        用于签到领取等场景：领取接口的 1001(已领)/1002(无资格)/1003(活动结束) 等业务码
        属于正常业务结果，需要由调用方根据 code 区分处理，而非当作错误抛掉。
        """
        headers = self.get_headers()
        url = f"{BACKEND}{path}"
        try:
            with httpx.Client(timeout=15, limits=_HTTP_LIMITS) as c:
                if method.upper() == "GET":
                    r = c.get(url, headers=headers)
                else:
                    r = c.post(url, headers=headers, json=json_body or {})
        except Exception as e:
            raise RuntimeError(f"后端请求网络失败 {method} {path}: {e}")
        try:
            return r.json()
        except Exception as e:
            raise RuntimeError(f"后端返回非 JSON {method} {path} HTTP {r.status_code}: {r.text[:200]}")

    def _enterprise_path_key(self) -> str:
        """返回模型列表 endpoint 里的 enterprise 段：personal 或 enterpriseId。"""
        s = self._session()
        acct = s.get("account") or {}
        if acct.get("type") == "personal":
            return "personal"
        eid = acct.get("enterpriseId")
        return eid if eid else "personal"

    @staticmethod
    def _parse_credit_multiplier(credits) -> float | None:
        """把 'x0.05' / 'x0.00 credits' 解析成浮点倍率，解析不出返回 None。"""
        if not credits:
            return None
        m = re.search(r"x\s*([0-9]+(?:\.[0-9]+)?)", str(credits))
        return float(m.group(1)) if m else None

    def fetch_models(self) -> list[dict]:
        """获取后端真实模型列表（含 id/name/credits 等元信息）。

        兼容两种返回结构：
          - 顶层 data.models 为对象数组（每项含 id/name/credits...）；
          - 仅 data.agents[].models 为字符串数组时，回退收集并去重。
        """
        eid = self._enterprise_path_key()
        data = self._request_backend("GET", f"/v2/enterprises/{eid}/models")
        payload = data.get("data", {})
        models = payload.get("models")
        if isinstance(models, list) and models:
            return models
        # 兜底：从 agents 里收集模型名
        collected: list[dict] = []
        for a in payload.get("agents", []) or []:
            for m in a.get("models", []) or []:
                if isinstance(m, dict) and m.get("id"):
                    collected.append(m)
                elif isinstance(m, str) and m:
                    collected.append({"id": m})
        seen = set()
        result: list[dict] = []
        for m in collected:
            mid = m.get("id")
            if mid and mid not in seen:
                seen.add(mid)
                result.append(m)
        if not result:
            raise RuntimeError("后端模型列表格式异常：缺少 data.models 且 agents 中无模型")
        return result

    def fetch_models_raw(self) -> dict:
        """取后端模型目录的**原始响应**（保留 models 全字段 + agents 名单）。

        fetch_models() 只回 models 数组，丢掉了 agents 名单（cli agent 才是官方
        CLI 实际可选的模型集合）与 reasoning/supportsImages 等能力字段。
        能力层需要完整结构，故单独提供本方法。
        """
        eid = self._enterprise_path_key()
        return self._request_backend("GET", f"/v2/enterprises/{eid}/models")

    def generate_image(self, body: dict, timeout: float = 300.0) -> dict:
        """调后端生图接口 /v2/images/generations（同步返回）。

        生图耗时远高于普通 RPC（实测数秒~数十秒），故不复用 _request_backend 的 15s 超时。

        后端返回形态（实测）：
          {"code":0,"msg":"OK","requestId":"...",
           "data":{"created":..., "data":[{"url":"...","revised_prompt":"..."}],
                   "usage":{"output_image_counts":N,"credit":X}}}
        """
        headers = self.get_headers()
        url = f"{BACKEND}/v2/images/generations"
        try:
            with httpx.Client(timeout=timeout, limits=_HTTP_LIMITS) as c:
                r = c.post(url, headers=headers, json=body)
        except Exception as e:
            raise RuntimeError(f"生图请求网络失败：{e}")
        try:
            data = r.json()
        except Exception:
            raise RuntimeError(f"生图返回非 JSON HTTP {r.status_code}: {r.text[:200]}")
        if r.status_code != 200 or data.get("code") != 0:
            raise RuntimeError(f"生图失败 HTTP {r.status_code} / {data.get('msg', data)}")
        return data

    def fetch_balance(self) -> dict:
        """获取当前账号积分汇总，仅返回总量与剩余（可用积分）。

        注意：必须使用 CycleCapacityRemain（当前周期剩余），而不是 CapacityRemain。
        CodeBuddy 个人体验版等一次性资源在账号层级 CapacityRemain 仍显示原始额度，
        但当期已用完后 CycleCapacityRemain 为 0；界面上的「累积剩余」也以周期剩余为准。
        """
        data = self._request_backend("POST", "/v2/billing/meter/get-user-resource", {})
        resp = data.get("data", {}).get("Response", {}).get("Data", {}) or {}
        total = 0
        total_size = 0
        for a in resp.get("Accounts") or []:
            if a.get("CapacityUnit") != "credits":
                continue
            # 当前周期剩余才是真实可用额度
            total += a.get("CycleCapacityRemain") or 0
            total_size += a.get("CapacitySize") or 0
        return {
            "total": total_size,    # 总积分
            "remain": total,        # 可用积分（当前周期剩余额度）
        }


# ---------------------------------------------------------------------------
# 模型列表
# ---------------------------------------------------------------------------

# 静态兜底模型列表（仅在**取不到上游实时目录**时使用）。
# 内容对齐实测的上游 cli agent 名单（2026-09 实测），不再用手写的过时清单。
DEFAULT_MODELS = [
    "auto", "hy4-preview", "hy3", "hy3-x",
    "deepseek-v4.1-flash", "deepseek-v4-pro", "deepseek-v4-flash",
    "glm-5.3", "glm-5.3-flash", "glm-5.2", "glm-5.1", "glm-5v-turbo",
    "kimi-k3-1", "kimi-k2.8-preview", "kimi-k2.7", "kimi-k2.6",
    "minimax-m3",
]

# 后端资源缓存（TTL，秒）
_RESOURCE_CACHE_TTL = 60.0
_MODELS_CACHE = {"ts": 0.0, "data": None, "error": None}
_BALANCE_CACHE = {"ts": 0.0, "data": None, "error": None}

# 后端请求体里出现过的额外字段（透传时若客户端给了就保留）
#
# thinking / enable_thinking 必须列入：三方客户端（如 mirai-mifan）用
# thinking={"type":"enabled"} + enable_thinking=true 表达「开启思考」，
# 而它们的默认推理强度是空（「自动」），此时**只发这两个字段**。
# 若在此被丢弃，请求到达上游时既无 reasoning_effort 也无 thinking，
# 上游便按默认不思考 → 客户端「开了思考却没有思考内容」。
# 这两个字段本身上游不直接识别（thinking.enabled 会被忽略），
# 由 upstream_compat 的思考开关翻译层转成扁平 reasoning_effort；
# thinking.disabled 则是上游唯一认可的关闭方式，必须原样放行。
PASSTHROUGH_BODY_KEYS = {
    "model", "messages", "tools", "tool_choice", "temperature",
    "max_tokens", "max_completion_tokens", "top_p", "stream",
    "stream_options", "stop", "presence_penalty", "frequency_penalty",
    "n", "response_format", "seed", "user", "reasoning_effort",
    "verbosity", "reasoning_summary",
    "thinking", "enable_thinking",
}

# ---------------------------------------------------------------------------
# FastAPI 应用
# ---------------------------------------------------------------------------

# 文档默认关闭 —— 与主后台同一开关。挂在 /gw 下时它同样是公网可达的
# 「完整攻击面清单」（/gw/openapi.json 会列出所有端点与参数 schema）。
_ENABLE_DOCS = os.getenv("ADMIN_ENABLE_DOCS", "0") == "1"
app = FastAPI(title="codebuddy2openai", version="2.0",
              docs_url="/docs" if _ENABLE_DOCS else None,
              redoc_url="/redoc" if _ENABLE_DOCS else None,
              openapi_url="/openapi.json" if _ENABLE_DOCS else None)
CONFIG: dict = {"api_key": "", "cred": None, "log_path": None,
                "desensitize": False, "no_compact": False}  # cred: CredentialManager | None


# ---------------------------------------------------------------------------
# 日志（写文件）
# ---------------------------------------------------------------------------

_LOG_LOCK = threading.Lock()


def _log(msg: str):
    """写一行日志到 CONFIG['log_path'] 指定的文件（追加，带时间戳）。未设置则丢弃。"""
    path = CONFIG.get("log_path")
    if not path:
        return
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n"
    try:
        with _LOG_LOCK:
            with open(path, "a", encoding="utf-8") as f:
                f.write(line)
    except OSError:
        pass  # 日志失败不应影响主流程




def _truncate(s: str, n: int = 80) -> str:
    s = str(s).replace("\n", " ").strip()
    return s[:n] + ("…" if len(s) > n else "")


def _check_auth(authorization: Optional[str], x_api_key: Optional[str]):
    key = CONFIG["api_key"]
    if not key:
        return
    token = ""
    if authorization and authorization.startswith("Bearer "):
        token = authorization[7:].strip()
    if not token and x_api_key:
        token = x_api_key
    if token != key:
        raise HTTPException(status_code=401, detail={"error": {"message": "invalid api key", "type": "auth_error"}})


def _cred() -> CredentialManager:
    if CONFIG["cred"] is None:
        raise HTTPException(status_code=503, detail={"error": {"message": "未找到登录凭据，请先在桌面端登录 CodeBuddy/WorkBuddy", "type": "auth_error"}})
    return CONFIG["cred"]


def _cached_models(cred) -> list[dict]:
    """带 TTL 缓存的真实模型列表；失败时抛异常由调用方回退。"""
    global _MODELS_CACHE
    now = time.time()
    if _MODELS_CACHE["data"] is not None and now - _MODELS_CACHE["ts"] < _RESOURCE_CACHE_TTL:
        return _MODELS_CACHE["data"]
    try:
        models = cred.fetch_models()
    except Exception as e:
        _MODELS_CACHE["error"] = str(e)
        raise
    _MODELS_CACHE = {"ts": now, "data": models, "error": None}
    return models


# 规范化后的模型目录缓存（含能力元数据）。与 _MODELS_CACHE 同 TTL，
# 但存的是 upstream_compat.normalize_catalog 的结果，供 /v1/models 与
# reasoning_effort 归一使用。
_CATALOG_CACHE = {"ts": 0.0, "data": None}


def _cached_catalog(cred) -> dict:
    """带 TTL 缓存的规范化模型目录：{chat: [...], image: [...], all_ids: [...]}。

    失败时抛异常，由调用方回退（绝不缓存半成品）。
    """
    global _CATALOG_CACHE
    now = time.time()
    if _CATALOG_CACHE["data"] is not None and now - _CATALOG_CACHE["ts"] < _RESOURCE_CACHE_TTL:
        return _CATALOG_CACHE["data"]
    raw = cred.fetch_models_raw()
    catalog = upstream_compat.normalize_catalog(raw)
    _CATALOG_CACHE = {"ts": now, "data": catalog}
    return catalog


def _supported_efforts_for(cred, model: str) -> list[str] | None:
    """取该模型上游**实时声明**的 reasoning 档位；取不到返回 None（不做任何降级）。

    注意：刻意不用静态兜底表。实测本账号 deepseek-v4.1-flash 上游声明
    supportedEfforts=["high"]，但实际 low/medium/high/xhigh/max/minimal
    全部 200 且有思维链——用静态表会把 max 硬降成 high，正是要修的 bug。
    因此这里只信实时目录，未知即透传。
    """
    if not model:
        return None
    try:
        for m in _cached_catalog(cred)["chat"]:
            if m["id"] == model and m["supports_efforts"]:
                return m["supports_efforts"]
    except Exception as e:
        _log(f"读取模型能力失败（{model}），reasoning_effort 将原样透传：{e}")
    return None


def _cached_balance(cred) -> dict:
    """带 TTL 缓存的真实积分额度；失败时抛异常由调用方回退。"""
    global _BALANCE_CACHE
    now = time.time()
    if _BALANCE_CACHE["data"] is not None and now - _BALANCE_CACHE["ts"] < _RESOURCE_CACHE_TTL:
        return _BALANCE_CACHE["data"]
    try:
        balance = cred.fetch_balance()
    except Exception as e:
        _BALANCE_CACHE["error"] = str(e)
        raise
    _BALANCE_CACHE = {"ts": now, "data": balance, "error": None}
    return balance


@app.get("/health")
def health(authorization: Optional[str] = Header(default=None),
           x_api_key: Optional[str] = Header(default=None, alias="X-Api-Key")):
    """探活。**必须鉴权**，且只回最小字段。

    安全审计「高风险」项：原实现既不调用 `_check_auth()`，又把
    `auth_file`（服务器绝对路径）、`credential`（账号 uid + **手机号**昵称）
    与 `balance`（积分余额）一起返回给**任何匿名访问者**。
    这些是画像/社工/横向移动的优质素材，且同一文件里的 `/v1/models`、
    `/v1/balance` 本来就是校验的 —— 唯独它漏了。

    现在：未带正确 Key → 401；带了也只回「活着 + 凭据是否加载」，
    不再暴露路径、账号标识与余额。
    """
    _check_auth(authorization, x_api_key)
    return {
        "status": "ok",
        "platform": sys.platform,
        "python": sys.version.split()[0],
        "credential_loaded": CONFIG["cred"] is not None,
        "mode": "direct-proxy (native function calling)",
    }


@app.get("/v1/models")
def list_models(authorization: Optional[str] = Header(default=None),
                x_api_key: Optional[str] = Header(default=None, alias="X-Api-Key")):
    _check_auth(authorization, x_api_key)
    cred = CONFIG["cred"]
    if cred is not None:
        try:
            catalog = _cached_catalog(cred)
            data = [_model_entry(m, kind="chat") for m in catalog["chat"]]
            data += [_model_entry(m, kind="image") for m in catalog["image"]]
            if data:
                return {"object": "list", "data": data, "source": "backend"}
        except Exception as e:
            _log(f"获取真实模型列表失败，回退到 DEFAULT_MODELS: {e}")
    data = [{"id": m, "object": "model", "created": 1700000000, "owned_by": "codebuddy"}
            for m in DEFAULT_MODELS]
    return {"object": "list", "data": data, "source": "fallback"}


def _model_entry(m: dict, kind: str = "chat") -> dict:
    """把规范化模型条目转成 /v1/models 的输出条目（能力字段全量透出）。"""
    entry = {
        "id": m["id"],
        "object": "model",
        "created": 1700000000,
        "owned_by": "codebuddy",
        "name": m.get("name") or m.get("id"),
        "kind": kind,
        "credits": m.get("credits"),
        "credit_multiplier": _parse_credit_multiplier_safe(m.get("credits")),
        "description": m.get("description"),
        "vendor": m.get("vendor"),
        "tags": m.get("tags") or [],
        "context_length": m.get("context_window"),
        "max_output_tokens": m.get("max_output_tokens"),
        "supports_images": m.get("supports_images"),
        "supports_reasoning": m.get("supports_reasoning"),
        "supports_tool_call": m.get("supports_tool_call"),
        "only_reasoning": m.get("only_reasoning"),
        "can_disable_thinking": m.get("can_disable_thinking"),
    }
    # 档位能力：上游**实时**声明才输出；没有就整个字段省略（不输出空数组）
    efforts = m.get("supports_efforts") or []
    if efforts:
        entry["reasoning_supported_efforts"] = efforts
        if m.get("default_effort"):
            entry["reasoning_default_effort"] = m["default_effort"]
    if m.get("icon_url"):
        entry["icon_url"] = m["icon_url"]
    return entry


def _parse_credit_multiplier_safe(credits):
    try:
        return CredentialManager._parse_credit_multiplier(credits)
    except Exception:
        return None


@app.get("/v1/balance")
def get_balance(authorization: Optional[str] = Header(default=None),
                x_api_key: Optional[str] = Header(default=None, alias="X-Api-Key")):
    _check_auth(authorization, x_api_key)
    cred = _cred()
    try:
        return {"object": "balance", "source": "backend", **_cached_balance(cred)}
    except Exception as e:
        raise HTTPException(status_code=502, detail={"error": {"message": f"获取额度失败：{e}", "type": "upstream_error"}})


# ---------------------------------------------------------------------------
# 生图端点（OpenAI Images API 兼容）
# ---------------------------------------------------------------------------

# 默认生图模型：上游目录里 tags 含 text-to-image 的那一个。
DEFAULT_IMAGE_MODEL = "hunyuan-image-v3.0"


def _pick_image_model(cred, requested: str | None) -> str:
    """选生图模型：客户端指定则用指定的，否则用目录里的第一个生图模型。"""
    if requested:
        return requested
    try:
        img = _cached_catalog(cred)["image"]
        if img:
            return img[0]["id"]
    except Exception as e:
        _log(f"读取生图模型列表失败，回退 {DEFAULT_IMAGE_MODEL}: {e}")
    return DEFAULT_IMAGE_MODEL


@app.post("/v1/images/generations")
async def images_generations(request: Request,
                             authorization: Optional[str] = Header(default=None),
                             x_api_key: Optional[str] = Header(default=None, alias="X-Api-Key")):
    """OpenAI Images API 兼容端点：POST /v1/images/generations。

    上游实测可用端点：POST /v2/images/generations（返回 data[].url）。
    本端点把 OpenAI 的入参映射为上游入参，再把上游返回规整成 OpenAI 形态。
    """
    _check_auth(authorization, x_api_key)
    cred = _cred()

    try:
        payload = await request.json()
    except Exception as e:
        raise HTTPException(status_code=400, detail={"error": {"message": f"bad json: {e}", "type": "invalid_request_error"}})

    prompt = payload.get("prompt")
    if not prompt:
        raise HTTPException(status_code=400, detail={
            "error": {"message": "prompt is required", "type": "invalid_request_error"}})

    model = _pick_image_model(cred, payload.get("model"))
    n = payload.get("n") or 1
    rid = os.urandom(4).hex()
    _log(f"[{rid}] ▶ IMAGES {model} | n={n} | prompt={_truncate(str(prompt), 60)!r}")

    # OpenAI → 上游入参映射。上游只认 prompt/model/部分尺寸字段，
    # 未识别字段会被忽略；这里只透传确定被支持的。
    body: dict = {"prompt": prompt, "model": model}
    for k in ("size", "width", "height", "negative_prompt", "seed"):
        if payload.get(k) is not None:
            body[k] = payload[k]
    if n and int(n) > 1:
        body["n"] = int(n)

    t0 = time.time()
    try:
        result = cred.generate_image(body)
    except Exception as e:
        _log(f"[{rid}] ✗ IMAGES {model} | {e}")
        raise HTTPException(status_code=502, detail={
            "error": {"message": f"生图失败：{e}", "type": "upstream_error"}})

    data = result.get("data") or {}
    items = data.get("data") or []
    usage = data.get("usage") or {}
    created = data.get("created") or int(time.time())

    out = []
    for it in items:
        if not isinstance(it, dict):
            continue
        entry = {"url": it.get("url")}
        if it.get("revised_prompt"):
            entry["revised_prompt"] = it["revised_prompt"]
        # b64_json 请求：上游只给 url，这里不自行下载转码（避免大对象与额外耗时），
        # 客户端需要 base64 时请自行取 url。URL 存在性由 response_format 提示。
        out.append(entry)

    _log(f"[{rid}] ◀ IMAGES {model} | {time.time()-t0:.1f}s | images={len(out)}"
         f" | credit={usage.get('credit')}")
    return {
        "created": created,
        "data": out,
        "usage": usage,
        "model": model,
    }


@app.get("/v1/images/models")
def images_models(authorization: Optional[str] = Header(default=None),
                  x_api_key: Optional[str] = Header(default=None, alias="X-Api-Key")):
    """列出可用的生图模型（上游 tags 含 text-to-image）。"""
    _check_auth(authorization, x_api_key)
    cred = CONFIG["cred"]
    if cred is None:
        return {"object": "list", "data": [{"id": DEFAULT_IMAGE_MODEL, "object": "model"}],
                "source": "fallback"}
    try:
        catalog = _cached_catalog(cred)
        data = [_model_entry(m, kind="image") for m in catalog["image"]]
        if data:
            return {"object": "list", "data": data, "source": "backend"}
    except Exception as e:
        _log(f"获取生图模型列表失败，回退默认: {e}")
    return {"object": "list", "data": [{"id": DEFAULT_IMAGE_MODEL, "object": "model"}],
            "source": "fallback"}


@app.post("/v1/chat/completions")
async def chat_completions(request: Request,
                           authorization: Optional[str] = Header(default=None),
                           x_api_key: Optional[str] = Header(default=None, alias="X-Api-Key")):
    _check_auth(authorization, x_api_key)
    cred = _cred()

    try:
        payload = await request.json()
    except Exception as e:
        raise HTTPException(status_code=400, detail={"error": {"message": f"bad json: {e}", "type": "invalid_request_error"}})

    messages = payload.get("messages") or []
    if not messages:
        raise HTTPException(status_code=400, detail={"error": {"message": "messages is required", "type": "invalid_request_error"}})

    # 构造后端 body：只透传已知的合法字段
    client_wants_stream = bool(payload.get("stream"))
    body = {k: payload[k] for k in PASSTHROUGH_BODY_KEYS if k in payload}
    body.setdefault("model", "auto")

    # 协议兼容层：role/tool_choice 归一、孤儿 tool_call 清理、思考开关翻译、
    # 档位归一、reasoning_content 回填、（可选）指纹脱敏。详见 upstream_compat.py。
    # 档位归一只依据上游**实时**能力，取不到就原样透传（不猜测性降级）。
    # 思考开关只翻译客户端已表达的意图，未传则不开启思考。
    supported = _supported_efforts_for(cred, body.get("model"))
    upstream_compat.prepare_upstream_body(
        body,
        sanitize=bool(CONFIG.get("desensitize")),
        supported_efforts=supported,
    )

    # 日志：请求摘要
    model_name = payload.get("model", "auto")
    tool_names = [t.get("function", {}).get("name") for t in (payload.get("tools") or [])
                  if isinstance(t, dict)]
    last_user = _last_user_text(messages)
    rid = os.urandom(4).hex()
    _log(f"[{rid}] ▶ REQUEST {model_name} | stream={client_wants_stream} | msgs={len(messages)}"
         + (f" | tools={tool_names}" if tool_names else "")
         + (f" | last_user={_truncate(last_user, 60)!r}" if last_user else ""))
    # 完整请求体（发往后端的实际内容；若启用脱敏，这里已是脱敏后）
    _log(f"[{rid}] ── REQUEST BODY (发往后端) ──\n{json.dumps(body, ensure_ascii=False, indent=2)}")

    headers = cred.get_headers()
    headers.update(_client_ip_headers(request))
    url = f"{BACKEND}/v2/chat/completions"
    t0 = time.time()

    if client_wants_stream:
        return StreamingResponse(
            _stream_upstream(url, headers, body, model_name, t0, rid),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # 非流式：后端只支持流式，这里把后端 SSE 聚合成单个 chat.completion 响应
    try:
        async with httpx.AsyncClient(timeout=_stream_timeout(), limits=_HTTP_LIMITS) as c:
            async with c.stream("POST", url, headers=headers, json=body) as r:
                if r.status_code != 200:
                    raw = await r.aread()
                    _log(f"[{rid}] ✗ HTTP {r.status_code} | {model_name} | {_truncate(raw.decode('utf-8','replace'),200)}")
                    _log(f"[{rid}] ── ERROR BODY ──\n{raw.decode('utf-8','replace')}")
                    raise HTTPException(status_code=r.status_code, detail=_safe_err_raw(raw, r.status_code))
                collected = await _collect_stream(r)
    except HTTPException:
        raise
    except httpx.HTTPError as e:
        _log(f"[{rid}] ✗ 网络错误 | {model_name} | {e}")
        raise HTTPException(status_code=502, detail={"error": {"message": f"upstream error: {e}", "type": "upstream_error"}})
    _log_finish(model_name, t0, collected, rid)
    return JSONResponse(content=collected)


def _last_user_text(messages: list) -> str:
    """取最后一条 user 消息的文本，用于日志预览。"""
    for m in reversed(messages):
        if m.get("role") != "user":
            continue
        content = m.get("content", "")
        if isinstance(content, list):
            for blk in content:
                if isinstance(blk, dict) and blk.get("type") == "text":
                    return str(blk.get("text", ""))
            return ""
        return str(content)
    return ""


def _log_finish(model_name: str, t0: float, result: dict, rid: str = ""):
    """记录一次完成的请求：耗时 / finish_reason / usage / 工具调用 / 审核拦截 + 完整响应。"""
    elapsed = time.time() - t0
    prefix = f"[{rid}] " if rid else ""
    choice = (result.get("choices") or [{}])[0]
    finish = choice.get("finish_reason")
    msg = choice.get("message") or {}
    tcs = msg.get("tool_calls") or []
    usage = result.get("usage") or {}
    tag = ""
    if finish == "content-filter":
        tag = " ⚠️内容审核拦截"
    tc_names = [t.get("function", {}).get("name") for t in tcs]
    _log(f"{prefix}◀ RESPONSE {model_name} | {elapsed:.1f}s | finish={finish}{tag}"
         + (f" | tool_calls={tc_names}" if tc_names else "")
         + f" | tokens={usage.get('total_tokens', '?')}")
    # 完整响应体
    _log(f"{prefix}── RESPONSE BODY ──\n{json.dumps(result, ensure_ascii=False, indent=2)}")


async def _collect_stream(response: httpx.Response) -> dict:
    """消费后端的 OpenAI SSE 流，聚合成单个非流式 chat.completion 对象。

    合并所有 chunk 的 delta（content / reasoning_content / tool_calls），
    并取 usage / finish_reason。

    注意：上游会把思维链放在 delta.reasoning_content 里（实测 deepseek 系带
    reasoning_effort 时必吐）。早期版本只读 delta.content，导致非流式请求
    的思考内容被整段丢弃——这里显式保留。
    """
    content_parts: list[str] = []
    reasoning_parts: list[str] = []
    # tool_calls: index -> {id, name, arguments(分片拼接)}
    tool_calls: dict[int, dict] = {}
    model: str | None = None
    finish_reason: str | None = None
    usage: dict | None = None
    # 兼容上游偶发返回 message（而非 delta）的形态
    saw_content_delta = False

    async for line in response.aiter_lines():
        line = line.strip()
        if not line or not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            break
        try:
            chunk = json.loads(data)
        except json.JSONDecodeError:
            continue
        model = chunk.get("model") or model
        if chunk.get("usage"):
            usage = chunk["usage"]
        for choice in chunk.get("choices") or []:
            if choice.get("finish_reason"):
                finish_reason = choice["finish_reason"]
            delta = choice.get("delta") or {}
            if delta.get("content"):
                content_parts.append(delta["content"])
                saw_content_delta = True
            # 思维链：与 content 同等对待，原样保留
            if delta.get("reasoning_content"):
                reasoning_parts.append(delta["reasoning_content"])
            # 兜底：上游非 delta 形态（只在从未收到 content delta 时）
            msg = choice.get("message") or {}
            if not saw_content_delta and msg.get("content"):
                content_parts.append(msg["content"])
            if not reasoning_parts and msg.get("reasoning_content"):
                reasoning_parts.append(msg["reasoning_content"])
            for tc in delta.get("tool_calls") or []:
                idx = tc.get("index", 0)
                slot = tool_calls.setdefault(idx, {"id": None, "name": None, "arguments": ""})
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

    # 输出阶段（length 截断）剔除参数不完整的工具调用，避免客户端解析到半个 JSON
    message = {"role": "assistant", "content": "".join(content_parts) or None}
    if tcs:
        message["tool_calls"] = tcs
        if finish_reason == "length":
            dropped = upstream_compat.drop_truncated_tool_calls(message)
            if dropped:
                tcs = message.get("tool_calls")
                _log(f"剔除 {dropped} 个参数截断的 tool_call（finish_reason=length）")
    reasoning = "".join(reasoning_parts)
    if reasoning:
        message["reasoning_content"] = reasoning
    return {
        "id": "chatcmpl-" + os.urandom(12).hex(),
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model or "unknown",
        "choices": [{"index": 0, "message": message,
                     "finish_reason": finish_reason or "stop"}],
        "usage": usage or {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


def _safe_err_raw(raw: bytes, status: int) -> dict:
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except Exception:
        return {"error": {"message": raw.decode("utf-8", "replace")[:500], "type": "upstream_error", "code": status}}


async def _stream_upstream(url: str, headers: dict, body: dict,
                           model_name: str = "?", t0: float = 0.0, rid: str = ""):
    """把后端 SSE 原样转发给客户端（后端已是标准 OpenAI SSE，含 tool_calls）。

    同时轻量解析流，统计 finish_reason / tool_calls / usage 用于日志，不阻塞转发。
    完整原始 SSE 累积后落盘到日志（调试用）。
    """
    finish_reason = None
    tool_names: list[str] = []
    usage: dict = {}
    saw_filter = False
    buf = b""
    raw_parts: list[bytes] = []   # 累积完整原始 SSE
    prefix = f"[{rid}] " if rid else ""

    def _feed(chunk: bytes):
        nonlocal finish_reason, saw_filter, buf
        # 行缓冲解析：把累计的 chunk 按 data: 行切出来统计
        buf += chunk
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            line = line.strip()
            if not line.startswith(b"data:"):
                continue
            data = line[5:].strip()
            if data == b"[DONE]":
                continue
            try:
                obj = json.loads(data)
            except Exception:
                continue
            if obj.get("usage"):
                usage.update(obj["usage"])
            for ch in obj.get("choices") or []:
                if ch.get("finish_reason"):
                    finish_reason = ch["finish_reason"]
                for tc in (ch.get("delta") or {}).get("tool_calls") or []:
                    nm = (tc.get("function") or {}).get("name")
                    if nm:
                        tool_names.append(nm)
            # 内容审核拦截常以 content-filter 或特殊中文文案返回
            try:
                text_repr = data.decode("utf-8", "replace")
            except Exception:
                text_repr = ""
            if "content-filter" in text_repr or "敏感" in text_repr or "审核" in text_repr:
                saw_filter = True

    try:
        # 原来是 timeout=None（完全不设限）：连接一旦静默死掉就永久挂住，
        # 既不报错也不归还资源。改成静默超时 —— 有数据就续期、静默到上限才判死，
        # 等价于参考实现 internal/upstream/idle.go 的空闲监控，且不设总时长。
        async with httpx.AsyncClient(timeout=_stream_timeout(), limits=_HTTP_LIMITS) as c:
            async with c.stream("POST", url, headers=headers, json=body) as r:
                if r.status_code != 200:
                    err = await r.aread()
                    _log(f"{prefix}✗ HTTP {r.status_code} | {model_name} | {_truncate(err.decode('utf-8','replace'),200)}")
                    _log(f"{prefix}── ERROR BODY ──\n{err.decode('utf-8','replace')}")
                    yield _err_event(err, r.status_code)
                    return
                async for chunk in r.aiter_bytes():
                    if chunk:
                        raw_parts.append(chunk)
                        _feed(chunk)
                        yield chunk
    except httpx.HTTPError as e:
        _log(f"{prefix}✗ 网络错误 | {model_name} | {e}")
        yield _err_event(str(e).encode(), 502)

    # 流结束：输出完成日志
    elapsed = time.time() - t0 if t0 else 0
    tag = " ⚠️内容审核拦截" if (saw_filter or finish_reason == "content-filter") else ""
    _log(f"{prefix}◀ RESPONSE {model_name} | {elapsed:.1f}s | stream finish={finish_reason}{tag}"
         + (f" | tool_calls={tool_names}" if tool_names else "")
         + f" | tokens={usage.get('total_tokens', '?')}")
    # 完整原始 SSE（后端返回的全部内容）
    _log(f"{prefix}── RESPONSE RAW SSE ──\n{b''.join(raw_parts).decode('utf-8','replace')}")


def _safe_err(r: httpx.Response) -> dict:
    try:
        return {"error": r.json()}
    except Exception:
        return {"error": {"message": r.text[:500], "type": "upstream_error", "code": r.status_code}}


def _err_event(msg: bytes, status: int) -> bytes:
    # 以 OpenAI SSE 错误 chunk 形式返回
    import json as _json, time as _time
    chunk = {
        "error": {"message": msg.decode("utf-8", "replace")[:500], "type": "upstream_error", "code": status},
    }
    return f"data: {_json.dumps(chunk, ensure_ascii=False)}\n\n".encode("utf-8")


def _looks_like_content_filter_text(text: str) -> bool:
    text = (text or "").lower()
    return (
        "content-filter" in text
        or "content_filter" in text
        or "敏感内容" in text
        or "内容审核" in text
        or "无法响应您的请求" in text
    )


def _chat_body_desensitize(body: dict, *, force_compact: bool = False) -> dict:
    if not CONFIG.get("desensitize"):
        return body
    return desensitize_body(
        body,
        roles=("system", "developer"),
        desensitize_harness_user=True,
        desensitize_tools=True,
        compact_harness=(force_compact or not CONFIG.get("no_compact")),
        strip_tool_metadata=True,
    )


async def _post_backend_once(url: str, headers: dict, body: dict) -> tuple[int, bytes]:
    # 这也是在消费一个 SSE 流（只是聚合成一次性字节），所以用同一套超时：
    # connect 短、read 管静默、**不设总时长**。原来的 timeout=120 会把 connect
    # 也设成 120s，遇到连不上的节点就一直挂着不换号。
    async with httpx.AsyncClient(timeout=_stream_timeout(), limits=_HTTP_LIMITS) as c:
        async with c.stream("POST", url, headers=headers, json=body) as r:
            chunks: list[bytes] = []
            async for chunk in r.aiter_bytes():
                if chunk:
                    chunks.append(chunk)
            return r.status_code, b"".join(chunks)


async def _post_backend_with_filter_retry(url: str, headers: dict, body: dict,
                                          rid: str = "", model_name: str = "?") -> tuple[int, bytes, dict]:
    prefix = f"[{rid}] " if rid else ""
    status, raw = await _post_backend_once(url, headers, body)
    text = raw.decode("utf-8", "replace")
    if status == 200 and _looks_like_content_filter_text(text) and CONFIG.get("desensitize") and CONFIG.get("no_compact"):
        retry_body = _chat_body_desensitize(body, force_compact=True)
        _log(f"{prefix}↻ RESPONSES {model_name} | content filter detected, retry with compact harness")
        _log(f"{prefix}── RESPONSES RETRY CHAT BODY ──\n{json.dumps(retry_body, ensure_ascii=False, indent=2)}")
        retry_status, retry_raw = await _post_backend_once(url, headers, retry_body)
        retry_text = retry_raw.decode("utf-8", "replace")
        if retry_status == 200 and not _looks_like_content_filter_text(retry_text):
            return retry_status, retry_raw, retry_body
    return status, raw, body


# ---------------------------------------------------------------------------
# Responses API 端点（Codex CLI 兼容）
# ---------------------------------------------------------------------------

@app.post("/v1/responses")
async def create_response(request: Request,
                          authorization: Optional[str] = Header(default=None),
                          x_api_key: Optional[str] = Header(default=None, alias="X-Api-Key")):
    """OpenAI Responses API 兼容端点。

    Codex CLI 使用 Responses API（wire_api = "responses"）而非 Chat Completions。
    本端点接收 Responses 格式请求，转换为 Chat 格式发往后端，再将后端的 Chat SSE
    转换为 Responses 语义事件流返回。
    """
    _check_auth(authorization, x_api_key)
    cred = _cred()

    try:
        payload = await request.json()
    except Exception as e:
        raise HTTPException(status_code=400, detail={"error": {"message": f"bad json: {e}", "type": "invalid_request_error"}})

    # 转换请求：Responses → Chat
    try:
        chat_body = responses_request_to_chat(payload)
    except Exception as e:
        raise HTTPException(status_code=400, detail={"error": {"message": f"request conversion error: {e}", "type": "invalid_request_error"}})

    chat_body, projection_stats = project_responses_chat_body(chat_body)
    chat_body.setdefault("model", "auto")

    # 协议兼容层（与 chat 端点共用同一套改写）：role/tool_choice 归一、
    # 孤儿 tool_call 清理、思考开关翻译、档位归一、reasoning_content 回填、可选脱敏。
    supported = _supported_efforts_for(cred, chat_body.get("model"))
    upstream_compat.prepare_upstream_body(
        chat_body,
        sanitize=bool(CONFIG.get("desensitize")),
        supported_efforts=supported,
    )

    client_wants_stream = payload.get("stream", True)  # Codex CLI 默认 stream
    model_name = payload.get("model", "auto")
    rid = os.urandom(4).hex()
    _log(f"[{rid}] ▶ RESPONSES {model_name} | stream={client_wants_stream} | input_items={len(payload.get('input', []))}")
    _log(
        f"[{rid}] ── RESPONSES PROJECTION ── "
        f"mode={projection_stats.get('mode')} "
        f"| msgs {projection_stats.get('original_messages')}→{projection_stats.get('projected_messages')} "
        f"| chars {projection_stats.get('original_message_chars')}→{projection_stats.get('projected_message_chars')} "
        f"| tools {projection_stats.get('original_tools')}→{projection_stats.get('projected_tools')} "
        f"| tool_chars {projection_stats.get('original_tool_chars')}→{projection_stats.get('projected_tool_chars')} "
        f"| summarized_history={projection_stats.get('summarized_history_messages', 0)} "
        f"| dropped_harness={projection_stats.get('dropped_harness_messages', 0)} "
        f"| anchor_user={projection_stats.get('anchor_user_preserved', False)}"
    )
    _log(f"[{rid}] ── RESPONSES → CHAT BODY ──\n{json.dumps(chat_body, ensure_ascii=False, indent=2)}")

    headers = cred.get_headers()
    headers.update(_client_ip_headers(request))
    url = f"{BACKEND}/v2/chat/completions"
    t0 = time.time()

    if client_wants_stream:
        return StreamingResponse(
            _stream_responses(url, headers, chat_body, model_name, t0, rid),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # 非流式：聚合后端 SSE → 非流式 Response 对象
    try:
        status_code, raw, final_body = await _post_backend_with_filter_retry(url, headers, chat_body, rid, model_name)
        if status_code != 200:
            _log(f"[{rid}] ✗ HTTP {status_code} | {model_name} | {_truncate(raw.decode('utf-8','replace'),200)}")
            raise HTTPException(status_code=status_code, detail=_safe_err_raw(raw, status_code))
        converter = ResponsesStreamConverter(model=model_name)
        for line in raw.decode("utf-8", "replace").splitlines():
            converter.feed_line(line)
        chat_body = final_body
    except HTTPException:
        raise
    except httpx.HTTPError as e:
        _log(f"[{rid}] ✗ 网络错误 | {model_name} | {e}")
        raise HTTPException(status_code=502, detail={"error": {"message": f"upstream error: {e}", "type": "upstream_error"}})

    result = converter.get_nonstream_response()
    elapsed = time.time() - t0
    _log(f"[{rid}] ◀ RESPONSES {model_name} | {elapsed:.1f}s")
    _log(f"[{rid}] ── RESPONSE OBJ ──\n{json.dumps(result, ensure_ascii=False, indent=2)}")
    return JSONResponse(content=result)


async def _stream_responses(url: str, headers: dict, body: dict,
                            model_name: str = "?", t0: float = 0.0, rid: str = ""):
    """消费后端 Chat SSE，实时转换为 Responses API 事件流输出。"""
    converter = ResponsesStreamConverter(model=model_name)
    prefix = f"[{rid}] " if rid else ""

    try:
        status_code, raw, _ = await _post_backend_with_filter_retry(url, headers, body, rid, model_name)
        if status_code != 200:
            _log(f"{prefix}✗ HTTP {status_code} | {model_name} | {_truncate(raw.decode('utf-8','replace'),200)}")
            error_evt = {"type": "error", "error": {"message": raw.decode('utf-8','replace')[:500], "code": status_code}}
            yield f"data: {json.dumps(error_evt, ensure_ascii=False)}\n\n".encode("utf-8")
            return
        raw_sse_lines = []
        for line in raw.decode("utf-8", "replace").splitlines():
            if line.strip():
                raw_sse_lines.append(line)
            events = converter.feed_line(line)
            if events:
                yield events.encode("utf-8")
    except httpx.HTTPError as e:
        _log(f"{prefix}✗ 网络错误 | {model_name} | {e}")
        error_evt = {"type": "error", "error": {"message": str(e)[:500], "code": 502}}
        yield f"data: {json.dumps(error_evt, ensure_ascii=False)}\n\n".encode("utf-8")
        return

    # 发送收尾事件
    finish_events = converter.finish()
    if finish_events:
        yield finish_events.encode("utf-8")

    elapsed = time.time() - t0 if t0 else 0
    _log(f"{prefix}◀ RESPONSES {model_name} | {elapsed:.1f}s | stream done")
    _log(f"{prefix}── RESPONSES RAW SSE ──\n" + "\n".join(raw_sse_lines[-30:]))


# ---------------------------------------------------------------------------
# Anthropic Messages API 端点（Claude Code / CC Switch 兼容）
# ---------------------------------------------------------------------------

@app.post("/v1/messages")
async def create_message(request: Request,
                         authorization: Optional[str] = Header(default=None),
                         x_api_key: Optional[str] = Header(default=None, alias="X-Api-Key")):
    """Anthropic Messages API 兼容端点。

    Claude Code / CC Switch 使用 Anthropic Messages API（POST /v1/messages）。
    本端点接收 Anthropic 格式请求，转换为 Chat 格式发往后端，再将后端的 Chat SSE
    转换为 Anthropic SSE 事件流返回。
    """
    _check_auth(authorization, x_api_key)
    cred = _cred()

    try:
        payload = await request.json()
    except Exception as e:
        raise HTTPException(status_code=400, detail={"error": {"message": f"bad json: {e}", "type": "invalid_request_error"}})

    # 将 Anthropic 格式消息、工具规范在进入后端前统一转换为 OpenAI Chat 格式。
    messages = payload.get("messages") or []
    if not messages:
        raise HTTPException(status_code=400, detail={"error": {"message": "messages is required", "type": "invalid_request_error"}})

    try:
        chat_body = anthropic_request_to_chat(payload)
    except Exception as e:
        raise HTTPException(status_code=400, detail={"error": {"message": f"request conversion error: {e}", "type": "invalid_request_error"}})

    chat_body.setdefault("model", "auto")
    chat_body["stream"] = True
    if "stream_options" not in chat_body:
        chat_body["stream_options"] = {"include_usage": True}

    if CONFIG.get("desensitize"):
        chat_body = desensitize_body(chat_body, roles=("system", "developer"),
                                     desensitize_harness_user=True,
                                     desensitize_tools=True,
                                     compact_harness=not CONFIG.get("no_compact"),
                                     strip_tool_metadata=True)

    model_name = payload.get("model", "auto")
    chat_messages = chat_body.get("messages", [])
    rid = os.urandom(4).hex()
    _log(f"[{rid}] ▶ ANTHROPIC {model_name} | msgs={len(chat_messages)} | anthropic_msgs={len(messages)}")
    _log(f"[{rid}] ── ANTHROPIC → CHAT BODY ──\n{json.dumps(chat_body, ensure_ascii=False, indent=2)}")

    # Anthropic 协议中 stream 缺省为 **false**（非流式）。
    # 此前本端点无条件返回 StreamingResponse，导致：
    #   ① stream:false 的客户端拿到 SSE 却按 JSON 解析 → 直接报错；
    #   ② stream 缺省的客户端（Anthropic SDK 非流式调用）同样拿到 SSE。
    # 现按协议语义分流：显式 stream:true 才走流式。
    # （日志确认此前无 /v1/messages 真实流量，改默认值无回归风险。）
    client_wants_stream = bool(payload.get("stream"))

    headers = cred.get_headers()
    headers.update(_client_ip_headers(request))
    url = f"{BACKEND}/v2/chat/completions"
    t0 = time.time()

    if client_wants_stream:
        return StreamingResponse(
            _stream_anthropic(url, headers, chat_body, model_name, t0, rid),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # 非流式：聚合后端 SSE → 组装单个 Anthropic Message 对象
    try:
        async with httpx.AsyncClient(timeout=_stream_timeout(), limits=_HTTP_LIMITS) as c:
            async with c.stream("POST", url, headers=headers, json=chat_body) as r:
                if r.status_code != 200:
                    raw = await r.aread()
                    _log(f"[{rid}] ✗ ANTHROPIC HTTP {r.status_code} | {model_name} | "
                         f"{_truncate(raw.decode('utf-8','replace'), 200)}")
                    raise HTTPException(status_code=r.status_code,
                                        detail=_safe_err_raw(raw, r.status_code))
                agg = await _collect_stream(r)
    except HTTPException:
        raise
    except httpx.HTTPError as e:
        _log(f"[{rid}] ✗ ANTHROPIC 网络错误 | {model_name} | {e}")
        raise HTTPException(status_code=502, detail={
            "error": {"message": f"upstream error: {e}", "type": "upstream_error"}})

    result = _nonstream_anthropic(agg, model_name)
    _log(f"[{rid}] ◀ ANTHROPIC(non-stream) {model_name} | {time.time()-t0:.1f}s | "
         f"stop={result.get('stop_reason')} | tokens={(agg.get('usage') or {}).get('total_tokens', '?')}")
    return JSONResponse(content=result)


def _nonstream_anthropic(agg: dict, model_name: str) -> dict:
    """把聚合后的 chat.completion 转成 Anthropic Message 对象。"""
    choice = (agg.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    content: list[dict] = []
    if msg.get("reasoning_content"):
        content.append({"type": "thinking",
                        "thinking": msg["reasoning_content"],
                        "signature": ""})
    if msg.get("content"):
        content.append({"type": "text", "text": msg["content"]})
    for tc in msg.get("tool_calls") or []:
        fn = tc.get("function") or {}
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except Exception:
            args = {}
        content.append({"type": "tool_use", "id": tc.get("id") or "",
                        "name": fn.get("name") or "", "input": args})

    stop_map = {"stop": "end_turn", "length": "max_tokens",
                "tool_calls": "tool_use", "content_filter": "end_turn"}
    usage = agg.get("usage") or {}
    return {
        "id": "msg_" + os.urandom(12).hex(),
        "type": "message",
        "role": "assistant",
        "model": model_name,
        "content": content or [{"type": "text", "text": ""}],
        "stop_reason": stop_map.get(choice.get("finish_reason") or "stop", "end_turn"),
        "stop_sequence": None,
        "usage": {
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
        },
    }


async def _stream_anthropic(url: str, headers: dict, body: dict,
                            model_name: str = "?", t0: float = 0.0, rid: str = ""):
    """消费后端 OpenAI Chat SSE，实时转换为 Anthropic Messages SSE 事件流。"""
    converter = AnthropicStreamConverter(model=model_name)
    prefix = f"[{rid}] " if rid else ""

    try:
        # 原来是 timeout=None（完全不设限）：连接一旦静默死掉就永久挂住，
        # 既不报错也不归还资源。改成静默超时 —— 有数据就续期、静默到上限才判死，
        # 等价于参考实现 internal/upstream/idle.go 的空闲监控，且不设总时长。
        async with httpx.AsyncClient(timeout=_stream_timeout(), limits=_HTTP_LIMITS) as c:
            async with c.stream("POST", url, headers=headers, json=body) as r:
                if r.status_code != 200:
                    err = await r.aread()
                    _log(f"{prefix}✗ HTTP {r.status_code} | {model_name} | {_truncate(err.decode('utf-8','replace'),200)}")
                    error_evt = {"type": "error", "error": {"message": err.decode('utf-8','replace')[:500], "type": "api_error", "code": r.status_code}}
                    yield f"event: error\ndata: {json.dumps(error_evt, ensure_ascii=False)}\n\n".encode("utf-8")
                    return
                async for line in r.aiter_lines():
                    events = converter.feed_line(line)
                    if events:
                        yield events.encode("utf-8")
    except httpx.HTTPError as e:
        _log(f"{prefix}✗ 网络错误 | {model_name} | {e}")
        error_evt = {"type": "error", "error": {"message": str(e)[:500], "type": "api_error", "code": 502}}
        yield f"event: error\ndata: {json.dumps(error_evt, ensure_ascii=False)}\n\n".encode("utf-8")
        return

    finish_events = converter.finish()
    if finish_events:
        yield finish_events.encode("utf-8")

    elapsed = time.time() - t0 if t0 else 0
    _log(f"{prefix}◀ ANTHROPIC {model_name} | {elapsed:.1f}s | stream done")


@app.post("/v1/messages/count_tokens")
async def count_tokens(request: Request,
                       authorization: Optional[str] = Header(default=None),
                       x_api_key: Optional[str] = Header(default=None, alias="X-Api-Key")):
    """Anthropic token 计数端点（stub）。

    Claude Code 可能在发送消息前调用此端点。
    返回一个简单估算值，不做实际 token 计数。
    """
    _check_auth(authorization, x_api_key)
    return {"input_tokens": 0}


# ---------------------------------------------------------------------------
# 启动
# ---------------------------------------------------------------------------

def preflight() -> bool:
    af = find_auth_file()
    sys.stderr.write("==== 预检 ====\n")
    sys.stderr.write(f"平台      : {sys.platform}\n")
    sys.stderr.write(f"Python    : {sys.version.split()[0]}\n")
    sys.stderr.write(f"后端      : {BACKEND} (直连，原生 function calling)\n")
    sys.stderr.write(f"登录文件  : {af or '(未找到)'}\n")
    if auth_dirs():
        sys.stderr.write(f"已查目录  : {', '.join(str(d) for d in auth_dirs())}\n")
    ok = True
    if af is None:
        sys.stderr.write("\n[警告] 未找到登录文件。请在桌面端完成登录（CodeBuddy/WorkBuddy）。\n")
        ok = False
    else:
        try:
            cm = CredentialManager(af)
            info = cm.summary()
            sys.stderr.write(f"账号      : {info.get('nickname')} / {info.get('enterpriseName')}\n")
            sys.stderr.write(f"token过期 : {'是(将自动刷新)' if info['token_expired'] else '否'}\n")
        except Exception as e:
            sys.stderr.write(f"[警告] 读取凭据失败：{e}\n")
            ok = False
    sys.stderr.write("================\n")
    return ok


def main():
    ap = argparse.ArgumentParser(description="CodeBuddy -> OpenAI 兼容转换器（直连后端）")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--api-key", default=os.environ.get("CODEBUDDY2OPENAI_KEY", ""),
                    help="可选：要求客户端携带的 API key（默认不校验）")
    ap.add_argument("--log", default=None, metavar="PATH",
                    help="开启日志并写到该文件（如 --log converter.log 或 --log /tmp/cb.log）。"
                         "不传则不记日志。")
    ap.add_argument("--desensitize", action="store_true",
                    help="启用脱敏：对 system 消息里的合规模板敏感词（DoS/exploit/credential 等）"
                         "插入零宽空格，缓解被后端内容审核误拦。默认关闭。")
    ap.add_argument("--no-compact", action="store_true",
                    help="配合 --desensitize 使用：跳过 system/harness 压缩，仅做零宽脱敏。"
                         "保留原始 system prompt 完整内容（如 Claude Code 的行为指令），"
                         "但审核误拦风险略高于默认压缩模式。")
    ap.add_argument("--skip-check", action="store_true", help="跳过启动预检")
    args = ap.parse_args()

    CONFIG["api_key"] = args.api_key
    CONFIG["desensitize"] = args.desensitize
    CONFIG["no_compact"] = args.no_compact
    # --log 直接指定文件路径即开启；不传则不记
    CONFIG["log_path"] = args.log if args.log else os.environ.get("CODEBUDDY2OPENAI_LOG")
    af = find_auth_file()
    CONFIG["cred"] = CredentialManager(af) if af else None

    if not args.skip_check:
        preflight()

    sys.stderr.write(f"\n✅ 监听 http://{args.host}:{args.port}（直连后端，原生 function calling）\n")
    sys.stderr.write("   GET  /v1/models\n")
    sys.stderr.write("   GET  /v1/balance            (当前账号可用积分额度)\n")
    sys.stderr.write("   POST /v1/chat/completions   (原生 tools/tool_calls，支持流式)\n")
    sys.stderr.write("   POST /v1/responses          (Responses API，Codex CLI 兼容)\n")
    sys.stderr.write("   POST /v1/messages           (Anthropic API，Claude Code / CC Switch 兼容)\n")
    sys.stderr.write("   GET  /health\n")
    if args.api_key:
        sys.stderr.write("   鉴权已启用（API key 已设置）\n")
    if CONFIG["log_path"]:
        sys.stderr.write(f"   日志      : {CONFIG['log_path']}\n")
    if args.desensitize:
        mode = "零宽脱敏 + 保留全文" if args.no_compact else "零宽脱敏 + 压缩摘要"
        sys.stderr.write(f"   脱敏      : 已启用（{mode}）\n")
    sys.stderr.write("按 Ctrl+C 退出。\n\n")

    # 启动时写一条标记
    _log(f"==== converter 启动 ====")

    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
