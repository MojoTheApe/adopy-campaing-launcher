@echo off
setlocal EnableExtensions
title Install Adopy Campaigns Launcher

REM ---------------------------------------------------------------------------
REM The one file a campaign manager receives. Everything a person had to do by
REM hand — install Python, install Git, reopen the terminal because PATH was
REM stale, clone, run the installer, make a shortcut — happens here.
REM
REM The PATH problem is the reason this exists at all. `winget install` puts
REM python.exe on the PATH of FUTURE processes, not this one, so a script that
REM installs Python and then calls it fails on the machine it just prepared.
REM That cost the second campaign manager two attempts and an hour. So after
REM installing anything, this file restarts ITSELF in a new window, which is the
REM only way a batch file gets a fresh environment.
REM
REM Safe to run again: an existing checkout is updated, not re-cloned, and
REM nothing here touches the machine's settings in %LOCALAPPDATA%\ACL.
REM ---------------------------------------------------------------------------

set "REPO=https://github.com/MojoTheApe/adopy-campaing-launcher.git"
set "TARGET=%USERPROFILE%\Documents\ACL"

echo.
echo  Adopy Campaigns Launcher
echo  Install
echo  ========================
echo.
echo  Installing into: %TARGET%
echo.

REM ------------------------------------------------------------------ winget
where winget >nul 2>&1
if errorlevel 1 (
    echo ERROR: winget is not available on this computer, so Python and Git
    echo cannot be installed automatically.
    echo.
    echo Update "App Installer" from the Microsoft Store, then run this file
    echo again. If that is not possible, install these two by hand and re-run:
    echo   https://www.python.org/downloads/   ^(tick "Add python.exe to PATH"^)
    echo   https://git-scm.com/download/win
    echo.
    pause
    exit /b 1
)

REM ------------------------------------------------------------------ Python
set "NEEDRESTART="
python --version >nul 2>&1
if errorlevel 1 (
    echo Python is not installed. Installing it now, this takes a few minutes...
    winget install --id Python.Python.3.13 -e --accept-source-agreements --accept-package-agreements
    set "NEEDRESTART=1"
) else (
    python -c "import sys; raise SystemExit(0 if sys.version_info >= (3,11) else 1)" >nul 2>&1
    if errorlevel 1 (
        echo The installed Python is older than 3.11. Installing a current one...
        winget install --id Python.Python.3.13 -e --accept-source-agreements --accept-package-agreements
        set "NEEDRESTART=1"
    ) else (
        for /f "tokens=*" %%v in ('python --version') do echo Found %%v
    )
)

REM --------------------------------------------------------------------- Git
git --version >nul 2>&1
if errorlevel 1 (
    echo Git is not installed. Installing it now...
    winget install --id Git.Git -e --accept-source-agreements --accept-package-agreements
    set "NEEDRESTART=1"
) else (
    for /f "tokens=*" %%v in ('git --version') do echo Found %%v
)

REM A new PATH reaches this window only through a new window.
if defined NEEDRESTART (
    echo.
    echo  Python/Git installed. Reopening to pick them up...
    echo  A new window will continue the installation.
    timeout /t 3 >nul
    start "" "%~f0"
    exit /b 0
)

REM ------------------------------------------------------------ Get the code
echo.
if exist "%TARGET%\.git" (
    echo Updating the existing installation...
    git -C "%TARGET%" fetch --quiet origin master --tags
    if errorlevel 1 (
        echo.
        echo ERROR: could not reach GitHub. Check the connection and sign-in.
        pause
        exit /b 1
    )
) else (
    if exist "%TARGET%" (
        echo ERROR: %TARGET% already exists but is not an ACL installation.
        echo Rename or remove that folder, then run this file again.
        echo Nothing was changed.
        pause
        exit /b 1
    )
    echo Downloading ACL. A sign-in window may open - use the company GitHub account.
    git clone --quiet "%REPO%" "%TARGET%"
    if errorlevel 1 (
        echo.
        echo ERROR: download failed. Most often this is GitHub access:
        echo ask for access to the repository, then run this file again.
        pause
        exit /b 1
    )
)

REM Install the release, not whatever master happens to be. A release tag is a
REM commit that passed the build and was promoted on purpose.
set "TAG="
for /f "delims=" %%t in ('git -C "%TARGET%" tag --points-at origin/master --list acl-release-* 2^>nul') do set "TAG=%%t"
if defined TAG (
    echo Installing release %TAG%
    git -C "%TARGET%" checkout --quiet "%TAG%"
) else (
    echo   no release tag on the channel head - installing the channel itself
    git -C "%TARGET%" checkout --quiet master
    git -C "%TARGET%" merge --ff-only --quiet origin/master
)
if errorlevel 1 (
    echo.
    echo ERROR: could not switch to the release. If ACL is running, close its
    echo black window and run this file again.
    pause
    exit /b 1
)

REM ---------------------------------------------------------------- Shortcut
REM Before the installer, not after: the installer ends by starting the
REM dashboard and holding the window, so anything queued behind it would wait
REM for a keypress the person has no reason to expect.
powershell -NoProfile -ExecutionPolicy Bypass -File "%TARGET%\scripts\install_dashboard_shortcut.ps1" >nul 2>&1
if errorlevel 1 (
    echo   note: the desktop shortcut could not be created; start ACL with
    echo   %TARGET%\start_dashboard.bat
) else (
    echo Desktop shortcut created.
)

REM ------------------------------------- Install dependencies, check, and run
REM install_acl.bat owns the rest: dependencies, machine configuration, the
REM doctor, and — when the doctor is green — starting the dashboard.
echo.
call "%TARGET%\install_acl.bat"
exit /b %ERRORLEVEL%
