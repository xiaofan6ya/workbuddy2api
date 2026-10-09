"""复用 converter.CredentialManager 操作单个账号的后端会话。

账号凭据以 .info 原文形式存于 MySQL；用时落盘成临时文件交给 CredentialManager，
用完读回（token 可能被刷新），写回 MySQL。
"""
import json
import os
import re
import sys
import tempfile
import time
import uuid
from datetime import datetime
from pathlib import Path

import httpx

from converter import BACKEND, CredentialManager  # 复用既有后端鉴权 / 刷新 / 模型 / 额度逻辑

#: 项目根加入 sys.path，便于导入根目录的 wb_install（与 converter 同级）。
#: admin 包可能在 `uvicorn admin.server:app` 下被导入，此时根目录不一定在
#: sys.path 里，所以显式补一次。
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from wb_install import WB  # noqa: E402  安装位置 / 版本号 / 风控配置自动发现
from admin import client_profile as cprofile  # noqa: E402  客户端参数档案（可后台配置/同步）

# ---------------------------------------------------------------------------
# Ardot 画布（create_canvas）—— 真实 id 的解析口径
# ---------------------------------------------------------------------------
# 全部取自官方客户端源码（app.asar.unpacked 内 ardot 遥测模块实测）：
#   * 成功结果的 fileId 来自 MCP CallToolResult 的 structuredContent.fileId；
#   * 文本兜底形如 `Created Ardot design file: <name>, fileId <id>, url <url>`；
#   * 打开形如 `Opening Ardot editor for file <id>`；
#   * **fileId 是纯数字**（`\bfileId\s+(\d+)\b`，路径 `/file/(\d+)`）。
#
# 最后这条是判据关键：既然真实 id 必须是纯数字，任何形如
# `ardot-file-<8位>「或」wb-<毫秒>` 的字符串都**不可能**是真实画布 id，
# 后端核对必然失败。所以本模块只接受纯数字 id，拿不到就如实返回失败 ——
# 绝不自造一个「看起来像」的 id 去骗过计数。
ARDOT_CREATED_RE = re.compile(
    r"Created Ardot design file:\s*(.+?),\s*fileId\s+(\d+),\s*url\s+(https?://[^\s\"'\\]+)", re.I)
ARDOT_FILE_ID_RE = re.compile(r"\bfileId\s+(\d+)\b", re.I)
ARDOT_FILE_URL_RE = re.compile(r"\burl\s+(https?://[^\s\"'\\]+)", re.I)
ARDOT_OPENING_RE = re.compile(r"\bOpening Ardot editor for file\s+(\d+)\b", re.I)
ARDOT_FILE_IN_PATH_RE = re.compile(r"/file/(\d+)/?$")

#: Ardot 画布分享/打开 URL 的 origin（与官方 resolveArdotEndpoint 的 prod 一致）。
ARDOT_BASE = "https://ardot.tencent.com"
#: Ardot MCP 端点。实测无凭据时返回 `401 {"error":"missing bearer token"}`；
#: 带上 fetch_ardot_token() 换来的 token 即可 initialize / tools/list / tools/call。
ARDOT_MCP_URL = ARDOT_BASE + "/mcp"
#: create_design 的入参只有可选的 fileName（实测 tools/list 的 inputSchema）。
#: 画布内容由 MCP 侧生成，我们不需要（也无法）传 design DSL。
ARDOT_DEFAULT_FILE_NAME = "WorkBuddy 设计画布"


def _extract_ardot_file_id(result: object) -> str:
    """从 Ardot MCP 的工具结果里提取真实 fileId（必须是纯数字）。

    解析顺序（对齐官方 tool-result-artifact-manager 的取值口径）：
      1. 结构化字段 `structuredContent.fileId` / `file_id`
      2. 结果对象自身的 `fileId` / `file_id`
      3. `content[].text` 里的
         `Created Ardot design file: <name>, fileId <id>, url <url>`
      4. 文本里的 `fileId <id>`
      5. 文本里的 `/file/<id>` 路径

    **只接受纯数字**：官方 fileId 口径就是数字（`\\bfileId\\s+(\\d+)\\b`、
    `/file/(\\d+)`）。非数字一律丢弃 —— 这正是「不自造 id」的落点：宁可这个
    任务不完成，也不往上游上报一个不存在的画布对象。
    """
    texts: list[str] = []
    direct: list[str] = []

    def _walk(obj: object, depth: int = 0) -> None:
        if depth > 5 or not isinstance(obj, dict):
            return
        for key in ("fileId", "file_id"):
            v = obj.get(key)
            if isinstance(v, (str, int)) and str(v).strip():
                direct.append(str(v).strip())
        sc = obj.get("structuredContent")
        if isinstance(sc, dict):
            _walk(sc, depth + 1)
        for item in (obj.get("content") or []):
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                texts.append(item["text"])
        for key in ("result", "data", "output", "artifact"):
            inner = obj.get(key)
            if isinstance(inner, dict):
                _walk(inner, depth + 1)

    _walk(result)
    for c in direct:
        if c.isdigit():
            return c
    blob = "\n".join(texts)
    for rx, grp in ((ARDOT_CREATED_RE, 2), (ARDOT_FILE_ID_RE, 1),
                    (ARDOT_FILE_IN_PATH_RE, 1)):
        m = rx.search(blob)
        if m:
            return m.group(grp)
    return ""

#: 「创建设计画布」的真实意图 prompt。
#: 官方前端在 design craft 模式下把意图判定为 create 后，会下发
#: `<ardot_file_directive>` 指令让模型**只调用一次** create_design。
#: 这里用等价的自然语言设计请求来触发同一条路径（我们无法注入前端指令）。
ARDOT_CANVAS_PROMPT = (
    "请帮我设计一个产品落地页的画布（web 页面），包含首屏标题、三个卖点区块和底部行动按钮。"
)


# 连接池：减少 TLS 握手，与 Go 项目 MaxIdleConnsPerHost=20 对齐。
HTTP_LIMITS = httpx.Limits(max_connections=100, max_keepalive_connections=20)

#: 各域名与 UA（成长任务在不同域上报，头形状必须与对应客户端一致）
WEB_BASE = "https://www.workbuddy.cn"      # web 域：资料库等浏览器行为
BILL_BASE = "https://www.codebuddy.cn"     # billing 域：常规业务上报
WEB_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36")
#: 桌面客户端版本与内嵌 CLI 版本。
#:
#: **取值走「客户端参数档案」**（`admin/client_profile.py`），优先级：
#:     环境变量 > （现场探测 / 后台保存，取决于 source）> 内置兜底
#:
#: 为什么不直接读 wb_install：线上服务器没装桌面端，探测不到任何东西，
#: 只能退化成兜底值 —— 于是线上 UA 报的是一个「谁也不认识的版本」。
#: 而本地机器明明装得好好的。档案把这些值变成**可保存、可同步**的数据，
#: 本地探测一次推到线上，线上就能报出与真实客户端一致的版本。
#:
#: 下面两个模块级常量在导入时算一次（供静态引用）；**发请求请用函数版**，
#: 那样后台一改就立即生效，不必重启进程。
#:
#: 用 `effective_local()` 而不是 `effective()`：导入期**不查库**。
#: 否则没有 MySQL 的环境（standalone converter）会在 import 阶段白等几秒。
_PROFILE_AT_IMPORT = cprofile.effective_local()
DESKTOP_VERSION = _PROFILE_AT_IMPORT["desktop_version"]
CLI_VERSION = _PROFILE_AT_IMPORT["cli_version"]
DESKTOP_UA = _PROFILE_AT_IMPORT["user_agent"]


def _desktop_ua() -> str:
    """**实时的**三段式 UA：`WorkBuddy/<v> WorkBuddy/<v> CLI/<cli>`。

    与模块级 `DESKTOP_UA` 的区别：后者在进程启动时算好，前者每次调用都问
    档案（内部有 30 秒缓存，保存时立即失效）。所以后台改了版本号、
    或者运行期装了新客户端，都能立刻生效。
    """
    return cprofile.ua()


def desktop_version() -> str:
    """当前生效的桌面端版本（实时，带缓存）。"""
    return cprofile.desktop_version()


def cli_version() -> str:
    """当前生效的内嵌 CLI 版本（实时，带缓存）。"""
    return cprofile.cli_version()


def describe_client() -> str:
    """一行客户端诊断信息，供启动日志 / 自检使用。"""
    try:
        src = cprofile.source()
        mode = "钉住已保存值" if src == cprofile.SOURCE_SAVED else "优先现场探测"
        return (f"档案={mode}｜UA={cprofile.ua()}｜{WB.describe()}")
    except Exception:
        return WB.describe()

#: 资料库介绍页（Library_read 的 pageURL，必须是真实可访问的文档页）
LIBRARY_DOC_URL = f"{WEB_BASE}/space/d/o0KWYeynteVv06UnAZqIFm"

#: 企鹅教师助手 Buddy 应用 id。
#: 客户端「发现应用」入口里的应用标识，无公开列表接口（探测过 open-platform /
#: buddy 等路径均 404），只能从客户端内置清单取。一组 buddyapp 事件同时满足
#: 「发现应用」和「企鹅教师助手」两个任务。
BUDDY_QQ_APP = ("cb_y5Dy46tPQGGWtueMxXbe", "企鹅教师助手")


def stable_device_id(uid: str, salt: str) -> str:
    """由 uid 稳定派生设备标识（machineId / sessionId 用）。

    为什么必须稳定：同一账号在服务端眼里应当始终是**同一台设备**。
    每次随机 = 频繁换设备 = 明显异常；所有账号共用一个常量则更糟
    （多账号同一设备，是最容易被批量识别的特征）。

    取 master 项目 deriveID 同款算法（md5(salt:uid) 截 36 位），
    不参与任何业务逻辑，仅用于事件指纹。
    """
    import hashlib
    return hashlib.md5(f"{salt}:{uid}".encode()).hexdigest()[:36]



def parse_auth_meta(auth_json: str) -> dict:
    """从 .info 原文里抽取账号元信息（uid / enterpriseId / domain / 昵称）。"""
    try:
        data = json.loads(auth_json)
    except Exception:
        return {}
    auth = data.get("auth") or {}
    acct = data.get("account") or {}
    return {
        "uid": str(acct.get("uid") or ""),
        "enterprise_id": str(acct.get("enterpriseId") or ""),
        "domain": str(auth.get("domain") or ""),
        "nickname": str(acct.get("nickname") or ""),
    }


class AccountSession:
    """把一个账号的 auth_json 包成可用的后端会话。"""

    def __init__(self, auth_json: str):
        self._path = tempfile.mktemp(suffix=".info")
        with open(self._path, "w", encoding="utf-8") as f:
            f.write(auth_json)
        self.cm = CredentialManager(Path(self._path))

    def get_headers(self, extra: dict | None = None) -> dict:
        return self.cm.get_headers(extra=extra)

    def fetch_models(self) -> list:
        return self.cm.fetch_models()

    def fetch_balance(self) -> dict:
        return self.cm.fetch_balance()

    def fetch_account_profile(self) -> dict:
        """用**上游**取这个凭证对应的真实账号信息（uid / 昵称 / 手机号）。

        用途：验证上传的凭证是不是伪造的。

        `validate_credential` 只能证明「JWT 结构完整、字段齐全」——
        这些字段全都可以手编（签名不验），所以必须让**上游**来回答
        「这个令牌到底属于谁」。拿到真实 uid 后与凭证里声明的 uid 比对，
        就能挡住「拿一份真凭证、把 account.uid 改成别的号」这种拼接伪造。

        打的是 `/v2/plugin/accounts`。**实测确认**：该端点返回的账号完全由
        Bearer 令牌决定 —— 把 `X-User-Id` 改空、改成别的 uid，返回的仍是
        令牌自己的账号。所以它天然是可信的「令牌归属」来源，
        而不是「按我们声明的 uid 去查」（那样验证就失去意义了）。
        """
        data = self.cm._request_backend("GET", "/v2/plugin/accounts")
        accounts = ((data.get("data") or {}).get("accounts")) or []
        if not accounts:
            return {}
        acc = accounts[0] or {}
        return {
            "uid": acc.get("uid") or "",
            "nickname": acc.get("nickname") or "",
            "uin": acc.get("uin") or "",
            "type": acc.get("type") or "",
            "phoneNumber": acc.get("phoneNumber") or "",
            "enterpriseId": acc.get("enterpriseId") or "",
            "enterpriseName": acc.get("enterpriseName") or "",
        }

    def fetch_credit_details(self) -> list[dict]:
        """获取积分明细（每个积分包的总量/剩余/到期时间）。

        通过 summary/paid/free 独立采集与旧接口兼容回退，返回的 list
        附带 metadata。来源不完整时由快照写入层保留上次完整结果。

        对应截图中的「版本基础用量」「权益赠送包」等条目。

        **原样返回上游字段**，不做改名、不做取值决策：
        上游用 `CycleCapacityRemainPrecise`（字符串小数）与
        `CycleCapacityRemain`（整数）两套字段表达同一语义，
        到底该信哪个、`DeductionEndTime`（可能是 2034/2049 的占位值）
        与 `CycleEndTime` 谁是真实到期时间 —— 这些口径判断全部收拢在
            `admin/credits.py::resource_summary`，本方法只负责「把数据取回来」。

        为什么必须这样分层：这里一旦把字段改名成 `remain`/`deduction_end`，
        口径判断就被锁死在取值现场了 —— 而 2034 占位值这个问题正是
        「在取值现场只看 DeductionEndTime」造成的。原来这里还做了一层
        改名，导致 `admin/credits.py` 按上游原名读取时**一个字段都匹配不上**，
        所有包都被判成「无到期时间 / 剩余 0」，调度依据静默失效。
        """
        from admin.billing import collect_billing
        return collect_billing(self.cm._request_backend)

    def fetch_request_usage(self, start_time: str, end_time: str, page_num: int = 1, page_size: int = 10) -> dict:
        """获取模型请求用量（对接 WorkBuddy 已有接口，不自建日志）。"""
        return self.cm._request_backend("POST", "/billing/meter/get-user-request-usage", {
            "startTime": start_time,
            "endTime": end_time,
            "pageNum": page_num,
            "pageSize": page_size,
        })

    # -----------------------------------------------------------------------
    # 每日签到领取 100 积分（Buddy 加油站活动）
    # -----------------------------------------------------------------------

    def get_checkin_status(self) -> dict:
        """查询当前账号的签到活动状态。

        返回后端 data 字段（含 active / today_checked_in / end_time / activity_name 等）。
        end_time 即活动结束时间，是「下次停止领取」配置的依据。
        """
        data = self.cm._request_backend_soft("POST", "/v2/billing/meter/checkin-activity-status", {})
        return data.get("data") or {}

    def claim_daily_checkin(self) -> dict:
        """执行每日签到领取。

        成功返回 {"ok": True, "credit": int, "streak_days": int}；
        业务失败（已领/无资格/活动结束）返回 {"ok": False, "code": int, "status": str}。
        """
        data = self.cm._request_backend_soft("POST", "/v2/billing/meter/daily-checkin", {})
        code = data.get("code")
        payload = data.get("data") or {}
        if code and code != 0:
            return {"ok": False, "code": code, "status": _map_checkin_status(code), "msg": data.get("msg")}
        credit = payload.get("credit")
        if credit is None:
            credit = data.get("credit")
        streak = payload.get("streak_days")
        if streak is None:
            streak = data.get("streak_days")
        return {"ok": True, "credit": credit or 0, "streak_days": streak or 0}

    # -----------------------------------------------------------------------
    # 猫猫旅行（/activity/growth/buddy/*）
    #
    # 流程：同意协议 → 首次领养（+300 积分）→ 派出 → 到站领奖。
    # 域为 chatBase（CN = copilot.tencent.com，不带 /v2 前缀），与 billing 域不同。
    # -----------------------------------------------------------------------

    #: 领养门槛未达标的业务错误关键词（HTTP 400 时出现），属预期而非失败。
    BUDDY_TASK_INCOMPLETE_MARKER = "first_buddy task not completed yet"

    def _growth(self, method: str, path: str, body: dict | None = None) -> dict:
        """发 growth 域请求；返回 {ok, status, code, msg, data}，不抛异常。

        与 billing 域的 `_request_backend_soft` 不同：growth 域的「门槛未达」
        等业务失败走 HTTP 400，需要调用方读取 msg 判定，故这里把结果结构化返回。
        """
        headers = self.cm.get_headers()
        url = f"{BACKEND}{path}"
        try:
            with httpx.Client(timeout=15, limits=HTTP_LIMITS) as c:
                if method.upper() == "GET":
                    r = c.get(url, headers=headers)
                else:
                    r = c.post(url, headers=headers, json=body if body is not None else {})
        except Exception as e:
            return {"ok": False, "status": 0, "code": None, "msg": f"网络失败: {e}", "data": {}}
        try:
            payload = r.json()
        except Exception:
            return {
                "ok": False, "status": r.status_code, "code": None,
                "msg": f"非 JSON 响应 HTTP {r.status_code}: {r.text[:200]}", "data": {},
            }
        code = payload.get("code")
        ok = r.status_code == 200 and code == 0
        return {
            "ok": ok,
            "status": r.status_code,
            "code": code,
            "msg": payload.get("msg") or "",
            "data": payload.get("data") or {},
        }

    def buddy_info(self) -> dict | None:
        """查询当前猫档案；None 表示无猫（data.buddy 为 null），即尚未领养。"""
        res = self._growth("GET", "/activity/growth/buddy/info")
        if not res["ok"]:
            raise RuntimeError(f"查询猫档案失败: {res['msg']}")
        buddy = res["data"].get("buddy")
        return buddy if isinstance(buddy, dict) and buddy else None

    def buddy_agreement(self) -> dict:
        """同意活动协议（幂等，重复调用无副作用）。"""
        return self._growth("POST", "/activity/growth/buddy/agreement", {"agree": True})

    def buddy_first(self) -> dict:
        """首次领养。成功即发放 300 积分。"""
        return self._growth("POST", "/activity/growth/buddy/first", {})

    def travel_status(self) -> dict:
        """查询猫猫旅行状态：state(idle/traveling/arrived) / record_id / reward_credit。"""
        res = self._growth("GET", "/activity/growth/buddy/travel/status")
        if not res["ok"]:
            raise RuntimeError(f"查询旅行状态失败: {res['msg']}")
        return res["data"] or {}

    def travel_depart(self, location_id: int = 4) -> dict:
        """派出猫旅行。4 个地点收益/时长区间完全相同，固定用 4（古镇客栈）。"""
        return self._growth("POST", "/activity/growth/buddy/travel/depart", {"location_id": location_id})

    def travel_claim(self, record_id: int) -> dict:
        """领取到站奖励；成功时 data.reward_credit 为实发积分。"""
        return self._growth("POST", "/activity/growth/buddy/travel/claim", {"record_id": record_id})

    def _is_threshold_not_met(self, res: dict) -> bool:
        """判定「领养门槛未达标」：HTTP 400 + first_buddy 关键词。"""
        return (
            res.get("status") == 400
            and self.BUDDY_TASK_INCOMPLETE_MARKER in str(res.get("msg", "")).lower()
        )

    #: 领养门槛所需的最小对话次数。服务端原文为
    #: "first_buddy task not completed yet (need at least one conversation)"，
    #: 实测发 1 次 chat_request_send 事件即可让 first_buddy 变为 completed。
    BUDDY_GATE_CHATS = 1

    def _clear_buddy_gate(self) -> tuple[bool, str]:
        """尝试清除领养门槛（first_buddy 任务）。

        实测结论：发一次带 chat_request_send 的对话事件，first_buddy 即从
        not_accepted 直接变为 completed，无需先调 accept。事件走的是正常的
        /v2/chat/completions（免费模型 + max_tokens=1，HTTP 200，成本为 0）。

        Returns:
            (是否已达标, 说明文字)
        """
        for i in range(self.BUDDY_GATE_CHATS):
            try:
                r = self.growth_fire_event(["chat_request_send"])
            except Exception as e:
                return False, f"触发对话异常：{e}"
            if not r.get("ok"):
                return False, f"触发对话失败：{r.get('msg') or r.get('status')}"
            if i + 1 < self.BUDDY_GATE_CHATS:
                time.sleep(1.5)

        # 复查任务是否达标
        time.sleep(1.5)
        try:
            for t in self.growth_tasks():
                if t.get("task_code") == "first_buddy":
                    if t.get("accept_status") in ("completed", "claimed"):
                        return True, "已完成首次对话，门槛达标"
                    pr = t.get("progress") or {}
                    return False, (f"对话后仍未达标（{pr.get('current')}/{pr.get('target')}）")
        except Exception as e:
            return False, f"复查任务状态异常：{e}"
        return False, "未找到 first_buddy 任务"

    def run_cat_travel(self, location_id: int = 4) -> dict:
        """执行一趟猫猫旅行，返回结构化分步结果（供前端逐步提示）。

        步骤语义：
          - agreement : 无猫时才做，同意活动协议
          - gate      : 无猫且门槛未达标时，补一次对话以解锁领养
          - adopt     : 无猫时才做，首次领养，成功 +300 积分
          - depart    : 空闲时派出
          - claim     : 到站时领奖，reward 为实发积分

        领养门槛：领养要求 first_buddy 任务完成，该任务的条件是
        「至少一次对话」。本流程会自动补上这次对话（免费模型、成本 0），
        因此新手账号也能一次跑通，不需要人工先去聊一句。
        """
        steps: list[dict] = []
        credits = 0

        def add(step: str, ok: bool, message: str, reward: int = 0, skipped: bool = False):
            nonlocal credits
            credits += reward
            steps.append({
                "step": step, "ok": ok, "skipped": skipped,
                "reward": reward, "message": message,
            })

        # ── 1) 查猫档案 ────────────────────────────────────────────────
        try:
            buddy = self.buddy_info()
        except Exception as e:
            add("info", False, str(e))
            return {"ok": False, "credits": credits, "steps": steps,
                    "summary": "查询猫档案失败", "outcome": "error"}

        # ── 2) 无猫则领养 ──────────────────────────────────────────────
        if buddy is None:
            agr = self.buddy_agreement()
            if not agr["ok"]:
                add("agreement", False, f"同意协议失败：{agr['msg'] or agr['status']}")
                return {"ok": False, "credits": credits, "steps": steps,
                        "summary": "同意协议失败", "outcome": "error"}
            add("agreement", True, "已同意活动协议")

            first = self.buddy_first()

            # 门槛未达标：自动补一次对话后再试，避免新手账号必然失败
            if not first["ok"] and self._is_threshold_not_met(first):
                add("gate", True, "领养门槛未达标，自动补一次对话以解锁")
                gate_ok, gate_msg = self._clear_buddy_gate()
                add("gate", gate_ok, gate_msg, skipped=not gate_ok)
                if gate_ok:
                    first = self.buddy_first()

            if first["ok"]:
                # 领养成功发放 300 积分；上游在 data 里回传实际到账值
                data = first.get("data") or {}
                got = int(data.get("credit") or 0)
                energy = int(data.get("energy") or 0)
                reward = got if got > 0 else 300
                msg = f"领养成功，已发放 {reward} 积分"
                if energy:
                    msg += f" + {energy} 能量"
                badge = (data.get("badge") or {}).get("name")
                if badge:
                    msg += f"，解锁徽章「{badge}」"
                add("adopt", True, msg, reward=reward)
                return {"ok": True, "credits": credits, "steps": steps,
                        "summary": msg, "outcome": "adopted"}
            if self._is_threshold_not_met(first):
                add("adopt", True, "本次无法领养：对话门槛未达标", skipped=True)
                return {"ok": True, "credits": credits, "steps": steps,
                        "summary": "本次无法领养（对话门槛未达标）",
                        "outcome": "gate_blocked"}
            add("adopt", False, f"领养失败：{first['msg'] or first['status']}")
            return {"ok": False, "credits": credits, "steps": steps,
                    "summary": "领养失败", "outcome": "error"}

        add("adopt", True, f"已有猫：{buddy.get('name') or buddy.get('id')}", skipped=True)

        # ── 3) 查旅行状态 ──────────────────────────────────────────────
        try:
            st = self.travel_status()
        except Exception as e:
            add("status", False, str(e))
            return {"ok": False, "credits": credits, "steps": steps,
                    "summary": "查询旅行状态失败", "outcome": "error"}

        state = str(st.get("state") or "").strip()
        record_id = int(st.get("record_id") or 0)

        # ── 4) 到站领奖 / 空闲派出 ─────────────────────────────────────
        if state == "arrived":
            if record_id <= 0:
                add("claim", False, "已到站但缺少 record_id，无法领奖")
                return {"ok": False, "credits": credits, "steps": steps,
                        "summary": "领奖失败（缺少 record_id）", "outcome": "error"}
            cl = self.travel_claim(record_id)
            if cl["ok"]:
                reward = int((cl["data"] or {}).get("reward_credit") or 0)
                add("claim", True, f"领奖成功，获得 {reward} 积分", reward=reward)
                outcome = "travel_claimed" if reward > 0 else "travel_none"
            else:
                add("claim", False, f"领奖失败：{cl['msg'] or cl['status']}")
                return {"ok": False, "credits": credits, "steps": steps,
                        "summary": "领奖失败", "outcome": "error"}
        elif state == "idle":
            outcome = "travel_none"
            if st.get("daily_limit_reached"):
                add("depart", True, "今日已派出过，明日 00:00 后可再次派出", skipped=True)
            else:
                dp = self.travel_depart(location_id)
                if dp["ok"]:
                    add("depart", True, "已派出猫咪旅行，到站后可领奖")
                else:
                    add("depart", False, f"派出失败：{dp['msg'] or dp['status']}")
                    return {"ok": False, "credits": credits, "steps": steps,
                            "summary": "派出失败", "outcome": "error"}
        elif state == "traveling":
            outcome = "traveling"
            add("depart", True, f"猫咪正在旅行中（record={record_id}），到站后可领奖",
                skipped=True)
        else:
            outcome = "unknown"
            add("status", True, f"未知旅行状态 {state!r}，未执行动作", skipped=True)

        total = sum(s["reward"] for s in steps)
        if total > 0:
            summary = f"完成，共获得 {total} 积分"
        else:
            summary = "完成，本次无新增积分"
        return {"ok": True, "credits": credits, "steps": steps,
                "summary": summary, "outcome": outcome}

    # -----------------------------------------------------------------------
    # 成长计划任务（/v2/activity/growth/tasks*）
    #
    # 与猫猫旅行同域（chatBase）。完整流程：
    #   拉列表 → 参与(accept) → 触发行为 → 领奖(claim)
    #
    # 关键机制（逆向得出）：任务进度由「请求体里的 extra_vars.growthEvent」
    # 驱动，而非独立的上报接口。事件形如：
    #   extra_vars.growthEvent = '[{"eventCode":"chat_request_send","id":"<会话ID>"}]'
    # 服务端只按 eventCode 记账，不校验模型是否真的被调用。
    # -----------------------------------------------------------------------

    def growth_tasks(self) -> list[dict]:
        """拉取成长任务列表（含进度与状态）。"""
        res = self._growth("GET", "/v2/activity/growth/tasks")
        if not res["ok"]:
            raise RuntimeError(f"拉取任务列表失败: {res['msg']}")
        return res["data"].get("tasks") or []

    def growth_accept(self, task_codes: list[str]) -> dict:
        """批量参与任务。

        未参与（not_accepted）的任务不会累计进度，必须先 accept。

        返回 `{task_code: status}`，status 为 `accepted` / `already_accepted` /
        `error`。

        ⚠️ **失败不抛异常**：上游在 HTTP 200 的信封里回
        `{"status": "error", "message": "..."}`。所以调用方**必须**检查
        status，只看有没有抛异常会把「参与失败」当成功 —— 后面照样发事件、
        照样报成功，实际一个都没计入。

        上游的 `message` 是排查的关键（实测它会直接告诉你原因，例如
        `prerequisite not met: first_buddy (no buddy instance found)`），
        这里顺手缓存下来供 `last_accept_message()` 取用。
        """
        res = self._growth("POST", "/v2/activity/growth/tasks/accept",
                           {"task_codes": list(task_codes)})
        if not res["ok"]:
            raise RuntimeError(f"参与任务失败: {res['msg']}")
        out: dict[str, str] = {}
        self._accept_msgs = {}
        for r in (res["data"].get("results") or []):
            code = r.get("task_code")
            out[code] = r.get("status")
            if r.get("message"):
                self._accept_msgs[code] = str(r["message"])
        return out

    def last_accept_message(self, task_code: str) -> str:
        """取上一次 `growth_accept` 里该任务的失败原因（没有则空串）。"""
        return getattr(self, "_accept_msgs", {}).get(task_code, "")

    def growth_claim(self, task_code: str) -> dict:
        """领取单任务奖励。

        注意路径形态与其它 growth 接口不同：是 /activity/growth/tasks/{code}/claim
        （无 v2 前缀，任务码在路径中，POST 空体）。
        """
        return self._growth("POST", f"/activity/growth/tasks/{task_code}/claim")

    def growth_profile(self) -> dict:
        """成长档案（等级 / 已完成数等）。"""
        res = self._growth("GET", "/v2/activity/growth/profile")
        return res["data"] if res["ok"] else {}

    def growth_fire_event(self, event_codes: list[str], model: str = "hy3",
                          event_id: str | None = None,
                          conversation_id: str | None = None) -> dict:
        """通过带 growthEvent 的模型请求触发任务进度。

        发的是一个「完全合法」的请求（正常响应 200），避免在上游留下
        异常日志：真实模型 + max_tokens=1，只取最小输出。
        默认用免费 0 倍率模型（hy3），成本为零。

        Args:
            event_codes: 事件名列表，如 ["chat_request_send"]。
            model: 使用的模型，默认免费模型。
            event_id: 事件 id（会写入 growthEvent）。
            conversation_id: 会话 id，用于服务端去重与归因。
        """
        # 会话 id 必须每个账号、每次调用都不同。
        # 之前这里是一个硬编码的假 UUID，导致所有账号共用同一个会话 id ——
        # 这种「多账号同会话」是很容易被批量识别的特征，也影响服务端归因。
        conv = conversation_id or event_id or str(uuid.uuid4())
        events = [{"eventCode": c, "id": conv} for c in event_codes]
        body = {
            "model": model,
            "stream": True,
            "max_tokens": 1,  # 最小输出：只为触发记账，不需要真实内容
            "messages": [{"role": "user", "content": "hi"}],
            "extra_vars": {"growthEvent": json.dumps(events, ensure_ascii=False)},
        }
        headers = self.cm.get_headers()
        headers["Content-Type"] = "application/json"
        try:
            with httpx.Client(timeout=60, limits=HTTP_LIMITS) as c:
                with c.stream("POST", f"{BACKEND}/v2/chat/completions",
                              headers=headers, json=body) as r:
                    status = r.status_code
                    # 读完（或读到足够判断的量）后关闭，避免连接悬挂
                    n = 0
                    for _ in r.iter_lines():
                        n += 1
                        if n > 50:
                            break
        except Exception as e:
            return {"ok": False, "status": 0, "msg": f"网络失败: {e}"}
        return {"ok": status == 200, "status": status,
                "msg": "" if status == 200 else f"HTTP {status}"}

    # ------------------------------------------------------------------
    # 事件上报（POST /v2/report）
    # ------------------------------------------------------------------
    # 有一部分成长任务不吃 chat/completions 的 growthEvent，而是要求客户端
    # 上报**真实业务事件**。这类事件必须带上与对应客户端一致的指纹头，
    # 否则要么不计数，要么被当成异常客户端。
    #
    # 三个域各有一套形状，不能混用：
    #   billing (codebuddy.cn)     常规业务事件（灵感案例等）
    #   chat    (copilot.tencent.com) 桌面端事件
    #   web     (workbuddy.cn)     浏览器行为（资料库等）
    # ------------------------------------------------------------------

    def _sess_auth(self) -> dict:
        return (self.cm._session() or {}).get("auth") or {}

    def _sess_acct(self) -> dict:
        return (self.cm._session() or {}).get("account") or {}

    def _uid(self) -> str:
        return str(self._sess_acct().get("uid") or "")

    def _nick(self) -> str:
        return str(self._sess_acct().get("nickname") or "")

    def _domain(self) -> str:
        return self._sess_auth().get("domain") or "copilot.tencent.com"

    def _billing_headers(self) -> dict:
        """billing 域头：CLI 形状。"""
        return {
            "Authorization": "Bearer " + (self._sess_auth().get("accessToken") or ""),
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "CLI/2.63.2 CodeBuddy/2.63.2",
            "Origin": BILL_BASE,
            "Referer": BILL_BASE + "/",
            "X-User-Id": self._uid(),
            "X-Domain": self._domain(),
        }

    def _web_headers(self, page_url: str) -> dict:
        """web 域头：浏览器形状。

        X-Domain 必须显式覆盖成 web 域：auth 里的 domain 可能是
        copilot.tencent.com，发往 www.workbuddy.cn 会造成跨域不一致。
        """
        return {
            "Authorization": "Bearer " + (self._sess_auth().get("accessToken") or ""),
            "Accept": "application/json",
            "Content-Type": "application/json",
            "x-client-platform": "web",
            "Origin": WEB_BASE,
            "Referer": page_url,
            "User-Agent": WEB_UA,
            "X-User-Id": self._uid(),
            "X-Domain": WEB_BASE,
        }

    def report_billing_event(self, events: list[dict]) -> dict:
        """向 billing 域上报业务事件。"""
        uid = self._uid()
        arr = []
        for e in events:
            m = dict(e)
            m.setdefault("userId", uid)
            arr.append(m)
        return self._post_report(BILL_BASE + "/v2/report",
                                 self._billing_headers(), arr)

    def report_web_event(self, event_code: str, page_url: str,
                         element_id: str, element_name: str) -> dict:
        """向 web 域上报一次浏览器元素点击（资料库任务用）。"""
        now = int(time.time() * 1000)
        uid = self._uid()
        ev = {
            "eventCode": event_code, "timestamp": now, "reportDelay": 0,
            "pageURL": page_url, "elementId": element_id,
            "elementName": element_name,
            "os": "Win32", "arch": "", "osVersion": "10.0", "userAgent": WEB_UA,
            "machineId": stable_device_id(uid, "webmachine"),
            "userId": uid, "userNickname": self._nick(),
        }
        return self._post_report(WEB_BASE + "/v2/report",
                                 self._web_headers(page_url), [ev])

    def _post_report(self, url: str, headers: dict, events: list[dict]) -> dict:
        """上报事件；返回 {ok, status, code, msg}，不抛异常。"""
        try:
            with httpx.Client(timeout=20, limits=HTTP_LIMITS) as c:
                r = c.post(url, headers=headers, json=events)
        except Exception as e:
            return {"ok": False, "status": 0, "code": None,
                    "msg": f"网络失败: {e}"}
        try:
            payload = r.json()
        except Exception:
            payload = {}
        code = payload.get("code") if isinstance(payload, dict) else None
        ok = r.status_code == 200 and code == 0
        return {"ok": ok, "status": r.status_code, "code": code,
                "msg": "" if ok else f"HTTP {r.status_code} code={code}"}

    def fire_playbook_prompt(self) -> dict:
        """灵感案例任务：上报一次「使用官方案例提示词」。

        事件必须带齐 skills/expertId 等业务字段——上游按内容判断是不是
        真实使用案例，只发一个空壳事件不计数。
        """
        now = int(time.time() * 1000)
        uid = self._uid()
        cid = str(uuid.uuid4())
        ev = {
            "eventCode": "playbook_prompt_send", "timestamp": now,
            "reportDelay": 0, "id": f"pb-{now}", "name": "playbook",
            "type": "other", "promptLength": 12, "isOfficial": 1,
            "skills": "", "skillNames": "", "expertId": "", "expertName": "",
            "categoryId": "", "categoryName": "", "query": "",
            "source": "discover", "conversationId": cid,
            "requestId": f"{cid}-{now}", "ext1": "discover", "userId": uid,
        }
        return self.report_billing_event([ev])

    def fire_library_read(self) -> dict:
        """资料库任务：上报一次资料库介绍页的点击。"""
        return self.report_web_event(
            "web_element_click", LIBRARY_DOC_URL,
            "library_doc_intro_click", "WorkBuddy资料库介绍")

    def _chat_headers(self) -> dict:
        """chat 域头：市场/场景等接口用（CLI 形状，X-Domain 走账号自己的域）。"""
        return {
            "Authorization": "Bearer " + (self._sess_auth().get("accessToken") or ""),
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "CLI/2.63.2 CodeBuddy/2.63.2",
            "Origin": BILL_BASE,
            "Referer": BILL_BASE + "/",
            "X-User-Id": self._uid(),
            "X-Domain": self._domain(),
        }

    def _chat_json(self, path: str, body: dict | None = None) -> dict:
        """向 chat 域发请求并返回 data 段；失败返回 {}。"""
        url = BACKEND.rstrip("/") + path
        try:
            with httpx.Client(timeout=25, limits=HTTP_LIMITS) as c:
                if body is None:
                    r = c.get(url, headers=self._chat_headers())
                else:
                    r = c.post(url, headers=self._chat_headers(), json=body)
            if r.status_code != 200:
                return {}
            return (r.json() or {}).get("data") or {}
        except Exception:
            return {}

    # ------------------------------------------------------------------
    # 真实对象来源（全部只读；绝不编造 id）
    # ------------------------------------------------------------------
    # 这些任务要求事件里带「真实存在的对象 id」。id 一律从官方接口现拉，
    # 拉不到就放弃该任务，绝不退化成自造 id —— 伪造业务对象一旦被后端
    # 核对就会暴露。
    # ------------------------------------------------------------------

    def _next_index(self, n: int) -> int:
        """返回 0..n-1 的轮转下标，每次调用递增。

        同一个任务要跑 N 次，必须每次用**不同的**对象 id ——
        服务端按 id 去重，重复报同一个 id 只算一次
        （实测 team 任务连报 3 次同一个专家，进度只 +2）。
        用实例计数器而不是时间片，保证连续调用一定取到不同值。
        """
        if n <= 0:
            return 0
        i = getattr(self, "_rotor", 0)
        self._rotor = i + 1
        return i % n

    def fetch_experts(self, team_only: bool = False,
                      keyword: str | None = None, limit: int = 6) -> list[dict]:
        """真实专家列表（市场接口）。team_only 只取专家团。"""
        body: dict = {"page": 1, "page_size": 50}
        if team_only:
            # 市场支持按 expert_type 过滤；不带该参数时 400 个专家里只有 1 个 team
            body["expert_type"] = "team"
        if keyword:
            body["keyword"] = keyword
        data = self._chat_json("/v2/operation-platform/market/expert/list", body)
        out = []
        for e in data.get("experts") or []:
            eid = e.get("expert_id") or e.get("source_id")
            if not eid:
                continue
            etype = e.get("expert_type") or "agent"
            if team_only and etype != "team":
                continue
            out.append({
                "id": eid, "expertType": etype,
                "name": e.get("display_name_zh") or e.get("profession_zh") or eid,
                "category": (e.get("categories") or [""])[0] or "",
                "version": e.get("version") or "",
            })
            if len(out) >= limit:
                break
        return out

    def fetch_scenes(self, limit: int = 6) -> list[dict]:
        """真实场景（模板）列表。"""
        data = self._chat_json("/console/as/support/scenes?locale=zh-CN")
        out = []
        for s in data.get("scenes") or []:
            if s.get("id") is None:
                continue
            out.append({"id": str(s["id"]), "name": s.get("name") or ""})
            if len(out) >= limit:
                break
        return out

    def fetch_appearance_themes(self, keyword: str = "和平精英") -> list[dict]:
        """真实外观主题资源（走 billing 域）。"""
        url = BILL_BASE + "/v2/operation-platform/appearance/resources"
        body = {"platform": "client", "kind": "theme", "version": "2.63.2",
                "lang": "zh-CN"}
        try:
            with httpx.Client(timeout=25, limits=HTTP_LIMITS) as c:
                r = c.post(url, headers=self._chat_headers(), json=body)
            if r.status_code != 200:
                return []
            data = (r.json() or {}).get("data") or {}
        except Exception:
            return []
        out = []
        for x in data.get("resources") or []:
            nm = x.get("name") or ""
            if keyword and keyword not in nm and keyword.lower() not in nm.lower():
                continue
            if not x.get("id"):
                continue
            out.append({"id": x["id"], "name": nm,
                        "vipLevel": x.get("vip_level") or "free",
                        "series": x.get("series") or "craft"})
        return out

    # ------------------------------------------------------------------
    # 具体任务触发器
    # ------------------------------------------------------------------

    def fire_expert_use(self, team_only: bool = False) -> dict:
        """召唤专家任务：用真实专家 id 上报 expert_actual_use。

        每次调用取一个不同专家（按 uid 轮转，避免永远只报第一个）。
        target 次数由调用方循环完成。
        """
        teams = team_only
        experts = self.fetch_experts(team_only=teams, limit=12)
        if not experts:
            return {"ok": False, "status": 0,
                    "msg": "取不到真实专家 id，跳过（不自造）"}
        e = experts[self._next_index(len(experts))]
        now = int(time.time() * 1000)
        cid = str(uuid.uuid4())
        ev = {
            "eventCode": "expert_actual_use", "timestamp": now, "reportDelay": 0,
            "mode": "CLOUD", "id": e["id"], "name": e["name"],
            "expertTitle": e["name"], "type": e["category"],
            "expertType": "team" if teams else (e["expertType"] or "agent"),
            "source": "builtin", "version": e["version"], "cost": 0,
            "characterCount": 12, "conversationId": cid,
            "requestId": f"{cid}-{now}", "messageId": f"{cid}-{now}",
            "requestModelId": "deepseek-v4-flash",
            "requestModelName": "DeepSeek V4 Flash", "userId": self._uid(),
        }
        return self.report_billing_event([ev])

    def fire_expert_team(self) -> dict:
        """召唤专家团任务：只取 expert_type=team 的真实团队。"""
        return self.fire_expert_use(team_only=True)

    def fire_template_use(self) -> dict:
        """使用模板任务：用真实场景 id 上报 agent_task_created_with_template。"""
        scenes = self.fetch_scenes(limit=12)
        if not scenes:
            return {"ok": False, "status": 0, "msg": "取不到真实场景 id，跳过"}
        s = scenes[self._next_index(len(scenes))]
        now = int(time.time() * 1000)
        cid = str(uuid.uuid4())
        ev = {
            "eventCode": "agent_task_created_with_template", "timestamp": now,
            "reportDelay": 0, "isCustomModel": True, "id": s["id"],
            "name": s["name"], "requestId": f"{cid}-{now}",
            "conversationId": cid, "userId": self._uid(),
        }
        return self.report_billing_event([ev])

    def fire_lighthouse_expert(self) -> dict:
        """轻量云专家任务：关键词筛出真实轻量云专家后上报。"""
        for kw in ("lighthouse", "轻量云"):
            experts = self.fetch_experts(keyword=kw, limit=5)
            for e in experts:
                name = e["name"] or ""
                if any(k in (e["id"] + name).lower()
                       for k in ("lighthouse", "轻量", "light")):
                    now = int(time.time() * 1000)
                    cid = str(uuid.uuid4())
                    ev = {
                        "eventCode": "expert_actual_use", "timestamp": now,
                        "reportDelay": 0, "mode": "CLOUD", "id": e["id"],
                        "name": name, "expertTitle": name, "type": e["category"],
                        "expertType": e["expertType"] or "agent",
                        "source": "builtin", "version": e["version"], "cost": 0,
                        "characterCount": 12, "conversationId": cid,
                        "requestId": f"{cid}-{now}", "messageId": f"{cid}-{now}",
                        "requestModelId": "deepseek-v4-flash",
                        "requestModelName": "DeepSeek V4 Flash",
                        "userId": self._uid(),
                    }
                    return self.report_billing_event([ev])
        return {"ok": False, "status": 0, "msg": "未找到轻量云专家，跳过"}

    def fire_appearance_skin(self) -> dict:
        """和平精英主题任务：用真实主题 resourceKey 上报换肤。"""
        themes = self.fetch_appearance_themes()
        if not themes:
            return {"ok": False, "status": 0, "msg": "取不到真实主题 id，跳过"}
        t = themes[0]
        now = int(time.time() * 1000)
        ev = {
            "eventCode": "appearance_skin_apply", "timestamp": now,
            "reportDelay": 0, "action": "apply", "source": "settings_close",
            "id": t["id"], "vipLevel": t["vipLevel"], "series": t["series"],
            "type": "unknown", "name": t["name"], "userId": self._uid(),
        }
        return self.report_billing_event([ev])

    # ------------------------------------------------------------------
    # 桌面端事件链（走 chat 域 /v2/report + 桌面指纹）
    # ------------------------------------------------------------------
    # 这几个任务（发现应用 / 企鹅教师助手 / 桌面端对话）上游按「桌面客户端
    # 行为」判定。任务说明里写的「需升级到 5.5.3+」是**客户端侧**的门槛，
    # 服务端只认事件本身，实测直接上报事件链即可完成，无需真的装桌面端。
    # 事件必须带桌面指纹，否则不会被识别为桌面端来源。
    # ------------------------------------------------------------------

    def _desktop_headers(self) -> dict:
        """chat 域桌面指纹请求头。"""
        return {
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json;charset=UTF-8",
            "User-Agent": _desktop_ua(),
            "X-Domain": self._domain(),
            "X-Product": "SaaS",
            "X-Request-ID": stable_device_id(self._uid(), "req")
                            + str(time.time_ns() % 1000000),
            "X-User-Id": self._uid(),
            "Authorization": "Bearer " + (self._sess_auth().get("accessToken") or ""),
        }

    def _desktop_fingerprint(self) -> dict:
        """桌面客户端公共指纹（注入每个事件，覆盖同名业务键）。

        machineId / sessionId 由 uid 稳定派生：同一账号每次上报都是同一台
        「设备」，不要每次随机 —— 频繁换设备反而是异常信号。

        其余字段来自**客户端参数档案**（可后台配置并同步到线上），
        所以线上实例也能报出与真实客户端一致的指纹。
        """
        uid = self._uid()
        now = int(time.time() * 1000)
        fp = cprofile.fingerprint(
            uid,
            machine_id=stable_device_id(uid, "machine"),
            session_id=stable_device_id(uid, "session"),
        )
        fp.update({
            "userId": uid,
            "username": self._nick(),
            "userNickname": self._nick(),
            "timestamp": now,
            "presentAt": now,
        })
        return fp

    def report_desktop_events(self, events: list[dict]) -> dict:
        """向 chat 域批量上报桌面事件（每个事件注入桌面指纹）。"""
        fp = self._desktop_fingerprint()
        arr = []
        for e in events:
            m = dict(e)
            m.update(fp)
            arr.append(m)
        url = BACKEND.rstrip("/") + "/v2/report"
        try:
            with httpx.Client(timeout=25, limits=HTTP_LIMITS) as c:
                r = c.post(url, headers=self._desktop_headers(), json=arr)
        except Exception as e:
            return {"ok": False, "status": 0, "code": None, "msg": f"网络失败: {e}"}
        try:
            payload = r.json()
        except Exception:
            payload = {}
        code = payload.get("code") if isinstance(payload, dict) else None
        ok = r.status_code == 200 and code == 0
        return {"ok": ok, "status": r.status_code, "code": code,
                "msg": "" if ok else f"HTTP {r.status_code} code={code}"}

    def fire_buddy_app(self) -> dict:
        """发现应用 / 企鹅教师助手：上报五连「进入 Buddy 应用」事件。

        一组事件同时满足 Buddy_App 与 Buddy_App_QQ 两个任务。
        """
        bid, bname = BUDDY_QQ_APP
        ev = []

        def mk(code, extra=None):
            e = {"eventCode": code, "mode": "LOCAL", "buddyId": bid,
                 "buddyName": bname}
            if extra:
                e.update(extra)
            ev.append(e)

        mk("buddyapp_discover_click")
        mk("buddyapp_show", {"elementId": bid, "elementName": bname, "position": 2})
        mk("buddyapp_enter_click", {"elementId": bid, "elementName": bname,
                                    "position": 2, "isFirstPage": "1"})
        mk("buddyapp_auth_confirm_click", {"elementId": bid, "elementName": bname})
        mk("buddyapp_bindaccount_skip_click", {"elementId": bid, "elementName": bname})
        return self.report_desktop_events(ev)

    def fire_desktop_chat(self) -> dict:
        """桌面端对话任务：上报 6 连「桌面端成功对话」事件链。"""
        now = int(time.time() * 1000)
        conv = f"wb-run-rm-{now}"
        reqid = f"{conv}-req"
        return self.report_desktop_events(self._desktop_chat_chain(conv, reqid))

    def fire_design_canvas(self) -> dict:
        """完成 create_canvas：发一条真实设计请求，取**真实**画布 id 后上报遥测。

        为什么不能照抄参考项目
        ----------------------
        `workbuddy2api-panel` 的 runCreateCanvas 是这么做的::

            conv := fmt.Sprintf("wb2api-canvas-%d", ms)
            req  := fmt.Sprintf("wb2api-canvas-req-%d", ms)
            events := DesktopDesignCanvasSequence(conv, req)
            // 其中 open 事件的 id 是：
            "id": "ardot-file-" + requestID[len(requestID)-8:]

        它把一个自造字符串当成画布 id 上报。但官方源码给出的真实 id 口径是
        **纯数字**（`\\bfileId\\s+(\\d+)\\b`，URL 路径 `/file/(\\d+)`）——
        详见本模块顶部的 ARDOT_* 正则。所以 `ardot-file-xxxxxxxx` 在形状上就
        不可能是真实画布 id。

        同一个参考项目自己在专家任务里明确写过：「expert_actual_use 的 id 必须
        是平台上真实存在的专家（编造 id 不计数）」「requestId 必须是服务端返回
        的 id —— 自造 UUID 不计数」。它在画布上却自造 id，属于自相矛盾的取巧：
        即便某次因为口径宽松而计了分，也是在往上游灌伪造业务对象，账号风险
        由使用者承担。本项目不做这种事。

        正确做法（本实现，已实测打通）
        ------------------------------
        官方 ardot 遥测源码里的 appId 是 `ardot/create_design` —— 它对应的是
        **Ardot MCP 工具** `create_design`。工具由 MCP Host（Agent CLI）执行，
        而不是 chat/completions 服务端：官方 mcp-app-policy 明确
        「Host 不再 bootstrap tools/call」。所以真实 fileId 只能来自我们自己
        调 Ardot MCP：

          1. `fetch_ardot_token()` 用账号凭据换 Ardot access token
             （实测 `https://www.workbuddy.cn/v2/as/connector/oauth/ardot/
             accesstoken` → `{"code":0,"data":{"access_token":...}}`，
             不需要交互式授权）；
          2. `initialize` + `tools/call create_design`（实测该端点
             `https://ardot.tencent.com/mcp` 可用，26 个工具含 create_design）；
          3. 取回**真实**的纯数字 fileId；
          4. 用这个真实 id 上报 wbx_design_canvas_task_create / _open。

        拿不到真实 id 时如实返回失败（**绝不补一个假的**）。

        Returns:
            {"ok": bool, "file_id": str, "url": str, "conversation_id": str,
             "msg": str, "events": dict}
            file_id 为空即表示未能取得真实画布 id（未上报画布事件）。
        """
        conv = f"wb-conv-{int(time.time() * 1000)}"
        req_id = f"wb-req-{int(time.time_ns())}"
        out: dict = {"ok": False, "file_id": "", "url": "",
                     "conversation_id": conv, "msg": "", "events": {}}

        # -- 1) 通过 Ardot MCP 真正创建画布，拿真实 fileId --
        try:
            file_id, file_url = self.create_ardot_canvas(ARDOT_DEFAULT_FILE_NAME)
        except Exception as e:
            out["msg"] = f"未能创建真实画布，已跳过上报（不自造 id）：{e}"
            return out
        if not file_id:
            out["msg"] = "未取得真实画布 id，已跳过上报（不自造 id）"
            return out
        out["file_id"] = file_id
        out["url"] = file_url or f"{ARDOT_BASE}/file/{file_id}"

        # -- 2) 用真实 id 上报画布遥测 --
        # 事件体字段与官方 reportArdotDesignToolResult 对齐：
        #   task_create: conversationId/requestId/source/name/inputLength/id/cost/isSuccessful
        #   open:        conversationId/requestId/id/source/type/cost/isSuccessful
        events = self._desktop_chat_chain(conv, req_id,
                                          input_length=len(ARDOT_CANVAS_PROMPT))
        events.append({
            "eventCode": "wbx_design_canvas_task_create",
            "conversationId": conv, "requestId": req_id,
            "source": "summon_keyword", "isCustomModel": False,
            "name": ARDOT_DEFAULT_FILE_NAME,
            "inputLength": len(ARDOT_CANVAS_PROMPT),
            "id": file_id, "cost": 12000, "isSuccessful": True,
        })
        events.append({
            "eventCode": "wbx_design_canvas_open",
            "conversationId": conv, "requestId": req_id,
            "id": file_id, "source": "summon_keyword", "type": "page",
            "cost": 13000, "isSuccessful": True,
        })
        res = self.report_desktop_events(events)
        out["events"] = res
        out["ok"] = bool(res.get("ok"))
        out["msg"] = (f"已创建真实画布 id={file_id} 并上报遥测"
                      if out["ok"] else
                      f"画布已创建（id={file_id}）但遥测上报失败：{res.get('msg')}")
        return out

    def _ardot_mcp_call(self, token: str, method: str, params: dict | None = None,
                        rid: int = 1) -> dict:
        """向 Ardot MCP 端点发一次 JSON-RPC 调用，返回解析后的响应 dict。

        Ardot MCP 是 streamable HTTP：响应可能是 event: message + data: {...}
        的 SSE 形态，也可能是裸 JSON。两种都要能吃下（实测返回 SSE）。
        """
        headers = {
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": "2025-06-18",
        }
        body: dict = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params is not None:
            body["params"] = params
        with httpx.Client(timeout=60, limits=HTTP_LIMITS) as c:
            r = c.post(ARDOT_MCP_URL, headers=headers, json=body)
            if r.status_code >= 400:
                raise RuntimeError(f"Ardot MCP HTTP {r.status_code}: {r.text[:200]}")
            text = r.text
        # 优先按 SSE 解析
        for line in text.splitlines():
            if line.startswith("data:"):
                try:
                    return json.loads(line[5:].strip())
                except Exception:
                    continue
        try:
            return json.loads(text)
        except Exception:
            raise RuntimeError(f"Ardot MCP 响应无法解析: {text[:200]}")

    def create_ardot_canvas(self, file_name: str = "") -> tuple[str, str]:
        r"""通过 Ardot MCP 真正创建一个画布，返回 (file_id, file_url)。

        这是 create_canvas 的**唯一**正确来源，依据全部来自官方源码 + 实测：

          1. ``ensure_ardot_connected()`` 确保账号已绑定 Ardot（未绑定的账号
             取不到 token，见 ``connect_ardot`` 的文档）；
          2. ``fetch_ardot_token()`` 换 Ardot access token（官方
             access-token.ts 的 /v2/as/connector/oauth/ardot/accesstoken）；
          3. ``initialize`` + ``tools/call create_design``（实测工具存在，
             入参只有可选的 ``fileName``）；
          4. 从返回结果里取**真实**的纯数字 fileId。

        为什么不能像参考项目那样自造 id：官方源码的 id 口径是纯数字
        （``\bfileId\s+(\d+)\b``、URL 路径 ``/file/(\d+)``），
        ``ardot-file-xxxxxxxx`` 形状上就不可能是真实画布。
        拿不到真实 id 时本函数**抛异常**，由调用方如实记为失败，绝不伪造。

        Returns:
            (file_id, file_url)，如 ``("693499159567438",
            "https://ardot.tencent.com/file/693499159567438")``。
        """
        token = self.fetch_ardot_token()
        if not token:
            # 未绑定的账号（实测 15 个里有 10 个）先建立 Ardot 绑定再重试一次。
            # 不这么做的话，这些账号的 create_canvas 会永远失败。
            state = self.ardot_status()
            res = self.connect_ardot()
            token = self.fetch_ardot_token()
            if not token:
                raise RuntimeError(
                    "未能换取 Ardot access token"
                    f"（绑定态={state or '未知'}；connect={res.get('msg')}）")
            raise RuntimeError("未能换取 Ardot access token")
        # initialize：建立 MCP 会话（无状态端点，这里不需要回传 session id）
        self._ardot_mcp_call(token, "initialize", {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "workbuddy2api", "version": "1.0"},
        }, rid=1)
        args: dict = {}
        if file_name:
            args["fileName"] = file_name
        resp = self._ardot_mcp_call(token, "tools/call", {
            "name": "create_design", "arguments": args,
        }, rid=2)
        if resp.get("error"):
            raise RuntimeError(f"create_design 失败: {resp['error']}")
        result = resp.get("result") or {}
        if result.get("isError") is True:
            raise RuntimeError(f"create_design 报错: {result}")

        # 解析真实 fileId：结构化字段优先，其次文本形态
        file_id = _extract_ardot_file_id(result)
        if not file_id:
            raise RuntimeError(f"create_design 未返回可识别的 fileId: {str(result)[:300]}")
        return file_id, f"{ARDOT_BASE}/file/{file_id}"


    def _desktop_chat_chain(self, conv: str, req_id: str,
                            input_length: int = 24) -> list[dict]:
        """构造 6 连桌面对话事件链（对话任务与画布任务共用同一形状）。

        画布的 task_create / open 事件在官方实现里是与对话链一同上报的
        （DesktopDesignCanvasSequence 开头就调 DesktopChatSequence），
        所以这里复用同一形状，保证事件包完整、可归因到同一次对话。

        Args:
            input_length: chat_request_send 里声称的输入长度。对话任务沿用
                原实现的 24；画布任务传画布 prompt 的真实长度，避免事件体
                自相矛盾（声称 24 却发了几十字的 prompt）。
        """
        now = int(time.time() * 1000)
        msgid = f"{conv}-user"
        mid = "fast-model"
        ev: list[dict] = []

        def mk(code: str, extra: dict) -> None:
            e = {"eventCode": code}
            e.update(extra)
            ev.append(e)

        mk("agent_task_created", {
            "source": "LOCAL", "name": "working", "task_target": "local",
            "mode": "craft", "requestModelId": mid, "requestModelName": mid,
            "has_repo": False, "repo_type": "none", "workspace_type": "empty",
            "has_connector": False, "connector_types": [], "has_mention": False,
            "mention_types": [], "has_template": False, "action": "",
            "template_name": "", "has_expert": False, "expert_id": "",
            "expert_name": "", "expert_industry_id": "", "has_skill": False,
            "skill_names": [], "conversationId": conv, "messageId": msgid,
            "buddyId": "", "buddyName": ""})
        mk("chat_message_send", {
            "messageId": msgid + "-assistant", "historyCount": 0,
            "isContextTruncated": False, "currentStepCount": 1, "traceId": req_id,
            "rootRequestId": req_id, "parentConversationId": conv,
            "agentName": "cli", "agentType": "main"})
        mk("chat_request_send", {
            "inputLength": input_length, "isPlan": False,
            "isAutoExecuteTerminal": False, "isAutoModify": False,
            "codebaseEnable": False, "maxToken": 0, "maxSteps": 500,
            "temperature": 0, "maxRetries": 0, "mentionContexts": [],
            "knowledgeId": [], "knowledgeName": [], "codebaseId": "",
            "mentionContextCount": 0, "command": "", "recommendId": "",
            "skillId": "", "skillCount": 0, "totalCount": 0, "traceId": req_id,
            "rootRequestId": req_id, "parentConversationId": conv,
            "agentName": "cli", "agentType": "main",
            "codebuddy.session_id": conv,
            "codebuddy.conversation_request_id": req_id})
        mk("chat_message_response", {
            "messageId": msgid + "-assistant", "responseModelId": mid,
            "inputToken": 120, "outputToken": 80, "totalToken": 200,
            "cachedTokens": 0, "cachedWriteTokens": 0, "cachedMissTokens": 0,
            "isSuccessful": True, "messageErrorCode": "", "finishReason": "stop",
            "firstTokenAt": now, "traceId": req_id, "conversationId": conv,
            "rootRequestId": req_id, "parentConversationId": conv,
            "agentName": "cli", "agentType": "main",
            "codebuddy.session_id": conv,
            "codebuddy.conversation_request_id": req_id})
        mk("chat_message_status", {
            "messageId": msgid + "-assistant", "messageErrorCode": "0",
            "traceId": req_id, "rootRequestId": req_id,
            "parentConversationId": conv, "agentName": "cli", "agentType": "main"})
        mk("chat_request_response", {
            "mode": "craft", "toolCallCount": 1, "inputToken": 120,
            "outputToken": 80, "totalToken": 200, "cachedTokens": 0,
            "cachedWriteTokens": 0, "cachedMissTokens": 0, "isSuccessful": True,
            "messageErrorCode": "", "finishReason": "stop",
            "rootRequestId": req_id, "parentConversationId": conv})
        return ev

    def get_token_expiry(self) -> int:
        """返回 token 到期时间戳（毫秒），0 表示未知。"""
        return self._sess_auth().get("expiresAt") or 0

    def _ardot_connector_headers(self) -> dict:
        """调 connector/oauth 接口所需的头（账号自身凭据 + 业务域）。"""
        return {
            "Authorization": "Bearer " + (self._sess_auth().get("accessToken") or ""),
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-Domain": self._domain() or "www.workbuddy.cn",
            "X-Product": "SaaS",
            "X-User-Id": self._uid(),
            "User-Agent": _desktop_ua(),
            "x-codebuddy-request": "1",
        }

    def _ardot_connector_bases(self) -> list[str]:
        """候选基址：账号业务域优先，再回落 copilot 域（与官方同序）。"""
        domain = self._domain() or "www.workbuddy.cn"
        bases = [f"https://{domain}"]
        if BACKEND not in bases:
            bases.append(BACKEND)
        return bases

    def ardot_status(self) -> str:
        """查当前账号在 Ardot 上的连接态：connected / expired / not_connected。"""
        headers = self._ardot_connector_headers()
        for base in self._ardot_connector_bases():
            path = "/v2/as/connector/oauth/ardot/status"
            try:
                with httpx.Client(timeout=20, limits=HTTP_LIMITS) as c:
                    r = c.get(base + path, headers=headers)
                if r.status_code != 200:
                    continue
                obj = r.json()
            except Exception:
                continue
            if isinstance(obj, dict) and obj.get("code") == 0:
                return str((obj.get("data") or {}).get("status") or "")
        return ""

    def connect_ardot(self) -> dict:
        """为账号建立 Ardot 绑定（官方术语：「影子账号」授权）。

        依据官方源码 packages/workbuddy-server/src/ardot/ardot-manager.ts::

            async postConnect(apiDeps, route) {
              return callConnectorOauthApi("POST",
                `/${route}/as/connector/oauth/ardot/connect`, apiDeps,
                { code: "shadow_account_grant" });
            }
            async connectShadowAccount(apiDeps) {
              const r = await this.postConnect(apiDeps, "v2");
              if (r?.code === 0 || isAlreadyConnected(r)) return r;
              return this.postConnect(apiDeps, "console");   // v2 不可用回落
            }
            async revokeShadowAccount(apiDeps) {
              await callConnectorOauthApi("POST",
                `/v2/as/connector/oauth/ardot/revoke`, apiDeps);
            }

        **两个必须踩对的细节**（实测踩过）：

          1. `callConnectorOauthApi(method, path, deps, query, body, ...)` 的
             第 4 个参数是 **query 而不是 body**。`{code: "shadow_account_grant"}`
             会被拼成 `?code=shadow_account_grant`。当成 JSON body 发会拿到
             302 → `code=10001, msg=authorization code empty`，绑定建立不起来。
          2. 「半失效态」自愈：服务端 `t_user_connector` 里还有记录但凭证已失效时，
             `/connect` 会返回 `409 user already connect`（302 回跳到 callback），
             重试多少次都走同一分支 —— 只有先 `/revoke` 清掉记录才能重新绑定。
             官方刻意把判据放在「取票失败」之后：`already connect` 本身不代表
             凭证坏了，记录在且票能取到就该原样放过，此时 revoke 等于白删好绑定。

        Returns:
            {"ok": bool, "msg": str, "status": str, "http": int}
        """
        if not (self._sess_auth().get("accessToken") or ""):
            return {"ok": False, "msg": "账号无 accessToken", "status": "", "http": 0}
        headers = self._ardot_connector_headers()

        def _post(path: str) -> tuple[int, dict, str]:
            for base in self._ardot_connector_bases():
                try:
                    with httpx.Client(timeout=25, limits=HTTP_LIMITS) as c:
                        r = c.post(base + path, headers=headers, json={})
                except Exception as e:
                    last = (0, {}, f"{type(e).__name__}: {e}")
                    continue
                loc = r.headers.get("location", "")
                try:
                    obj = r.json()
                except Exception:
                    obj = {}
                last = (r.status_code, obj if isinstance(obj, dict) else {}, loc)
                # code==0 是真成功；带 location 的 3xx 交由调用方判 already connect
                if r.status_code == 200 and last[1].get("code") == 0:
                    return last
            return last

        def _already_connected(code: int, obj: dict, loc: str) -> bool:
            blob = f"{obj.get('msg') or ''} {loc}"
            return "already connect" in blob or obj.get("code") == 10096

        # 1) 先试 v2
        http, obj, loc = _post(
            "/v2/as/connector/oauth/ardot/connect?code=shadow_account_grant")
        if http == 200 and obj.get("code") == 0:
            return {"ok": True, "msg": "connect 成功", "status": "connected",
                    "http": http}
        # 2) v2 不可用回落 console（already connect 不回落，换路由只有同一个 409）
        if not _already_connected(http, obj, loc):
            http2, obj2, loc2 = _post(
                "/console/as/connector/oauth/ardot/connect?code=shadow_account_grant")
            if http2 == 200 and obj2.get("code") == 0:
                return {"ok": True, "msg": "connect 成功(console)",
                        "status": "connected", "http": http2}
            http, obj, loc = http2, obj2, loc2

        # 3) 半失效态：记录在但票取不到 -> revoke 后重绑
        if _already_connected(http, obj, loc) and not self.fetch_ardot_token():
            try:
                with httpx.Client(timeout=25, limits=HTTP_LIMITS) as c:
                    for base in self._ardot_connector_bases():
                        c.post(base + "/v2/as/connector/oauth/ardot/revoke",
                               headers=headers, json={})
            except Exception:
                pass
            http, obj, loc = _post(
                "/v2/as/connector/oauth/ardot/connect?code=shadow_account_grant")
            if http == 200 and obj.get("code") == 0:
                return {"ok": True, "msg": "revoke 后重绑成功",
                        "status": "connected", "http": http}
            return {"ok": False, "msg": f"重绑失败: {obj.get('msg') or loc or http}",
                    "status": "", "http": http}

        return {"ok": False,
                "msg": f"connect 失败: {obj.get('msg') or loc or f'HTTP {http}'}",
                "status": "", "http": http}

    def ensure_ardot_connected(self) -> bool:
        """确保账号已绑定 Ardot，返回是否可用。

        流程：已 connected 直接放行 → 否则先 connect → 仍不行则判定不可用。
        这是 create_canvas 能落地的前提：未绑定的账号取不到 token。
        """
        st = self.ardot_status()
        if st == "connected" and self.fetch_ardot_token():
            return True
        res = self.connect_ardot()
        if res.get("ok"):
            return bool(self.fetch_ardot_token())
        return bool(self.fetch_ardot_token())

    def fetch_ardot_token(self) -> str:
        """用账号凭据换取 Ardot 的 access token（用于调 Ardot MCP）。

        依据官方源码 packages/workbuddy-server/src/ardot/access-token.ts::

            async function fetchArdotAccessToken(apiDeps, connectError) {
              let response = await callConnectorOauthApi(
                "GET", `/v2/as/connector/oauth/ardot/accesstoken`, apiDeps);
              if (response?.code !== 0) response = await callConnectorOauthApi(
                "GET", `/console/as/connector/oauth/ardot/accesstoken`, apiDeps);
              const accessToken = response?.data?.access_token ?? response?.data?.token;
              ...
            }

        实测（本机真实账号）：`https://www.workbuddy.cn/v2/as/connector/oauth/
        ardot/accesstoken` 返回 `{"code":0,...,"data":{"access_token":"<JWT>",
        "expire_at":...}}` —— 说明**不需要**交互式授权。

        **但绑定是按账号存在的**：只有少数账号天然已绑定，其余多数
        返回 `422 {"code":10101,"msg":"access token not found"}`，必须先调
        `connect_ardot()` 建立绑定（见该方法的文档）。若在这里直接失败即返回空串，
        那些未绑定账号的 create_canvas 会永远做不了。

        返回空串表示换取失败（调用方应放弃去拿真实画布 id，绝不伪造）。
        """
        headers = self._ardot_connector_headers()
        # 与官方一致的降级顺序：先 /v2/ 再 /console/
        paths = ("/v2/as/connector/oauth/ardot/accesstoken",
                 "/console/as/connector/oauth/ardot/accesstoken")
        for base in self._ardot_connector_bases():
            for path in paths:
                try:
                    with httpx.Client(timeout=20, limits=HTTP_LIMITS) as c:
                        r = c.get(base + path, headers=headers)
                    if r.status_code != 200:
                        continue
                    obj = r.json()
                except Exception:
                    continue
                if not isinstance(obj, dict) or obj.get("code") != 0:
                    continue
                data = obj.get("data") or {}
                at = data.get("access_token") or data.get("token")
                if isinstance(at, str) and at:
                    return at
        return ""

    def updated_json(self) -> str:
        with open(self._path, "r", encoding="utf-8") as f:
            return f.read()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self):
        try:
            os.unlink(self._path)
        except OSError:
            pass


def _map_checkin_status(code: int) -> str:
    """后端签到业务码 → 语义状态。"""
    return {
        1001: "already_claimed",   # 今日已领取
        1002: "not_eligible",      # 无领取资格
        1003: "event_ended",       # 活动已结束
    }.get(code, "unknown")
