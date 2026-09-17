@echo off
REM ====================================================================
REM  Start Agent Jo (from source, using your .venv)
REM
REM  Double-click this file to launch. Unlike dist\AgentJo.exe, this runs
REM  the actual source with your installed packages - so voice input works
REM  and you always get the latest code without rebuilding anything.
REM
REM  Make a desktop shortcut: right-click this file -> Send to -> Desktop.
REM ====================================================================
cd /d "%~dp0"
title Agent Jo

REM --- find a usable Python: prefer the project's .venv --------------------
set "PY="
if exist ".venv\Scripts\python.exe"  set "PY=.venv\Scripts\python.exe"
if not defined PY if exist "venv\Scripts\python.exe"  set "PY=venv\Scripts\python.exe"
if not defined PY if exist "env\Scripts\python.exe"   set "PY=env\Scripts\python.exe"

if not defined PY (
  echo.
  echo   Could not find a virtual environment next to this file.
  echo   Expected: .venv\Scripts\python.exe  in
  echo     %~dp0
  echo.
  echo   Set one up once, from this folder, with:
  echo     python -m venv .venv
  echo     .venv\Scripts\python.exe -m pip install -r requirements-web.txt
  echo     .venv\Scripts\python.exe -m pip install faster-whisper
  echo.
  pause
  exit /b 1
)

echo.
echo   Starting Agent Jo with %PY%
echo   (leave this window open; close it to stop the app)
echo.

REM --- warn if voice isn't installed, but start anyway ---------------------
"%PY%" -c "import faster_whisper" 1>nul 2>nul
if errorlevel 1 (
  echo   Note: voice input ^(faster-whisper^) is not installed in this venv.
  echo         Everything else works. To enable voice, run:
  echo             %PY% -m pip install faster-whisper
  echo.
)

REM --- launch. run_web.py opens your browser and picks a free port --------
REM  Pass any extra args through, e.g. start_agent_jo.bat --port 9000
"%PY%" run_web.py %*

echo.
echo   Agent Jo has stopped.
pause
