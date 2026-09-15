@echo off
REM chcp 65001 >nul

set "SCRIPT_DIR=%~dp0"

set PYTHONUTF8=1

REM ============================================================
REM  Pick a usable Python interpreter.
REM
REM  Pitfall: on Windows the "App Execution Aliases" may point
REM  python3 / python to a Microsoft Store stub. It is found by
REM  `where`, but running it just opens the Store instead of
REM  running Python.
REM
REM  So we do NOT rely on the mere existence of the command.
REM  Instead we actually run `--version` and only accept a
REM  candidate that prints "Python 3.x" (the stub only prints a
REM  "Python was not found..." notice, which never matches).
REM
REM  Note: keep this file ASCII-only. Chinese comments in a .bat
REM  get garbled (and can even break REM) under a GBK code page.
REM ============================================================
set "PY="

call :try_python python3
if not defined PY call :try_python python
if not defined PY call :try_python py

if not defined PY (
    echo DA ZE! No usable Python 3 found ^(tried python3 / python / py^).
    echo Please install Python 3.8+ and make sure it runs in this console.
    exit /b 1
)

%PY% "%SCRIPT_DIR%ai_agent_prompt.py" %*
exit /b


:try_python
REM %~1 = candidate command. Accept it only if it prints "Python 3.x".
set "PYVER="
for /f "delims=" %%V in ('%~1 --version 2^>nul') do if not defined PYVER set "PYVER=%%V"
if not defined PYVER exit /b 0
echo %PYVER%|findstr /R "^Python 3" >nul
if not errorlevel 1 set "PY=%~1"
exit /b 0
