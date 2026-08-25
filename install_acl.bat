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

REM ------------------------------------------------------------ Dependencies
REM Versions are pinned in requirements.txt on purpose - see the note there.
echo.
echo Installing dependencies...
python -m pip install --disable-pip-version-check -q -r requirements.txt
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
) else (
    echo   .env already present - left untouched
)

REM ------------------------------------------------------------------ Check
echo.
echo Checking this machine...
echo.
python scripts\acl_doctor.py
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
