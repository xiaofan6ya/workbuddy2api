#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""运行环境自检：Python / Node / 依赖 / 桌面端 / 风控 SDK / 数据库 / Redis。

为什么需要这个
--------------
用户机器上的环境差异很大，而失败方式往往**很难看懂**：

* 没装 Node -> `turing_helper.js` 起不来 -> 设备 token 取不到 -> 签到/对话被风控，
  但日志里只有一句「token 为空」，看不出是「缺 Node」；
* 没装桌面端 / 装在别处 -> 版本号退化成兜底值 -> UA 与真实客户端不一致；
* 少装某个 Python 包 -> 启动时 ImportError，但不知道缺哪个、装哪个；
* MySQL / Redis 不可达 -> 后台降级，功能静默缺失。

所以我们把所有环境前提**集中检查一遍**，每项给出「是什么 / 什么状态 /
缺了怎么办」三件事。默认只报告，不修改任何东西。

设计取向
--------
* **只读**：不装包、不写配置、不改注册表。
* **不抛异常**：任何探测失败都记成一项 FAIL/WARN，而不是让自检本身崩掉。
* **分级**：REQUIRED（缺了核心不能用）/ OPTIONAL（缺了功能降级）/
  DEV（只影响逆向取证）。
* **可机器读**：`check_all()` 返回结构化结果，也可 `--json` 输出，
  方便被安装脚本/CI 消费。
"""
from __future__ import annotations

import importlib
import importlib.util
import json
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path

#: 级别：越高越要紧
LEVEL_REQUIRED = "required"
LEVEL_OPTIONAL = "optional"
LEVEL_DEV = "dev"

_OK = "ok"
_WARN = "warn"
_FAIL = "fail"


class Item:
    """一条检查结果。"""

    __slots__ = ("key", "title", "level", "status", "detail", "hint")

    def __init__(self, key, title, level, status, detail="", hint=""):
        self.key = key
        self.title = title
        self.level = level
        self.status = status
        self.detail = detail
        self.hint = hint

    def as_dict(self) -> dict:
        return {
            "key": self.key, "title": self.title, "level": self.level,
            "status": self.status, "detail": self.detail, "hint": self.hint,
        }

    def line(self) -> str:
        mark = {_OK: "[ok]  ", _WARN: "[warn]", _FAIL: "[FAIL]"}[self.status]
        s = f"{mark} {self.title}"
        if self.detail:
            s += f"：{self.detail}"
        return s


# ---------------------------------------------------------------------------
# Python
# ---------------------------------------------------------------------------
#: 项目运行必需的第三方包（缺任何一个都起不来）
REQUIRED_MODULES = (
    ("fastapi", "fastapi"),
    ("uvicorn", "uvicorn"),
    ("httpx", "httpx"),
    ("sqlalchemy", "SQLAlchemy"),
    ("dotenv", "python-dotenv"),
    ("jwt", "PyJWT"),
    ("redis", "redis"),
)
#: 数据库驱动：按 ADMIN_DATABASE_URL 的方言**择一**即可，所以不放进
#: REQUIRED_MODULES（那是个「全部必须」的列表）。MySQL → PyMySQL，
#: PostgreSQL → psycopg；SQLite 走标准库，无需第三方驱动。
DB_DRIVER_MODULES = (
    ("pymysql", "PyMySQL", "mysql"),
    ("psycopg", "psycopg", "postgres"),
    ("psycopg2", "psycopg2-binary", "postgres"),
)
#: 可选：缺了只是个别功能降级
#: 注意：**不含 passlib/bcrypt** —— 本项目密码哈希用标准库
#: `hashlib.pbkdf2_hmac`（见 admin/security.py），不需要它们。
#: 列进来会误导用户去装没用的包。
OPTIONAL_MODULES = (
    ("cryptography", "cryptography"),        # JWT RS256 / 部分加密
    ("multipart", "python-multipart"),       # 表单上传
    ("pydantic", "pydantic"),
)


def _db_dialect_of(url: str) -> str:
    """从 ADMIN_DATABASE_URL 粗判方言：mysql / postgres / sqlite / 空串。"""
    u = (url or "").strip().lower()
    if u.startswith("mysql"):
        return "mysql"
    if u.startswith("postgres"):
        return "postgres"
    if u.startswith("sqlite"):
        return "sqlite"
    return ""



def check_python() -> list[Item]:
    items = []
    v = sys.version_info
    ver = f"{v.major}.{v.minor}.{v.micro}"
    # 代码用了 `X | None` 之类语法，需要 3.10+
    if v >= (3, 10):
        items.append(Item("python.version", "Python 版本", LEVEL_REQUIRED,
                          _OK, ver))
    else:
        items.append(Item("python.version", "Python 版本", LEVEL_REQUIRED,
                          _FAIL, ver,
                          "需要 Python 3.10 或更高（代码使用了 `X | None` 语法）"))
    items.append(Item("python.executable", "Python 解释器", LEVEL_OPTIONAL,
                      _OK, sys.executable))
    return items


def check_modules() -> list[Item]:
    items = []
    for mod, pkg in REQUIRED_MODULES:
        ok = importlib.util.find_spec(mod) is not None
        items.append(Item(
            f"py.{mod}", f"依赖 {pkg}", LEVEL_REQUIRED,
            _OK if ok else _FAIL,
            "已安装" if ok else "未安装",
            "" if ok else f"pip install {pkg}"))
    # 数据库驱动：只在「当前配置的方言」对应的驱动缺失时才算失败。
    # 例如配 PostgreSQL 而只装了 PyMySQL —— 那才是真缺；反过来同理。
    dialect = _db_dialect_of(os.getenv("ADMIN_DATABASE_URL", "")
                             or _configured_db_url())
    for mod, pkg, dialect_of in DB_DRIVER_MODULES:
        installed = importlib.util.find_spec(mod) is not None
        # 同一方言有多个驱动候选（psycopg / psycopg2），命中一个就算齐
        sibling_ok = any(importlib.util.find_spec(m) is not None
                         for m, _p, d in DB_DRIVER_MODULES
                         if d == dialect_of and m != mod)
        relevant = (dialect == dialect_of)
        if relevant and not (installed or sibling_ok):
            items.append(Item(
                f"py.{mod}", f"数据库驱动 {pkg}", LEVEL_REQUIRED, _FAIL, "未安装",
                f"当前 ADMIN_DATABASE_URL 是 {dialect}，请 pip install {pkg}"))
        elif installed:
            items.append(Item(
                f"py.{mod}", f"数据库驱动 {pkg}", LEVEL_OPTIONAL, _OK, "已安装"))
        else:
            # 与当前方言无关的驱动：不提示安装，避免误导
            items.append(Item(
                f"py.{mod}", f"数据库驱动 {pkg}", LEVEL_OPTIONAL, _OK,
                f"未安装（当前方言 {dialect or '未配置'} 不需要）"))
    for mod, pkg in OPTIONAL_MODULES:
        ok = importlib.util.find_spec(mod) is not None
        items.append(Item(
            f"py.{mod}", f"可选依赖 {pkg}", LEVEL_OPTIONAL,
            _OK if ok else _WARN,
            "已安装" if ok else "未安装（相关功能降级）",
            "" if ok else f"pip install {pkg}"))
    return items


def _configured_db_url() -> str:
    """从 admin.config 读 DATABASE_URL；读不到返回空串（未安装依赖时也会走到）。"""
    try:
        from admin.config import settings  # type: ignore
        return settings.DATABASE_URL or ""
    except Exception:
        return ""



# ---------------------------------------------------------------------------
# Node（设备风控 token 依赖）
# ---------------------------------------------------------------------------
def _node_version(node: str) -> str:
    try:
        out = subprocess.run([node, "--version"], capture_output=True,
                             text=True, timeout=8, check=False,
                             creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        return (out.stdout or "").strip()
    except Exception:
        return ""


def check_node() -> list[Item]:
    items = []
    node = shutil.which("node") or shutil.which("node.exe")
    if node:
        ver = _node_version(node)
        # 官方 CLI 要求 >= 18.20.8（见 cli/bin/codebuddy 的检查）
        major = 0
        try:
            major = int(ver.lstrip("v").split(".")[0])
        except Exception:
            pass
        if major >= 18:
            items.append(Item("node", "Node.js", LEVEL_OPTIONAL, _OK,
                              f"{ver} ({node})"))
        else:
            items.append(Item(
                "node", "Node.js", LEVEL_OPTIONAL, _WARN,
                f"{ver} 版本偏低 ({node})",
                "Turing SDK 建议 Node 18.20.8 以上；过低会导致取设备 token 失败"))
    else:
        items.append(Item(
            "node", "Node.js", LEVEL_OPTIONAL, _WARN, "未找到 node",
            "设备风控 token（X-Device-Token）依赖 Node 调 Turing SDK。"
            "没装 Node 时签到/对话可能被上游判为异常客户端；"
            "装 Node 18+ 即可，或用 WORKBUDDY_TURING_SDK_DIR 指定 SDK 目录"))
    return items


def check_npm() -> list[Item]:
    npm = shutil.which("npm") or shutil.which("npm.cmd")
    if npm:
        return [Item("npm", "npm", LEVEL_DEV, _OK, npm)]
    return [Item(
        "npm", "npm", LEVEL_DEV, _WARN, "未找到 npm",
        "仅在需要手工解包 app.asar 时需要；本项目已内置纯 Python 的 asar "
        "读取/抽取（wb_asar.py），不做逆向取证可以不装")]


# ---------------------------------------------------------------------------
# 桌面端 / asar / 风控 SDK
# ---------------------------------------------------------------------------
def check_desktop() -> list[Item]:
    items = []
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from wb_install import WB
    except Exception as e:
        return [Item("desktop", "WorkBuddy 桌面端", LEVEL_OPTIONAL, _WARN,
                     f"无法加载 wb_install：{e}")]

    inst = WB.install_dir()
    if inst:
        items.append(Item("desktop", "WorkBuddy 桌面端", LEVEL_OPTIONAL, _OK,
                          f"{inst}｜{WB.describe().split('|', 1)[-1].strip()}"))
    else:
        items.append(Item(
            "desktop", "WorkBuddy 桌面端", LEVEL_OPTIONAL, _WARN,
            "未发现安装目录（版本用兜底值）",
            "服务仍可运行，但出站 UA 报的版本可能与本机客户端不一致。"
            "设置 WORKBUDDY_INSTALL_DIR 指向安装目录，"
            "或把安装盘符加进 WORKBUDDY_DRIVES"))

    # app.asar
    asar = WB.asar_path()
    items.append(Item(
        "desktop.asar", "app.asar", LEVEL_DEV,
        _OK if asar else _WARN,
        str(asar) if asar else "未找到",
        "" if asar else "逆向取证/抽取源码需要它；可在 .env 里设 WORKBUDDY_ASAR_PATH"))

    # unpacked（product.json / turing-sdk 都在这里）
    up = WB.unpacked_dir()
    if up:
        missing = [r for r in ("cli/product.json", "cli/package.json")
                   if not (up / r).is_file()]
        if missing:
            items.append(Item(
                "desktop.unpacked", "app.asar.unpacked", LEVEL_OPTIONAL, _WARN,
                f"存在但缺 {', '.join(missing)}",
                "部分元数据将回退到 app.asar 内部读取（版本号仍可拿到）"))
        else:
            items.append(Item("desktop.unpacked", "app.asar.unpacked",
                              LEVEL_OPTIONAL, _OK, str(up)))
    else:
        items.append(Item(
            "desktop.unpacked", "app.asar.unpacked", LEVEL_OPTIONAL, _WARN,
            "未找到",
            "cli/product.json 等元数据不可读；桌面端版本会自动回退到 "
            "app.asar 内部的 /package.json（功能不受影响）"))

    # 风控 SDK
    sdk = None
    env_sdk = (os.getenv("WORKBUDDY_TURING_SDK_DIR") or "").strip()
    if env_sdk and Path(env_sdk).is_dir():
        sdk = Path(env_sdk)
    elif up:
        cand = up / "native" / "turing-sdk"
        if cand.is_dir():
            sdk = cand
    items.append(Item(
        "desktop.turing_sdk", "Turing 风控 SDK", LEVEL_OPTIONAL,
        _OK if sdk else _WARN,
        str(sdk) if sdk else "未找到",
        "" if sdk else "设备风控 token 需要它；缺了会导致敏感请求被风控"))

    # 实际能不能取到 token（最有说服力，但慢；失败不影响其它检查）
    try:
        import admin.turing_token as tt  # type: ignore
        tok = tt.get_device_token()
        items.append(Item(
            "desktop.device_token", "设备风控 token", LEVEL_OPTIONAL,
            _OK if tok else _WARN,
            f"已获取（{len(tok)} 字符）" if tok else "取不到",
            "" if tok else "确认已装 Node 与桌面端；"
                           "可跑 `python turing_helper.js` 看具体报错"))
    except Exception as e:
        items.append(Item("desktop.device_token", "设备风控 token",
                          LEVEL_OPTIONAL, _WARN, f"检查失败：{e}"))
    return items


def check_reverse_source() -> list[Item]:
    """逆向产物是否可用；不可用时能否自动产出。"""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from wb_install import WB
    except Exception as e:
        return [Item("source", "逆向产物", LEVEL_DEV, _WARN, str(e))]

    dest = WB.source_dir()
    has = dest.is_dir() and any(dest.iterdir())
    if has:
        n = sum(1 for _ in dest.rglob("*") if _.is_file())
        return [Item("source", "逆向产物", LEVEL_DEV, _OK,
                     f"{dest}（{n} 个文件）")]
    asar = WB.asar_path()
    if asar:
        return [Item(
            "source", "逆向产物", LEVEL_DEV, _WARN,
            f"未产出（{dest}）",
            "需要时可用 `python -m wb_asar ...` 或 WB.ensure_source() "
            "从 app.asar 自动抽取，无需 Node/npm")]
    return [Item("source", "逆向产物", LEVEL_DEV, _WARN,
                 "未产出，且找不到 app.asar", "设置 WORKBUDDY_ASAR_PATH")]


# ---------------------------------------------------------------------------
# 网络 / 服务
# ---------------------------------------------------------------------------
def _tcp_ok(host: str, port: int, timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False


def _parse_hostport(url: str, default_port: int):
    """从 `mysql+pymysql://u:p@h:3306/db` / `postgresql+psycopg://u:p@h:5432/db`
    这类 URL 里取 host/port；没写端口时用 default_port。"""
    try:
        tail = url.split("://", 1)[-1]
        tail = tail.split("@", 1)[-1] if "@" in tail else tail
        tail = tail.split("/", 1)[0]
        if ":" in tail:
            h, p = tail.rsplit(":", 1)
            return h or "127.0.0.1", int(p)
        return tail or "127.0.0.1", default_port
    except Exception:
        return "127.0.0.1", default_port


def check_services() -> list[Item]:
    items = []
    db_url = os.getenv("ADMIN_DATABASE_URL", "")
    redis_url = os.getenv("ADMIN_REDIS_URL", "")
    try:
        from admin.config import settings  # type: ignore
        db_url = db_url or settings.DATABASE_URL
        redis_url = redis_url or settings.REDIS_URL
    except Exception:
        pass

    if db_url:
        # 库名与默认端口按方言选：MySQL 3306 / PostgreSQL 5432。
        # 原来一律写死 "MySQL" + 3306，配 PG 时会把 5432 的实例当成 MySQL 报错。
        dialect = _db_dialect_of(db_url)
        label = {"mysql": "MySQL", "postgres": "PostgreSQL",
                 "sqlite": "SQLite"}.get(dialect, "数据库")
        default_port = 5432 if dialect == "postgres" else 3306
        if dialect == "sqlite":
            path = db_url.split("///", 1)[-1]
            ok = bool(path) and os.path.exists(path)
            items.append(Item("svc.db", f"SQLite ({path})", LEVEL_REQUIRED,
                              _OK if ok else _WARN,
                              "文件已存在" if ok else "文件尚未创建（首次启动会自动建）"))
        else:
            h, p = _parse_hostport(db_url, default_port)
            ok = _tcp_ok(h, p)
            items.append(Item("svc.db", f"{label} ({h}:{p})", LEVEL_REQUIRED,
                              _OK if ok else _FAIL,
                              "可连接" if ok else "连不上",
                              "" if ok else "检查服务是否启动、端口/账号是否正确"))
    else:
        items.append(Item("svc.db", "数据库", LEVEL_REQUIRED, _WARN,
                          "未配置 ADMIN_DATABASE_URL"))

    if redis_url:
        h, p = _parse_hostport(redis_url, 6379)
        ok = _tcp_ok(h, p)
        items.append(Item("svc.redis", f"Redis ({h}:{p})", LEVEL_OPTIONAL,
                          _OK if ok else _WARN,
                          "可连接" if ok else "连不上",
                          "" if ok else "缺了只影响缓存/限流计数，不影响核心转发"))
    else:
        items.append(Item("svc.redis", "Redis", LEVEL_OPTIONAL, _WARN,
                          "未配置 ADMIN_REDIS_URL"))
    return items


# ---------------------------------------------------------------------------
# 汇总
# ---------------------------------------------------------------------------
def check_all(include_slow: bool = True) -> list[Item]:
    """跑全部检查。include_slow=False 时跳过取 token 这类慢检查。"""
    items: list[Item] = []
    items += check_python()
    items += check_modules()
    items += check_node()
    items += check_npm()
    items += check_desktop() if include_slow else []
    items += check_reverse_source()
    items += check_services()
    return items


def summarize(items: list[Item]) -> dict:
    fails = [i for i in items if i.status == _FAIL]
    warns = [i for i in items if i.status == _WARN]
    blocking = [i for i in fails if i.level == LEVEL_REQUIRED]
    return {
        "ok": not fails,
        "usable": not blocking,
        "total": len(items),
        "fail": len(fails),
        "warn": len(warns),
        "blocking": [i.key for i in blocking],
        "items": [i.as_dict() for i in items],
    }


def report(items: list[Item]) -> str:
    """人类可读的报告（按级别分组）。"""
    lines = []
    order = (LEVEL_REQUIRED, LEVEL_OPTIONAL, LEVEL_DEV)
    titles = {LEVEL_REQUIRED: "必需", LEVEL_OPTIONAL: "可选（缺失则功能降级）",
              LEVEL_DEV: "开发/逆向取证"}
    for lv in order:
        group = [i for i in items if i.level == lv]
        if not group:
            continue
        lines.append(f"\n===== {titles[lv]} =====")
        for i in group:
            lines.append("  " + i.line())
            if i.status != _OK and i.hint:
                lines.append(f"         -> {i.hint}")
    s = summarize(items)
    lines.append("")
    if s["usable"]:
        lines.append(f"结论：可以运行（{s['total']} 项检查，"
                     f"{s['fail']} 失败 / {s['warn']} 警告）")
    else:
        lines.append(f"结论：**缺少必需组件，无法正常运行**："
                     f"{', '.join(s['blocking'])}")
    return "\n".join(lines)


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(
        description="workbuddy2api 运行环境自检（只读，不修改任何东西）")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--fast", action="store_true",
                    help="跳过慢检查（如实际获取设备 token）")
    args = ap.parse_args(argv)

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    items = check_all(include_slow=not args.fast)
    if args.json:
        print(json.dumps(summarize(items), ensure_ascii=False, indent=2))
    else:
        print(report(items))
    return 0 if summarize(items)["usable"] else 1


if __name__ == "__main__":
    sys.exit(main())
