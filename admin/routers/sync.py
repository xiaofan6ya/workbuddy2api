"""线上同步：把本地账号 / 模型配置 / 客户端参数推送到线上数据库。

设计：
  - 本地数据视为最新（source of truth）。
  - 在「系统设置」里配置 远程 URL + 密钥（HMAC 签名用）。
  - 点「同步到线上」→ 把本地 accounts + model_configs + client_profile 推送过去。
  - 同一个服务也可作为「线上实例」接收数据（/api/sync/receive），
    只要线上实例配置了相同的 密钥 即可校验并写入自己的库。

关于 client_profile（客户端参数档案）
-------------------------------------
线上服务器**没装 WorkBuddy 桌面端**，所以它自己探测不到 UA / 版本号 / 风控头，
只能退化成内置兜底值 —— 线上 UA 于是报出一个「谁也不认识的版本」。
而本地机器往往装得好好的、探测得到真实值。

所以同步时一并推送 client_profile，并在**接收端自动把 source 设为 saved**：
线上不再尝试探测（它本来就探测不到），直接钉住同步过来的真实值。
"""
import hashlib
import hmac
import json
import logging
import secrets
import time

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from admin import client_profile as cprofile
from admin.db import get_db
from admin.models import Account, ModelConfig, SystemSetting
from admin.security import require_admin

_logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/sync", tags=["sync"])

_K_URL = "sync_remote_url"
_K_SECRET = "sync_secret"


def _generate_device_secret() -> str:
    """生成同步密钥：`secrets.token_hex(32)` = 256 bit 密码学安全随机。

    安全审计第 4 条：原实现是
        sha256(f"{hostname}-{machine}-{processor}-{mac}")[:32]
    只有 128 bit，且输入是**主机特征**而非随机数 —— 可预测性明显高于纯随机
    （攻击者若能猜到宿主名/CPU/MAC 就能离线枚举验证）。
    改成纯随机 256 bit 后，密钥空间不再依赖任何可观测特征。

    注意：只有**新生成**的密钥受影响；已存库的密钥照旧使用，
    且 `/api/sync/receive` 校验的是库里的值（不是重新推导），所以不影响既有配对。
    """
    return secrets.token_hex(32)


class SyncConfigIn(BaseModel):
    remote_url: str = ""
    secret: str = ""


def _get_setting(db: Session, key: str) -> str:
    row = db.query(SystemSetting).filter(SystemSetting.key == key).first()
    return row.value if row else ""


def _set_setting(db: Session, key: str, value: str):
    row = db.query(SystemSetting).filter(SystemSetting.key == key).first()
    if row:
        row.value = value
    else:
        db.add(SystemSetting(key=key, value=value))
    db.commit()


def _sign(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


@router.get("/config")
def get_config(_: bool = Depends(require_admin), db: Session = Depends(get_db)):
    secret = _get_setting(db, _K_SECRET)
    # 首次访问且未配置时自动生成基于设备的唯一密钥
    is_auto = False
    if not secret:
        secret = _generate_device_secret()
        _set_setting(db, _K_SECRET, secret)
        is_auto = True
    return {
        "remote_url": _get_setting(db, _K_URL),
        "secret": secret,  # 前端可展示完整密钥（仅已登录管理员可见）
        "secret_masked": ("*" * 8 + secret[-4:]) if len(secret) > 4 else ("****" if secret else ""),
        "is_auto_generated": is_auto,  # 本次是否为自动生成
    }


@router.post("/config")
def save_config(body: SyncConfigIn, _: bool = Depends(require_admin), db: Session = Depends(get_db)):
    if body.remote_url:
        _set_setting(db, _K_URL, body.remote_url.rstrip("/"))
    # 密钥：用户未填写时自动生成设备唯一密钥
    secret = body.secret.strip() if body.secret and body.secret.strip() else (_get_setting(db, _K_SECRET) or _generate_device_secret())
    if secret:
        _set_setting(db, _K_SECRET, secret)
    return {"ok": True, "secret_set": bool(secret)}


@router.post("/push")
def push(
    body: SyncConfigIn | None = None,
    _: bool = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """把本地账号/模型配置推送到线上。

    支持两种调用方式：
      1. 客户端调用前已先调用 /api/sync/config 保存过——这里直接读 DB。
      2. 客户端把当前输入框的 remote_url/secret 通过 body 一起带上——
         服务端会先写入 DB 再推送，这样前端不必强制先「保存配置」。

    这样无论用户是否记得点「保存配置」，只要输入框里有值就能直接推送。
    """
    # 优先用请求体里的值（避免前端遗漏保存步骤），再回退到 DB
    incoming_url = (body.remote_url or "").strip() if body else ""
    incoming_secret = (body.secret or "").strip() if body else ""
    if incoming_url:
        _set_setting(db, _K_URL, incoming_url.rstrip("/"))
    if incoming_secret:
        _set_setting(db, _K_SECRET, incoming_secret)

    remote_url = _get_setting(db, _K_URL)
    secret = _get_setting(db, _K_SECRET)
    if not remote_url or not secret:
        raise HTTPException(
            status_code=400,
            detail="请先在「同步设置」中填写并保存 远程 URL 与 密钥",
        )

    accounts = []
    for a in db.query(Account).all():
        accounts.append({
            "uid": a.uid,
            "name": a.name,
            "enterprise_id": a.enterprise_id,
            "domain": a.domain,
            "status": a.status,
            "auth_json": a.auth_json,
            "balance_total": a.balance_total,
            "balance_remain": a.balance_remain,
        })
    models = []
    for m in db.query(ModelConfig).all():
        models.append({
            "level": m.level,
            "model_id": m.model_id,
            "enabled": m.enabled,
            "credit_multiplier": m.credit_multiplier or 0,
            "credits_raw": m.credits_raw or "",
            "note": m.note or "",
        })

    # 客户端参数档案：把本地探测到的真实 UA / 版本号 / 风控头 / 指纹带过去。
    #
    # 这里推的是 **effective**（现场探测 + 已保存 + 兜底合并后的结果）而不是
    # 仅 saved：线上需要一份「完整的、当下真实生效的参数」，不是本地那点
    # 增量配置。不然线上还得自己补齐一堆默认值，容易两边不一致。
    #
    # 注意 records 里只放可同步的字段（machineId/sessionId 是账号级的，
    # 由线上按自己库里的 uid 派生，不能跟着传 —— 否则所有账号会共用同一台
    # 「设备」，那正是最容易被批量识别的特征）。
    eff = cprofile.effective(db)
    profile_payload = {
        "desktop_version": eff.get("desktop_version"),
        "cli_version": eff.get("cli_version"),
        "user_agent": eff.get("user_agent"),
        "ide_name": eff.get("ide_name"),
        "ide_type": eff.get("ide_type"),
        "product": eff.get("product"),
        "fp_product": eff.get("fp_product"),
        "ext_name": eff.get("ext_name"),
        "os": eff.get("os"),
        "arch": eff.get("arch"),
        "os_version": eff.get("os_version"),
        "cpu_cores": eff.get("cpu_cores"),
        "memory_size": eff.get("memory_size"),
        "timezone": eff.get("timezone"),
        "report_delay": eff.get("report_delay"),
        "commit": eff.get("commit"),
        "release_date_ms": eff.get("release_date_ms"),
        "turing_channel_id": eff.get("turing_channel_id"),
        "turing_product_name": eff.get("turing_product_name"),
        "headers": eff.get("headers") or {},
    }
    profile_payload = {k: v for k, v in profile_payload.items()
                       if v not in (None, "", 0, {})}

    payload = {
        "source": "workbuddy2api-admin",
        "pushed_at": int(time.time()),
        "accounts": accounts,
        "models": models,
        # 线上实例收到后会自动把 source 设为 saved（它探测不到客户端）
        "client_profile": profile_payload,
        "client_profile_detected": bool(cprofile.snapshot_live()),
    }
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    sig = _sign(secret, raw)

    target = f"{remote_url}/api/sync/receive"
    try:
        with httpx.Client(timeout=30) as client:
            r = client.post(target, content=raw, headers={
                "Content-Type": "application/json",
                "X-Sync-Signature": sig,
            })
            if r.status_code >= 400:
                raise HTTPException(status_code=502, detail=f"线上返回错误 {r.status_code}: {r.text[:300]}")
            resp = r.json()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"推送失败: {e}")

    return {"ok": True, "remote": remote_url, "accounts": len(accounts),
            "models": len(models),
            "client_profile": bool(profile_payload),
            "profile_fields": sorted(profile_payload),
            "remote_result": resp}


@router.post("/receive")
async def receive(request: Request, db: Session = Depends(get_db)):
    """线上实例接收端：校验签名后 upsert 账号与模型配置。

    用本实例 system_settings 里的 sync_secret 校验（线上实例需配置相同密钥）。
    """
    secret = _get_setting(db, _K_SECRET)
    if not secret:
        raise HTTPException(status_code=400, detail="线上实例未配置同步密钥，无法接收")
    raw = await request.body()
    sig = request.headers.get("X-Sync-Signature", "")
    expected = _sign(secret, raw)
    if not hmac.compare_digest(sig, expected):
        raise HTTPException(status_code=401, detail="签名校验失败")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except Exception:
        raise HTTPException(status_code=400, detail="无效 JSON")

    acc_added = acc_updated = 0
    for a in payload.get("accounts", []):
        uid = a.get("uid")
        if not uid or not a.get("auth_json"):
            continue
        existing = db.query(Account).filter(Account.uid == uid).first()
        if existing:
            existing.auth_json = a["auth_json"]
            existing.name = a.get("name") or existing.name
            existing.enterprise_id = a.get("enterprise_id") or ""
            existing.domain = a.get("domain") or ""
            existing.status = a.get("status") or "active"
            existing.balance_total = int(a.get("balance_total") or 0)
            existing.balance_remain = int(a.get("balance_remain") or 0)
            acc_updated += 1
        else:
            db.add(Account(
                name=a.get("name") or "",
                uid=uid,
                enterprise_id=a.get("enterprise_id") or "",
                domain=a.get("domain") or "",
                status=a.get("status") or "active",
                auth_json=a["auth_json"],
                balance_total=int(a.get("balance_total") or 0),
                balance_remain=int(a.get("balance_remain") or 0),
            ))
            acc_added += 1

    model_added = model_updated = 0
    for m in payload.get("models", []):
        mid = m.get("model_id")
        if not mid:
            continue
        existing = db.query(ModelConfig).filter(ModelConfig.model_id == mid).first()
        if existing:
            existing.enabled = int(m.get("enabled", existing.enabled))
            existing.credit_multiplier = float(m.get("credit_multiplier", existing.credit_multiplier or 0))
            existing.credits_raw = m.get("credits_raw") or ""
            existing.note = m.get("note") or existing.note
            model_updated += 1
        else:
            db.add(ModelConfig(
                level=m.get("level") or "system",
                model_id=mid,
                enabled=int(m.get("enabled", 1)),
                credit_multiplier=float(m.get("credit_multiplier", 0) or 0),
                credits_raw=m.get("credits_raw") or "",
                note=m.get("note") or "",
            ))
            model_added += 1

    db.commit()

    # --- 客户端参数档案 ---
    # 接收端把 source 钉成 saved：线上实例探测不到桌面端，如果还用 auto，
    # 一旦本地探测为空就会退化成兜底值，把刚同步过来的真实参数又盖掉了。
    profile_result: dict = {"applied": False}
    prof = payload.get("client_profile")
    if isinstance(prof, dict) and prof:
        res = cprofile.save(prof, new_source=cprofile.SOURCE_SAVED, db=db,
                            merge=False)
        profile_result = {
            "applied": bool(res.get("ok")),
            "source": res.get("source"),
            "fields": sorted(prof),
            "errors": res.get("errors") or [],
        }
        if res.get("ok"):
            _logger.info("已应用同步过来的客户端参数（%d 个字段，source=saved）",
                         len(prof))
        else:
            _logger.warning("客户端参数应用失败：%s", profile_result["errors"])

    return {
        "ok": True,
        "accounts": {"added": acc_added, "updated": acc_updated},
        "models": {"added": model_added, "updated": model_updated},
        "client_profile": profile_result,
    }
