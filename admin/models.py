"""ORM 模型：账号、API Key、用量日志。"""
from datetime import datetime

from sqlalchemy import Column, DateTime, Float, Index, Integer, String, Text

from admin.db import Base


class Account(Base):
    """一个 WorkBuddy / CodeBuddy 登录态（.info 凭据）。"""

    __tablename__ = "accounts"
    # 选号热路径索引：每次代理请求都要按 status + 余额 + 冷却扫描候选池。
    # 号池通常只有几十行，但 idx_status 让「活跃账号」这条永不消失的过滤
    # 不必全表扫，冷启动与批量任务下更稳。
    __table_args__ = (
        Index("ix_accounts_status", "status"),
        Index("ix_accounts_uid", "uid"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(120), nullable=False, default="")
    uid = Column(String(120), default="")
    enterprise_id = Column(String(120), default="")
    domain = Column(String(120), default="")
    auth_json = Column(Text, nullable=False)  # 原始 .info 内容（含 token）
    status = Column(String(16), default="active")  # active | disabled
    balance_total = Column(Integer, default=0)
    balance_remain = Column(Integer, default=0)
    last_sync_at = Column(DateTime, nullable=True)
    last_used_at = Column(DateTime, nullable=True)
    # 稳定性状态机：错误计数 / 冷却 / 防撞号 / 禁用原因
    err_count = Column(Integer, default=0)          # 连续上游 5xx 计数
    cool_until = Column(DateTime, nullable=True)    # 冷却截止时间
    cool_kind = Column(String(16), default="")       # hard_credit | soft_rate | error_threshold | not_found
    last_err_at = Column(DateTime, nullable=True)
    last_err_msg = Column(String(255), default="")
    last_picked_at = Column(DateTime, nullable=True)  # 最近一次被选中（仅观测；防撞号窗口已移入进程内存）
    # 熔断：连续失败达阈值后指数退避，封顶 breaker_cooldown_max。
    # 与 cool_until 是**或门**关系（任一未到期即不可选），生效的是更晚的那个。
    breaker_until = Column(DateTime, nullable=True)
    breaker_fails = Column(Integer, default=0)
    # 连续 12153「session dead」计数：达到阈值才禁用。
    # 一次就禁用等于误杀 —— 该错误在真实环境会被临时性触发（上游抖动/并发刷新）。
    session_dead_fails = Column(Integer, default=0)
    # 连败降权（「不知道原因的失败」兜底：未知 4xx 与传输层抖动）：
    # 带权威分类的错误各有自己的惩罚，不喂这个计数器，避免重复计罚。
    consecutive_fails = Column(Integer, default=0)
    degrade_until = Column(DateTime, nullable=True)
    #: 快过期积分（供三因子权重的「快过期先用」项）。
    credits_expiring = Column(Integer, default=0)
    # ---- 积分到期口径（详见 admin/credits.py）----------------------------
    #: 7 天内到期的剩余额度合计。与 credits_expiring 的区别：后者是历史字段，
    #: 窗口口径曾为 30 天；本列严格按 EXPIRING_SOON_DAYS(=7) 统计，
    #: 是「再不烧就作废」的那部分，也是选号的首要依据。
    credits_expiring_soon = Column(Integer, default=0)
    #: 所有**还有剩余**的积分包里最早的到期时间（真实到期，非占位值）。
    #: 为空表示手里全是长期有效额度（没有作废风险）。
    credits_soonest_expire_at = Column(DateTime, nullable=True)
    #: 长期有效（无到期时间）的剩余额度合计。
    credits_evergreen = Column(Integer, default=0)
    #: 已过期但账面仍有剩余的额度（上游扣减延迟时出现），仅供对账。
    credits_expired = Column(Integer, default=0)
    #: 逐包快照明细（JSON 文本，按到期升序）。存库而不是每次现查，
    #: 是为了让「最近快到期的积分包」这类展示与统计不必对上游逐号发请求。
    credits_snapshot = Column(Text, default="")
    #: 快照采集时间，用于展示「N 分钟前更新」并判断数据是否可信。
    credits_synced_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class AccountModelCooldown(Base):
    """账号×模型级的冷却（与账号级冷却**互相独立**）。

    为什么必须独立（上游 code 6004「该模型使用量超限」）：
      * 账号级冷却会让「换个模型就能用」的号被整体摘出池子 —— 明明切模型立即可用，
        却白扔一个账号直到冷却结束；
      * 反过来，若把模型级限流当成账号级，用户会遇到「限额后换不动号」。

    所以 6004 只冷却**触发调用的那个模型**：该账号对其他模型照常可选，
    而对同一模型则被 `_select_account` 跳过。

    code 11102「该后端无此模型」复用同一张表：它不是限流而是确定性答复
    （重试无意义），所以 TTL 更长（默认 6h）且按指数退避。
    """

    __tablename__ = "account_model_cool"
    __table_args__ = (Index("ix_account_model_cool_model_until", "model", "until"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    account_id = Column(Integer, nullable=False, index=True)
    model = Column(String(120), nullable=False, default="")
    until = Column(DateTime, nullable=True)          # 冷却截止
    kind = Column(String(24), default="")            # model_daily | model_rate | model_block
    reason = Column(String(255), default="")
    hits = Column(Integer, default=0)                # 命中次数（model_block 指数退避用）
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class ApiKey(Base):
    """对外共享的 API Key，带积分限额。"""

    __tablename__ = "api_keys"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(120), default="")
    key_hash = Column(String(128), unique=True, nullable=False)
    key_prefix = Column(String(16), default="")  # 展示用前缀
    key_full = Column(String(2048), default="")  # 完整密钥（仅管理后台查看用，base64 编码存储）
    credit_limit = Column(Float, default=0)  # 限额（credits）；unlimited=True 时忽略
    credit_used = Column(Float, default=0)
    unlimited = Column(Integer, default=0)  # 0/1
    status = Column(String(16), default="active")  # active | revoked
    note = Column(String(255), default="")
    # 绑定的模型分组 id；0 = 未绑定（可用全部启用模型）。分组被删时按 0 处理（自动降级）
    group_id = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class ModelGroup(Base):
    """模型分组：一组模型的命名集合，供 API Key 绑定以限制可用模型范围。"""

    __tablename__ = "model_groups"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(120), nullable=False, default="")
    note = Column(String(255), default="")
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class ModelGroupItem(Base):
    """分组内的模型成员（一个分组多条）。"""

    __tablename__ = "model_group_items"

    id = Column(Integer, primary_key=True, autoincrement=True)
    group_id = Column(Integer, nullable=False, index=True)
    model_id = Column(String(120), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)


class UsageLog(Base):
    """每次代理调用的用量记录（用于管理后台审计）。

    记录内容：调用方 API Key、实际使用的上游账号、模型、积分消耗、token 明细，
    以及发起请求的真实客户端 IP（来自 X-Forwarded-For / X-Real-IP / 直连 socket），
    便于风控对账与上游客用途日志对齐。
    """

    __tablename__ = "usage_logs"
    # 索引说明（都是为「使用记录 / 日志」的筛选与排序服务的）：
    #   ix_usage_logs_created_id  —— 覆盖「按时间倒序分页」这条**永不消失**的主查询。
    #       原来是 PRIMARY(id) 倒序扫：日志一多，`ORDER BY id DESC LIMIT 20`
    #       在有 where 条件时仍要走全表再排序；有了 (created_at, id) 复合索引，
    #       时间范围过滤 + 倒序取前 N 条能直接在索引上完成，回表只取 20 行。
    #   ix_usage_logs_account_created —— 「按账号查询」的专用索引（本次新增需求）。
    #       账号维度是排查「某个号今天花了多少」最常用的入口，
    #       单列 account_id 索引仍要排序，复合索引把过滤 + 排序一次做完。
    #   ix_usage_logs_key_created / ix_usage_logs_model_created 同理，覆盖按 Key/模型筛选。
    __table_args__ = (
        Index("ix_usage_logs_created_id", "created_at", "id"),
        Index("ix_usage_logs_account_created", "account_id", "created_at"),
        Index("ix_usage_logs_key_created", "api_key_id", "created_at"),
        Index("ix_usage_logs_model_created", "model", "created_at"),
        Index("ix_usage_logs_error_created", "error_kind", "created_at"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    api_key_id = Column(Integer, nullable=False, default=0)
    account_id = Column(Integer, nullable=False, default=0)
    model = Column(String(120), default="")
    credits = Column(Float, default=0)
    # 详细用量：优先取上游 usage 字段；缺省为 NULL
    prompt_tokens = Column(Integer, nullable=True, default=None)
    completion_tokens = Column(Integer, nullable=True, default=None)
    total_tokens = Column(Integer, nullable=True, default=None)
    cached_tokens = Column(Integer, nullable=True, default=None)
    # 发起请求的真实客户端 IP（经反代时取 X-Forwarded-For 首个，否则 X-Real-IP / 直连 IP）
    client_ip = Column(String(64), default="")
    # 用途标识（透传给上游的 X-Agent-Purpose），便于风控审计与上游请求用量对齐
    use_case = Column(String(64), default="")
    # 请求级表格日志字段（ logging）：TTFB / 总耗时 / 序号 / 错误分类
    seq = Column(Integer, default=0)
    ttfb_ms = Column(Integer, nullable=True, default=None)
    latency_ms = Column(Integer, nullable=True, default=None)
    error_kind = Column(String(32), default="")  # hard_credit | soft_rate | server | not_found | session_dead | transport | client | success
    created_at = Column(DateTime, default=datetime.utcnow)


class ModelConfig(Base):
    """模型白名单配置（系统级 / 用户级）。"""

    __tablename__ = "model_configs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    level = Column(String(16), default="system")  # system | user
    model_id = Column(String(120), nullable=False)  # 模型 ID，如 "deepseek-v4-flash"
    enabled = Column(Integer, default=1)  # 0/1
    note = Column(String(255), default="")
    credit_multiplier = Column(Float, default=0)  # 积分消耗倍率；0=免费模型
    credits_raw = Column(String(120), default="")  # 原始 credits 字符串（如 "x0.05" / "x0.00 credits"）
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class SystemSetting(Base):
    """简单的键值配置（同步地址 / 密钥 / 其他开关）。"""

    __tablename__ = "system_settings"

    key = Column(String(120), primary_key=True)
    value = Column(Text, default="")
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class Schedule(Base):
    """后台定时任务（如：整点刷新平台总积分、每日同步模型列表）。"""

    __tablename__ = "schedules"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(120), default="")
    task = Column(String(40), default="refresh_balances")  # refresh_balances | sync_models | daily_checkin
    interval_minutes = Column(Integer, default=60)
    enabled = Column(Integer, default=1)  # 0/1
    last_run_at = Column(DateTime, nullable=True)
    next_run_at = Column(DateTime, nullable=True)
    last_result = Column(Text, default="")  # 上次运行结果摘要
    # 停止领取时间（仅 daily_checkin 任务使用）：到达该时间后不再执行领取请求，
    # 避免活动下线后继续请求触发上游风控。可由活动 end_time 预填或运行中发现 EventEnded 自动写入。
    stop_after = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
