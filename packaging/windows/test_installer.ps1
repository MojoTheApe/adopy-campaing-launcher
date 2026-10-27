[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$Version,
    [string]$InstallerPath = ""
)

$ErrorActionPreference = "Stop"
$ArtifactRoot = Join-Path $PSScriptRoot ".artifacts"
$LogDirectory = Join-Path $ArtifactRoot "installer-test-logs"
if (-not $InstallerPath) {
    $InstallerPath = Join-Path $ArtifactRoot "installer/Adopy-Campaign-Launcher-Setup-$Version.exe"
}
$ProgramRoot = Join-Path $env:LOCALAPPDATA "Programs/ACL"
$InstallDirectory = Join-Path $ProgramRoot "versions/$Version"
$ConfigDirectory = Join-Path $env:LOCALAPPDATA "ACL"
$Sentinel = Join-Path $ConfigDirectory "installer-preservation-sentinel.txt"
$CurrentManifest = Join-Path $ProgramRoot "current.json"
$PreservedFiles = @{}
foreach ($Name in @(".env", "ad_account_map.csv", "oauth_client.json", "token_accounts.json", "targeting_registry.json")) {
    $PreservedFiles[$Name] = "synthetic-preservation-$Name"
}
# ACL-1358: the dashboard moves the Google token files into Windows protected
# storage (header ACLDPAPI1 + DPAPI ciphertext, current user) once it is healthy.
# That is the app, not the installer, changing the file: open it and compare the
# content, so a replaced or emptied file is still caught.
function Read-ConfigurationText([string]$Path) {
    $Bytes = [System.IO.File]::ReadAllBytes($Path)
    $Magic = [System.Text.Encoding]::ASCII.GetBytes("ACLDPAPI1`n")
    $Protected = $Bytes.Length -gt $Magic.Length
    for ($i = 0; $Protected -and $i -lt $Magic.Length; $i++) {
        if ($Bytes[$i] -ne $Magic[$i]) { $Protected = $false }
    }
    if ($Protected) {
        Add-Type -AssemblyName System.Security
        $Cipher = $Bytes[$Magic.Length..($Bytes.Length - 1)]
        $Entropy = [System.Text.Encoding]::ASCII.GetBytes("ACL-token-vault-v1")
        $Bytes = [System.Security.Cryptography.ProtectedData]::Unprotect(
            $Cipher, $Entropy, [System.Security.Cryptography.DataProtectionScope]::CurrentUser)
    }
    return [System.Text.Encoding]::UTF8.GetString($Bytes).TrimStart([char]0xFEFF)
}
function Assert-ConfigurationPreserved {
    foreach ($Name in $PreservedFiles.Keys) {
        $Content = Read-ConfigurationText (Join-Path $ConfigDirectory $Name)
        if ($Content.Trim() -ne $PreservedFiles[$Name]) { throw "Configuration changed: $Name" }
    }
}
$GuiExecutable = Join-Path $InstallDirectory "ACL.exe"
$ConsoleExecutable = Join-Path $InstallDirectory "ACLConsole.exe"
$InstallLog = Join-Path $LogDirectory "install.log"
$RepairLog = Join-Path $LogDirectory "repair.log"
$UninstallLog = Join-Path $LogDirectory "uninstall.log"

function Invoke-Installer([string]$LogPath) {
    $Arguments = @(
        "/VERYSILENT",
        "/SUPPRESSMSGBOXES",
        "/NORESTART",
        "/TASKS=desktopicon",
        "/LOG=$LogPath"
    )
    $Process = Start-Process -FilePath $InstallerPath -ArgumentList $Arguments -Wait -PassThru
    if ($Process.ExitCode -ne 0) {
        throw "Installer failed with exit code $($Process.ExitCode). See $LogPath"
    }
}

function Assert-Exists([string]$Path, [string]$Label) {
    if (-not (Test-Path $Path)) { throw "$Label not found: $Path" }
}

Assert-Exists $InstallerPath "Installer"
New-Item -ItemType Directory -Force -Path $LogDirectory | Out-Null
New-Item -ItemType Directory -Force -Path $ConfigDirectory | Out-Null
Set-Content -Path $Sentinel -Value "preserve-me" -Encoding UTF8
foreach ($Name in $PreservedFiles.Keys) {
    Set-Content (Join-Path $ConfigDirectory $Name) -Value $PreservedFiles[$Name] -Encoding UTF8
}

Write-Host "Installing ACL without elevation..."
Invoke-Installer $InstallLog
Assert-Exists $GuiExecutable "Installed GUI launcher"
Assert-Exists $ConsoleExecutable "Installed diagnostic launcher"
Assert-Exists $CurrentManifest "Active-version pointer"

$Current = Get-Content $CurrentManifest -Raw | ConvertFrom-Json
if ($Current.version -ne $Version) {
    throw "current.json reports '$($Current.version)', expected '$Version'."
}
if (-not ((Get-Content $Sentinel -Raw).Contains("preserve-me"))) {
    throw "Existing machine configuration was overwritten during install."
}

$StartMenuShortcut = Join-Path ([Environment]::GetFolderPath("StartMenu")) "Programs/Adopy Campaign Launcher/Open Campaign Launcher.lnk"
$SetupShortcut = Join-Path ([Environment]::GetFolderPath("StartMenu")) "Programs/Adopy Campaign Launcher/Setup Campaign Launcher.lnk"
$DesktopShortcut = Join-Path ([Environment]::GetFolderPath("Desktop")) "Adopy Campaign Launcher.lnk"
$GuideShortcut = Join-Path ([Environment]::GetFolderPath("StartMenu")) "Programs/Adopy Campaign Launcher/Setup guide.lnk"
Assert-Exists $StartMenuShortcut "Start Menu shortcut"
Assert-Exists $SetupShortcut "Setup Wizard shortcut"
Assert-Exists $DesktopShortcut "Desktop shortcut"
Assert-Exists $GuideShortcut "Setup guide shortcut"
Assert-Exists (Join-Path $InstallDirectory "_internal/setup-guide.html") "Setup guide page"

Assert-ConfigurationPreserved
Write-Host "Proving the installed bundle does not need Python or Git on PATH..."
$SavedPath = $env:PATH
try {
    $env:PATH = "$env:SystemRoot\System32;$env:SystemRoot"
    & $ConsoleExecutable --acl-version
    if ($LASTEXITCODE -ne 0) { throw "Installed version command failed without Python/Git on PATH." }
    & $ConsoleExecutable --smoke-check
    if ($LASTEXITCODE -ne 0) { throw "Installed smoke check failed without Python/Git on PATH." }
}
finally {
    $env:PATH = $SavedPath
}

Write-Host "Starting and stopping the installed backend twice..."
for ($Attempt = 1; $Attempt -le 2; $Attempt++) {
    & (Join-Path $PSScriptRoot "test_bundle.ps1") -BundleDirectory $InstallDirectory -Port 19090
}

Write-Host "Testing the normal hidden-console launcher and single-instance path..."
$Launch = Start-Process -FilePath $GuiExecutable -ArgumentList "--launcher-test" -PassThru
if (-not $Launch.WaitForExit(70000)) { throw "Normal launcher did not return after its health timeout." }
if ($Launch.ExitCode -ne 0) { throw "Normal launcher failed with exit code $($Launch.ExitCode)." }
$Health = Invoke-RestMethod -Uri "http://127.0.0.1:9090/health" -TimeoutSec 5
if ($Health.ok -ne $true -or $Health.app -ne "ACL") { throw "Normal launcher health response is invalid." }
# ACL-1358: once the dashboard has answered /health as this version, it moves the
# Google token file into Windows protected storage (about 15 s later).
$TokenFile = Join-Path $ConfigDirectory "token_accounts.json"
$Magic = [System.Text.Encoding]::ASCII.GetBytes("ACLDPAPI1`n")
$TokenProtected = $false
for ($Wait = 0; $Wait -lt 60 -and -not $TokenProtected; $Wait++) {
    $Head = [System.IO.File]::ReadAllBytes($TokenFile)
    $TokenProtected = $Head.Length -gt $Magic.Length
    for ($i = 0; $TokenProtected -and $i -lt $Magic.Length; $i++) {
        if ($Head[$i] -ne $Magic[$i]) { $TokenProtected = $false }
    }
    if (-not $TokenProtected) { Start-Sleep -Seconds 1 }
}
if (-not $TokenProtected) { throw "token_accounts.json was not moved to protected storage after the healthy start." }
$SecondLaunch = Start-Process -FilePath $GuiExecutable -ArgumentList "--launcher-test" -PassThru
if (-not $SecondLaunch.WaitForExit(10000)) { throw "Second launcher invocation did not return." }
if ($SecondLaunch.ExitCode -ne 0) { throw "Second launcher invocation failed." }
$Stop = Start-Process -FilePath $GuiExecutable -ArgumentList "--stop-server" -Wait -PassThru
if ($Stop.ExitCode -ne 0) { throw "ACL launcher could not safely stop its own server." }

Write-Host "Testing repair without changing machine configuration..."
$RepairTarget = Join-Path $InstallDirectory "_internal/src/web/static/favicon.svg"
Assert-Exists $RepairTarget "Repair target"
Remove-Item $RepairTarget -Force
Invoke-Installer $RepairLog
Assert-Exists $RepairTarget "File restored by repair"
if (-not ((Get-Content $Sentinel -Raw).Contains("preserve-me"))) {
    throw "Machine configuration was overwritten during repair."
}

Assert-ConfigurationPreserved
Write-Host "Uninstalling program files while preserving machine configuration..."
$Uninstaller = Join-Path $ProgramRoot "unins000.exe"
Assert-Exists $Uninstaller "Uninstaller"
$UninstallProcess = Start-Process `
    -FilePath $Uninstaller `
    -ArgumentList @("/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/LOG=$UninstallLog") `
    -Wait `
    -PassThru
if ($UninstallProcess.ExitCode -ne 0) {
    throw "Uninstall failed with exit code $($UninstallProcess.ExitCode)."
}
if (Test-Path $InstallDirectory) { throw "Version directory remains after uninstall: $InstallDirectory" }
if (Test-Path $CurrentManifest) { throw "current.json remains after uninstall: $CurrentManifest" }
Assert-Exists $Sentinel "Preserved machine configuration"
if (Test-Path $StartMenuShortcut) { throw "Start Menu shortcut remains after uninstall." }
if (Test-Path $SetupShortcut) { throw "Setup Wizard shortcut remains after uninstall." }
if (Test-Path $DesktopShortcut) { throw "Desktop shortcut remains after uninstall." }
if (Test-Path $GuideShortcut) { throw "Setup guide shortcut remains after uninstall." }

Assert-ConfigurationPreserved
Write-Host "ACL installer install/launch/repair/uninstall test OK."
