@echo off
REM ===========================================================================
REM workbuddy2api daemon launcher
REM ---------------------------------------------------------------------------
REM Why this exists:
REM   Running "python main.py" inside a terminal / agent session gets the
REM   process reaped when that session ends. This script uses PowerShell
REM   Start-Process to detach the service, with a watchdog that restarts it.
REM
REM Usage:
REM   start_daemon.bat          start (detached, close this window safely)
REM   start_daemon.bat stop     stop service + watchdog
REM ===========================================================================
setlocal
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\daemon.ps1" %*
endlocal
