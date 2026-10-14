@echo off
REM Build the Windows installer:  dist\AgentJo-Setup-<version>.exe
REM
REM Needs Inno Setup 6 (free). If it isn't installed, this installs it with
REM winget first. Double-click it, or run it from anywhere; --ci skips the
REM pause at the end (the GitHub workflow uses that).
REM
REM Same rules as install.bat: cd once, never mention %~dp0 again, and goto
REM labels rather than parenthesised blocks - a folder name with a ")" in it
REM breaks a block.
setlocal
cd /d "%~dp0.."
set "MODE=%~1"

call :find_iscc
if defined ISCC goto have_iscc
where winget >nul 2>&1
if errorlevel 1 goto no_iscc
echo.
echo   Inno Setup isn't installed - installing it with winget...
winget install --id JRSoftware.InnoSetup -e --silent --accept-package-agreements --accept-source-agreements
call :find_iscc
if defined ISCC goto have_iscc

:no_iscc
echo.
echo   Inno Setup 6 is needed to build the installer, and it isn't installed.
echo   Get it free from https://jrsoftware.org/isdl.php, then run this again.
goto fail

:find_iscc
set "ISCC="
if exist "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if exist "%ProgramFiles%\Inno Setup 6\ISCC.exe" set "ISCC=%ProgramFiles%\Inno Setup 6\ISCC.exe"
if exist "%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe" set "ISCC=%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe"
goto :eof

:have_iscc
set "VER="
for /f "usebackq delims=" %%V in (`powershell -NoProfile -ExecutionPolicy Bypass -File "installer\version.ps1"`) do set "VER=%%V"
if not defined VER set "VER=0.0.0.0"
echo.
echo   Building Agent Jo %VER% with:
echo     %ISCC%
"%ISCC%" /Q /DAppVersion=%VER% "installer\AgentJo.iss"
if errorlevel 1 goto fail
echo.
echo   Done:  dist\AgentJo-Setup-%VER%.exe
goto done

:fail
echo.
echo   The installer wasn't built.
if not "%MODE%"=="--ci" pause
endlocal
exit /b 1

:done
if not "%MODE%"=="--ci" pause
endlocal
exit /b 0
