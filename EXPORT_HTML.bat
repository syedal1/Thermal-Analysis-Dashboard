@echo off
cd /d "%~dp0"
ThermalDashboard.exe --export-html %*
if errorlevel 1 (
    pause
) else (
    start "" "output\dashboard.html"
)