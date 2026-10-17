#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""main.py — 一键拉起 workbuddy2api（单端口单进程部署）。

本项目对外只暴露「一个端口」即可：
  - 管理后台前端（index.html）与后端 API：http://127.0.0.1:8790/admin
  - 对外共享的托管网关（带 Key 校验 / 配额 / 用量记账）：http://127.0.0.1:8790/v1/chat/completions、/v1/models
  - 内嵌的独立网关 converter（本机桌面登录态直连，额外支持 /v1/responses、/v1/messages、/v1/balance）：
        http://127.0.0.1:8790/gw/v1/...

converter 已在 admin/server.py 中挂载到 /gw 前缀，因此无需再单独开 8787 端口/进程。
（若你确实需要独立运行 converter 在 8787，直接 `python converter.py --desensitize` 即可，与本脚本互不冲突。）

子进程输出实时 tee 到控制台 + logs/ 日志；Ctrl+C / 关闭终端优雅关闭。

用法:
  python main.py                  # 前台常驻，监听 0.0.0.0:8790
  python main.py --port 8790
  python main.py --host 127.0.0.1
  python main.py --restart        # 端口被占用时强制结束占用者再启动

端口占用处理:
  直接再跑一次 `python main.py` 就能重启 —— 检测到占用端口的**是本项目自己的
  旧实例**（uvicorn admin.server:app / 路径指向本项目）时会自动结束它并接管。
  只有占用者是**别的程序**时才拒绝启动（避免误杀），此时若确认要停掉它，
  用 `--restart`（等价于旧版的 `--force`）。

环境变量（可选，覆盖默认；部署务必设置）:
  ADMIN_PORT / ADMIN_HOST
      ADMIN_HOST 会覆盖管理台里的「服务监听地址」开关（环境变量是运维逃生门，
      优先级最高）。想用管理台开关控制监听地址，请把 ADMIN_HOST 留空或删除。
  ADMIN_JWT_SECRET  : 生产务必覆盖为 >=32 字节随机串（默认是弱密钥，会报警）
  ADMIN_USERNAME / ADMIN_PASSWORD     : 后台登录凭据（**必填**，无默认值；未配置则登录 503）
  CONVERTER_DESENSITIZE / CONVERTER_LOG : 内嵌网关的脱敏开关与日志路径
"""
from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PY = sys.executable  # 用运行本脚本的同一个解释器（venv / 系统都可）
LOGS = ROOT / "logs"
LOGS.mkdir(exist_ok=True)


def _load_dotenv() -> None:
    """尽早把项目根目录的 .env 注入到 os.environ。

    必须在下面的部署安全检查之前执行：此前这些检查读的是「尚未加载 .env」的
    环境，于是即使 .env 里已经配好了 ADMIN_JWT_SECRET / ADMIN_USERNAME /
    ADMIN_PASSWORD，启动时依然会打印「未设置强 ADMIN_JWT_SECRET」「未配置
    ADMIN_USERNAME / ADMIN_PASSWORD」这类**误导性告警**，让人误以为配置丢了。
    （子系统自身会加载 .env，所以这只是虚惊一场，但足够让人排查半天。）
    """
    env_file = ROOT / ".env"
    if not env_file.is_file():
        return
    try:
        from dotenv import load_dotenv
        load_dotenv(env_file)
        return
    except Exception:
        pass
    # 没装 python-dotenv 时退化为一个极简解析器（够用：KEY=VALUE，忽略注释）
    try:
        for raw in env_file.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if k and k not in os.environ:
                os.environ[k] = v
    except Exception:
        pass


_load_dotenv()

_PRINT_LOCK = threading.Lock()


def _log(msg: str) -> None:
    """安全打印：绝不因为控制台编码问题把整个监督进程搞崩。

    真实事故：Windows 控制台默认 GBK，`print("❌ ...")` 抛
    `UnicodeEncodeError: 'gbk' codec can't encode character '\\u274c'`。
    而 `_log` 是**监督进程**用来报告「子进程已退出」的关键路径 ——
    它一崩，父进程直接退出，uvicorn 子进程变成孤儿：
    端口还在监听，但已经没人管它，表现为**服务无响应且日志不再增长**，
    排查时极难看出真正原因（报错只写在 stderr 里）。

    所以这里显式按 UTF-8 写 stdout，失败再退到「替换不可编码字符」，
    保证日志功能永远不会反过来杀死进程。
    """
    with _PRINT_LOCK:
        try:
            print(msg, flush=True)
        except UnicodeEncodeError:
            # 退路 1：按 UTF-8 直接写底层 buffer（绕过控制台编码）
            try:
                buf = getattr(sys.stdout, "buffer", None)
                if buf is not None:
                    buf.write((msg + "\n").encode("utf-8", "replace"))
                    buf.flush()
                    return
            except Exception:
                pass
            # 退路 2：替换掉编不了的字符，至少把信息打出来
            try:
                enc = getattr(sys.stdout, "encoding", None) or "utf-8"
                print(msg.encode(enc, "replace").decode(enc, "replace"), flush=True)
            except Exception:
                pass


def _pump(stream, log_path: Path, tag: str) -> None:
    """把子进程输出实时打到控制台并写入日志文件（类 tee，文本模式）。

    ⚠️ **这个函数一旦停止读取，整个服务就会「僵死」** —— 必须永不退出。

    原理：子进程的 stdout 是一根**管道**（PIPE）。管道缓冲区满了以后，
    子进程再写就会**阻塞在 write 上**；而 uvicorn 的 access log 是在
    事件循环里打的，于是「打日志」这一步把事件循环冻住：
    端口还在监听、进程 CPU 归零、**任何请求（连 404 路由）都不再返回**。
    实测就是这样：`admin.log` 停在某一刻不再增长，全站随后无响应。

    原实现把整个 `for` 循环包在一个 `try/except: pass` 里 —— 只要写入
    控制台或日志文件抛一次异常（最典型是 Windows GBK 控制台遇到中文/emoji
    的 `UnicodeEncodeError`），**读取线程直接结束**，管道再没人排空，
    子进程随即卡死。而 `pass` 让这件事连一行痕迹都没留下。

    现在的做法：读循环绝不因为「写失败」而退出 —— 写不进去就丢弃该行，
    继续排空管道。宁可丢日志，也不能让服务僵死。
    """
    lf = None
    try:
        lf = open(log_path, "a", encoding="utf-8")
    except Exception:
        lf = None
    try:
        for raw in iter(stream.readline, ""):
            if not raw:
                break
            # 1) 控制台回显：失败不算事，接着读
            try:
                _log(f"[{tag}] {raw.rstrip()}")
            except Exception:
                pass
            # 2) 落盘：失败也不算事，接着读
            if lf is not None:
                try:
                    lf.write(raw)
                    lf.flush()
                except Exception:
                    try:
                        lf.close()
                    except Exception:
                        pass
                    lf = None       # 之后只排空管道，不再尝试写盘
    finally:
        # 无论如何都要把管道读到 EOF：这是保证子进程不被写阻塞的最后一道保险。
        try:
            for _ in iter(stream.readline, ""):
                pass
        except Exception:
            pass
        if lf is not None:
            try:
                lf.close()
            except Exception:
                pass


# 运行后台所需的核心依赖；当前解释器缺任一即尝试切换到带依赖的虚拟环境。
_REQUIRED_DEPS = ["pymysql", "uvicorn", "fastapi", "redis", "sqlalchemy"]


def _interpreter_with_deps() -> str | None:
    """返回带全部依赖的 python 解释器路径。

    优先用当前解释器；若缺包，则回退到项目内 .venv/venv 或本机 managed venv。
    都找不到返回 None（调用方给出安装提示后退出）。
    """
    try:
        import importlib
        for m in _REQUIRED_DEPS:
            importlib.import_module(m)
        return sys.executable
    except Exception:
        pass
    candidates = [
        ROOT / ".venv" / "Scripts" / "python.exe",
        ROOT / "venv" / "Scripts" / "python.exe",
        ROOT / ".venv" / "bin" / "python",
        ROOT / "venv" / "bin" / "python",
        Path(r"C:\Users\Administrator\.workbuddy\binaries\python\envs\default\Scripts\python.exe"),
    ]
    for c in candidates:
        if not c.exists():
            continue
        try:
            subprocess.run(
                [str(c), "-c", "import " + ", ".join(_REQUIRED_DEPS)],
                check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            return str(c)
        except Exception:
            continue
    return None


def _resolve_host_port(args) -> tuple[str, int]:
    """解析监听地址/端口。

    **host 优先级：显式命令行 --host > 环境变量 ADMIN_HOST > 后台设置(DB) > 默认 0.0.0.0。**
    **port 优先级：显式命令行 --port > 环境变量 ADMIN_PORT > 默认 8790。**

    端口那段的原实现写成 `os.getenv("ADMIN_PORT", str(args.port))` —— 环境变量优先，
    于是 `.env` 里的 `ADMIN_PORT=8790` 会**吞掉** `--port 58634`，
    命令行上写的端口被静默忽略、仍然去起 8790：接管逻辑随即杀掉线上实例。
    实测中这造成过两次线上服务被误停（用 `--port` 起测试实例时）。
    命令行是操作者的**当次明确意图**，必须压过配置文件里的常驻值。

    host 多出的「后台设置(DB)」这一级：管理台 UI 的「服务监听地址」开关把值存进
    `system_settings.net_bind_host`（见 `admin/netcfg.py`）。监听地址只在进程启动时
    确定，所以必须由这里读出来拼进 uvicorn 命令行。环境变量仍然是运维逃生门，优先于 DB。
    """
    if args.host is not None:
        host = args.host  # 命令行是当次明确意图，最高优先（保持原语义）
    elif (os.getenv("ADMIN_HOST") or "").strip():
        host = os.getenv("ADMIN_HOST").strip()  # 环境变量次之（运维逃生门）
    else:
        # 未显式配置：读后台保存值。失败一律退回默认，绝不让启动失败。
        try:
            from admin import netcfg
            host = netcfg.effective_host()
        except Exception as e:
            _log(f"[main] 读取后台监听地址失败，使用默认 0.0.0.0：{e}")
            host = "0.0.0.0"
    if args.port is not None:
        port = int(args.port)
    else:
        port = int(os.getenv("ADMIN_PORT", "8790"))
    return host, port


def _build_admin_cmd(args) -> tuple[list[str], int, str]:
    host, port = _resolve_host_port(args)
    cmd = [PY, "-m", "uvicorn", "admin.server:app",
           "--host", host, "--port", str(port), "--log-level", "info"]
    return cmd, port, host


#: 宝塔面板的 pid 文件路径。写在这里，面板「停止」按钮才杀得对进程。
#: Windows 上没有该目录树（`/www/...` 会被解析成 `<盘符>:\www\...`，
#: 于是在项目之外凭空造目录），改用项目内的隐藏文件。
_PIDFILE = (Path("/www/server/python_project/vhost/pids/workbuddy2api.pid")
            if os.name != "nt" else ROOT / ".workbuddy2api.pid")


def _write_pidfile() -> None:
    """把本进程真实 pid 写入 pid 文件（宝塔「停止」按钮据此结束服务）。"""
    try:
        _PIDFILE.parent.mkdir(parents=True, exist_ok=True)
        _PIDFILE.write_text(str(os.getpid()), encoding="utf-8")
        _log(f"[main] pid {os.getpid()} 已写入 {_PIDFILE}")
    except Exception:
        pass


def _remove_pidfile() -> None:
    """退出时清理 pid 文件；仅当文件里确实是本进程时才删，避免误删新实例的。"""
    try:
        if _PIDFILE.is_file() and _PIDFILE.read_text(encoding="utf-8").strip() == str(os.getpid()):
            _PIDFILE.unlink()
    except Exception:
        pass


def _kill_children(procs: list) -> None:
    """结束所有子进程（含其进程组），确保不留孤儿占着端口。"""
    for _tag, p in procs:
        try:
            if p.poll() is not None:
                continue
            # 子进程以 start_new_session=True 启动，自成进程组 → 整组结束
            try:
                os.killpg(os.getpgid(p.pid), signal.SIGTERM)
            except Exception:
                p.terminate()
        except Exception:
            pass
    # 给一点时间优雅退出，仍存活则强杀
    deadline = time.time() + 12
    for _tag, p in procs:
        try:
            remain = max(0.5, deadline - time.time())
            p.wait(timeout=remain)
        except Exception:
            try:
                os.killpg(os.getpgid(p.pid), signal.SIGKILL)
            except Exception:
                try:
                    p.kill()
                except Exception:
                    pass


def _port_in_use(host: str, port: int) -> bool:
    """探测端口是否已被监听（用于启动前自检，避免撞端口后疯狂重启）。"""
    import socket
    probe_host = "127.0.0.1" if host in ("0.0.0.0", "::", "") else host
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1.0)
        return s.connect_ex((probe_host, port)) == 0


# ---------------------------------------------------------------------------
# 进程 / 端口识别（跨平台）
#
# 为什么需要这一层：原先的实现**只认 Linux** ——
#   * `_pid_listening_on()` 调用 `ss -lntpH`；
#   * `_is_our_instance()` 读 `/proc/<pid>/cwd`。
# 在 Windows 上前者抛异常（没有 ss）、后者恒为 False，于是
# 「被本项目的旧实例占用 → 接管」这条分支**永远不可能命中**：
# 第二次 `python main.py` 只会拿到一句「端口已被占用」然后退出 ——
# 明明是自己上次没关干净，却没有任何办法重启。
# ---------------------------------------------------------------------------

def _norm_text(s) -> str:
    """路径/命令行归一化，便于跨平台比较（统一斜杠、忽略大小写）。"""
    return str(s or "").replace("\\", "/").lower()


def _run_quiet(cmd: list[str], timeout: float = 15.0) -> str:
    """执行命令并返回 stdout；任何失败都返回空串。

    识别进程失败不该让「启动」这件事崩掉，所以这里吞掉所有异常。
    """
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
        return r.stdout or ""
    except Exception:
        return ""


#: PowerShell：列出「pid<TAB>命令行」。整表一次取出，而不是逐个 pid 起进程 ——
#: 每次 PowerShell 启动要几百毫秒，逐个查会慢到无法接受。
_PS_LIST_PROCS = (
    "[Console]::OutputEncoding=[Text.Encoding]::UTF8; "
    "Get-CimInstance Win32_Process | "
    'ForEach-Object { "$($_.ProcessId)`t$($_.CommandLine)" }'
)


def _listener_pids(port: int) -> list[int]:
    """返回监听指定端口的 pid 列表（Windows: netstat → CIM；Linux: ss）。"""
    if os.name == "nt":
        # netstat 最快且系统自带；解析不出来再退回 CIM（更准但慢）
        return _listener_pids_netstat(port) or _listener_pids_cim(port)
    return _listener_pids_ss(port)


def _listener_pids_ss(port: int) -> list[int]:
    import re as _re
    out = _run_quiet(["ss", "-lntpH", f"sport = :{port}"], timeout=5.0)
    pids: list[int] = []
    for m in _re.finditer(r"pid=(\d+)", out):
        pid = int(m.group(1))
        if pid not in pids:
            pids.append(pid)
    return pids


def _listener_pids_netstat(port: int) -> list[int]:
    """解析 `netstat -ano` 的 LISTENING 行（Windows）。

    形如：  TCP    0.0.0.0:8790    0.0.0.0:0    LISTENING    12345
    """
    out = _run_quiet(["netstat", "-ano", "-p", "TCP"], timeout=10.0)
    want = f":{port}"
    pids: list[int] = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) != 5:
            continue
        proto, local, _remote, state, pid_s = parts
        if proto.upper() != "TCP" or not state.upper().startswith("LISTEN"):
            continue
        # 用 endswith 而非等值：本地地址可能是 0.0.0.0:8790 或 [::]:8790
        if not local.endswith(want):
            continue
        try:
            pid = int(pid_s)
        except ValueError:
            continue
        if pid not in pids:
            pids.append(pid)
    return pids


def _listener_pids_cim(port: int) -> list[int]:
    out = _run_quiet(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command",
         "[Console]::OutputEncoding=[Text.Encoding]::UTF8; "
         f"Get-NetTCPConnection -LocalPort {port} -State Listen "
         "-ErrorAction SilentlyContinue | "
         "Select-Object -ExpandProperty OwningProcess"],
        timeout=20.0)
    pids: list[int] = []
    for line in out.splitlines():
        s = line.strip()
        if s.isdigit():
            pid = int(s)
            if pid not in pids:
                pids.append(pid)
    return pids


def _cmdline_of(pid: int) -> str:
    """取某进程的命令行（取不到返回空串，不抛异常）。"""
    if os.name != "nt":
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                return f.read().replace(b"\0", b" ").decode("utf-8", "ignore").strip()
        except Exception:
            return ""
    out = _run_quiet(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command",
         "[Console]::OutputEncoding=[Text.Encoding]::UTF8; "
         f"(Get-CimInstance Win32_Process -Filter 'ProcessId={pid}').CommandLine"])
    if out.strip():
        return out.strip()
    # 老系统上 wmic 仍在，作为兜底
    out = _run_quiet(["wmic", "process", "where", f"processid={pid}",
                      "get", "commandline", "/value"], timeout=10.0)
    for line in out.splitlines():
        if line.lower().startswith("commandline="):
            return line.split("=", 1)[1].strip()
    return ""


def _is_our_instance(pid: int) -> bool:
    """该 pid 是否就是**本项目**的进程（宁松勿严：认出自己人才敢动手）。

    两个判据，任一命中即可：
      * Linux：`/proc/<pid>/cwd` 指向本项目根目录；
      * 通用 ：命令行里出现本项目根目录的绝对路径。

    第二条是关键 —— uvicorn 子进程的命令行形如
    `<项目>/.venv/Scripts/python.exe -m uvicorn admin.server:app ...`，
    本身就带着项目路径，所以**在 Windows 上也能认出来**。
    """
    if os.name != "nt":
        try:
            if os.path.realpath(f"/proc/{pid}/cwd") == os.path.realpath(str(ROOT)):
                return True
        except Exception:
            pass
    cmd = _cmdline_of(pid)
    return bool(cmd) and _norm_text(ROOT) in _norm_text(cmd)


def _our_related_pids() -> list[int]:
    """命令行里带本项目**入口**的所有进程（含 main.py 包装进程）。

    结束监听端口的 uvicorn 后，拉起它的 `main.py` 通常会发现子进程没了而自行退出；
    但它若卡住就会留下一个不占端口的孤儿，下次启动仍会困惑。这里一并收掉。

    ⚠️ 判据必须严：**不能**只看「命令行里出现项目路径」。实测发现那样会命中
    无关进程 —— 例如在本目录下启动的编辑器、终端、其它工具链（它们的命令行或
    cwd 参数里同样含这个路径）。误杀的代价不可逆，所以这里复用
    `_looks_like_our_service()`：必须是我们的 ASGI app，或确实在跑本项目入口脚本。
    """
    me = os.getpid()
    protected = _ancestor_pids() | {me}
    pids: list[int] = []
    if os.name == "nt":
        out = _run_quiet(["powershell", "-NoProfile", "-NonInteractive",
                          "-Command", _PS_LIST_PROCS], timeout=25.0)
        for line in out.splitlines():
            if "\t" not in line:
                continue
            pid_s, cmd = line.split("\t", 1)
            if not _looks_like_our_service(cmd):
                continue
            try:
                pid = int(pid_s.strip())
            except ValueError:
                continue
            if pid not in protected and pid not in pids:
                pids.append(pid)
    else:
        try:
            for name in os.listdir("/proc"):
                if not name.isdigit():
                    continue
                pid = int(name)
                if pid in protected or pid in pids:
                    continue
                if _looks_like_our_service(_cmdline_of(pid)):
                    pids.append(pid)
        except Exception:
            pass
    return pids


def _terminate_tree(pid: int) -> None:
    """结束一个进程及其子进程（跨平台，尽力而为，失败不抛）。"""
    if os.name == "nt":
        # /T 连子进程一起收；必须带 /F —— 控制台进程对不带 /F 的 taskkill
        # 通常不响应（会回一句 could not be terminated）。
        _run_quiet(["taskkill", "/PID", str(pid), "/T", "/F"], timeout=20.0)
        return
    # POSIX：优先整组结束（子进程以 start_new_session 启动、自成进程组）
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(os.getpgid(pid), sig)
        except Exception:
            try:
                os.kill(pid, sig)
            except ProcessLookupError:
                return
            except Exception:
                return
        time.sleep(1.0 if sig == signal.SIGTERM else 0.3)


def _wait_port_released(host: str, port: int, timeout: float) -> bool:
    """等端口释放；返回是否真的释放了。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not _port_in_use(host, port):
            return True
        time.sleep(0.3)
    return not _port_in_use(host, port)


#: 重启场景下，等待旧实例释放端口的最长时间（秒）。
#: 宝塔面板的「重启」是 stop → sleep(1) → start：只要旧进程优雅退出略慢于 1 秒，
#: 新进程启动时端口就仍被占用。给一段等待窗口，重启才不会误判失败。
_RESTART_WAIT = 25.0


#: 本服务独有的命令行特征：uvicorn 的 ASGI 目标。
#: 比「路径里含项目名」更硬 —— 即使解释器在项目目录之外（系统 Python、
#: managed venv）也能认出这是我们自己的 uvicorn，而不是别人的服务。
_OUR_UVICORN_TARGET = "admin.server:app"

#: 本项目的入口脚本。仅当命令行里**同时**出现项目根目录与其中之一时，
#: 才认定是「我们的进程」。单看路径不够：在项目目录下开着的编辑器、终端、
#: 其它工具链，命令行里同样会带这个路径。
_OUR_ENTRY_SCRIPTS = ("main.py", "converter.py", "admin/server.py")


def _looks_like_our_service(cmd: str) -> bool:
    """命令行是否属于**本项目**的服务进程（严格判据，宁可漏杀不可误杀）。

    命中任一：
      1. 含 uvicorn 目标 `admin.server:app` —— 最强特征，与路径无关；
      2. 命令行以 **python 解释器** 开头，且含项目根目录与本项目入口脚本名。

    为什么不能只看「路径出现在命令行里」：实测踩过两次 ——
      * 在项目目录下启动的编辑器 / 终端 / node 工具链，命令行里也含该路径；
      * **外面那层 shell**：`powershell -Command "...Start-Process python main.py..."`，
        命令行同时含项目路径与 `main.py`，结果把「启动服务的那个 shell」杀掉了。
    这个判据会用来决定**结束进程**，误杀的代价不可逆，所以必须严。
    """
    c = (cmd or "").replace("\\", "/")
    if not c:
        return False
    if _OUR_UVICORN_TARGET in c:
        return True
    if _norm_text(ROOT) not in _norm_text(c):
        return False
    low = c.lower()
    if not any(s in low for s in _OUR_ENTRY_SCRIPTS):
        return False
    # 必须真由 python 解释器执行。cmd/powershell/bash 这层「包装器」不算 ——
    # 杀掉它等于杀掉用户自己的终端。
    return _is_python_exe(_first_token(c))


def _is_python_exe(token: str) -> bool:
    """该可执行文件名是否像 python 解释器（python / python3.13 / pythonw …）。"""
    name = token.replace("\\", "/").rsplit("/", 1)[-1].strip('"').lower()
    name = name[:-4] if name.endswith(".exe") else name
    return name.startswith("python") or name.startswith("pypy")


def _first_token(cmd: str) -> str:
    """取命令行首个 token，正确处理带空格的引号路径。"""
    s = (cmd or "").lstrip()
    if s.startswith('"'):
        end = s.find('"', 1)
        return s[1:end] if end > 0 else s
    return s.split(" ", 1)[0]


def _ancestor_pids() -> set[int]:
    """本进程的所有祖先 pid。**绝不能**结束它们 —— 那是用户的终端/启动器。"""
    out: set[int] = set()
    try:
        if os.name == "nt":
            raw = _run_quiet(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command",
                 "[Console]::OutputEncoding=[Text.Encoding]::UTF8; "
                 "Get-CimInstance Win32_Process | "
                 'ForEach-Object { "$($_.ProcessId) $($_.ParentProcessId)" }'],
                timeout=25.0)
            parent: dict[int, int] = {}
            for line in raw.splitlines():
                parts = line.split()
                if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
                    parent[int(parts[0])] = int(parts[1])
            cur = os.getpid()
            for _ in range(64):
                nxt = parent.get(cur)
                if not nxt or nxt in out or nxt == cur:
                    break
                out.add(nxt)
                cur = nxt
        else:
            cur = os.getpid()
            for _ in range(64):
                with open(f"/proc/{cur}/status", encoding="utf-8") as f:
                    ppid = 0
                    for line in f:
                        if line.startswith("PPid:"):
                            ppid = int(line.split()[1])
                            break
                if not ppid or ppid in out or ppid == cur:
                    break
                out.add(ppid)
                cur = ppid
    except Exception:
        pass
    return out


def _describe_pid(pid: int) -> str:
    cmd = _cmdline_of(pid)
    if not cmd:
        return f"    pid {pid}: (取不到命令行)"
    if len(cmd) > 200:
        cmd = cmd[:197] + "..."
    return f"    pid {pid}: {cmd}"


def _ensure_port_free(host: str, port: int, force: bool = False) -> None:
    """启动前检查端口，避免「撞端口 → 无限重启」的重启风暴。

    线上症状：systemd 与宝塔面板同时在拉起本服务，后启动的因
    `address already in use` 立刻退出，而 `Restart=always` 让它每 5 秒重试，
    累计重启 12603 次，日志被刷爆。这里在 bind 之前就明确判断：

      * 端口空闲                → 正常启动；
      * 被**本项目的旧实例**占用  → 说明是「重启」：结束旧实例、等端口释放后接管；
      * 被**其它进程**占用        → 报错退出；只有显式 `--force` 才结束它。

    为什么要主动接管而不是直接退出：实测发现，宝塔停止项目时若只 kill 了
    `main.py` 而没能带走它派生的 uvicorn 子进程，端口就会被这个孤儿一直占着。
    此时新实例若只是「退出报错」，面板会显示启动失败，且 pid 文件记录的是
    已死的进程 —— 之后每次「停止」都杀不掉真正在跑的 uvicorn，形成死结。
    主动接管可以自愈这种情况。

    参数 `force` 由 `--force` 传入：**只**用于「明知占端口的不是本项目、但
    确认要重启」的场景（例如上次是别的方式拉起来的、命令行特征对不上）。
    默认关闭，因为误杀别人的服务是不可逆的。
    """
    if not _port_in_use(host, port):
        return

    pids = _listener_pids(port)

    # 拿不到 pid 时不做任何猜测（例如权限不足）：直接报错，交给用户处理。
    if not pids:
        _log(f"❌ 端口 {port} 已被占用，但无法识别占用进程的 pid"
             f"（可能是权限不足，试试以管理员/root 运行）。")
        _log(f"    → 或改用其他端口： python main.py --port {port + 1}")
        sys.exit(3)

    cmds = {p: _cmdline_of(p) for p in pids}
    # 祖先进程绝不能杀（那是调用我们的终端 / 启动器）。若发现端口的持有者
    # 竟是自己的祖先，说明判断链有问题，宁可停手报错。
    ancestors = _ancestor_pids() | {os.getpid()}
    danger = [p for p in pids if p in ancestors]
    if danger:
        _log(f"❌ 端口 {port} 的占用进程 {danger} 是本进程的祖先（终端/启动器），"
             f"拒绝结束以免杀掉你自己。")
        _log("    → 请手动处理，或改用其他端口。")
        sys.exit(3)

    ours = [p for p in pids if _looks_like_our_service(cmds.get(p, ""))]

    if ours and len(ours) == len(pids):
        _log(f"[main] 端口 {port} 被本项目的旧实例占用"
             f"（pid {', '.join(map(str, ours))}），按「重启」处理：先结束旧实例…")
    elif force:
        _log(f"[main] ⚠️  --force：端口 {port} 被非本项目进程占用"
             f"（pid {', '.join(map(str, pids))}），仍按你的要求结束它…")
    else:
        _log(f"❌ 端口 {port} 已被占用，无法启动（占用者不是本项目进程）。")
        for pid in pids:
            _log(_describe_pid(pid))
        _log(f"    → 确认它确实该停，再强制重启： python main.py --force"
             f"（会结束 pid {', '.join(map(str, pids))}）")
        _log(f"    → 或改用其他端口： python main.py --port {port + 1}")
        _log("    提示：同一端口只应由一个管理器负责（systemd 或宝塔面板，二选一）。")
        sys.exit(3)

    # 结束监听进程（含子进程）。先杀监听者，再清理同项目的残留包装进程。
    for pid in pids:
        _log(f"    → 结束 pid {pid}")
        _terminate_tree(pid)

    if not _wait_port_released(host, port, _RESTART_WAIT):
        # 最后手段：再杀一轮同项目的相关进程（有时真正握着 socket 的是兄弟进程）
        extra = [p for p in _our_related_pids() if p not in pids]
        if extra:
            _log(f"    端口仍未释放，继续结束同项目的相关进程：{extra}")
            for pid in extra:
                _terminate_tree(pid)
        if not _wait_port_released(host, port, 8.0):
            _log(f"❌ 旧实例在 {_RESTART_WAIT:.0f}s 内仍未释放端口 {port}，本次不启动。")
            still = _listener_pids(port)
            for pid in still:
                _log(_describe_pid(pid))
            _log("    → 请手动检查后重试。")
            sys.exit(3)

    _log("[main] 旧实例已结束，端口已释放，继续启动。")


def main() -> None:
    # 依赖自检：当前解释器缺包则自动切换到带依赖的虚拟环境（修复系统 Python 缺 pymysql 导致启动即崩退出）
    py = _interpreter_with_deps()
    if py is None:
        _log("❌ 当前 Python 缺少依赖：" + ", ".join(_REQUIRED_DEPS))
        _log("   请先安装：pip install -r requirements.txt，或激活已装好依赖的虚拟环境后再运行。")
        sys.exit(2)
    if py != sys.executable:
        _log(f"[main] 当前解释器缺依赖，自动改用虚拟环境：{py}")
        os.execv(py, [py, os.path.abspath(__file__)] + sys.argv[1:])

    ap = argparse.ArgumentParser(description="workbuddy2api 一键启动（单端口：管理后台 + 内嵌网关）")
    # 默认值设为 None：用来区分「命令行显式指定」与「没写、应回落到 .env」。
    # 若默认写成 0.0.0.0/8790，就分不清用户是否真的要这个值，也就无法让
    # 命令行压过 .env（见 `_resolve_host_port`）。
    ap.add_argument("--host", default=None,
                    help="监听地址（默认取 ADMIN_HOST，再取管理台「服务监听地址」设置，再默认 0.0.0.0；"
                         "显式指定后，管理台里的「服务监听地址」开关将不再生效）")
    ap.add_argument("--port", type=int, default=None,
                    help="服务端口（默认取 ADMIN_PORT，再默认 8790）")
    ap.add_argument("--restart", action="store_true",
                    help="端口被占用时先结束占用者再启动（本项目旧实例默认就会自动接管；"
                         "此开关用于占用者是其它进程、但确认要强制重启的情况）")
    ap.add_argument("--force", action="store_true",
                    help="--restart 的别名（强制结束占用端口的进程，慎用）")
    args = ap.parse_args()

    # 端口自检：已有实例在跑就明确退出，绝不反复撞端口（线上重启风暴的根因）。
    # 本项目的旧实例会被自动接管（等价于重启）；别人的进程默认不碰，除非 --restart/--force。
    _check_host, _check_port = _resolve_host_port(args)
    _ensure_port_free(_check_host, _check_port,
                      force=bool(args.restart or args.force))

    # 部署安全检查
    secret = os.getenv("ADMIN_JWT_SECRET", "")
    if not secret or secret.startswith("workbuddy-admin-jwt-secret-please-change"):
        _log("⚠️  未设置强 ADMIN_JWT_SECRET，将使用默认弱密钥 —— 部署请务必通过环境变量覆盖！")
    if not os.getenv("ADMIN_PASSWORD", "") or not os.getenv("ADMIN_USERNAME", ""):
        _log("⚠️  未配置 ADMIN_USERNAME / ADMIN_PASSWORD，后台登录将返回 503。")
        _log("    请在 .env 中设置这两项后重启（已不再提供 admin/admin123 默认口令）。")

    procs: list[tuple[str, subprocess.Popen]] = []
    # 子进程日志文件句柄：必须持有引用，否则被 GC 回收会把子进程的 stdout 关掉
    _logfiles: list = []
    stop = threading.Event()

    def _launch(tag: str, cmd: list[str], port: int, host: str, logfile: Path) -> None:
        _log(f"[main] 启动 {tag} (http://{host}:{port}) : {' '.join(cmd)}")
        # 交互式终端才用「管道 + 转发线程」以便边跑边看；
        # **后台/服务方式则让子进程直接写日志文件**。
        #
        # 为什么：管道是有容量上限的，必须有人持续排空。而「没人排空」的后果
        # 不是丢日志，是**子进程卡死在 write 上**（uvicorn 打 access log 时
        # 冻结事件循环 → 全站无响应）。服务方式下没人看控制台，
        # 用管道换取回显并不值得冒这个风险：直接重定向到文件即可，
        # 文件写入永远不会因为「没有读者」而阻塞。
        interactive = False
        try:
            interactive = bool(sys.stdout) and sys.stdout.isatty()
        except Exception:
            interactive = False
        if interactive:
            p = subprocess.Popen(
                cmd, cwd=str(ROOT),
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                start_new_session=True,
                text=True, bufsize=1, env=os.environ.copy(),
            )
            threading.Thread(target=_pump, args=(p.stdout, logfile, tag),
                             daemon=True).start()
        else:
            fh = open(logfile, "a", encoding="utf-8", errors="replace")
            _logfiles.append(fh)        # 持有引用，避免被 GC 关掉
            p = subprocess.Popen(
                cmd, cwd=str(ROOT),
                stdout=fh, stderr=subprocess.STDOUT,
                start_new_session=True,
                text=True, bufsize=1, env=os.environ.copy(),
            )
        procs.append((tag, p))

    _launch("admin", *_build_admin_cmd(args), LOGS / "admin.log")

    # 自己写 pid 文件：外部启动器（宝塔 cmd.sh / runuser 包装 / systemd）拿到的
    # `$!` 未必是本进程的真实 pid（例如经 runuser 包装时记录的是包装进程）。
    # 「停止」按钮按这份 pid 去杀，就会杀错对象、留下仍占着 8790 的 uvicorn 孤儿，
    # 之后每次启动都撞端口。由进程自己写，才与「真正在跑的服务」一一对应。
    _write_pidfile()

    def _shutdown(signum, _frame) -> None:
        _log(f"\n[main] 收到信号 {signum}，正在关闭…")
        stop.set()
        _kill_children(procs)
        _remove_pidfile()

    signal.signal(signal.SIGINT, _shutdown)
    try:
        signal.signal(signal.SIGTERM, _shutdown)
    except Exception:
        pass

    admin_host, admin_port = _resolve_host_port(args)
    # 打印监听地址来源，排障时一眼可见「到底是谁决定的」（命令行/环境变量/后台设置/默认）
    try:
        from admin import netcfg as _nc
        _host_src = _nc.current()["source"]
    except Exception:
        _host_src = "未知"
    _log(f"[main] 单端口服务已启动（监听 {admin_host}:{admin_port}，来源：{_host_src}）：")
    _log(f"       管理后台   : http://{admin_host}:{admin_port}/admin")
    _log(f"       托管网关   : http://{admin_host}:{admin_port}/v1/chat/completions  (带 Key 配额)")
    _log(f"       内嵌网关   : http://{admin_host}:{admin_port}/gw/v1/...            (桌面登录态 / responses / messages)")
    _log("[main] 按 Ctrl+C 停止。")

    # 主循环：子进程异常退出则整体退出，避免孤儿进程
    while not stop.is_set():
        for tag, p in list(procs):
            rc = p.poll()
            if rc is not None and not stop.is_set():
                _log(f"[main] ❌ {tag} 已退出 (code={rc})，关闭服务…")
                stop.set()
                _kill_children(procs)
                _remove_pidfile()
                sys.exit(rc if rc != 0 else 1)
        time.sleep(0.5)

    _kill_children(procs)
    _remove_pidfile()
    _log("[main] 已停止。")


if __name__ == "__main__":
    main()
