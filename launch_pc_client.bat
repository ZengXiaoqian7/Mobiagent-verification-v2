@echo off
setlocal
set "MOBIAGENT_REPO=%~dp0"
cd /d "%MOBIAGENT_REPO%"
python -m pc_client
endlocal
