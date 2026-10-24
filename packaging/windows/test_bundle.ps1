[CmdletBinding()]
param(
    [string]$BundleDirectory = "",
    [int]$Port = 19090
)

$ErrorActionPreference = "Stop"
if (-not $BundleDirectory) {
    $BundleDirectory = Join-Path $PSScriptRoot ".artifacts/dist/ACL"
}
$Executable = Join-Path $BundleDirectory "ACLConsole.exe"
$LogDirectory = Join-Path $PSScriptRoot ".artifacts/test-logs"
$StdoutLog = Join-Path $LogDirectory "acl.stdout.log"
$StderrLog = Join-Path $LogDirectory "acl.stderr.log"

function Assert-LastExitCode([string]$Step) {
    if ($LASTEXITCODE -ne 0) {
        throw "$Step failed with exit code $LASTEXITCODE"
    }
}

if (-not (Test-Path $Executable -PathType Leaf)) {
    throw "ACL executable not found: $Executable"
}

$SavedConfigDirectory = $env:ACL_CONFIG_DIR
$SavedReleaseRepository = $env:ACL_RELEASES_REPO
$SavedUpdateToken = $env:ACL_UPDATE_TOKEN
$Process = $null
try {
# Keep smoke checks offline, including release builds with an embedded channel.
# Whitespace overrides the embedded values and normalizes to an empty channel.
$env:ACL_RELEASES_REPO = " "
$env:ACL_UPDATE_TOKEN = " "
$env:ACL_CONFIG_DIR = Join-Path $PSScriptRoot ".artifacts/bundle-test-config"
$env:ACL_NO_TELEMETRY = "1"
New-Item -ItemType Directory -Force -Path $env:ACL_CONFIG_DIR | Out-Null
$TargetFixture = '{"Local synthetic":{"name":"Local synthetic","interests":"Finance","narrow":"Owners"}}'
Set-Content -Path (Join-Path $env:ACL_CONFIG_DIR "targeting_registry.json") -Value $TargetFixture -Encoding utf8
Write-Host "Checking embedded version metadata..."
$VersionOutput = (& $Executable --acl-version) -join "`n"
Assert-LastExitCode "Version check"
$Manifest = Get-Content (Join-Path $BundleDirectory "_internal/acl-build-manifest.json") -Raw | ConvertFrom-Json
if ($VersionOutput -notmatch [regex]::Escape("runtime source: $($Manifest.runtime_source_sha)") -or
    $VersionOutput -notmatch [regex]::Escape("installer source: $($Manifest.installer_source_sha)")) {
    throw "Packaged source identity differs from its embedded manifest."
}
Write-Host $VersionOutput

Write-Host "Checking packaged imports..."
& $Executable --smoke-check
Assert-LastExitCode "Packaged import smoke check"

Write-Host "Checking bundled account name rules..."
if (-not (Test-Path (Join-Path $BundleDirectory "_internal/config/account_name_rules.json") -PathType Leaf)) {
    throw "config/account_name_rules.json is missing from the bundle."
}

Write-Host "Checking bundled BI campaign-manager mapping..."
if (-not (Test-Path (Join-Path $BundleDirectory "_internal/config/bi_cm_map.json") -PathType Leaf)) {
    throw "config/bi_cm_map.json is missing from the bundle."
}

Write-Host "Checking bundled Meta rules registry..."
foreach ($RegistryFile in @("meta.json", "rules.json", "sources.json")) {
    $RegistryPath = Join-Path $BundleDirectory "_internal/config/meta_rules/$RegistryFile"
    if (-not (Test-Path $RegistryPath -PathType Leaf)) {
        throw "Meta rules registry file is missing from the bundle: $RegistryFile"
    }
}

# Verify the encoder shipped inside this bundle, not one installed on the runner.
Write-Host "Checking bundled creative encoder..."
$EncoderDirectory = Join-Path $BundleDirectory "_internal/imageio_ffmpeg/binaries"
$Encoders = @(Get-ChildItem -Path $EncoderDirectory -Filter "ffmpeg*.exe" -File)
if ($Encoders.Count -ne 1) {
    throw "Expected one bundled ffmpeg executable, found $($Encoders.Count)."
}
& $Encoders[0].FullName -hide_banner -version
Assert-LastExitCode "Bundled creative encoder"

Write-Host "Checking packaged Setup Wizard imports..."
& $Executable --wizard-smoke-check
Assert-LastExitCode "Packaged Setup Wizard import check"

Write-Host "Checking sanitized diagnostic export..."
& $Executable --diagnostics-smoke-check
Assert-LastExitCode "Packaged diagnostic export check"

New-Item -ItemType Directory -Force -Path $LogDirectory | Out-Null
Remove-Item $StdoutLog, $StderrLog -Force -ErrorAction SilentlyContinue

    Write-Host "Starting packaged dashboard on port $Port..."
    $Process = Start-Process `
        -FilePath $Executable `
        -ArgumentList @("--no-browser", "--port", "$Port") `
        -RedirectStandardOutput $StdoutLog `
        -RedirectStandardError $StderrLog `
        -PassThru

    $Deadline = (Get-Date).AddSeconds(60)
    $Healthy = $false
    do {
        if ($Process.HasExited) {
            throw "ACL exited before its health endpoint became ready (exit $($Process.ExitCode))."
        }
        try {
            $Response = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/health" -TimeoutSec 2
            if ($Response.ok -eq $true -and $Response.app -eq "ACL") {
                $Healthy = $true
                break
            }
        }
        catch {
            Start-Sleep -Milliseconds 500
        }
    } while ((Get-Date) -lt $Deadline)

    if (-not $Healthy) {
        throw "ACL health endpoint did not become ready within 60 seconds."
    }
    $Identity = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/acl/update/check" -TimeoutSec 10
    if ($Response.version -ne $Manifest.version -or $Identity.current.version -ne $Manifest.version -or
        $Identity.ok -ne $true -or $Identity.channel -ne "release" -or
        $Identity.available -ne $false -or $Identity.supervised -ne $true -or $null -ne $Identity.candidate) {
        throw "Packaged dashboard identity/update policy is wrong."
    }
    $Targets = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/targeting-profiles" -TimeoutSec 20
    $LocalTarget = $Targets.profiles | Where-Object name -eq "Local synthetic"
    if (-not $Targets.ok -or $LocalTarget.narrow -ne "Owners") {
        throw "Packaged targeting library could not read its local profile."
    }
    Write-Host "ACL packaged health, source identity and targeting checks OK."
}
catch {
    Write-Host "--- ACL stdout ---"
    if (Test-Path $StdoutLog) { Get-Content $StdoutLog }
    Write-Host "--- ACL stderr ---"
    if (Test-Path $StderrLog) { Get-Content $StderrLog }
    throw
}
finally {
    $env:ACL_CONFIG_DIR = $SavedConfigDirectory
    $env:ACL_RELEASES_REPO = $SavedReleaseRepository
    $env:ACL_UPDATE_TOKEN = $SavedUpdateToken
    if ($Process -and -not $Process.HasExited) {
        Stop-Process -Id $Process.Id -Force
        $Process.WaitForExit()
    }
}
