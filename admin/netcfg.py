#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""服务监听地址配置：仅本机(127.0.0.1) / 局域网(0.0.0.0)。

要解决什么问题
--------------
切「仅本机」和「局域网」原本只能改 `.env` 里的 `ADMIN_HOST` 再重启，很别扭。
把监听地址也做成「后台可改」的配置：`system_settings.net_bind_host`。

优先级（**环境变量仍然最高**）
------------------------------
    命令行 --host  >  环境变量 ADMIN_HOST  >  后台保存值(DB)  >  默认 0.0.0.0

这个顺序与 `admin/wb_paths.py` 一致：环境变量是运维逃生门，容器 / systemd
注入的值不该被后台悄悄盖掉；反过来，后台改完能压住默认值。

生效方式
--------
监听地址只在 uvicorn **进程启动时**确定，运行期无法热切换。后台保存后需
**重启服务**才生效。本模块只负责「读/写/展示」，不负责重启。

谁读这个值
----------
- `main.py` 的 `_resolve_host_port()`：启动时读 `effective_host()` 拼 uvicorn 命令行。
- `admin/routers/netcfg.py`：管理台 UI 读写。
两者共用本模块，保证是同一个事实源。

约定
----
- env 高于 DB。
- `threading.RLock` + 读改写串行化（同 `wb_paths.save`）。
- **绝不抛异常**：DB 挂了退默认 0.0.0.0，而不是 fail-closed 到 127.0.0.1
  （否则 DB 一抖，服务就再也连不上局域网）。
- 缓存是**进程内**的：main.py 与 uvicorn 是两个进程，各持一份，各自在启动/
  首次调用时读一次 DB，互不干扰。
"""
from __future__ import annotations

import logging
import os
import threading

_logger = logging.getLogger(__name__)

#: 对应的环境变量名（显式配置即锁定后台开关）
ENV = "ADMIN_HOST"

#: system_settings 里的键名
KEY = "net_bind_host"

LOCAL = "127.0.0.1"
LAN = "0.0.0.0"

#: 后台允许写入的取值（归一后的标准形式）
ALLOWED = (LOCAL, LAN)

#: 默认值。与历史默认一致(0.0.0.0)，不改变现有部署行为。
DEFAULT = LAN

_lock = threading.RLock()
_cache: dict | None = None


def _env_host() -> str:
    """env 显式配置（且非空）时的原始 host；否则空串。"""
    return (os.getenv(ENV) or "").strip()


def _normalize(v) -> str | None:
    """宽松归一：接受 localhost / 0.0.0.0 / 127.0.0.1 等写法；非法返回 None。"""
    s = (str(v) if v is not None else "").strip().lower()
    if s in ("127.0.0.1", "localhost", "::1", "local"):
        return LOCAL
    if s in ("0.0.0.0", "lan", "all"):
        return LAN
    return None


def _read_db(db=None) -> str:
    """DB 里保存的 host（归一后）；任何异常返回空串（绝不抛）。"""
    from admin.db import SessionLocal
    from admin.models import SystemSetting

    own = db is None
    if own:
        try:
            db = SessionLocal()
        except Exception as e:
            _logger.debug("打开数据库会话失败：%s", e)
            return ""
    try:
        row = db.query(SystemSetting).filter(SystemSetting.key == KEY).first()
        return (_normalize(row.value) or "") if row else ""
    except Exception as e:
        _logger.debug("读取监听地址设置失败：%s", e)
        return ""
    finally:
        if own and db is not None:
            db.close()


def _compute(db=None) -> dict:
    """按 env > DB > 默认 计算当前值 + 来源 + 是否可编辑（不加锁，由调用方持锁）。"""
    env = _env_host()
    if env:
        h = _normalize(env)
        return {
            "host": h or DEFAULT,
            "source": f"环境变量 {ENV}",
            "env_raw": env,
            "editable": False,
            "saved": _read_db(db) or None,
        }
    saved = _read_db(db)
    return {
        "host": saved or DEFAULT,
        "source": "后台设置" if saved else "默认值",
        "env_raw": "",
        "editable": True,
        "saved": saved or None,
    }


def current(db=None, fresh: bool = False) -> dict:
    """当前生效监听地址 + 来源 + 是否可编辑（带缓存；绝不抛）。

    返回 dict：`{host, source, editable, env_raw, saved}`。
    """
    global _cache
    with _lock:
        if _cache is None or fresh:
            try:
                _cache = _compute(db)
            except Exception as e:  # 兜底：宁可退化也不 500
                _logger.warning("监听地址配置计算失败，退回默认：%s", e)
                _cache = {"host": DEFAULT, "source": "默认值",
                          "env_raw": "", "editable": False, "saved": None}
        return dict(_cache)


def effective_host(db=None) -> str:
    """当前应使用的监听地址（供 main.py 启动时调用）。"""
    return current(db=db)["host"]


def save(host: str, db=None) -> dict:
    """保存监听地址（仅当 env 未锁定）。

    返回 `{ok, host, source, editable, saved, errors}`。env 锁定或非法值都
    返回 `ok=False` 且不改库。
    """
    global _cache
    if _env_host():
        return {"ok": False,
                "errors": [f"当前由环境变量 {ENV} 决定，无法从后台修改"],
                **current(db)}

    h = _normalize(host)
    if h is None:
        return {"ok": False,
                "errors": [f"非法取值 {host!r}，只允许 {', '.join(ALLOWED)}"],
                **current(db)}

    from admin.db import SessionLocal
    from admin.models import SystemSetting

    own = db is None
    if own:
        db = SessionLocal()
    try:
        with _lock:  # 读改写串行化（参照 wb_paths.save）
            row = db.query(SystemSetting).filter(SystemSetting.key == KEY).first()
            if row:
                row.value = h
            else:
                db.add(SystemSetting(key=KEY, value=h))
            db.commit()
    finally:
        if own and db is not None:
            db.close()

    with _lock:
        _cache = None
    return {"ok": True, "errors": [], **current(db, fresh=True)}


def invalidate() -> None:
    """清除进程内缓存（下次读取时重新计算）。"""
    global _cache
    with _lock:
        _cache = None
