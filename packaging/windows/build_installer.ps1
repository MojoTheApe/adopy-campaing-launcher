[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$Version,
    [string]$BundleDirectory = ""
)

$ErrorActionPreference = "Stop"
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "../..")).Path
$BuildRoot = Join-Path $PSScriptRoot ".build"
$OutputDirectory = Join-Path $PSScriptRoot ".artifacts/installer"
$CurrentManifest = Join-Path $BuildRoot "current.json"
$InstallerScript = Join-Path $PSScriptRoot "installer.iss"
if (-not $BundleDirectory) {
    $BundleDirectory = Join-Path $PSScriptRoot ".artifacts/dist/ACL"
}

python (Join-Path $PSScriptRoot "verify_core_isolation.py") --require-clean
if ($LASTEXITCODE -ne 0) { throw "Installer isolation check failed." }

$RequiredFiles = @(
    (Join-Path $BundleDirectory "ACL.exe"),
    (Join-Path $BundleDirectory "ACLConsole.exe"),
    $CurrentManifest,
    $InstallerScript
)
foreach ($Path in $RequiredFiles) {
    if (-not (Test-Path $Path -PathType Leaf)) {
        throw "Installer input not found: $Path"
    }
}

if ($Version -notmatch '^[0-9A-Za-z][0-9A-Za-z._-]{0,63}$') {
    throw "Invalid installer version."
}
$Current = Get-Content $CurrentManifest -Raw | ConvertFrom-Json
$BundleManifestPath = Join-Path $BundleDirectory "_internal/acl-build-manifest.json"
$BundleManifest = Get-Content $BundleManifestPath -Raw | ConvertFrom-Json
if ($Current.version -ne $Version -or $BundleManifest.version -ne $Version -or
    $Current.source_sha -ne $BundleManifest.installer_source_sha) {
    throw "Installer inputs do not describe the same version and source commit. Rebuild the bundle."
}

$IsccCommand = Get-Command ISCC.exe -ErrorAction SilentlyContinue
$Candidates = @()
if ($IsccCommand) { $Candidates += $IsccCommand.Source }
if (${env:ProgramFiles(x86)}) {
    $Candidates += (Join-Path ${env:ProgramFiles(x86)} "Inno Setup 6/ISCC.exe")
}
if ($env:ProgramFiles) {
    $Candidates += (Join-Path $env:ProgramFiles "Inno Setup 6/ISCC.exe")
}
$IsccPath = $Candidates | Where-Object { Test-Path $_ -PathType Leaf } | Select-Object -First 1
if (-not $IsccPath) {
    throw "Inno Setup 6 compiler (ISCC.exe) was not found."
}

New-Item -ItemType Directory -Force -Path $OutputDirectory | Out-Null
& $IsccPath `
    "/DMyAppVersion=$Version" `
    "/DBundleDir=$BundleDirectory" `
    "/DCurrentManifest=$CurrentManifest" `
    "/DProjectRoot=$RepoRoot" `
    "/DInstallerOutputDir=$OutputDirectory" `
    $InstallerScript
if ($LASTEXITCODE -ne 0) {
    throw "Inno Setup build failed with exit code $LASTEXITCODE"
}

$Installer = Join-Path $OutputDirectory "Adopy-Campaign-Launcher-Setup-$Version.exe"
if (-not (Test-Path $Installer -PathType Leaf)) {
    throw "Expected installer was not produced: $Installer"
}
Write-Host "Adopy Campaign Manager per-user installer created: $Installer"
