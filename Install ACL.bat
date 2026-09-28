@echo off
setlocal EnableExtensions
title Install Adopy Campaigns Launcher
set "ACL_BOOTSTRAP_FILE=%~f0"
powershell -NoProfile -ExecutionPolicy Bypass -Command "$source = Get-Content -LiteralPath $env:ACL_BOOTSTRAP_FILE -Raw; & ([ScriptBlock]::Create(($source -split '(?m)^# ACL_POWERSHELL_BEGIN\r?$', 2)[1]))"
set "ACL_INSTALL_RC=%ERRORLEVEL%"
if not "%ACL_INSTALL_RC%"=="0" pause
exit /b %ACL_INSTALL_RC%
# ACL_POWERSHELL_BEGIN
$ErrorActionPreference = 'Stop'

# Standalone bootstrap: this entire file can be sent to a new campaign manager.
# No restart loop and no dependency on scripts from an unverified checkout.
function Update-AclPath {
    $parts = @([Environment]::GetEnvironmentVariable('Path', 'Machine'),
               [Environment]::GetEnvironmentVariable('Path', 'User'), $env:Path)
    $env:Path = ($parts | Where-Object { $_ }) -join ';'
}

function Invoke-AclProbe([string]$Executable, [string[]]$ProbeArgs) {
    $oldPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        $output = & $Executable @ProbeArgs 2>$null
        return @{ Code = $LASTEXITCODE; Output = ($output -join "`n").Trim() }
    } finally { $ErrorActionPreference = $oldPreference }
}

function Find-AclPython {
    $candidates = @()
    $command = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($command) { $candidates += $command.Source }
    $launcher = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($launcher) {
        foreach ($version in @('-3.13', '-3.12', '-3.11')) {
            $probe = Invoke-AclProbe $launcher.Source @($version, '-c', 'import sys; print(sys.executable)')
            if ($probe.Code -eq 0 -and $probe.Output) { $candidates += [string]$probe.Output }
        }
    }
    foreach ($root in @('HKCU:\Software\Python\PythonCore', 'HKLM:\Software\Python\PythonCore', 'HKLM:\Software\WOW6432Node\Python\PythonCore')) {
        foreach ($version in @(Get-ChildItem $root -ErrorAction SilentlyContinue | Sort-Object PSChildName -Descending)) {
            $key = Get-Item ($version.PSPath + '\InstallPath') -ErrorAction SilentlyContinue
            if ($key) { $candidates += Join-Path ($key.GetValue('')) 'python.exe' }
        }
    }
    foreach ($pattern in @("$env:LOCALAPPDATA\Programs\Python\Python*\python.exe", "$env:ProgramFiles\Python*\python.exe")) {
        $candidates += @(Get-Item $pattern -ErrorAction SilentlyContinue | Sort-Object FullName -Descending | ForEach-Object { $_.FullName })
    }
    foreach ($candidate in ($candidates | Select-Object -Unique)) {
        if ($candidate -match '\\WindowsApps\\' -or !(Test-Path -LiteralPath $candidate)) { continue }
        $probe = Invoke-AclProbe $candidate @('-c', 'import sys; raise SystemExit(0 if sys.version_info >= (3,11) else 1)')
        if ($probe.Code -eq 0) { return $candidate }
    }
    return $null
}

function Find-AclGit {
    $candidates = @()
    $command = Get-Command git.exe -ErrorAction SilentlyContinue
    if ($command) { $candidates += $command.Source }
    $candidates += @("$env:ProgramFiles\Git\cmd\git.exe", "$env:LOCALAPPDATA\Programs\Git\cmd\git.exe")
    foreach ($candidate in ($candidates | Select-Object -Unique)) {
        if (!(Test-Path -LiteralPath $candidate)) { continue }
        $probe = Invoke-AclProbe $candidate @('--version')
        if ($probe.Code -eq 0) { return $candidate }
    }
    return $null
}

function Install-AclDependency([string]$Id) {
    $winget = Get-Command winget.exe -ErrorAction SilentlyContinue
    if (!$winget) {
        throw "Missing $Id and winget is unavailable. Install Python 3.11+ from python.org and Git from git-scm.com, then run this file again. Existing Python/Git do not require winget."
    }
    Write-Host "Installing $Id ..."
    & $winget.Source install --id $Id -e --accept-source-agreements --accept-package-agreements | Out-Host
    if ($LASTEXITCODE -ne 0) { throw "Installation of $Id failed or was cancelled (exit $LASTEXITCODE). Fix it and rerun this file." }
    Update-AclPath
}

function Get-AclTools {
    Update-AclPath
    $python = Find-AclPython
    if (!$python) {
        Install-AclDependency 'Python.Python.3.13'
        $python = Find-AclPython
        if (!$python) { throw 'Python installation finished but a working Python 3.11+ was not found. Restart Windows or repair Python, then rerun this file.' }
    }
    $git = Find-AclGit
    if (!$git) {
        Install-AclDependency 'Git.Git'
        $git = Find-AclGit
        if (!$git) { throw 'Git installation finished but git.exe was not found. Restart Windows or repair Git, then rerun this file.' }
    }
    # install_acl.bat invokes python/git by name; pin them to the verified tools.
    $env:Path = (Split-Path $python) + ';' + (Split-Path $git) + ';' + $env:Path
    return @{ Python = $python; Git = $git }
}

function Assert-AclStopped {
    # serve.py may have been launched with a relative path, so cwd cannot be
    # inferred from CommandLine. Conservatively stop for any ACL-style server.
    $servers = @(Get-CimInstance Win32_Process -ErrorAction Stop | Where-Object {
        $_.Name -match '^(pythonw?|acl).*\.exe$' -and
        ($_.CommandLine -match '(^|[\s"\\/])serve\.py([\s"]|$)' -or $_.CommandLine -match 'src\.web\.app:app')
    })
    $listener = @(Get-NetTCPConnection -State Listen -LocalPort 9090 -ErrorAction SilentlyContinue)
    if ($servers.Count -or $listener.Count) {
        throw 'ACL or another service is running (server/port 9090). Close the ACL launcher and finish any RPA job, then rerun the installer. No checkout or dependencies were changed.'
    }
}

function Invoke-AclGit([string]$Git, [string]$Target, [string[]]$GitArgs) {
    $output = & $Git -C $Target @GitArgs 2>&1
    if ($LASTEXITCODE -ne 0) { throw "Git failed ($($GitArgs -join ' ')): $output" }
    return ($output -join "`n").Trim()
}

function Get-AclRepoName { 'MojoTheApe/adopy-campaing-launcher' }

function Get-AclGitHubToken([string]$Git) {
    # Same sources as scripts/acl_update_gate.py. The stored Git credential is
    # read without any prompt: a missing one means "not verified", not a login window.
    if ($env:ACL_UPDATE_GATE_TOKEN) { return $env:ACL_UPDATE_GATE_TOKEN.Trim() }
    $saved = @{ GIT_TERMINAL_PROMPT = $env:GIT_TERMINAL_PROMPT; GCM_INTERACTIVE = $env:GCM_INTERACTIVE }
    $oldPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        $env:GIT_TERMINAL_PROMPT = '0'; $env:GCM_INTERACTIVE = 'never'
        $query = "protocol=https`nhost=github.com`npath=$(Get-AclRepoName).git`n`n"
        $answer = $query | & $Git credential fill 2>$null
        if ($LASTEXITCODE -eq 0) {
            foreach ($line in @($answer)) { if ($line -like 'password=*') { return $line.Substring(9).Trim() } }
        }
    } catch {
    } finally {
        $ErrorActionPreference = $oldPreference
        $env:GIT_TERMINAL_PROMPT = $saved.GIT_TERMINAL_PROMPT; $env:GCM_INTERACTIVE = $saved.GCM_INTERACTIVE
    }
    foreach ($value in @($env:GH_TOKEN, $env:GITHUB_TOKEN)) { if ($value) { return $value.Trim() } }
    return ''
}

function Get-AclCiRun([string]$Git, [string]$RunId) {
    $token = Get-AclGitHubToken $Git
    if (!$token) { throw 'no GitHub credential on this machine to verify it' }
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
    try {
        return Invoke-RestMethod -UseBasicParsing -TimeoutSec 20 -Uri "https://api.github.com/repos/$(Get-AclRepoName)/actions/runs/$RunId" -Headers @{
            Authorization = "Bearer $token"; Accept = 'application/vnd.github+json'
            'X-GitHub-Api-Version' = '2022-11-28'; 'User-Agent' = 'acl-installer' }
    } catch {
        # Only the status: the request itself (and its token) never reaches the message.
        $code = $null
        if ($_.Exception.Response) { $code = [int]$_.Exception.Response.StatusCode }
        if ($code) { throw "GitHub answered HTTP $code" } else { throw 'GitHub API is unreachable' }
    }
}

function Test-AclCiRun($Run, [string]$RunId, [string]$Sha) {
    # The API, not the tag text, decides: this repository, ci.yml on main,
    # the exact tagged commit, completed with success.
    if (!$Run) { return 'no API answer' }
    if ([string]$Run.id -cne $RunId) { return 'is not the linked run' }
    if ([string]$Run.repository.full_name -ne (Get-AclRepoName)) { return 'belongs to another repository' }
    if ([string]$Run.path -cne '.github/workflows/ci.yml') { return 'is not the CI workflow' }
    if ([string]$Run.head_sha -cne $Sha) { return 'was not run on the tagged commit' }
    if ([string]$Run.head_branch -cne 'main') { return 'did not run on main' }
    if (@('push', 'workflow_dispatch') -notcontains [string]$Run.event) { return 'was not a push/dispatch run' }
    if ([string]$Run.status -cne 'completed' -or [string]$Run.conclusion -cne 'success') { return 'did not succeed' }
    return ''
}

function Get-AclRelease([string]$Git, [string]$Target, [bool]$FreshInstall) {
    # Same gate as scripts/acl_update_gate.py: exact promoted main head,
    # fast-forward, annotated release tag whose linked CI run the GitHub API confirms.
    $sha = Invoke-AclGit $Git $Target @('rev-parse', 'origin/main^{commit}')
    if ($sha -notmatch '^[0-9a-f]{40}$') { throw 'Cannot resolve the production release.' }
    if (!$FreshInstall) {
        $dirty = Invoke-AclGit $Git $Target @('status', '--porcelain', '--untracked-files=no')
        if ($dirty) { throw 'Tracked local changes present. Save/review them before updating ACL.' }
        Invoke-AclGit $Git $Target @('merge-base', '--is-ancestor', 'HEAD', $sha) | Out-Null
    }
    $problems = @()
    $runLink = 'https://github\.com/' + [regex]::Escape((Get-AclRepoName)) + '/actions/runs/(\d+)\b'
    $tags = (Invoke-AclGit $Git $Target @('tag', '--points-at', $sha, '--list', 'acl-release-*')) -split "`n"
    foreach ($tag in $tags) {
        if (!$tag) { continue }
        if ((Invoke-AclGit $Git $Target @('cat-file', '-t', $tag)) -ne 'tag') { continue }
        $annotation = Invoke-AclGit $Git $Target @('cat-file', '-p', $tag)
        if ($annotation -notmatch "(?m)^object $sha`$") { continue }
        $runIds = @([regex]::Matches($annotation, $runLink, 'IgnoreCase') | ForEach-Object { $_.Groups[1].Value } | Select-Object -Unique)
        if (!$runIds.Count) { $problems += "$tag links no CI run of $(Get-AclRepoName)"; continue }
        foreach ($runId in $runIds) {
            try { $why = Test-AclCiRun (Get-AclCiRun $Git $runId) $runId $sha } catch { $why = "could not be verified: $($_.Exception.Message)" }
            if (!$why) { return @{ Sha = $sha; Tag = $tag; Run = $runId } }
            $problems += "$tag CI run $runId $why"
        }
    }
    $detail = ''
    if ($problems.Count) { $detail = ' (' + ($problems -join '; ') + ')' }
    throw "Production has no verified annotated ACL release tag with a successful CI run confirmed by GitHub$detail. Nothing will be installed from an unverified commit."
}

function Install-Acl {
    $repo = 'https://github.com/MojoTheApe/adopy-campaing-launcher.git'
    $target = Join-Path ([Environment]::GetFolderPath('MyDocuments')) 'ACL'
    Write-Host "Adopy Campaigns Launcher - installing into $target"
    Assert-AclStopped
    $tools = Get-AclTools
    $fresh = !(Test-Path -LiteralPath (Join-Path $target '.git'))
    if ($fresh -and (Test-Path -LiteralPath $target)) {
        throw "$target already exists but is not an ACL installation. Nothing was changed. Choose another folder for those files before retrying."
    }
    $checkout = $target
    if ($fresh) { $checkout = Join-Path ([IO.Path]::GetTempPath()) ('acl-install-' + [guid]::NewGuid().ToString('N')) }
    try {
        if ($fresh) {
            # Start from production even though GitHub's default is a feature.
            & $tools.Git clone --quiet --no-checkout --branch main $repo $checkout
            if ($LASTEXITCODE -ne 0) { throw 'Download failed. Check GitHub access and connection.' }
        } else {
            $remote = Invoke-AclGit $tools.Git $checkout @('remote', 'get-url', 'origin')
            if ($remote -notmatch '^https://github\.com/MojoTheApe/adopy-campaing-launcher(?:\.git)?/?$' -and
                $remote -notmatch '^git@github\.com:MojoTheApe/adopy-campaing-launcher(?:\.git)?$') {
                throw 'This folder has a different Git origin; refusing to update it.'
            }
        }
        Invoke-AclGit $tools.Git $checkout @('fetch', '--quiet', 'origin', 'main:refs/remotes/origin/main', '--tags') | Out-Null
        $release = Get-AclRelease $tools.Git $checkout $fresh
        Assert-AclStopped
        Write-Host "Installing release $($release.Tag) ($($release.Sha))"
        Invoke-AclGit $tools.Git $checkout @('checkout', '--quiet', '--detach', $release.Sha) | Out-Null
        if ($fresh) { Move-Item -LiteralPath $checkout -Destination $target }
    } finally {
        # Only this invocation's disposable clone is removed; never the install.
        if ($fresh -and (Test-Path -LiteralPath $checkout)) { Remove-Item -LiteralPath $checkout -Recurse -Force }
    }
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $target 'scripts\install_dashboard_shortcut.ps1')
    if ($LASTEXITCODE -ne 0) { Write-Warning 'Desktop shortcut could not be created; use start_dashboard.bat.' }
    & (Join-Path $target 'install_acl.bat')
    if ($LASTEXITCODE -ne 0) { throw "ACL setup did not pass (exit $LASTEXITCODE). See the checks above." }
}

try {
    Install-Acl
    exit 0
} catch {
    Write-Host "ERROR: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}
