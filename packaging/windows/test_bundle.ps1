[CmdletBinding()]
param(
    [string]$BundleDirectory = "",
    [int]$Port = 19090
)

$ErrorActionPreference = "Stop"
if (-not $BundleDirectory) {
    $BundleDirectory = Join-Path $PSScriptRoot ".artifacts/dist/ACL"
}
$Executable = Join-Path $BundleDirectory "ACL.exe"
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

Write-Host "Checking embedded version metadata..."
& $Executable --acl-version
Assert-LastExitCode "Version check"

Write-Host "Checking packaged imports..."
& $Executable --smoke-check
Assert-LastExitCode "Packaged import smoke check"

New-Item -ItemType Directory -Force -Path $LogDirectory | Out-Null
Remove-Item $StdoutLog, $StderrLog -Force -ErrorAction SilentlyContinue

$Process = $null
try {
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
    Write-Host "ACL packaged health check OK."
}
catch {
    Write-Host "--- ACL stdout ---"
    if (Test-Path $StdoutLog) { Get-Content $StdoutLog }
    Write-Host "--- ACL stderr ---"
    if (Test-Path $StderrLog) { Get-Content $StderrLog }
    throw
}
finally {
    if ($Process -and -not $Process.HasExited) {
        Stop-Process -Id $Process.Id -Force
        $Process.WaitForExit()
    }
}
