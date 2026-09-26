@echo off
REM ============================================================
REM  Agent Jo - desktop launcher
REM  Double-click this (or the desktop shortcut that points here)
REM  to start the web interface. Closing this window stops Agent Jo.
REM ============================================================
title Agent Jo
cd /d "%~dp0"

REM --- Engine + local model ----------------------------------------
REM   AGENT_BACKEND:  hybrid (local+Claude, saves tokens) | anthropic | ollama
REM   AGENT_OLLAMA_MODEL: the Ollama model to use for local turns
REM Change the values below to switch. Anything you set system-wide
REM (via setx) takes precedence and these lines are skipped.
if "%AGENT_BACKEND%"=="" set "AGENT_BACKEND=hybrid"
if "%AGENT_OLLAMA_MODEL%"=="" set "AGENT_OLLAMA_MODEL=qwen2.5-coder:32b"
REM -----------------------------------------------------------------

REM Prefer the project's virtual environment if it exists.
if exist ".venv\Scripts\python.exe" (
    set "PYTHON=.venv\Scripts\python.exe"
) else (
    set "PYTHON=python"
)

if "%ANTHROPIC_API_KEY%"=="" (
    echo.
    echo   ANTHROPIC_API_KEY is not set.
    echo   Set it once with:   setx ANTHROPIC_API_KEY sk-ant-...
    echo   then reopen. Starting anyway in case it is set elsewhere...
    echo.
)

echo Starting Agent Jo  ^(engine: %AGENT_BACKEND%, local model: %AGENT_OLLAMA_MODEL%^)
echo A browser tab will open shortly. Keep this window open while you use Agent Jo.
echo Close it to stop.
echo.
"%PYTHON%" app.py

if errorlevel 1 (
    echo.
    echo Agent Jo stopped with an error. Read the message above.
    pause
)
