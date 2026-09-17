@echo off
REM Agent Jo — one-click install.
REM
REM This does as little as possible: Windows may have blocked the downloaded
REM files, and PowerShell is needed only to put a Python on a machine that
REM has none. Everything after that happens in install_agent_jo.py, where a failure can
REM explain itself.
setlocal
title Agent Jo - install
cd /d "%~dp0"

REM Files from the internet carry a "blocked" mark that stops scripts running.
REM Clearing it is the single commonest reason an installer fails on a second
REM machine, and it produces an error message that explains nothing.
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "Get-ChildItem -Path '%~dp0' -Recurse -File -ErrorAction SilentlyContinue | Unblock-File -ErrorAction SilentlyContinue" >nul 2>&1

REM Do we already have a usable Python? Try the launcher, then PATH.
set "PY="
for %%C in ("py -3" "python" "python3") do (
  if not defined PY (
    %%~C -c "import sys; raise SystemExit(0 if sys.version_info[:2]>=(3,10) else 1)" >nul 2>&1
    if not errorlevel 1 set "PY=%%~C"
  )
)

if not defined PY (
  echo.
  echo   Agent Jo needs Python 3.10 or newer, and this PC doesn't have it.
  echo.
  powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0get-python.ps1"
  if errorlevel 1 (
    echo.
    echo   Python wasn't installed. Nothing else was changed.
    pause
    exit /b 1
  )
  REM A fresh install isn't on this window's PATH yet.
  set "PY=py -3"
  %PY% -c "import sys" >nul 2>&1
  if errorlevel 1 (
    echo.
    echo   Python is installed, but this window can't see it yet.
    echo   Close this window, open a new one, and run install.bat again.
    pause
    exit /b 1
  )
)

%PY% "%~dp0install_agent_jo.py" %*
set "RC=%ERRORLEVEL%"

if not "%RC%"=="0" (
  echo.
  echo   Setup didn't finish. install.log has every command and its output.
  pause
  exit /b %RC%
)

REM --check is a survey, so don't offer to start anything after it.
echo %* | find "--check" >nul && goto :done
echo.
REM /T 30 /D N: if nobody answers within 30 seconds, don't start. An
REM installer that waits forever for a keypress is one that hangs an
REM unattended install rather than finishing it.
choice /C YN /N /T 30 /D N /M "  Start Agent Jo now? [Y/N] "
if errorlevel 2 goto :done
start "" "%~dp0start_agent_jo.bat"
:done
endlocal
