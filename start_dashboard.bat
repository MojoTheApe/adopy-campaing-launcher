@echo off
title Adopy Campaigns Launcher
cd /d "%~dp0"

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

echo Stopping any previous dashboard on port 9090...
for /f "tokens=5" %%a in ('netstat -aon ^| findstr /r /c:":9090 .*LISTENING"') do (
    taskkill /F /PID %%a >nul 2>&1
)

echo Starting server on port 9090... browser will open in a few seconds.
echo Keep this window open. Close with Ctrl+C when done.
echo.

python serve.py --port 9090
if errorlevel 1 (
    echo.
    echo Server failed to start. See error above.
    pause
)
