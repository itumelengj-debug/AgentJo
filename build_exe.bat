@echo off
REM ====================================================================
REM  Build the Agent Jo web app into a single Windows .exe
REM  Run this from the project root (the folder containing web_exe.py),
REM  e.g. double-click it, or in PowerShell:  .\build_exe.bat
REM ====================================================================
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo Could not find .venv\Scripts\python.exe
  echo Run this from your local-agent folder, with the virtual env set up.
  pause
  exit /b 1
)

echo.
echo [1/3] Installing build + web dependencies...
.\.venv\Scripts\python.exe -m pip install --upgrade pyinstaller >nul
.\.venv\Scripts\python.exe -m pip install -r requirements-web.txt >nul

echo [2/3] Building the executable (this can take a couple of minutes)...
.\.venv\Scripts\python.exe -m PyInstaller --noconfirm --clean agent-jo-web.spec

if exist "dist\AgentJo.exe" (
  echo.
  echo [3/3] Done.  Your app is here:   dist\AgentJo.exe
  echo Double-click it to launch Agent Jo. Set ANTHROPIC_API_KEY in your
  echo environment first so it can reach Claude.
) else (
  echo.
  echo Build finished but dist\AgentJo.exe was not found - check the log above.
)
echo.
pause
