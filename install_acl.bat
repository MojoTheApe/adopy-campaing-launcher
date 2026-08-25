@echo off
setlocal
title ACL - Install
cd /d "%~dp0"

echo.
echo  ACL - Adopy Campaigns Launcher
echo  Install / update
echo  =============================
echo.

REM ---------------------------------------------------------------- Python
python --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python not found on PATH.
    echo Install Python 3.11 or newer from python.org, tick "Add to PATH",
    echo then run this file again.
    pause
    exit /b 1
)
for /f "tokens=*" %%v in ('python --version') do echo Using %%v

REM ------------------------------------------------------- Isolated environment
REM ACL gets its own .venv so a buyer's other Python work cannot pull a
REM different pandas/playwright under it, and so an ACL upgrade cannot break
REM anything else on their machine. Verified 2026-08-25: a clean venv built
REM from requirements.txt imports every dashboard and RPA module.
REM
REM An existing install without .venv keeps working exactly as before - the
REM launcher only prefers .venv when it is actually there.
set "PY=python"
if exist ".venv\Scripts\python.exe" (
    echo Using existing .venv
    set "PY=.venv\Scripts\python.exe"
) else (
    echo Creating .venv ...
    python -m venv .venv
    if errorlevel 1 (
        echo WARNING: could not create .venv - installing into the system Python instead.
    ) else (
        set "PY=.venv\Scripts\python.exe"
    )
)

REM ------------------------------------------------------------ Dependencies
REM Versions are pinned in requirements.txt on purpose - see the note there.
echo.
echo Installing dependencies...
"%PY%" -m pip install --disable-pip-version-check -q -r requirements.txt
if errorlevel 1 (
    echo.
    echo ERROR: dependency install failed. The line above says why.
    pause
    exit /b 1
)
echo Dependencies OK.

REM ------------------------------------------------- Machine-local config dir
REM Deliberately OUTSIDE this folder: re-cloning or moving the checkout must
REM never take a buyer's accounts and tokens with it.
set "ACLDIR=%LOCALAPPDATA%\ACL"
if not exist "%ACLDIR%" mkdir "%ACLDIR%"
echo.
echo Machine config: %ACLDIR%

if not exist "%ACLDIR%\ad_account_map.csv" (
    copy /y "config\ad_account_map.example.csv" "%ACLDIR%\ad_account_map.csv" >nul
    echo   created ad_account_map.csv from the example - FILL IN YOUR ACCOUNTS
) else (
    echo   ad_account_map.csv already present - left untouched
)

if not exist ".env" (
    copy /y ".env.example" ".env" >nul
    echo   created .env from the example - ADD YOUR MULTILOGIN TOKEN
    REM Fresh install = a buyer machine, which should follow the shared repo.
    REM The marker is a file rather than a variable because .bat cannot read
    REM .env. An existing install never reaches this branch, so a developer's
    REM checkout is never switched to auto-update behind their back.
    echo auto-update enabled by install_acl.bat> "%ACLDIR%\auto_update"
    echo   auto-update ON - this machine will pull updates on start
) else (
    echo   .env already present - left untouched
)

REM ------------------------------------------------------------------ Check
echo.
echo Checking this machine...
echo.
REM Check with the SAME interpreter ACL will run under, not whatever `python`
REM resolves to - otherwise the check passes against packages the launcher
REM will never see.
"%PY%" scripts\acl_doctor.py
set RC=%ERRORLEVEL%

echo.
if %RC% NEQ 0 (
    echo Install finished, but something required is still missing - see [FAIL] above.
    echo Fix those, then run this file again.
) else (
    echo ACL is ready. Start it with start_dashboard.bat
)
echo.
pause
exit /b %RC%
