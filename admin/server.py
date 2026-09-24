"""管理后台 FastAPI 入口：登录、挂载路由、托管前端静态页、启动时建库建表。"""
import logging
from pathlib import Path

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from admin.config import settings
from admin.db import SessionLocal, init_db, wait_database_ready
from admin.models import SystemSetting
from admin.routers import (accounts, app_source, client_profile, groups, growth,
                           keys, logs, models, proxy, schedules, stats, sync)
from admin.ratelimit import (clear_failures, get_client_ip, get_trusted_client_ip,
                             is_locked, record_failure)
from admin.security import (
    create_admin_token,
    hash_password,
    require_admin,
    verify_password,
)

logger = logging.getLogger("admin.server")

# 可选内嵌独立网关 converter：把它挂到 /gw 前缀下，实现「单端口单进程」部署。
# converter 用本机桌面登录态直连后端，并额外提供 /v1/responses、/v1/messages（Anthropic）、
# /v1/balance 等协议；与管理后台自带的 /v1/chat/completions、/v1/models（带 Key 配额托管）
# 路径互不冲突。缺依赖时自动降级为只跑管理后台。
try:
    from converter import app as converter_app, CONFIG as _conv_cfg
    _CONVERTER_EMBEDDED = True
except Exception:  # pragma: no cover - 降级分支
    converter_app = None
    _CONVERTER_EMBEDDED = False

STATIC_DIR = Path(__file__).resolve().parent / "static"

app = FastAPI(title="WorkBuddy 共享管理后台", version="1.0",
              # 文档默认关闭：它对攻击者等于一份现成的完整攻击面清单。
              # 需要时用 ADMIN_ENABLE_DOCS=1 打开。
              docs_url="/docs" if settings.ENABLE_DOCS else None,
              redoc_url="/redoc" if settings.ENABLE_DOCS else None,
              openapi_url="/openapi.json" if settings.ENABLE_DOCS else None)

# CORS：共享网关以 Authorization 头鉴权、不使用 Cookie，故关闭 credentials；
# 避免 `allow_origins=["*"] + allow_credentials=True` 的危险组合。
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=False,
    allow_headers=["*"],
    allow_methods=["*"],
)


@app.middleware("http")
async def _security_headers(request: Request, call_next):
    """统一安全响应头：防点击劫持 / MIME 嗅探 / 敏感头泄露。"""
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    if request.url.path.startswith(("/api", "/admin")):
        response.headers.setdefault("Cache-Control", "no-store")
    return response

app.include_router(accounts.router)
app.include_router(keys.router)
app.include_router(models.router)
app.include_router(proxy.router)
app.include_router(sync.router)
app.include_router(schedules.router)
app.include_router(logs.router)
app.include_router(groups.router)
app.include_router(stats.router)
app.include_router(growth.router)
app.include_router(client_profile.router)
app.include_router(app_source.router)


@app.on_event("startup")
def _startup():
    # 未配置登录凭据时给出醒目告警：此时后台登录一律 503，不会退化成空口令
    if not settings.ADMIN_USERNAME or not settings.ADMIN_PASSWORD:
        logger.warning(
            "未配置 ADMIN_USERNAME / ADMIN_PASSWORD，后台登录已禁用；"
            "请在 .env 中设置后重启（不再提供 admin/admin123 这类默认口令）"
        )
    else:
        if settings.ADMIN_USERNAME == "admin" and settings.ADMIN_PASSWORD == "admin123":
            logger.warning("仍在使用 admin/admin123 弱口令，且服务可能监听 0.0.0.0，请尽快更换")

    # 数据库不可用（例如 MySQL 被 OOM 杀掉正在重启）时**不能**让启动失败：
    # 这里抛异常会让 uvicorn 直接退出进程，nginx 侧变成 502，且需要人工重启。
    # 改为重试 + 降级启动，进程保持存活，数据库恢复后自动可用。
    db_ok = wait_database_ready()
    if db_ok:
        try:
            init_db()
        except Exception:
            logger.exception("建表/迁移失败，服务继续启动（数据库可用后重启即可）")
    else:
        logger.error("数据库当前不可用，服务以降级模式启动：请求将返回 503，恢复后自动自愈")

    # 装配流量治理运行态：在途租约上限、会话粘性路由（含后台 GC 线程）。
    # 这些是纯进程内存状态，**不需要数据库**，所以放在 db_ok 判定之外 ——
    # 数据库降级期间号池选号仍要能在内存里正常工作。
    try:
        from admin.pool import POOL
        POOL.configure(
            max_in_flight=settings.MAX_IN_FLIGHT,
            sticky_enabled=settings.STICKY_ENABLED,
            sticky_ttl_s=settings.STICKY_TTL_SECONDS,
            sticky_gc_s=settings.STICKY_GC_SECONDS,
        )
        logger.info(
            "号池运行态已装配：单号在途上限=%s 会话粘性=%s(ttl=%ss) 轮转上限=%s",
            settings.MAX_IN_FLIGHT or "不限", "开" if settings.STICKY_ENABLED else "关",
            settings.STICKY_TTL_SECONDS, settings.MAX_ROTATE,
        )
    except Exception:
        logger.exception("号池运行态装配失败，将退化为无粘性/无在途限制")

    # 把后台保存的路径覆盖注入 wb_install（安装目录 / 逆向产物目录）。
    # 必须在下面「客户端版本发现」之前：否则首个请求拿到的还是扫描结果，
    # 后台改过的安装目录要等一次 refresh 才生效。
    # 环境变量优先级仍高于这里的覆盖值（见 wb_install._ov）。
    if db_ok:
        try:
            from admin import wb_paths
            ov = wb_paths.load_into_wb()
            if any(ov.values()):
                logger.info("已应用后台路径设置：%s", ov)
        except Exception:
            logger.exception("应用后台路径设置失败（继续用自动扫描）")

    # 打印客户端版本发现结果：出站 UA 与桌面事件指纹都靠它。
    # 显示「兜底」就说明没找到本机安装包 —— 服务能跑，但 UA 报的版本
    # 不一定与真实客户端一致，属于需要关注的状态，所以要在启动日志里可见。
    try:
        from wb_install import WB
        logger.info("客户端版本：%s", WB.describe())
        if not WB.is_version_dynamic():
            logger.warning(
                "未找到本机 WorkBuddy 安装目录，客户端版本退化为兜底值；"
                "如需与真实客户端一致，请设置 WORKBUDDY_INSTALL_DIR "
                "或把安装盘符加进 WORKBUDDY_DRIVES"
            )
        if getattr(settings, "DEV_TOOLS", True):
            logger.info("逆向产物目录：%s（开发工具已开启，可在后台一键拆包）",
                        WB.source_dir())
    except Exception:
        logger.exception("客户端版本发现失败（不影响启动）")

    # 调度器只在数据库可用时启动（否则它每 15s 都会失败一次，纯属噪音）
    if db_ok:
        from admin.scheduler import start_scheduler
        start_scheduler()

    # 预热设备风控 token：桌面端不存在时也会填上负缓存，
    # 避免每个请求都在事件循环里同步 fork 一次 node 造成阻塞。
    try:
        from admin.turing_token import warmup
        warmup()
    except Exception:
        pass


def _get_stored_hash() -> str:
    """读取密码哈希；显式配置 ADMIN_PASSWORD 时以 env 为准并同步数据库。

    返回空串表示「未配置登录凭据」——此时 login 一律拒绝，绝不允许空口令登录。
    """
    # 未配置密码：直接拒绝，且不往库里写任何东西
    if not settings.ADMIN_PASSWORD:
        return ""

    db = None
    try:
        db = SessionLocal()
        row = db.query(SystemSetting).filter(SystemSetting.key == "admin_password").first()

        # env 是部署配置的明确来源，优先级高于数据库中可能遗留的旧密码。
        if settings.ADMIN_PASSWORD_FROM_ENV:
            if row and row.value and verify_password(settings.ADMIN_PASSWORD, row.value):
                return row.value
            val = hash_password(settings.ADMIN_PASSWORD)
            if row:
                row.value = val
            else:
                db.add(SystemSetting(key="admin_password", value=val))
            db.commit()
            return val

        if row and row.value:
            val = row.value
            if not val.startswith("pbkdf2$"):  # 旧明文 → 迁移为哈希
                val = hash_password(val)
                row.value = val
                db.commit()
            return val
    finally:
        if db is not None:
            db.close()
    # 无记录：用配置密码并持久化哈希
    h = hash_password(settings.ADMIN_PASSWORD)
    db = None
    try:
        db = SessionLocal()
        db.add(SystemSetting(key="admin_password", value=h))
        db.commit()
    except Exception:
        pass
    finally:
        if db is not None:
            db.close()
    return h


@app.post("/api/login")
def login(
    username: str = Form(...),
    password: str = Form(...),
    request: Request = None,
):
    # 登录锁定必须用**不可伪造**的来源 IP：X-Forwarded-For 由 nginx 以
    # `$proxy_add_x_forwarded_for` 追加，客户端自带的假值会排在首位，
    # 取首段就能靠换 IP 绕过锁定（安全审计第 6 条）。
    ip = get_trusted_client_ip(
        request.headers.get("X-Real-IP") if request else None,
        request.headers.get("X-Forwarded-For") if request else None,
        request.client.host if request and request.client else None,
    )
    locked, wait = is_locked(ip)
    if locked:
        raise HTTPException(
            status_code=429,
            detail=f"登录尝试过于频繁，请 {wait} 秒后再试",
            headers={"Retry-After": str(wait)},
        )
    # 未配置凭据时直接拒绝（不能退化成「空口令可登录」）
    if not settings.ADMIN_USERNAME or not settings.ADMIN_PASSWORD:
        raise HTTPException(
            status_code=503,
            detail="后台未配置登录凭据，请在 .env 中设置 ADMIN_USERNAME 与 ADMIN_PASSWORD 后重启",
        )
    if username != settings.ADMIN_USERNAME or not verify_password(password, _get_stored_hash()):
        record_failure(ip)
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    # 仅用户名正确且密码正确才清空失败计数（避免可被探测用户名是否存在）
    clear_failures(ip)
    return {"token": create_admin_token()}


class PasswordChangeIn(BaseModel):
    old_password: str
    new_password: str


@app.patch("/api/admin/password")
def change_password(body: PasswordChangeIn, _: bool = Depends(require_admin)):
    if not verify_password(body.old_password, _get_stored_hash()):
        raise HTTPException(status_code=403, detail="当前密码不正确")
    if len(body.new_password) < 6:
        raise HTTPException(status_code=400, detail="新密码长度不能少于 6 位")
    db = SessionLocal()
    try:
        row = db.query(SystemSetting).filter(SystemSetting.key == "admin_password").first()
        if row:
            row.value = hash_password(body.new_password)
        else:
            db.add(SystemSetting(key="admin_password", value=hash_password(body.new_password)))
        db.commit()
    finally:
        db.close()
    return {"ok": True}


@app.get("/")
def index_redirect():
    return RedirectResponse(url="/admin")


@app.get("/admin")
@app.get("/admin/")
def admin_index():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# 内嵌 converter 网关到 /gw 前缀（单端口部署）。设置脱敏与日志路径以对齐独立运行时的行为。
if _CONVERTER_EMBEDDED:
    import os

    _conv_cfg["desensitize"] = os.getenv("CONVERTER_DESENSITIZE", "1") != "0"
    _conv_cfg["log_path"] = os.getenv(
        "CONVERTER_LOG", str(STATIC_DIR.parent.parent / "logs" / "converter-embedded.log")
    )

    # 补上登录凭据。CONFIG["cred"] 只在 converter.py 的 main() 里赋值，而这里是
    # import 进来挂载的（main() 永不执行），所以它默认恒为 None —— 结果是 /gw 下
    # 所有需要凭据的端点一律 503（含 Claude Code 用的 /gw/v1/messages）。
    # 这里按独立运行时的同样逻辑补一次初始化。
    # 注意：服务若以非桌面登录账户运行，需用 CODEBUDDY_AUTH_DIR 指向该账户的 auth 目录。
    if _conv_cfg.get("cred") is None:
        try:
            from converter import CredentialManager, find_auth_file

            _auth_file = find_auth_file()
            if _auth_file:
                _conv_cfg["cred"] = CredentialManager(_auth_file)
        except Exception:
            pass

    # /gw 的鉴权开关。converter._check_auth() 的实现是 `if not key: return` ——
    # key 为空就等于完全不鉴权，而本服务默认监听 0.0.0.0，那等于把桌面端账号的
    # 额度向整个网络开放。所以只认环境变量，不做任何默认值兜底。
    _conv_cfg["api_key"] = os.getenv("CONVERTER_API_KEY", "")

    _log = logging.getLogger("admin.server")
    if _conv_cfg.get("cred") is None:
        _log.warning(
            "/gw 未取得桌面端登录凭据，/gw/v1/* 将返回 503。"
            "请确认桌面端已登录，或用 CODEBUDDY_AUTH_DIR 指定 auth 目录。"
        )

    # ⚠️ fail-closed：没配 Key 就**不挂载** /gw，而不是挂上去裸奔。
    # 原来只打一条 warning 就照常 mount，等于「默认无鉴权」——
    # 只要换机/重部署时漏了 CONVERTER_API_KEY，整套 /gw/v1/*（含
    # /v1/chat/completions、/v1/messages）就对任何访问者开放，可任意白嫖额度。
    # 安全审计把它列为「中」风险，但那只是因为它看到线上恰好配了 Key。
    # 宁可少一个功能，也不要一个默认开放的后门。
    if not _conv_cfg["api_key"]:
        _log.error(
            "未配置 CONVERTER_API_KEY —— **拒绝挂载 /gw**（拒绝默认无鉴权的网关）。"
            "如需使用内嵌网关，请在 .env 中设置 CONVERTER_API_KEY 后重启。"
        )
    else:
        app.mount("/gw", converter_app)
        _log.info("/gw 已挂载（已配置 CONVERTER_API_KEY，/v1/* 强制校验）")
