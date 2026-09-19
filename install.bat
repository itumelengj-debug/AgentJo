@echo off
REM Agent Jo — one-click install.
REM
REM Written defensively about the folder it lands in. A downloaded copy often
REM sits somewhere like "AgentJo-2026-09-18 (1)", and cmd expands %~dp0 as
REM literal text: the ")" inside "(1)" closes any block it appears in, and the
REM trailing backslash escapes the quote after it. The result is
REM "\ was unexpected at this time" and nothing runs.
REM
REM So: cd once at the top, then never mention %~dp0 again — relative paths
REM only — and use goto labels rather than parenthesised blocks.
setlocal
title Agent Jo - install
cd /d "%~dp0"

REM Windows marks downloaded files as blocked, which stops scripts running.
REM Clearing it is the commonest reason an installer works on one PC and not
REM another. "." is this folder, already the current directory.
powershell -NoProfile -ExecutionPolicy Bypass -Command "Get-ChildItem -Path . -Recurse -File -ErrorAction SilentlyContinue | Unblock-File -ErrorAction SilentlyContinue" >nul 2>&1

set "PY="
call :try_py py -3
if defined PY goto have_py
call :try_py python
if defined PY goto have_py
call :try_py python3
if defined PY goto have_py

echo.
echo   Agent Jo needs Python 3.10 or newer, and this PC doesn't have it.
echo.
powershell -NoProfile -ExecutionPolicy Bypass -File ".\get-python.ps1"
if errorlevel 1 goto no_python

set "PY=py -3"
%PY% -c "import sys" >nul 2>&1
if errorlevel 1 goto reopen
goto have_py

:try_py
%* -c "import sys; raise SystemExit(0 if sys.version_info[:2]>=(3,10) else 1)" >nul 2>&1
if not errorlevel 1 set "PY=%*"
goto :eof

:no_python
echo.
echo   Python wasn't installed. Nothing else was changed.
pause
exit /b 1

:reopen
echo.
echo   Python is installed, but this window can't see it yet.
echo   Close this window, open a new one, and run install.bat again.
pause
exit /b 1

:have_py
%PY% ".\install_agent_jo.py" %*
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" goto failed

echo %* | find "--check" >nul
if not errorlevel 1 goto done

echo.
REM /T 30 /D N: don't wait forever for a keypress — that hangs an unattended
REM install rather than finishing it.
choice /C YN /N /T 30 /D N /M "  Start Agent Jo now? [Y/N] "
if errorlevel 2 goto done
start "" ".\start_agent_jo.bat"
goto done

:failed
echo.
echo   Setup didn't finish. install.log has every command and its output.
pause
endlocal
exit /b %RC%

:done
endlocal
