@echo off
title fb_agents Morning Dashboard
cd /d "%~dp0"

echo.
echo  Morning Report Dashboard
echo  ========================
echo.

python --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python not found. Install Python 3 and try again.
    pause
    exit /b 1
)

echo Starting server... browser will open in a few seconds.
echo Keep this window open. Close with Ctrl+C when done.
echo.

python serve.py
if errorlevel 1 (
    echo.
    echo Server failed to start. See error above.
    pause
)
