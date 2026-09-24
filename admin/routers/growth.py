"""成长计划任务：列表 / 参与 / 完成 / 领奖。

设计要点
--------
* **任务列表不绑账号**：任意一个可用登录态即可拉取任务定义，结果缓存在
  内存里供前端与定时任务复用（任务定义对所有账号一致，仓库里只存"定义"）。
* **完成操作按 task_code 走策略表**（admin/growth_plans.py）：只对已实测
  可行的任务发包，其余标记为需人工，避免盲目请求污染上游日志。
* **串行 + 限速**：逐个账号、逐个任务执行，中间 sleep，避免触发风控。
"""
import time
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from admin import backend, growth_plans, jobrunner
from admin.config import settings
from admin.db import SessionLocal, get_db
from admin.models import Account
from admin.security import require_admin

# ⚠️ 必须整组挂 require_admin（安全审计「严重」项）。
# 历史实现的这个 router **一个鉴权依赖都没有**，于是：
#   * `/api/growth/accounts/{id}/tasks` 可以被匿名者从 id=1 递增枚举，
#     直接拿到全部账号的 uid 与**显示名**（实测泄露 18022387641 这类手机号）；
#   * `/api/growth/run-async`、`/run`、`/accept`、`/claim` 可以被匿名者直接调用，
#     在服务器上启动批量任务、真实消耗账号额度并写库 —— 不只是信息泄露，
#     是**有副作用**的未授权操作。
# 挂在 router 上而不是逐个接口，是为了杜绝「以后新增接口又忘了加」。
router = APIRouter(prefix="/api/growth", tags=["growth"],
                   dependencies=[Depends(require_admin)])

#: 任务定义缓存 {"tasks": [...], "synced_at": iso}
_task_cache: dict = {}

#: 执行节流（秒）
_EVENT_GAP = 1.2          # 同一任务内两次触发之间
_ACCOUNT_GAP = 1.0        # 两个账号之间

#: 轮询参数：等上游落库时用「轮询到就绪」，而不是固定 sleep。
#: 超时只是兜底上限，正常情况远早于它返回。
_POLL_INTERVAL = 0.5      # 轮询间隔
_ACCEPT_TIMEOUT = 6.0     # 等 accept 参与状态落库
_VERIFY_TIMEOUT = 6.0     # 等触发后进度落库

_MAX_TIMES = 10           # 单任务最多触发次数上限

#: 单个账号的总时间预算（秒）。
#: 上游慢或某任务一直不达标时，各阶段的超时虽然都有上限，但会累加：
#: 4 个任务 × (accept 6s + 触发 N×1.2s + 复查 6s) 就可能到几分钟。
#: 一个异常账号足以让整个批量看起来「卡住不动」，所以再加一道账号级兜底。
_ACCOUNT_BUDGET = 45.0

#: 触发事件时优先使用的免费模型（0 倍率），用完再退回低倍率
_PREFERRED_MODELS = ["hy3", "hunyuan-chat"]


def _pick_free_model(db: Session) -> str:
    """挑一个免费（0 倍率）模型，没有则退回传参默认值。

    任务记账不依赖模型输出质量，用免费模型可以把成本压到 0。
    """
    from admin.models import ModelConfig
    for mid in _PREFERRED_MODELS:
        row = (db.query(ModelConfig)
               .filter(ModelConfig.model_id == mid, ModelConfig.enabled == 1)
               .first())
        if row and (row.credit_multiplier or 0) == 0:
            return mid
    row = (db.query(ModelConfig)
           .filter(ModelConfig.enabled == 1, ModelConfig.credit_multiplier == 0)
           .order_by(ModelConfig.model_id.asc()).first())
    return row.model_id if row else "hy3"


def _pick_account(db: Session) -> Account | None:
    """挑一个可用账号（仅用于拉取任务定义，不写任何东西）。"""
    return (db.query(Account)
            .filter(Account.status == "active")
            .order_by(Account.id.asc()).first())


def _classify_all(tasks: list[dict]) -> list[dict]:
    return [growth_plans.classify(t) for t in tasks]


class AcceptIn(BaseModel):
    account_ids: list[int]
    task_codes: list[str]


class RunIn(BaseModel):
    account_ids: list[int] = []           # 空 = 全部可用账号
    task_codes: list[str] | None = None   # 为空表示「所有可自动完成的任务」


class ClaimIn(BaseModel):
    account_ids: list[int]
    task_codes: list[str] | None = None   # 为空表示「所有已完成待领取的任务」


@router.get("/tasks")
def growth_tasks(refresh: bool = False, db: Session = Depends(get_db)):
    """获取全量任务列表（带分级信息），不绑定具体账号。

    任务定义对所有账号一致，所以随便用一个可用登录态拉取即可。
    """
    if _task_cache.get("tasks") and not refresh:
        cached = dict(_task_cache)
        cached["cached"] = True
        return cached

    acc = _pick_account(db)
    if not acc:
        raise HTTPException(400, "没有可用账号，无法拉取任务列表")

    try:
        with backend.AccountSession(acc.auth_json) as s:
            raw = s.growth_tasks()
    except Exception as e:
        # 拉取失败时退回旧缓存，避免面板整体不可用
        if _task_cache.get("tasks"):
            stale = dict(_task_cache)
            stale.update({"cached": True, "stale": True, "error": str(e)})
            return stale
        raise HTTPException(502, f"拉取任务列表失败: {e}")

    _task_cache.clear()
    _task_cache.update({
        "tasks": _classify_all(raw),
        "synced_at": datetime.utcnow().isoformat(timespec="seconds"),
        "source_account_id": acc.id,
        "cached": False,
    })
    return dict(_task_cache)


def _run_accept(acc: Account, codes: list[str]) -> dict:
    """在一个账号上执行参与，并回写可能被刷新的凭据。"""
    with backend.AccountSession(acc.auth_json) as s:
        try:
            st = s.growth_accept(codes)
        finally:
            acc.auth_json = _updated(s, acc)
    return st


@router.post("/accept")
def growth_accept(payload: AcceptIn, db: Session = Depends(get_db)):
    """批量参与任务（未参与的任务不会累计进度）。"""
    results = []
    for aid in payload.account_ids:
        acc = db.query(Account).get(aid)
        if not acc:
            results.append({"account_id": aid, "ok": False, "msg": "账号不存在"})
            continue
        try:
            st = _run_accept(acc, payload.task_codes)
            results.append({"account_id": aid, "ok": True, "results": st})
        except Exception as e:
            results.append({"account_id": aid, "ok": False, "msg": str(e)})
        time.sleep(_ACCOUNT_GAP)
    db.commit()
    return {"results": results}


def _find_task(session, task_code: str) -> dict | None:
    """重新读取单个任务的最新状态（accept 后刷新用）。

    注意：每次调用都会拉一次完整任务列表（上游 1.3~2.4 秒）。
    因此**绝不能**把它放进高频轮询里——否则轮询间隔会被单次请求耗时
    顶到几秒，8 秒的上限内反复拉列表，账号一多就非常慢。
    轮询应改用 _task_state（带缓存）。
    """
    try:
        for t in session.growth_tasks():
            if t.get("task_code") == task_code:
                return t
    except Exception:
        pass
    return None


class _TaskState:
    """轮询用的任务状态缓存：在 min_interval 内复用上一次的列表结果。

    上游拉一次列表要 1~2 秒，如果轮询里每次都重新拉，既慢又浪费，
    还会让「8 秒超时」变成实际十几秒。这里缓存一个短窗口（默认 1 秒），
    既能看到状态变化，又不会把上游打爆。
    """

    def __init__(self, session, min_interval: float = 1.0):
        self._s = session
        self._min = min_interval
        self._ts = 0.0
        self._snapshot: list | None = None

    def _list(self, force: bool = False) -> list:
        now = time.monotonic()
        if force or self._snapshot is None or (now - self._ts) >= self._min:
            try:
                self._snapshot = self._s.growth_tasks()
                self._ts = now
            except Exception:
                if self._snapshot is None:
                    self._snapshot = []
                # 拉取失败时保留旧快照，等下次窗口再试
        return self._snapshot or []

    def get(self, task_code: str) -> dict | None:
        for t in self._list():
            if t.get("task_code") == task_code:
                return t
        return None

    def refresh(self) -> list:
        """强制拉一次最新列表（需要精确状态时用）。"""
        return self._list(force=True)


def _updated(session, acc: Account) -> str:
    """AccountSession 关闭前读回最新凭据（token 可能被刷新）。"""
    try:
        return session.updated_json()
    except Exception:
        return acc.auth_json


def run_accounts(account_ids: list[int], task_codes: list[str] | None,
                 db: Session, on_done=None, on_beat=None) -> dict:
    """对一批账号执行「自动参与 + 触发完成 + 领取」。供接口与定时任务共用。

    这里沉淀了串行执行与节流逻辑，定时任务必须复用本函数，
    避免绕过 accept 落库等待而出现「任务不成功」的问题。

    与早期版本的差别：不再用固定 sleep 死等上游落库，改成**轮询到就绪为止**
    （`jobrunner.wait_for`）。固定 sleep 在两种情况下都吃亏：
      * 上游快时白等（原本每账号约 27 秒，大半是干等）
      * 上游慢时不够（复查时进度还没落库，于是报「已完成未领取」，
        用户得再点一次才领到 —— 就是体感上的「分成两三步」）

    Args:
        on_done: 每完成一个账号回调一次，用于上报进度（可为 None）。
    """
    model = _pick_free_model(db)
    out = []
    for aid in account_ids:
        acc = db.query(Account).get(aid)
        if not acc:
            item = {"account_id": aid, "ok": False, "msg": "账号不存在"}
            out.append(item)
            if on_done:
                on_done(item)
            continue

        # 开始处理这个账号时先报一次心跳，界面才能显示「正在处理 xxx」，
        # 否则慢账号期间进度条纹丝不动，看起来像卡死
        if on_beat:
            try:
                on_beat(acc.name or str(aid))
            except Exception:
                pass

        acc_log = {"account_id": aid, "name": acc.name, "model": model,
                   "ok": True, "tasks": []}
        snapshot = None
        # 单账号总预算：无论上游多慢、任务多少，超过就收尾进入下一个账号。
        # 没有这个兜底时，一个异常账号可能把整个批量拖住很久（表现为
        # 「卡在 N/M 不动了」）。达到预算会记录超时提示，不会静默丢弃。
        acc_deadline = time.monotonic() + _ACCOUNT_BUDGET
        try:
            with backend.AccountSession(acc.auth_json) as s:
                tasks = s.growth_tasks()
                by_code = {t.get("task_code"): t for t in tasks}

                wanted = task_codes or [
                    t.get("task_code") for t in tasks
                    if growth_plans.plan_for(t.get("task_code") or "").actionable
                    and t.get("accept_status") not in ("claimed", "completed")
                ]

                # 快速通道：没有可做的任务、也没有待领取的奖励时，
                # 直接返回。拉一次列表已经是全部开销，不再多打任何请求。
                # （号池做完后就是这种状态，10 个账号应秒过而不是等几十秒）
                claimable = [t.get("task_code") for t in tasks
                             if t.get("accept_status") == "completed"]
                if not wanted and not claimable:
                    auto_total = claimed_n = 0
                    for t in tasks:
                        plan = growth_plans.plan_for(t.get("task_code") or "")
                        if not plan.actionable:
                            continue
                        auto_total += 1
                        if t.get("accept_status") == "claimed":
                            claimed_n += 1
                    acc_log["tasks"] = [
                        {"task_code": t.get("task_code"),
                         "title": t.get("title") or t.get("task_code"),
                         "level": growth_plans.plan_for(t.get("task_code") or "").level,
                         "ok": True, "skipped": "无可做任务"}
                        for t in tasks
                        if growth_plans.plan_for(t.get("task_code") or "").actionable
                    ]
                    acc_log["noop"] = True
                    # 把「为什么没得做」说清楚，否则界面只有一句「无可做任务」，
                    # 看不出是「已全部领完」还是「任务被卡住了」
                    acc_log["noop_reason"] = (
                        f"可自动化的 {auto_total} 个任务已全部领取" if auto_total
                        else "该账号没有可自动化的任务"
                    )
                    if auto_total:
                        acc_log["skipped_claimed"] = claimed_n
                    out.append(acc_log)
                    if on_done:
                        on_done(acc_log)
                    continue

                for code in wanted:
                    info = by_code.get(code) or {}
                    plan = growth_plans.plan_for(code)
                    item = {"task_code": code, "title": info.get("title") or code,
                            "level": plan.level}

                    if info.get("accept_status") == "claimed":
                        item.update({"ok": True, "skipped": "已领取"})
                        acc_log["tasks"].append(item)
                        continue
                    if not plan.actionable:
                        # 需人工完成的任务直接跳过，绝不发请求：
                        # 之前这里也会走完整流程（含拉列表复查），是纯浪费
                        item.update({"ok": True, "skipped": plan.reason or "需人工完成"})
                        acc_log["tasks"].append(item)
                        continue
                    if time.monotonic() >= acc_deadline:
                        # 单账号预算用完：如实标记，不静默跳过
                        item.update({"ok": False, "skipped": "本账号超时，剩余任务留待下次"})
                        acc_log["tasks"].append(item)
                        acc_log["budget_exceeded"] = True
                        continue

                    # 参与（未参与的任务不计进度）：
                    # 轮询等 accept 落库，而不是固定 sleep 3 秒。
                    # 用缓存轮询：上游拉一次列表要 1~2 秒，直接放在轮询里
                    # 会把「8 秒上限」拖成实际十几秒。
                    state = _TaskState(s)
                    if info.get("accept_status") == "not_accepted":
                        try:
                            s.growth_accept([code])

                            def _accepted(c=code):
                                t = state.get(c)
                                return bool(t and t.get("accept_status") not in
                                            (None, "", "not_accepted"))

                            jobrunner.wait_for(_accepted, _ACCEPT_TIMEOUT,
                                               _POLL_INTERVAL)
                            info = state.refresh() and state.get(code) or info
                        except Exception as e:
                            item["accept_error"] = str(e)

                    prog = info.get("progress") or {}
                    target = prog.get("target")
                    current = prog.get("current") or 0
                    times = (target - current) if isinstance(target, int) and target > current else 1
                    times = max(1, min(times, _MAX_TIMES))

                    fire_model = plan.model or model
                    # Ardot 类任务的前置条件：账号必须已绑定 Ardot，否则取票会
                    # 得到 10101 access token not found，任务**必然**失败。
                    # 实测 15 个账号里 10 个初始未绑定，所以这里主动补绑定，
                    # 而不是让用户看到一条「未换取 Ardot access token」的报错。
                    if plan.firer == "fire_design_canvas":
                        try:
                            if not s.ensure_ardot_connected():
                                item["last_error"] = (
                                    "账号未能绑定 Ardot（connector 授权失败），"
                                    "该任务需要先完成 Ardot 授权")
                                acc_log["tasks"].append(item)
                                continue
                        except Exception as e:
                            item["last_error"] = f"Ardot 绑定检查失败：{e}"

                    fired = 0
                    for _ in range(times):
                        # 两种触发方式：
                        #   firer 非空 -> 调用 AccountSession 上的对应方法
                        #                （上报真实业务事件，如 fire_library_read）
                        #   firer 为空 -> 走 chat/completions 的 growthEvent
                        if plan.firer:
                            fn = getattr(s, plan.firer, None)
                            r = (fn() if fn else
                                 {"ok": False, "msg": f"缺少触发方法 {plan.firer}"})
                        else:
                            r = s.growth_fire_event(plan.event_codes, model=fire_model)
                        if r.get("ok"):
                            fired += 1
                        else:
                            item["last_error"] = r.get("msg")
                        time.sleep(_EVENT_GAP)

                    # 复查：轮询到进度变化为止，避免「已完成但没领」的假象
                    def _advanced(c=code, cur=current):
                        n = state.get(c)
                        if not n:
                            return False
                        st = n.get("accept_status")
                        if st in ("completed", "claimed"):
                            return True
                        np_ = (n.get("progress") or {}).get("current")
                        return isinstance(np_, int) and isinstance(cur, int) and np_ > cur

                    jobrunner.wait_for(_advanced, _VERIFY_TIMEOUT, _POLL_INTERVAL)

                    try:
                        snapshot = state.refresh()
                        now = {t.get("task_code"): t for t in snapshot}
                        cur_task = now.get(code) or {}
                        np_ = cur_task.get("progress") or {}
                        item["progress"] = f"{np_.get('current')}/{np_.get('target')}"
                        item["status"] = cur_task.get("accept_status")
                    except Exception:
                        pass
                    item.update({"ok": fired > 0, "fired": fired, "times": times})
                    if plan.model:
                        item["model"] = plan.model
                    acc_log["tasks"].append(item)

                # 本账号跑完顺手领取，避免用户还要再点一次「领奖」。
                # 复用最后那次复查拿到的快照，不再额外拉一次列表。
                try:
                    if snapshot is not None:
                        done_codes = [t.get("task_code") for t in snapshot
                                      if t.get("accept_status") == "completed"]
                    else:
                        done_codes = [t.get("task_code") for t in s.growth_tasks()
                                      if t.get("accept_status") == "completed"]
                    claimed = []
                    for code in done_codes:
                        try:
                            r = s.growth_claim(code)
                            d = r.get("data") or {}
                            claimed.append({"task_code": code, "ok": bool(r.get("ok")),
                                            "credit": d.get("credit") or 0,
                                            "energy": d.get("energy") or 0})
                        except Exception:
                            pass
                        # 只在真要领多个时才留间隔
                        if len(done_codes) > 1:
                            time.sleep(_EVENT_GAP)
                    if claimed:
                        acc_log["claimed"] = claimed
                        acc_log["credit"] = sum(c.get("credit") or 0 for c in claimed)
                        acc_log["energy"] = sum(c.get("energy") or 0 for c in claimed)
                except Exception:
                    pass

                acc.auth_json = _updated(s, acc)
        except Exception as e:
            acc_log.update({"ok": False, "msg": str(e)})
        out.append(acc_log)
        # 每跑完一个账号就上报进度。之前只在「账号不存在」分支调用，
        # 正常路径从不回调，前端因此一直停在 0/N，直到全部跑完才跳到 N/N。
        if on_done:
            on_done(acc_log)
        time.sleep(_ACCOUNT_GAP)

    db.commit()
    return {"results": out, "model": model}


def claim_accounts(account_ids: list[int], task_codes: list[str] | None,
                   db: Session) -> dict:
    """对一批账号领取奖励。供接口与定时任务共用。"""
    out = []
    for aid in account_ids:
        acc = db.query(Account).get(aid)
        if not acc:
            out.append({"account_id": aid, "ok": False, "msg": "账号不存在"})
            continue

        acc_log = {"account_id": aid, "name": acc.name, "ok": True,
                   "claimed": [], "total_credit": 0, "total_energy": 0}
        try:
            with backend.AccountSession(acc.auth_json) as s:
                if task_codes:
                    codes = list(task_codes)
                else:
                    tasks = s.growth_tasks()
                    codes = [t.get("task_code") for t in tasks
                             if t.get("accept_status") == "completed"]

                for code in codes:
                    try:
                        r = s.growth_claim(code)
                        d = r.get("data") or {}
                        credit = d.get("credit") or 0
                        energy = d.get("energy") or 0
                        acc_log["claimed"].append({
                            "task_code": code,
                            "ok": bool(r.get("ok")),
                            "already": bool(d.get("already_claimed")),
                            "credit": credit,
                            "energy": energy,
                            "msg": r.get("msg") or "",
                        })
                        acc_log["total_credit"] += credit
                        acc_log["total_energy"] += energy
                    except Exception as e:
                        acc_log["claimed"].append(
                            {"task_code": code, "ok": False, "msg": str(e)})
                    time.sleep(_EVENT_GAP)
                acc.auth_json = _updated(s, acc)
        except Exception as e:
            acc_log.update({"ok": False, "msg": str(e)})
        out.append(acc_log)
        time.sleep(_ACCOUNT_GAP)

    db.commit()
    return {"results": out}


@router.post("/run")
def growth_run(payload: RunIn, db: Session = Depends(get_db)):
    """执行任务：自动参与 + 触发完成事件。

    只处理策略表里标记为可自动的任务；其它任务跳过并在返回里说明原因。
    """
    return run_accounts(payload.account_ids, payload.task_codes, db)


#: 后台任务 key：同 key 同时只允许一个在跑
JOB_KEY = "growth_run"


def _resolve_ids(payload_ids: list[int] | None) -> list[int]:
    """把请求里的 ids 解析成实际要处理的账号 id 列表（空 = 全部 active）。"""
    db = SessionLocal()
    try:
        if payload_ids:
            return list(payload_ids)
        rows = (db.query(Account)
                .filter(Account.status == "active")
                .order_by(Account.id.asc()).all())
        return [a.id for a in rows]
    finally:
        db.close()


@router.post("/run-async")
def growth_run_async(payload: RunIn):
    """异步批量做任务：立即返回 job_id，前端轮询进度。

    为什么异步：一个账号约 27 秒（串行 + 限速），13 个账号要 6 分钟以上，
    同步等会让 nginx 先超时（默认 60s）→ 前端吃 504、体感「点了没反应」。

    重复点击不会叠起并发批量：同 key 已有任务在跑时直接返回现有 job。
    """
    running = jobrunner.RUNNER.get(JOB_KEY)
    if running and running.status == "running":
        return {"reused": True, **running.snapshot()}

    ids = payload.account_ids or _resolve_ids(None)
    tasks = payload.task_codes

    def worker(job: jobrunner.Job) -> dict:
        db = SessionLocal()
        try:
            job.set_phase("执行中")
            res = run_accounts(ids, tasks, db, on_done=job.add_item,
                               on_beat=job.beat)
        finally:
            db.close()
        results = res.get("results") or []
        credit = sum((r.get("credit") or 0) for r in results)
        energy = sum((r.get("energy") or 0) for r in results)
        done = sum(1 for r in results for t in (r.get("tasks") or [])
                   if t.get("ok") and not t.get("skipped"))
        return {"accounts": len(ids), "tasks_done": done,
                "credit": credit, "energy": energy,
                "failed": [r["account_id"] for r in results if not r.get("ok")]}

    job = jobrunner.RUNNER.start(JOB_KEY, len(ids), worker, title="批量做任务")
    return job.snapshot()


@router.get("/job/{job_id}")
def growth_job(job_id: str):
    """查询后台任务进度。"""
    job = jobrunner.RUNNER.by_id(job_id)
    if not job:
        raise HTTPException(404, "任务不存在或已过期")
    return job.snapshot()


@router.post("/claim")
def growth_claim(payload: ClaimIn, db: Session = Depends(get_db)):
    """批量领取奖励。

    task_codes 为空时，自动领取所有「已完成但未领取」（completed）的任务。
    """
    return claim_accounts(payload.account_ids, payload.task_codes, db)


@router.get("/accounts/{account_id}/tasks")
def account_tasks(account_id: int, db: Session = Depends(get_db)):
    """单个账号的任务完成情况（面板弹窗用）。"""
    acc = db.query(Account).get(account_id)
    if not acc:
        raise HTTPException(404, "账号不存在")
    try:
        with backend.AccountSession(acc.auth_json) as s:
            tasks = s.growth_tasks()
            profile = s.growth_profile()
    except Exception as e:
        raise HTTPException(502, f"查询失败: {e}")

    items = _classify_all(tasks)
    done = sum(1 for t in items if t.get("accept_status") in ("completed", "claimed"))
    #: 可领取奖励（completed 但未 claim）
    claimable = [t for t in items if t.get("accept_status") == "completed"]
    return {
        "account_id": account_id,
        "name": acc.name,
        "tasks": items,
        "profile": profile,
        "summary": {
            "total": len(items),
            "done": done,
            "claimable": len(claimable),
            "claimable_credit": sum(t.get("reward_credit") or 0 for t in claimable),
            "actionable": sum(1 for t in items if t.get("actionable")
                              and t.get("accept_status") != "claimed"),
        },
    }


@router.get("/plans")
def growth_plans_view():
    """任务策略表（分级依据），便于管理员了解哪些能自动完成。"""
    return {
        "levels": growth_plans.LEVEL_LABEL,
        "plans": [
            {"task_code": p.code, "level": p.level,
             "level_label": growth_plans.LEVEL_LABEL.get(p.level, ""),
             "event_codes": p.event_codes, "reason": p.reason,
             "actionable": p.actionable}
            for p in growth_plans.TASK_PLANS.values()
        ],
    }
