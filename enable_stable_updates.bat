@echo off
setlocal
title ACL - Enable stable updates
cd /d "%~dp0"

set "PY=.venv\Scripts\python.exe"
if not exist "%PY%" (
    echo ERROR: ACL virtual environment is missing. Run install_acl.bat first.
    pause
    exit /b 1
)

git fetch --quiet origin master --tags
if errorlevel 1 (
    echo ERROR: could not fetch the promoted channel origin/master.
    pause
    exit /b 1
)

"%PY%" scripts\acl_update_gate.py --candidate origin/master --enroll
if errorlevel 1 (
    echo.
    echo Stable updates were NOT enabled. This checkout is not the promoted release.
    pause
    exit /b 1
)

if not exist "%LOCALAPPDATA%\ACL" mkdir "%LOCALAPPDATA%\ACL"
type nul > "%LOCALAPPDATA%\ACL\auto_update"
echo.
echo Stable updates enabled for origin/master.
echo Disable by deleting: %LOCALAPPDATA%\ACL\auto_update
pause
exit /b 0
