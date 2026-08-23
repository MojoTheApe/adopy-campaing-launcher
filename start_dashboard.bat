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
start "" /b python serve.py --port %PORT%

REM Open the browser once the server actually answers.
set /a TRIES=0
:waitup
timeout /t 1 >nul
curl -s -o nul -m 3 %URL% 2>nul
if not errorlevel 1 goto isup
set /a TRIES+=1
if %TRIES% GEQ 30 (
    echo.
    echo Server did not come up within 30s. Run this to see why:
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
