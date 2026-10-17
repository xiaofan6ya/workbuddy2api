@echo off
title workbuddy2api - Start
cd /d "%~dp0"
setlocal
set "SYSDIR=%SystemRoot%\System32"

echo ========================================
echo   workbuddy2api Start Service
echo ========================================
echo.

REM Stop old instance / watchdog first
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\daemon.ps1" -Stop >/dev/null 2>&1

REM -Detach: Start-Process hidden window, survives closing this window
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\daemon.ps1" -Detach

echo Starting detached daemon + service, please wait...
"%SYSDIR%\timeout.exe" /t 6 /nobreak >/dev/null

"%SYSDIR%\netstat.exe" -ano | "%SYSDIR%\findstr.exe" ":8790" | "%SYSDIR%\findstr.exe" "LISTENING" >/dev/null 2>&1
if %errorlevel%==0 (
    echo Service started OK!
) else (
    echo Still starting... checking again in 5s
    "%SYSDIR%\timeout.exe" /t 5 /nobreak >/dev/null
    "%SYSDIR%\netstat.exe" -ano | "%SYSDIR%\findstr.exe" ":8790" | "%SYSDIR%\findstr.exe" "LISTENING" >/dev/null 2>&1
    if %errorlevel%==0 (
        echo Service started OK!
    ) else (
        echo Start FAILED - check logs\daemon.log and logs\service.err.log
    )
)

echo.
echo ========================================
echo   Admin UI : http://127.0.0.1:8790/admin
echo   Username : admin
echo   Password : Wb2api!Test2026
echo ========================================
echo.
echo Service runs detached. You can close this window now.
echo Watchdog auto-restarts the service if it dies.
echo To stop: double-click stop-service.bat
echo.
pause
