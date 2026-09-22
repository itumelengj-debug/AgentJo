@echo off
REM Agent Jo Jobs — the job search, on its own.
REM
REM Same rules as the main launcher: name the folder once, relative paths
REM after that, goto instead of parenthesised blocks. A folder called
REM "AgentJo (1)" breaks anything else.
setlocal
title Agent Jo Jobs
cd /d "%~dp0"

set "PY="
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
if not defined PY if exist "venv\Scripts\python.exe" set "PY=venv\Scripts\python.exe"
if not defined PY goto no_venv
goto have_venv

:no_venv
echo.
echo   No environment found next to this file.
echo   Run install.bat first.
echo.
pause
exit /b 1

:have_venv
"%PY%" run_jobs.py %*
endlocal
