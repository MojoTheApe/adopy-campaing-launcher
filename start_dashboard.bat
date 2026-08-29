@echo off
setlocal
title Adopy Campaigns Launcher
cd /d "%~dp0"

set PORT=9090
set URL=http://127.0.0.1:%PORT%/

echo.
echo  Adopy Campaigns Launcher
echo  ========================
echo.

python --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python not found. Install Python 3 and try again.
    pause
    exit /b 1
)

REM Prefer ACL's own environment when install_acl.bat has made one. An older
REM install that never had a .venv keeps running on the system Python exactly
REM as before - this only ever adds a choice, never removes one.
set "PY=python"
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"

REM Already serving? Then just open it. The old script always killed whatever
REM held the port and started its own copy — which collided with a dashboard
REM started from a terminal, the bind failed, and it printed "Server failed to
REM start" even though nothing was wrong.
curl -s -o nul -m 3 %URL% 2>nul
if not errorlevel 1 (
    echo Dashboard is already running - opening it.
    start "" "%URL%"
    timeout /t 2 >nul
    exit /b 0
)

REM ---------------------------------------------------------------- Updates
REM Deliberately placed AFTER the already-running check: we only update when we
REM are about to start the server anyway. Pulling new code out from under a
REM live serve.py can break it, because Python imports parts of the app lazily
REM and would then load a file that changed mid-flight.
REM
REM Only on machines the installer marked. A developer checkout must never be
REM git-pulled from under someone mid-edit, so the marker is written solely on
REM a fresh install. Failure here is never fatal: a buyer who cannot reach
REM GitHub still needs to launch campaigns.
if exist "%LOCALAPPDATA%\ACL\auto_update" (
    git rev-parse --git-dir >nul 2>&1
    if not errorlevel 1 call :update
)

echo Stopping any stale process on port %PORT%...
for /f "tokens=5" %%a in ('netstat -aon ^| findstr /r /c:":%PORT% .*LISTENING"') do (
    taskkill /F /PID %%a >nul 2>&1
)

REM Wait for the port to actually free up. Killing a process does not release
REM its socket instantly, and starting uvicorn too early fails to bind.
set /a TRIES=0
:waitport
netstat -aon | findstr /r /c:":%PORT% .*LISTENING" >nul 2>&1
if errorlevel 1 goto portfree
set /a TRIES+=1
if %TRIES% GEQ 10 (
    echo.
    echo ERROR: port %PORT% is still busy after 10s.
    echo Close whatever is using it, then try again.
    pause
    exit /b 1
)
timeout /t 1 >nul
goto waitport

:portfree
echo Starting server on port %PORT%...
start "" /b "%PY%" serve.py --port %PORT%

REM Open the browser once the server actually answers. First response is
REM slow on purpose: the app pulls GSheet and builds the report from a
REM ~71k-row Excel before it serves anything, measured 30-40s. A 30s budget
REM sometimes gave up moments before it was ready.
set /a TRIES=0
:waitup
timeout /t 1 >nul
curl -s -o nul -m 3 %URL% 2>nul
if not errorlevel 1 goto isup
set /a TRIES+=1
if %TRIES% GEQ 90 (
    echo.
    echo Server did not come up within 90s. Run this to see why:
    echo     python serve.py --port %PORT%
    pause
    exit /b 1
)
goto waitup

:isup
echo Dashboard is up - opening browser.
start "" "%URL%"
echo.
echo Keep this window open. Close it to stop the dashboard.
echo.
REM Hold the window so closing it also stops the server.
:hold
timeout /t 30 >nul
goto hold

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
for /f "delims=" %%h in ('git rev-parse HEAD 2^>nul') do set "HEAD_BEFORE=%%h"

REM fetch, not pull: this downloads without touching the working tree, which is
REM what makes "check before activating" possible at all.
git fetch --quiet
if errorlevel 1 (
    echo   could not reach GitHub - continuing with the installed version.
    exit /b 0
)

for /f "delims=" %%u in ('git rev-parse --abbrev-ref --symbolic-full-name @{u} 2^>nul') do set "UPSTREAM=%%u"
if not defined UPSTREAM (
    echo   no upstream branch - skipping update.
    exit /b 0
)
for /f "delims=" %%h in ('git rev-parse %UPSTREAM% 2^>nul') do set "HEAD_AFTER=%%h"
if "%HEAD_BEFORE%"=="%HEAD_AFTER%" (
    echo   already up to date.
    exit /b 0
)

REM The incoming requirements.txt, read out of the fetched commit without
REM checking it out. Falls back to the installed one if the new version has no
REM such file, so an empty temp file can never pass as "nothing to install".
echo   new version available - installing its dependencies first...
set "REQ=%TEMP%\acl_requirements_incoming.txt"
git show %UPSTREAM%:requirements.txt > "%REQ%" 2>nul
if errorlevel 1 copy /y requirements.txt "%REQ%" >nul

"%PY%" -m pip install --disable-pip-version-check -q -r "%REQ%"
if errorlevel 1 (
    echo.
    echo   WARNING: the new version's dependencies could not be installed.
    echo   The update was NOT applied - you are still on the working version.
    echo   Run install_acl.bat once you have a connection to try again.
    echo.
    exit /b 0
)

git merge --ff-only %UPSTREAM% >nul 2>&1
if errorlevel 1 (
    echo   local changes block the update - continuing with the installed version.
    exit /b 0
)

REM Installed cleanly is not the same as starts. Import the dashboard and RPA
REM entry points before declaring the new version good.
"%PY%" scripts\acl_smoke.py >nul 2>&1
if errorlevel 1 (
    echo   WARNING: the new version does not start on this machine - rolling back.
    REM --keep, never --hard: it refuses rather than destroying local work if
    REM this ever runs somewhere that has any.
    git reset --keep %HEAD_BEFORE% >nul 2>&1
    if errorlevel 1 (
        echo   ROLLBACK FAILED. Run install_acl.bat, and send this line on.
        exit /b 0
    )
    echo   rolled back to the previous working version.
    exit /b 0
)

echo   updated and verified.
exit /b 0
