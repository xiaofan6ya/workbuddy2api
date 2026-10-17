@echo off
title workbuddy2api - Stop
cd /d "%~dp0"
setlocal
set "SYSDIR=%SystemRoot%\System32"

echo ========================================
echo   workbuddy2api Stop Service
echo ========================================
echo.

REM Stop watchdog + service (+ leftover main.py)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\daemon.ps1" -Stop

"%SYSDIR%\netstat.exe" -ano | "%SYSDIR%\findstr.exe" ":8790" | "%SYSDIR%\findstr.exe" "LISTENING" >/dev/null 2>&1
if %errorlevel%==0 (
    echo Failed to stop! Trying taskkill fallback...
    for /f "tokens=5" %%a in ('"%SYSDIR%\netstat.exe" -ano ^| "%SYSDIR%\findstr.exe" ":8790" ^| "%SYSDIR%\findstr.exe" "LISTENING"') do (
        "%SYSDIR%\taskkill.exe" /PID %%a /F >/dev/null 2>&1
        echo Killed PID=%%a
    )
) else (
    echo Service stopped.
)

echo.
pause
