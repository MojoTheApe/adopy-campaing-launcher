<#
.SYNOPSIS
    Creates an "Adopy Campaign Manager" shortcut on the desktop.

.DESCRIPTION
    The shortcut runs start_dashboard.bat directly: opens a console window,
    starts the web server at http://127.0.0.1:9090/, and opens the browser.
    Close the console window (or Ctrl+C) to stop the server.

.NOTES
    Run once after cloning / moving the repo.
    Re-run any time the project folder moves — it will overwrite the existing shortcut.
#>

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# ── Resolve project paths ────────────────────────────────────────────────────
$ScriptDir     = Split-Path -Parent $MyInvocation.MyCommand.Definition
$ProjectRoot   = (Resolve-Path (Join-Path $ScriptDir "..")).Path
$DashboardBat  = Join-Path $ProjectRoot "start_dashboard.bat"
$IcoFile       = Join-Path $ProjectRoot "assets\dashboard.ico"

if (-not (Test-Path $DashboardBat)) {
    Write-Error "Dashboard launcher not found: $DashboardBat"
    exit 1
}

if (-not (Test-Path $IcoFile)) {
    Write-Error "ACL icon not found: $IcoFile`nRestore assets/dashboard.ico from the repository (the brand-kit icon)."
    exit 1
}

# ── Create shortcut ──────────────────────────────────────────────────────────
$DesktopPath  = [Environment]::GetFolderPath("Desktop")
$ShortcutPath = Join-Path $DesktopPath "Adopy Campaign Manager.lnk"
$LegacyPath   = Join-Path $DesktopPath "Morning Dashboard.lnk"
$OldNamePath  = Join-Path $DesktopPath "Adopy Campaigns Launcher.lnk"

if (Test-Path $LegacyPath) {
    Remove-Item -LiteralPath $LegacyPath -Force
    Write-Host "  Removed legacy shortcut: Morning Dashboard.lnk" -ForegroundColor Yellow
}

# Ярлык до ребрендинга (ACL-1705) удаляем, чтобы не было двух значков
if (Test-Path $OldNamePath) {
    Remove-Item -LiteralPath $OldNamePath -Force
    Write-Host "  Removed old shortcut: Adopy Campaigns Launcher.lnk" -ForegroundColor Yellow
}

$WshShell = New-Object -ComObject WScript.Shell
$Shortcut = $WshShell.CreateShortcut($ShortcutPath)
$Shortcut.TargetPath       = $DashboardBat
$Shortcut.WorkingDirectory = $ProjectRoot
$Shortcut.Description      = "Launch Adopy Campaign Manager"
$Shortcut.IconLocation     = (Resolve-Path $IcoFile).Path + ",0"
$Shortcut.Save()

Write-Host ""
Write-Host "  Shortcut created:" -ForegroundColor Green
Write-Host "    $ShortcutPath"
Write-Host ""
Write-Host "  Launcher:  $DashboardBat"
if (Test-Path $IcoFile) {
    Write-Host "  Icon:      $IcoFile"
}
Write-Host ""
Write-Host "  Double-click the shortcut to start the dashboard." -ForegroundColor Cyan
Write-Host "  Browser opens at http://127.0.0.1:9090/ (first load ~15-20s)."
Write-Host "  Close the console window to stop the server."
Write-Host ""
