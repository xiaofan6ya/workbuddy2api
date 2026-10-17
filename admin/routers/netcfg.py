"""服务监听地址（仅本机 / 局域网）配置路由。

安全说明
--------
本路由挂在 `/api` 前缀下，而 `/api` 已被 `ConsoleLocalOnlyMiddleware`
（`admin/server.py`）**锁定为仅本机可访问**。所以即便 `ADMIN_HOST=0.0.0.0`，
局域网设备也无法通过本端点修改监听地址 —— 与其它后台设置一致。

生效方式：保存后需**重启服务**才生效（监听地址只在进程启动时确定）。
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from admin import netcfg
from admin.config import settings
from admin.db import get_db
from admin.security import require_admin

router = APIRouter(prefix="/api/net", tags=["net"])


class NetConfigIn(BaseModel):
    host: str


@router.get("/config")
def get_config(_: bool = Depends(require_admin), db: Session = Depends(get_db)):
    d = netcfg.current(db, fresh=True)
    return {
        "host": d["host"],
        "mode": "local" if d["host"] == netcfg.LOCAL else "lan",
        "source": d["source"],
        "editable": d["editable"],
        "env_var": netcfg.ENV,
        "env_value": d.get("env_raw") or "",
        "saved": d.get("saved"),
        "allowed": list(netcfg.ALLOWED),
        "console_local_only": settings.CONSOLE_LOCAL_ONLY,
    }


@router.put("/config")
def put_config(body: NetConfigIn, _: bool = Depends(require_admin),
               db: Session = Depends(get_db)):
    res = netcfg.save(body.host, db)
    if not res.get("ok"):
        # 409：env 锁定（语义区别于参数错误，前端可据此给出「去改 .env」提示）
        # 400：非法取值
        code = 409 if not res.get("editable") else 400
        raise HTTPException(status_code=code, detail="；".join(res.get("errors") or []))
    return {
        "ok": True,
        "host": res["host"],
        "source": res["source"],
        "message": "已保存，重启服务后生效",
    }
