# =============================================================================
# workbuddy2api 守护脚本
# -----------------------------------------------------------------------------
# 解决的问题：把 `python main.py` 挂在某个终端 / Agent 后台会话里跑，
# 会话结束时子进程会被连带回收 —— 日志最后一行还是 "200 OK"，但端口没了。
#
# 本脚本用 Start-Process -WindowStyle Hidden 让服务脱离当前会话，
# 并循环探活：进程不在就拉起来。
#
# 用法：
#   powershell -File scripts\daemon.ps1            启动守护（本进程探活）
#   powershell -File scripts\daemon.ps1 -Once      只启动一次，不做守护
#   powershell -File scripts\daemon.ps1 -Stop      停止服务 + 守护
#   powershell -File scripts\daemon.ps1 -Status    查看状态
# =============================================================================
param(
    [switch]$Once,
    [switch]$Stop,
    [switch]$Status,
    [switch]$Detach,
    [int]$Port = 8790,
    [int]$IntervalSec = 10
)

$ErrorActionPreference = "Continue"
$Root = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $Root ".venv\Scripts\python.exe"
$PidFile = Join-Path $Root "logs\daemon.pid"
$WatchLog = Join-Path $Root "logs\daemon.log"
$OutLog = Join-Path $Root "logs\service.out.log"
$ErrLog = Join-Path $Root "logs\service.err.log"

function Write-WatchLog([string]$msg) {
    $line = "{0} {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $msg
    Write-Host $line
    try {
        $dir = Split-Path -Parent $WatchLog
        if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
        # -ErrorAction Stop 是必须的：Add-Content 在文件被并发写入时抛的是
        # **非终止**错误，`catch` 抓不住，照样往屏幕刷 "Add-Content : 参数无效"。
        # 加上它才会变成终止错误、被下面的 catch 吞掉。
        Add-Content -Path $WatchLog -Value $line -Encoding UTF8 -ErrorAction Stop
    } catch { }
}

function Get-ListenerPid {
    $c = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
         Select-Object -First 1
    if ($c) { return [int]$c.OwningProcess }
    return 0
}

function Start-ServiceProcess {
    $env:PYTHONIOENCODING = "utf-8"
    $env:PYTHONUTF8 = "1"
    try {
        $dir = Split-Path -Parent $OutLog
        if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
    } catch { }
    # -WindowStyle Hidden + Start-Process：完全脱离当前会话/控制台。
    # 父进程、启动窗口、Agent 会话退出都不会连带回收本服务。
    $p = Start-Process -FilePath $Python -ArgumentList "main.py" `
                       -WorkingDirectory $Root -WindowStyle Hidden `
                       -RedirectStandardOutput $OutLog -RedirectStandardError $ErrLog `
                       -PassThru
    Write-WatchLog "已启动服务 PID=$($p.Id)"
    return $p
}

function Stop-Watchdog {
    # 必须杀掉**所有**本脚本的守护进程，而不是只杀 pid 文件里那一个。
    #
    # 为什么：守护循环启动时会把 `$PID` **覆盖写**进 pid 文件；一旦因为
    # 重复启动 / 没先 -Stop 就 -Detach 而叠出多个守护，后起的会顶掉前一个的
    # 记录，前一个就成了「无主野守护」—— 于是 -Stop 杀一个，野守护 10s 内
    # 又把服务拉回来，表现就是「关闭脚本关不掉」。
    #
    # 按命令行特征扫描（不是按单个 pid），并用 taskkill /T 连子树一起收。
    Get-CimInstance Win32_Process -Filter "Name='powershell.exe' or Name='pwsh.exe'" -ErrorAction SilentlyContinue |
        Where-Object {
            $_.ProcessId -ne $PID -and
            $_.CommandLine -and
            $_.CommandLine -like "*daemon.ps1*" -and
            $_.CommandLine -notlike "*-Stop*" -and
            $_.CommandLine -notlike "*-Status*"
        } |
        ForEach-Object {
            & "$env:SystemRoot\System32\taskkill.exe" /PID $_.ProcessId /T /F *> $null
            Write-WatchLog "已停止守护 PID=$($_.ProcessId)"
        }
    Remove-Item $PidFile -Force -ErrorAction SilentlyContinue
}

function Stop-ServiceTree {
    # 停服务必须收**整棵进程树**。
    #
    # 为什么不能只扫 `*main.py*`：本服务实际是
    #   python main.py -> python main.py（execv 换解释器）
    #     -> python -m uvicorn（守卫父进程）
    #       -> python -m uvicorn（真正监听 8790 的那个）
    # 只杀 main.py 会留下两层 uvicorn 孤儿，端口还被占着，看起来像「没关掉」。
    #
    # 先杀监听者（taskkill /T 连子树），再扫残留。
    $svcPid = Get-ListenerPid
    if ($svcPid -gt 0) {
        & "$env:SystemRoot\System32\taskkill.exe" /PID $svcPid /T /F *> $null
        Write-WatchLog "已停止服务(含子树) PID=$svcPid"
    } else {
        Write-WatchLog "端口 $Port 上没有正在监听的服务"
    }

    Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
        Where-Object {
            $_.CommandLine -and (
                $_.CommandLine -like "*main.py*" -or
                $_.CommandLine -like "*uvicorn*admin.server*"
            )
        } |
        ForEach-Object {
            & "$env:SystemRoot\System32\taskkill.exe" /PID $_.ProcessId /T /F *> $null
            Write-WatchLog "已停止残留服务 PID=$($_.ProcessId)"
        }
}

function Test-OtherWatchdog {
    # 是否已有别的守护在跑（排除自己）。守护模式入口用它做防叠加。
    $others = Get-CimInstance Win32_Process -Filter "Name='powershell.exe' or Name='pwsh.exe'" -ErrorAction SilentlyContinue |
        Where-Object {
            $_.ProcessId -ne $PID -and
            $_.CommandLine -and
            $_.CommandLine -like "*daemon.ps1*" -and
            $_.CommandLine -notlike "*-Stop*" -and
            $_.CommandLine -notlike "*-Status*" -and
            $_.CommandLine -notlike "*-Once*" -and
            $_.CommandLine -notlike "*-Detach*"
        }
    return @($others)
}

# ---- -Detach：把自己以隐藏窗口重新拉起，当前进程即可退出 ----------------
# 启动 bat 用这个开关即可避开「路径含空格 + PowerShell 引号」的坑。
if ($Detach) {
    # 路径可能含空格（如 "2025College Student..."），必须显式加引号，
    # 否则 -File 会被按空格拆开，子进程立刻失败且无提示。
    $scriptQuoted = '"' + $PSCommandPath + '"'
    $argList = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $scriptQuoted)
    if ($Once) { $argList += '-Once' }
    if ($Stop) { $argList += '-Stop' }
    if ($Status) { $argList += '-Status' }
    Start-Process -FilePath 'powershell.exe' -ArgumentList $argList -WindowStyle Hidden
    exit 0
}

# ---- -Stop --------------------------------------------------------------
if ($Stop) {
    # 顺序很重要：先停守护，否则守护会在下面停服务后 10s 内把服务拉起来。
    Stop-Watchdog
    Stop-ServiceTree
    # 再扫一轮：taskkill /T 有时来不及带走刚 fork 的子进程。
    Start-Sleep -Seconds 1
    Stop-ServiceTree
    $left = Get-ListenerPid
    if ($left -gt 0) {
        Write-WatchLog "⚠️ 端口 $Port 仍被 PID=$left 占用"
    } else {
        Write-WatchLog "已确认端口 $Port 已释放"
    }
    exit 0
}

# ---- -Status ------------------------------------------------------------
if ($Status) {
    $svcPid = Get-ListenerPid
    if ($svcPid -gt 0) {
        Write-Host "服务运行中：PID=$svcPid  端口=$Port"
    } else {
        Write-Host "服务未运行（端口 $Port 空闲）"
    }
    if (Test-Path $PidFile) {
        $wpid = Get-Content $PidFile -ErrorAction SilentlyContinue
        Write-Host "守护进程 PID=$wpid"
    } else {
        Write-Host "守护进程未运行"
    }
    exit 0
}

# ---- -Once --------------------------------------------------------------
if ($Once) {
    if ((Get-ListenerPid) -gt 0) {
        Write-WatchLog "端口 $Port 已被占用，跳过启动"
        exit 0
    }
    Start-ServiceProcess | Out-Null
    Start-Sleep -Seconds 8
    $svcPid = Get-ListenerPid
    if ($svcPid -gt 0) { Write-WatchLog "启动成功 PID=$svcPid" }
    else { Write-WatchLog "启动失败，请检查 logs\admin.log / logs\service.err.log" }
    exit 0
}

# ---- 守护模式 -----------------------------------------------------------
# 防叠加：已有守护在跑就退出，绝不叠出第二个（叠加会让 -Stop 关不干净，
# 表现就是「关闭脚本关不掉」—— 野守护会不停把服务拉回来）。
$existing = Test-OtherWatchdog
if (@($existing).Count -gt 0) {
    $ids = (@($existing) | ForEach-Object { $_.ProcessId }) -join ','
    Write-WatchLog "已有守护在跑（PID=$ids），本实例退出以免叠加"
    exit 0
}

$PID | Out-File -FilePath $PidFile -Encoding ASCII -Force
Write-WatchLog "守护启动（每 ${IntervalSec}s 探活一次，端口 $Port）"

$missCount = 0
while ($true) {
    $svcPid = Get-ListenerPid
    if ($svcPid -eq 0) {
        $missCount++
        Write-WatchLog "检测到服务不在（连续 $missCount 次），正在拉起..."
        Start-ServiceProcess | Out-Null
        Start-Sleep -Seconds 8
        $newPid = Get-ListenerPid
        if ($newPid -gt 0) {
            Write-WatchLog "已恢复 PID=$newPid"
            $missCount = 0
        }
    } else {
        if ($missCount -ne 0) { $missCount = 0 }
    }
    Start-Sleep -Seconds $IntervalSec
}
