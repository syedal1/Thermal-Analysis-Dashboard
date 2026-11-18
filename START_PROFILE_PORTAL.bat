@echo off
if not "%~1"=="" goto custom
call "%~dp0START_THERMAL_DASHBOARD.bat"
exit /b %errorlevel%
:custom
setlocal
pushd "%~dp0"
set "py=python"
if exist "%~dp0.venv\Scripts\python.exe" set "py=%~dp0.venv\Scripts\python.exe"
"%py%" run_dashboard.py --profile-portal --port 8063 --service-url http://127.0.0.1:8063 %*
if errorlevel 1 pause
popd