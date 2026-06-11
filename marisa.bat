@echo off
REM chcp 65001 >nul

set "SCRIPT_DIR=%~dp0"

set PYTHONUTF8=1

where python3 >nul 2>nul
if %ERRORLEVEL%==0 (
    set PY=python3
) else (
    set PY=python
)

%PY% "%SCRIPT_DIR%ai_agent_prompt.py"
