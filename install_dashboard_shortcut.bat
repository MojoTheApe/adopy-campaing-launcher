@echo off
title Install Morning Dashboard Shortcut
cd /d "%~dp0"

echo.
echo  Adopy Campaign Manager — Install Desktop Shortcut
echo  =============================================
echo.

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\install_dashboard_shortcut.ps1"

if errorlevel 1 (
    echo.
    echo  ERROR: Shortcut could not be created. See message above.
    pause
    exit /b 1
)

pause
