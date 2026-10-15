[CmdletBinding()]
param(
    [string]$Version = "0.1.0-spike"
)

$ErrorActionPreference = "Stop"
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "../..")).Path
$BuildRoot = Join-Path $PSScriptRoot ".build"
$ArtifactRoot = Join-Path $PSScriptRoot ".artifacts"
$ManifestPath = Join-Path $BuildRoot "acl-build-manifest.json"
$CurrentPath = Join-Path $BuildRoot "current.json"
$DistPath = Join-Path $ArtifactRoot "dist"
$WorkPath = Join-Path $BuildRoot "pyinstaller"
$SpecPath = Join-Path $PSScriptRoot "acl_windows.spec"

function Assert-LastExitCode([string]$Step) {
    if ($LASTEXITCODE -ne 0) {
        throw "$Step failed with exit code $LASTEXITCODE"
    }
}

Push-Location $RepoRoot
try {
    python packaging/windows/verify_core_isolation.py --require-clean
    Assert-LastExitCode "Installer isolation check"

    New-Item -ItemType Directory -Force -Path $BuildRoot | Out-Null
    New-Item -ItemType Directory -Force -Path $ArtifactRoot | Out-Null

    python packaging/windows/build_manifest.py `
        --version $Version `
        --output $ManifestPath `
        --current-output $CurrentPath `
        --channel-output (Join-Path $BuildRoot "acl-update-channel.json")
    Assert-LastExitCode "Build manifest generation"

    python packaging/windows/setup_guide.py `
        --output (Join-Path $BuildRoot "setup-guide.html") `
        --version $Version
    Assert-LastExitCode "Setup guide rendering"

    $env:ACL_BUILD_MANIFEST = $ManifestPath
    python -m PyInstaller `
        --noconfirm `
        --clean `
        --distpath $DistPath `
        --workpath $WorkPath `
        $SpecPath
    Assert-LastExitCode "PyInstaller bundle"

    $Executable = Join-Path $DistPath "ACL/ACL.exe"
    $ConsoleExecutable = Join-Path $DistPath "ACL/ACLConsole.exe"
    if (-not (Test-Path $Executable -PathType Leaf)) {
        throw "Expected executable was not produced: $Executable"
    }
    if (-not (Test-Path $ConsoleExecutable -PathType Leaf)) {
        throw "Expected diagnostic executable was not produced: $ConsoleExecutable"
    }
    Write-Host "Adopy Campaign Manager Windows bundle created: $Executable"
}
finally {
    Remove-Item Env:ACL_BUILD_MANIFEST -ErrorAction SilentlyContinue
    Pop-Location
}
