"""管理后台配置（FastAPI + MySQL + Redis）。

所有项均从环境变量读取，推荐通过项目根目录的 `.env` 文件提供（不要写死在代码里）。
复制 `.env.example` 为 `.env` 并填入实际值后使用：

    cp .env.example .env
    # 然后编辑 .env 填入真实数据库密码 / 后台密码 / JWT 密钥等

本地开发默认值只是占位，生产环境务必在 `.env` 中覆盖敏感项。
"""
import os
import logging

from dotenv import load_dotenv

# 加载项目根目录的 .env（无论运行时 CWD 在哪都能找到）。
# 不会覆盖已经存在的系统环境变量（便于容器 / systemd 注入）。
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(_ROOT, ".env"))

_logger = logging.getLogger(__name__)


class Settings:
    # 数据库 / 缓存
    DATABASE_URL = os.getenv(
        "ADMIN_DATABASE_URL",
        "mysql+pymysql://root:root@127.0.0.1:3306/workbuddy_admin?charset=utf8mb4",
    )
    REDIS_URL = os.getenv("ADMIN_REDIS_URL", "redis://127.0.0.1:6379/0")

    # 后端（CodeBuddy / WorkBuddy）
    BACKEND = os.getenv("ADMIN_BACKEND", "https://copilot.tencent.com")

    # 本机 WorkBuddy/CodeBuddy 桌面端登录态目录（用于「扫描本机 / 注入切换」）
    CLIENT_AUTH_DIR = os.getenv(
        "ADMIN_CLIENT_AUTH_DIR",
        r"%LOCALAPPDATA%\CodeBuddyExtension\Data\Public\auth",
    )

    # 管理后台登录
    #
    # 不设默认值：留空表示「必须由部署方显式配置」。
    # 之前默认 admin / admin123，服务又默认监听 0.0.0.0，等于把号池额度
    # 向整个网络开放。现在未配置时登录会直接失败，而不是退化成一个
    # 众所周知的口令（见 admin/server.py 的启动校验与 login）。
    ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "")
    # 显式配置的环境变量优先于数据库中的历史密码。这样改 .env 后无需手动改库。
    ADMIN_PASSWORD_FROM_ENV = "ADMIN_PASSWORD" in os.environ
    ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")
    # 生产环境务必在 .env 中设置 ADMIN_JWT_SECRET 为 >=32 字节的随机串；
    # 未设置时回退到开发弱密钥并输出告警。
    _jwt = os.getenv("ADMIN_JWT_SECRET")
    if not _jwt:
        _logger.warning("ADMIN_JWT_SECRET 未设置，使用开发弱密钥；生产环境请在 .env 中配置")
        _jwt = "dev-insecure-jwt-secret-change-me"
    JWT_SECRET = _jwt
    JWT_EXPIRE_HOURS = int(os.getenv("ADMIN_JWT_EXPIRE_HOURS", "24"))

    # 服务监听
    HOST = os.getenv("ADMIN_HOST", "0.0.0.0")
    PORT = int(os.getenv("ADMIN_PORT", "8790"))

    # /v1/messages（Anthropic Messages API）的模型档次映射。
    # Claude Code 发的是 claude-opus-* / claude-sonnet-* / claude-haiku-* 这类名字，
    # 上游不认；这里按档次落到本后台白名单里的模型名。
    # 注意：不要默认成 auto —— 本后台的 auto 是「取第一个启用的模型」，
    # 在 28 个模型里可能挑到 hunyuan-chat 之类不适合写代码的，甚至图像模型。
    # 下面三个默认值按「最强 / 均衡 / 快速」选，可按自己号池的实际情况改。
    ANTHROPIC_MODEL_OPUS = os.getenv("ADMIN_ANTHROPIC_MODEL_OPUS", "deepseek-v4-pro")
    ANTHROPIC_MODEL_SONNET = os.getenv("ADMIN_ANTHROPIC_MODEL_SONNET", "glm-5.2")
    ANTHROPIC_MODEL_HAIKU = os.getenv("ADMIN_ANTHROPIC_MODEL_HAIKU", "glm-5.3-flash")

    # /v1/messages 的 harness 脱敏开关（与 converter 的 /gw 端点同款处理）。
    # Claude Code 的 system prompt / tools 是固定模板，内含 "DoS / exploit / credential"
    # 这类合规声明词，会被上游内容审核误判并整条拒绝，典型报错就是
    #   400 {"code":11128,"msg":"Illegal API invocation from an unapproved channel"}
    # 开启后会压缩 harness 并给敏感词插零宽空格。默认开启 —— 关掉大概率直接发不出去。
    ANTHROPIC_DESENSITIZE = os.getenv("ADMIN_ANTHROPIC_DESENSITIZE", "1") != "0"
    # 配合上项：跳过 harness 压缩，只做零宽脱敏（保留 system 原文，误拦风险略高）
    ANTHROPIC_NO_COMPACT = os.getenv("ADMIN_ANTHROPIC_NO_COMPACT", "0") == "1"

    # 计费：后端未回传 credits 时，按 total_tokens * 系数 / 1000 估算（系数单位为「每千 token 积分」）
    COST_PER_TOKEN = float(os.getenv("ADMIN_COST_PER_TOKEN", "0.01"))

    # 上游记录客户端 IP 时使用的 header 名（若上游有自定义要求，如 X-Client-Ip / X-Real-IP 等）
    # 为空则同时发送 X-Forwarded-For / X-Real-IP / X-Client-IP 等常见头
    UPSTREAM_CLIENT_HEADER = os.getenv("ADMIN_UPSTREAM_CLIENT_HEADER", "")

    # 上游请求用量「client」列显示的产品名。
    # 腾讯 CodeBuddy/WorkBuddy 后端通过 X-IDE-Name 头识别客户端，默认 "WorkBuddy"。
    UPSTREAM_CLIENT_NAME = os.getenv("ADMIN_UPSTREAM_CLIENT_NAME", "WorkBuddy")

    # 账号选择策略：remain（剩余最多优先）/ lru（最久未用优先）/ weighted（三因子加权随机）
    ACCOUNT_SELECT = os.getenv("ADMIN_ACCOUNT_SELECT", "remain")

    # ---------- 开发 / 测试辅助工具 ----------
    #: 逆向产物（客户端源码）管理：后台一键拆包 app.asar、手动指定安装目录。
    #:
    #: **只对开发/测试环境有意义**：生产服务器既没有客户端安装包，也不该在上面
    #: 放 225MB 的源码。默认开启（本地开发方便），生产部署设为 0 即可整体关闭
    #: —— 关闭后 /api/app-source/* 全部返回 404，后台也不显示该区域。
    DEV_TOOLS = os.getenv("ADMIN_DEV_TOOLS", "1") != "0"

    # ---------- 流量治理（限流 / 并发 / 保活） ----------
    #: 单账号最大在途请求数（0 = 不限）。
    #: 粘性路由与加权选号都会倾向少数账号，叠加长连接后单号容易过载，
    #: 上游按账号限速时会成片 429/5xx；在途上限把并发摊平到整个池子。
    MAX_IN_FLIGHT = int(os.getenv("ADMIN_POOL_MAX_IN_FLIGHT", "3"))
    #: 会话粘性路由开关。开启后同一会话固定同一账号：
    #: 上游前缀缓存不碎、多轮更快更省，且一次会话不在号池里逐轮跳号（拟人）。
    STICKY_ENABLED = os.getenv("ADMIN_SESSION_STICKY", "1") != "0"
    #: 会话绑定的空闲 TTL（秒），滚动续期，空闲即过期释放。
    STICKY_TTL_SECONDS = int(os.getenv("ADMIN_SESSION_STICKY_TTL", "1800"))
    #: 会话绑定 GC 周期（秒）。
    STICKY_GC_SECONDS = int(os.getenv("ADMIN_SESSION_STICKY_GC", "300"))
    #: 单请求最多换号次数（原实现硬编码 3，这里可配）。
    MAX_ROTATE = int(os.getenv("ADMIN_POOL_MAX_ROTATE", "3"))
    #: 软限流（429）冷却基数（秒）；连续触发按 2 倍指数退避，封顶 SOFT_RATE_MAX。
    SOFT_RATE_SECONDS = int(os.getenv("ADMIN_POOL_SOFT_RATE", "600"))
    #: 软冷却指数退避封顶（秒）。默认 2h，与参考实现一致。
    SOFT_RATE_MAX_SECONDS = int(os.getenv("ADMIN_POOL_SOFT_RATE_MAX", "7200"))
    #: 「纯请求速率」限流（code 14003 RateLimitError / subcategory
    #: `quota_request_limit`）的冷却基数（秒）。
    #:
    #: 为什么不和 SOFT_RATE_SECONDS 共用一个值：两者量级差一个数量级。
    #: SOFT_RATE_SECONDS=600 是给 6000–6008（每分钟/每小时请求数）这类
    #: **配额型**限流用的，等 10 分钟是合理的；而 14003 的语义是
    #: 「当前模型请求繁忙 / 请求过于频繁，请稍后重试」——上游自己给的建议
    #: 就是「稍后重试」，官方 UI 文案也是「请切换模型或稍后重试」。
    #: 给它 600 秒会把一次**瞬时**抖动放大成 10 分钟的整池停摆：
    #: 一次请求轮转 3 个号，几个并发请求就能把 15 个号的池子全部冷掉
    #: （2026-09-23 线上故障即为此：16 个号 76 秒内全部 soft_rate，
    #: 冷却到 10 分钟后，期间所有请求直接 503）。
    REQUEST_RATE_SECONDS = int(os.getenv("ADMIN_POOL_REQUEST_RATE", "20"))
    #: 纯请求速率限流的指数退避封顶（秒）。留足退避余量但不至于停摆太久。
    REQUEST_RATE_MAX_SECONDS = int(os.getenv("ADMIN_POOL_REQUEST_RATE_MAX", "120"))
    #: 连续失败熔断阈值与退避（次 / 秒 / 封顶秒）。
    BREAKER_THRESHOLD = int(os.getenv("ADMIN_POOL_BREAKER_THRESHOLD", "5"))
    BREAKER_COOLDOWN_SECONDS = int(os.getenv("ADMIN_POOL_BREAKER_COOLDOWN", "600"))
    BREAKER_COOLDOWN_MAX_SECONDS = int(os.getenv("ADMIN_POOL_BREAKER_COOLDOWN_MAX", "21600"))
    #: 连续 12153「session dead」达到该次数才永久禁用。
    #: 12153 在真实环境会被临时性触发（上游抖动/并发刷新），一次就禁用等于误杀。
    SESSION_DEAD_THRESHOLD = int(os.getenv("ADMIN_POOL_SESSION_DEAD_THRESHOLD", "3"))
    #: 模型级限流（code 6004）时的兜底退避（秒），无上游重置时间时使用。
    MODEL_SOFT_RATE_SECONDS = int(os.getenv("ADMIN_POOL_MODEL_SOFT_RATE", "600"))
    #: 该后端无此模型（code 11102）的负缓存 TTL（秒）。重试无意义，只能换模型/换号。
    MODEL_BLOCK_SECONDS = int(os.getenv("ADMIN_POOL_MODEL_BLOCK", "21600"))
    #: token 保活定时任务的整点（本地时区）。留空则用默认 [22]。
    _raw_keepalive = os.getenv("ADMIN_KEEPALIVE_HOURS", "22")
    KEEPALIVE_HOURS = [int(x) for x in _raw_keepalive.replace("，", ",").split(",")
                       if x.strip().isdigit()] or [22]
    #: token 保活是否启用。
    KEEPALIVE_ENABLED = os.getenv("ADMIN_KEEPALIVE_ENABLED", "1") != "0"
    #: 保活/批量任务里账号之间的最小间隔（秒），避免批量请求被识别为机器行为。
    KEEPALIVE_ACCOUNT_GAP = float(os.getenv("ADMIN_KEEPALIVE_ACCOUNT_GAP", "0.8"))

    # ---------- 超时（关键：流式绝不设总时长） ----------
    #: 连接超时（秒）：连不上就快速换号，不让用户干等。
    STREAM_CONNECT_TIMEOUT = float(os.getenv("ADMIN_STREAM_CONNECT_TIMEOUT", "15"))
    #: 流式**静默**超时（秒）：两次数据之间的最大间隔。
    #: 这是「只要上游在持续吐字，流就永不被我们自己掐断」的关键 ——
    #: 它约束的是空闲，不是总时长。官方客户端源码里 Node 默认的 300s
    #: 总时长 requestTimeout 会在流仍活跃时掐断 SSE，是必须避免的坑。
    STREAM_IDLE_TIMEOUT = float(os.getenv("ADMIN_STREAM_IDLE_TIMEOUT", "180"))
    #: 写入（上传 prompt）超时（秒）。
    STREAM_WRITE_TIMEOUT = float(os.getenv("ADMIN_STREAM_WRITE_TIMEOUT", "60"))
    #: 连接池等待超时（秒）：池子饱和就快速失败，交给上层退避，而不是无限排队。
    STREAM_POOL_TIMEOUT = float(os.getenv("ADMIN_STREAM_POOL_TIMEOUT", "20"))

    # 登录防爆破：同一 IP 在窗口内失败超过阈值即锁定一段时间
    LOGIN_MAX_ATTEMPTS = int(os.getenv("ADMIN_LOGIN_MAX_ATTEMPTS", "5"))
    LOGIN_WINDOW_SECONDS = int(os.getenv("ADMIN_LOGIN_WINDOW_SECONDS", "300"))  # 5 分钟窗口
    LOGIN_LOCK_SECONDS = int(os.getenv("ADMIN_LOGIN_LOCK_SECONDS", "900"))      # 锁 15 分钟

    # CORS：共享网关用 Authorization 头鉴权（无需 Cookie），故默认关闭 credentials；
    # 留空或 * 表示允许任意来源；如需限制可设 ADMIN_CORS_ORIGINS=https://a.com,https://b.com
    _raw_cors = os.getenv("ADMIN_CORS_ORIGINS", "*")
    CORS_ORIGINS = [o.strip() for o in _raw_cors.split(",") if o.strip()] or ["*"]


settings = Settings()
