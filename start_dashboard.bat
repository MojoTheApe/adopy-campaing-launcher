@echo off
setlocal
title Adopy Campaign Manager
cd /d "%~dp0"

set PORT=9090
set URL=http://127.0.0.1:%PORT%/
set HEALTH_URL=http://127.0.0.1:%PORT%/health

echo.
echo  Adopy Campaign Manager
echo  ========================
echo.

if not exist ".venv\Scripts\python.exe" (
    echo ERROR: ACL's isolated environment is missing.
    echo Run install_acl.bat, then start the dashboard again.
    pause
    exit /b 1
)

set "BOOTSTRAP_PY=.venv\Scripts\python.exe"
set "PY=%BOOTSTRAP_PY%"
"%BOOTSTRAP_PY%" -c "import sys; raise SystemExit(0 if sys.version_info >= (3,11) else 1)"
if errorlevel 1 (
    echo ERROR: ACL requires Python 3.11 or newer. Run install_acl.bat.
    pause
    exit /b 1
)

REM Already serving? Then just open it. The old script always killed whatever
REM held the port and started its own copy — which collided with a dashboard
REM started from a terminal, the bind failed, and it printed "Server failed to
REM start" even though nothing was wrong.
set "HEALTH_FILE=%TEMP%\acl_health_%RANDOM%.json"
curl -fsS -o "%HEALTH_FILE%" -m 3 "%HEALTH_URL%" 2>nul
if not errorlevel 1 (
    findstr /c:"ACL" "%HEALTH_FILE%" >nul 2>&1
    if not errorlevel 1 (
        del "%HEALTH_FILE%" >nul 2>&1
        REM A healthy ACL process is sufficient evidence to confirm a pending
        REM release even if the previous launcher window closed one line early.
        "%BOOTSTRAP_PY%" scripts\acl_runtime_manager.py confirm-running >nul 2>&1
        if errorlevel 1 (
            echo ERROR: the running dashboard's release could not be confirmed.
            echo Close it and send this window to support before using ACL.
            pause
            exit /b 1
        )
        echo Dashboard is already running - opening it.
        start "" "%URL%"
        timeout /t 2 >nul
        exit /b 0
    )
)
del "%HEALTH_FILE%" >nul 2>&1

REM --------------------------------------------------------- Release recovery
REM An update is pending until the new dashboard answers ACL's own /health.
REM If the previous launch died or was closed before that point, restore the
REM old commit before importing any code. Exit 10 means a reset happened; start
REM a fresh copy so cmd.exe reads the restored launcher rather than continuing
REM through a batch file that changed underneath it.
"%BOOTSTRAP_PY%" scripts\acl_runtime_manager.py recover-pending
set "RECOVERY_RC=%ERRORLEVEL%"
if "%RECOVERY_RC%"=="10" (
    echo Restarting the restored ACL release...
    start "" "%~f0"
    exit /b 0
)
if not "%RECOVERY_RC%"=="0" (
    echo ERROR: an unfinished ACL update could not be recovered safely.
    echo Do not edit or delete the update marker. Send this window to support.
    pause
    exit /b 1
)

REM Use an immutable runtime only when this exact commit passed dependency and
REM smoke validation. Initial/manual installs continue to use bootstrap .venv.
set "CURRENT_SHA="
for /f "delims=" %%h in ('git rev-parse HEAD 2^>nul') do set "CURRENT_SHA=%%h"
if defined CURRENT_SHA (
    "%BOOTSTRAP_PY%" scripts\acl_runtime_manager.py check-active --sha "%CURRENT_SHA%" >nul 2>&1
    if not errorlevel 1 set "PY=%LOCALAPPDATA%\ACL\runtimes\%CURRENT_SHA%\Scripts\python.exe"
)

REM ----------------------------------------------------------- Machine config
REM Move this machine's own config out of the checkout, BEFORE the update step.
REM Until now the only thing that did this was the doctor, which many machines
REM never run, so a pre-2026-08-25 install kept its account map inside the
REM folder a re-clone replaces and git clean empties. Doing it first also keeps
REM the ordering right for anything we drop from git later: a merge does delete
REM a file the incoming commit removed. No-op once the machine is migrated.
"%PY%" scripts\acl_migrate_config.py
if errorlevel 1 (
    echo ERROR: machine configuration migration failed. Update/start cancelled.
    pause
    exit /b 1
)

REM ---------------------------------------------------------------- Updates
REM Deliberately placed AFTER the already-running check: we only update when we
REM are about to start the server anyway. Pulling new code out from under a
REM live serve.py can break it, because Python imports parts of the app lazily
REM and would then load a file that changed mid-flight.
REM
REM Only on machines the installer marked. A developer checkout must never be
REM git-pulled from under someone mid-edit, so the marker is written solely on
REM a fresh install. Network/release-gate failures keep the installed version.
REM A candidate that was already activated and then failed is rolled back and
REM this invocation stops so the restored launcher can be read from disk.
REM Two ways in. `auto_update` is fleet enrollment and stays off. `update_once`
REM is written by the launcher itself when the dashboard's Update button asked
REM for a restart - a deliberate click, not a background surprise. It is deleted
REM BEFORE the update runs: a candidate that fails must not be retried on every
REM start for the rest of the machine's life.
set "UPDATE_NOW="
if exist "%LOCALAPPDATA%\ACL\auto_update" set "UPDATE_NOW=1"
if exist "%LOCALAPPDATA%\ACL\update_once" (
    del "%LOCALAPPDATA%\ACL\update_once" >nul 2>&1
    set "UPDATE_NOW=1"
)
if defined UPDATE_NOW (
    git rev-parse --git-dir >nul 2>&1
    if not errorlevel 1 call :update
    if errorlevel 20 (
        echo.
        echo The failed candidate was rolled back and quarantined.
        echo Start ACL again to use the restored release.
        pause
        exit /b 1
    )
)

netstat -aon | findstr /r /c:":%PORT% .*LISTENING" >nul 2>&1
if not errorlevel 1 (
    echo.
    echo ERROR: port %PORT% is used by another application.
    echo ACL did not terminate that process. Close it or run serve.py on another port.
    pause
    exit /b 1
)

:portfree
echo Starting server on port %PORT%...
REM Tells the dashboard a launcher is watching it, which is what makes the
REM header's Update button safe to offer: without this the button would stop a
REM server nothing would ever start again.
set "ACL_SUPERVISED=1"
start "" /b "%PY%" serve.py --port %PORT%

REM Open the browser once ACL's lightweight health route answers. The report
REM itself may continue loading workbooks after the browser opens.
set /a TRIES=0
:waitup
timeout /t 1 >nul
curl -fsS -o nul -m 3 "%HEALTH_URL%" 2>nul
if not errorlevel 1 goto isup
set /a TRIES+=1
if %TRIES% GEQ 90 (
    echo.
    echo Server did not come up within 90s. Run this to see why:
    echo     python serve.py --port %PORT%
    echo The pending release will be rolled back automatically on the next start.
    pause
    exit /b 1
)
goto waitup

:isup
"%BOOTSTRAP_PY%" scripts\acl_runtime_manager.py confirm-running >nul 2>&1
if errorlevel 1 (
    echo ERROR: ACL is healthy, but its release marker could not be confirmed.
    echo Close this window and send the error to support before using the dashboard.
    pause
    exit /b 1
)
echo Dashboard is up - opening browser.
start "" "%URL%"
echo.
echo Keep this window open. Close it to stop the dashboard.
echo.
REM Hold the window so closing it also stops the server - and watch for the one
REM thing the dashboard cannot do for itself. Clicking Update in the header
REM writes this marker and exits the server; the update then runs here, on the
REM same gated path as a normal start, with the same rollback.
set "RESTART_MARKER=%LOCALAPPDATA%\ACL\restart_request.json"
:hold
timeout /t 3 >nul
if not exist "%RESTART_MARKER%" goto hold

REM Swap the markers before doing anything else. Leaving the request file in
REM place would restart this machine forever if the next step failed.
del "%RESTART_MARKER%" >nul 2>&1
echo requested by the dashboard>"%LOCALAPPDATA%\ACL\update_once"

echo.
echo Update requested from the dashboard - restarting ACL...
REM Wait for the old server to release the port. Relaunching first would find a
REM healthy dashboard, decide there is nothing to do, and open the browser on
REM the version we were asked to replace.
set /a WAIT=0
:waitdown
curl -fsS -o nul -m 2 "%HEALTH_URL%" 2>nul
if errorlevel 1 goto isdown
set /a WAIT+=1
if %WAIT% GEQ 30 (
    echo.
    echo ERROR: the dashboard did not stop, so it was not updated.
    echo Close this window and start ACL again.
    pause
    exit /b 1
)
timeout /t 1 >nul
goto waitdown

:isdown
start "" "%~f0"
exit /b 0

REM ---------------------------------------------------------------------------
REM Reached only via `call` above.
REM
REM The order here is the whole point. New code is downloaded but NOT activated
REM until its dependencies are installed and the result is shown to start. The
REM previous version pulled first and only warned when the install failed, which
REM left the machine running new code against old libraries. That does not fail
REM at update time - it fails next morning, as a ModuleNotFoundError with
REM nothing in it to suggest last night's update, on a machine that is not in
REM the room with us.
REM
REM A buyer who cannot be updated is not blocked: they keep working on the
REM version that was running yesterday, which is the one we know works.
:update
echo Checking for updates...
set "HEAD_BEFORE="
set "HEAD_AFTER="
for /f "delims=" %%h in ('git rev-parse HEAD 2^>nul') do set "HEAD_BEFORE=%%h"

REM fetch, not pull: this downloads without touching the working tree, which is
REM what makes "check before activating" possible at all.
git fetch --quiet origin main --tags
if errorlevel 1 (
    echo   could not reach GitHub - continuing with the installed version.
    exit /b 0
)

set "STABLE_REF=origin/main"
for /f "delims=" %%h in ('git rev-parse %STABLE_REF% 2^>nul') do set "HEAD_AFTER=%%h"
if not defined HEAD_AFTER (
    echo   promoted channel origin/main is unavailable - skipping update.
    exit /b 0
)
if "%HEAD_BEFORE%"=="%HEAD_AFTER%" (
    echo   already up to date.
    exit /b 0
)

"%PY%" scripts\acl_update_gate.py --candidate origin/main
if errorlevel 1 (
    echo   candidate is not a promoted release - continuing with the installed version.
    exit /b 0
)

REM The incoming requirements.txt is read from the fetched commit without
REM checking it out. Dependencies go into an immutable runtime named by the
REM candidate SHA; the working .venv and the N-1 runtime are never modified.
echo   new version available - preparing its isolated runtime...
set "REQ=%TEMP%\acl_requirements_incoming.txt"
git show %STABLE_REF%:requirements.txt > "%REQ%" 2>nul
if errorlevel 1 copy /y requirements.txt "%REQ%" >nul

"%BOOTSTRAP_PY%" scripts\acl_runtime_manager.py prepare --sha "%HEAD_AFTER%" --requirements "%REQ%"
if errorlevel 1 (
    echo.
    echo   WARNING: the new version's isolated runtime could not be prepared.
    echo   The update was NOT applied - you are still on the working version.
    echo   A quarantined release remains skipped until a newer release arrives.
    echo.
    exit /b 0
)

set "RUNTIME_NEXT=%LOCALAPPDATA%\ACL\runtimes\%HEAD_AFTER%\Scripts\python.exe"
"%BOOTSTRAP_PY%" scripts\acl_runtime_manager.py mark-pending --previous "%HEAD_BEFORE%" --candidate "%HEAD_AFTER%"
if errorlevel 1 (
    echo   recoverable update marker could not be written - update skipped.
    exit /b 0
)

git merge --ff-only %STABLE_REF% >nul 2>&1
if errorlevel 1 (
    echo   local changes block the update - continuing with the installed version.
    "%BOOTSTRAP_PY%" scripts\acl_runtime_manager.py recover-pending >nul 2>&1
    exit /b 0
)

REM Installed cleanly is not the same as starts. Import the dashboard and RPA
REM entry points before declaring the new version good.
"%RUNTIME_NEXT%" scripts\acl_smoke.py >nul 2>&1
if errorlevel 1 (
    echo   WARNING: the new version does not start on this machine - rolling back.
    "%BOOTSTRAP_PY%" scripts\acl_runtime_manager.py recover-pending >nul 2>&1
    if not errorlevel 10 (
        echo   ROLLBACK FAILED. Run install_acl.bat, and send this line on.
        exit /b 20
    )
    echo   rolled back to the previous working version; candidate quarantined.
    exit /b 20
)

"%RUNTIME_NEXT%" scripts\acl_runtime_manager.py activate --sha "%HEAD_AFTER%" >nul 2>&1
if errorlevel 1 (
    echo   WARNING: the new runtime could not be activated - rolling back.
    "%BOOTSTRAP_PY%" scripts\acl_runtime_manager.py recover-pending >nul 2>&1
    if not errorlevel 10 (
        echo   ROLLBACK FAILED. Run install_acl.bat, and send this line on.
        exit /b 20
    )
    exit /b 20
)

set "PY=%RUNTIME_NEXT%"
echo   updated and smoke-verified; awaiting dashboard health confirmation.
exit /b 0
