@echo off
setlocal
if not exist "%~dp0run_dashboard.py" goto static
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start_live_workspace.ps1"
if errorlevel 1 (
    echo.
    pause
    exit /b 1
)
exit /b 0
:static
set "dashboard=%~dp0index.html"
if not exist "%dashboard%" set "dashboard=%~dp0output\shared\index.html"
if not exist "%dashboard%" (
    echo The static dashboard is not published yet.
    pause
    exit /b 1
)
start "Thermal Analysis Dashboard" "%dashboard%"
