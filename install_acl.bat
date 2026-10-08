@echo off
setlocal
title ACM - Install
cd /d "%~dp0"

echo.
echo  ACL - Adopy Campaign Manager
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
REM Existing installs create or reuse this bootstrap .venv. The launcher has
REM no system-Python fallback, so a partial environment can never look ready.
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

REM Recovery may replace this batch file with an older release. Keep the
REM recovery call and its exits in ONE parsed command block: cmd must not
REM continue reading instructions at the same byte offset in the old file.
REM Do not relaunch N-1 automatically; older files may have no restart guard.
if exist "scripts\acl_runtime_manager.py" (
    "%PY%" scripts\acl_runtime_manager.py recover-pending
    if errorlevel 10 (
        echo The previous ACL release was restored. Repair has stopped safely.
        echo Run install_acl.bat again to finish repair using the restored files.
        pause
        REM The running batch may no longer exist at cmd's current offset.
        REM End this installer command process; EXIT /B can report success
        REM after the file is replaced even with an explicit error code.
        exit 10
    )
    if errorlevel 1 (
        echo ERROR: an unfinished ACL update could not be recovered safely.
        pause
        exit /b 1
    )
)

:dependencies
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
    echo.
    pause
    exit /b %RC%
)

REM Green check, so open it. "ACL is ready. Start it with start_dashboard.bat"
REM ended the install one step short of anything a person could look at, and
REM that step is where a new campaign manager stalls: the installer said ready,
REM the screen said nothing, and the next move was to ask someone.
echo ACL is ready - starting the dashboard...
start "" "%~dp0start_dashboard.bat"
echo.
echo A second window has opened. Keep it open - closing it stops ACL.
echo.
pause
exit /b 0
