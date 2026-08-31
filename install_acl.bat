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
python -c "import sys; raise SystemExit(0 if sys.version_info >= (3,11) else 1)"
if errorlevel 1 (
    echo ERROR: ACL requires Python 3.11 or newer.
    pause
    exit /b 1
)

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
        echo ERROR: could not create ACL's isolated .venv.
        echo The system Python was NOT modified. Fix Python/permissions and retry.
        pause
        exit /b 1
    )
    set "PY=.venv\Scripts\python.exe"
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

"%PY%" scripts\acl_smoke.py
if errorlevel 1 (
    echo ERROR: dependencies installed, but this ACL version does not start.
    pause
    exit /b 1
)

REM ------------------------------------------------- Machine-local config dir
REM Deliberately OUTSIDE this folder: re-cloning or moving the checkout must
REM never take a buyer's accounts and tokens with it.
set "ACLDIR=%LOCALAPPDATA%\ACL"
echo.
echo Machine config: %ACLDIR%

REM Migrate legacy real files BEFORE creating examples, then provision only
REM what is still missing. This prevents a placeholder destination from hiding
REM a buyer's existing account map.
"%PY%" scripts\acl_migrate_config.py --provision
if errorlevel 1 (
    echo ERROR: machine configuration could not be provisioned safely.
    pause
    exit /b 1
)

REM Fleet auto-update stays OFF until this checkout is enrolled in a promoted
REM stable channel. Following an arbitrary feature branch is not a release.
echo   auto-update OFF - enable only after stable-channel enrollment

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
