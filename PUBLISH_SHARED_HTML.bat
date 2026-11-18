@echo off
pushd "%~dp0"
python run_dashboard.py --publish-profiles --publish-folder "output\shared"
if errorlevel 1 goto failed
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0publish_dashboard.ps1"
if errorlevel 1 goto failed
popd
exit /b 0
:failed
pause
popd
exit /b 1