@echo off
REM ============================================================
REM  Run this ONCE to put an "Agent Jo" icon on your desktop.
REM  After that, just double-click the desktop icon to start.
REM ============================================================
title Create Agent Jo shortcut
cd /d "%~dp0"

set "TARGET=%~dp0Start Atlas.bat"
set "ICON=%~dp0atlas.ico"
set "SHORTCUT=%USERPROFILE%\Desktop\Agent Jo.lnk"

set "DPATH=%~dp0"
set "SHORTCUT=%SHORTCUT%"
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ws=New-Object -ComObject WScript.Shell; $s=$ws.CreateShortcut($env:SHORTCUT); $s.TargetPath=$env:TARGET; $s.WorkingDirectory=$env:DPATH; if(Test-Path $env:ICON){$s.IconLocation=$env:ICON}; $s.Description='Start Agent Jo local AI agent'; $s.Save()"

if exist "%SHORTCUT%" (
    echo.
    echo   Done. An "Agent Jo" icon is now on your desktop.
    echo   Double-click it any time to start the app.
) else (
    echo.
    echo   Automatic creation did not work. Manual method:
    echo   right-click "Start Atlas.bat"  ^>  Send to  ^>  Desktop ^(create shortcut^)
)
echo.
pause
