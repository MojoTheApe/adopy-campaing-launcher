param([string]$Bootstrap)
$ErrorActionPreference = 'Stop'
$source = Get-Content -LiteralPath $Bootstrap -Raw
$payload = ($source -split '(?m)^# ACL_POWERSHELL_BEGIN\r?$', 2)[1]
$tokens=$null; $errors=$null
$ast = [System.Management.Automation.Language.Parser]::ParseInput($payload, [ref]$tokens, [ref]$errors)
if ($errors.Count) { throw ($errors | Out-String) }
# Load definitions only: tests must never launch a real installer or browser.
foreach ($node in $ast.EndBlock.Statements) {
    if ($node -is [System.Management.Automation.Language.FunctionDefinitionAst]) {
        . ([ScriptBlock]::Create($node.Extent.Text))
    }
}
function Assert($Condition, $Message) { if (!$Condition) { throw $Message } }
function Expect-Failure([ScriptBlock]$Body, [string]$Pattern) {
    try { & $Body } catch { Assert ($_.Exception.Message -match $Pattern) "Unexpected error: $_"; return }
    throw "Expected refusal: $Pattern"
}

# Test each scenario in its own scope so overrides cannot leak to the next one.
& {
    function Update-AclPath {}
    function Find-AclPython { 'C:\Python313\python.exe' }
    function Find-AclGit { 'C:\Git\cmd\git.exe' }
    function Install-AclDependency { throw 'winget must not be used when tools exist' }
    $tools = Get-AclTools
    Assert ($tools.Python -eq 'C:\Python313\python.exe') 'Manual Python not reused'
    Assert ($env:Path.StartsWith('C:\Python313;C:\Git\cmd;')) 'Verified tools must win over stale PATH'
}
& {
    function Update-AclPath {}
    $script:pythonChecks = 0
    $script:installs = @()
    function Find-AclPython { $script:pythonChecks++; if ($script:pythonChecks -gt 1) { 'C:\NewPython\python.exe' } }
    function Find-AclGit { 'C:\Git\git.exe' }
    function Install-AclDependency([string]$Id) { $script:installs += $Id }
    $tools = Get-AclTools
    Assert ($tools.Python -eq 'C:\NewPython\python.exe') 'Python must be resolved again after install'
    Assert ($script:installs.Count -eq 1) 'Dependency should be installed once'
}
& {
    function Update-AclPath {}
    function Find-AclPython { $null }
    $script:installs = 0
    function Install-AclDependency { $script:installs++ }
    Expect-Failure { Get-AclTools } 'working Python'
    Assert ($script:installs -eq 1) 'Missing PATH must not start an infinite reinstall loop'
}
& {
    function Update-AclPath {}
    function Find-AclPython { $null }
    function Install-AclDependency { throw 'Installation cancelled' }
    function Find-AclGit { throw 'must not continue after Python failure' }
    Expect-Failure { Get-AclTools } 'cancelled'
}
& {
    function Get-Command { $null }
    Expect-Failure { Install-AclDependency 'Python.Python.3.13' } 'winget is unavailable'
}
& {
    function Get-CimInstance { @([pscustomobject]@{Name='python.exe';CommandLine='python serve.py --port 9090'}) }
    function Get-NetTCPConnection { @() }
    Expect-Failure { Assert-AclStopped } 'running'
}
& {
    function Get-CimInstance { @() }
    function Get-NetTCPConnection { @([pscustomobject]@{LocalPort=9090}) }
    Expect-Failure { Assert-AclStopped } 'running'
}

# Real, isolated Git repo: check the release gate against actual object types.
$temp = Join-Path ([IO.Path]::GetTempPath()) ('acl-bootstrap-test-' + [guid]::NewGuid().ToString('N'))
$git = (Get-Command git.exe).Source
try {
    New-Item -ItemType Directory -Path $temp | Out-Null
    & $git -C $temp init --quiet
    & $git -C $temp -c user.name=Test -c user.email=test@example.invalid commit --allow-empty --quiet -m fixture
    $sha = (& $git -C $temp rev-parse HEAD).Trim()
    & $git -C $temp update-ref refs/remotes/origin/main $sha

    # ACL-1357: a fake GitHub API. Every scenario sets what the token and the
    # API answer; nothing here reaches the network or the machine's credentials.
    function New-FakeRun([hashtable]$Change) {
        $run = @{ id = 123; repository = @{ full_name = 'MojoTheApe/adopy-campaing-launcher' }; path = '.github/workflows/ci.yml'
                  head_sha = $sha; head_branch = 'main'; event = 'push'; status = 'completed'; conclusion = 'success' }
        foreach ($key in $Change.Keys) { $run[$key] = $Change[$key] }
        return [pscustomobject]$run
    }
    $realTokenLookup = ${function:Get-AclGitHubToken}
    function Get-AclGitHubToken { 'test-token' }
    function Invoke-AclGitHubApi { throw 'scenario must define the API' }

    Expect-Failure { Get-AclRelease $git $temp $true } 'no verified annotated'
    & $git -C $temp tag acl-release-test
    Expect-Failure { Get-AclRelease $git $temp $true } 'no verified annotated'
    & $git -C $temp tag -d acl-release-test | Out-Null
    & $git -C $temp -c user.name=Test -c user.email=test@example.invalid tag -a acl-release-test -m 'unverified release'
    Expect-Failure { Get-AclRelease $git $temp $true } 'no verified annotated'
    & $git -C $temp tag -d acl-release-test | Out-Null
    & $git -C $temp -c user.name=Test -c user.email=test@example.invalid tag -a acl-release-test -m 'CI success https://github.com/org/repo/actions/runs/123'
    & { function Invoke-AclGitHubApi { throw 'another repository must not reach the API' }
        Expect-Failure { Get-AclRelease $git $temp $true } 'links no CI run' }
    & $git -C $temp tag -d acl-release-test | Out-Null
    & $git -C $temp -c user.name=Test -c user.email=test@example.invalid tag -a acl-release-test -m 'CI success https://github.com/MojoTheApe/adopy-campaing-launcher/actions/runs/123'

    # Verified by the API.
    & { function Invoke-AclGitHubApi { @{ Code = 200; Body = (New-FakeRun @{}) } }
        $release = Get-AclRelease $git $temp $true
        Assert ($release.Sha -eq $sha) 'Annotated release must select its exact commit'
        Assert ($release.Run -eq '123' -and !$release.Unverified) 'Release must name the verified run' }

    # Definite negatives: refused in every mode.
    foreach ($case in @(
            @{ Change = @{ conclusion = 'failure' }; Why = 'did not succeed' },
            @{ Change = @{ repository = @{ full_name = 'attacker/fork' } }; Why = 'another repository' },
            @{ Change = @{ head_sha = ('c' * 40) }; Why = 'tagged commit' },
            @{ Change = @{ path = '.github/workflows/release.yml' }; Why = 'CI workflow' },
            @{ Change = @{ head_branch = 'stage' }; Why = 'main' },
            @{ Change = @{ event = 'pull_request' }; Why = 'push/dispatch' })) {
        & { function Invoke-AclGitHubApi { @{ Code = 200; Body = (New-FakeRun $case.Change) } }
            Expect-Failure { Get-AclRelease $git $temp $true } $case.Why }
    }
    & { function Invoke-AclGitHubApi([string]$Token, [string]$Path) {
            if ($Path) { @{ Code = 404 } } else { @{ Code = 200; Body = [pscustomobject]@{ full_name = 'MojoTheApe/adopy-campaing-launcher' } } } }
        Expect-Failure { Get-AclRelease $git $temp $true } 'not a real run' }

    # Nothing to verify with: warn falls back to the tag text, enforce refuses.
    $unverifiable = @(
        @{ Name = 'no token'; Token = ''; Api = { throw 'must not call the API without a token' }; Why = 'no GitHub credential' },
        @{ Name = '403'; Token = 't'; Api = { @{ Code = 403 } }; Why = 'HTTP 403' },
        @{ Name = 'offline'; Token = 't'; Api = { @{ Code = 0 } }; Why = 'unreachable' },
        @{ Name = '404, repo hidden'; Token = 't'; Api = { @{ Code = 404 } }; Why = 'HTTP 404' })
    foreach ($case in $unverifiable) {
        & { function Get-AclGitHubToken { $case.Token }
            function Invoke-AclGitHubApi { & $case.Api }
            $release = Get-AclRelease $git $temp $true 3>$null
            Assert ($release.Sha -eq $sha -and $release.Unverified -match $case.Why) "warn fallback failed: $($case.Name)"
            $env:ACL_UPDATE_GATE = 'enforce'
            try { Expect-Failure { Get-AclRelease $git $temp $true } $case.Why } finally { Remove-Item Env:ACL_UPDATE_GATE }
        }
    }

    # The fallback is still the old check: a tag that does not record success is refused.
    & $git -C $temp tag -d acl-release-test | Out-Null
    & $git -C $temp -c user.name=Test -c user.email=test@example.invalid tag -a acl-release-test -m 'Built https://github.com/MojoTheApe/adopy-campaing-launcher/actions/runs/123'
    & { function Get-AclGitHubToken { '' }
        Expect-Failure { Get-AclRelease $git $temp $true } 'does not record success' }
    & $git -C $temp tag -d acl-release-test | Out-Null
    & $git -C $temp -c user.name=Test -c user.email=test@example.invalid tag -a acl-release-test -m 'CI success https://github.com/MojoTheApe/adopy-campaing-launcher/actions/runs/123'

    # The token lookup runs git in the clone, with no prompt and no askpass.
    & {
        function Invoke-FakeGit {
            $script:gitArgs = $args
            $script:askpassSeen = "$env:GIT_ASKPASS|$env:SSH_ASKPASS|$env:GIT_TERMINAL_PROMPT|$env:GCM_INTERACTIVE"
            $global:LASTEXITCODE = 0
            'protocol=https'; 'host=github.com'; 'password=stored-token'
        }
        ${function:Get-AclGitHubToken} = $realTokenLookup
        $env:GIT_ASKPASS = 'C:\ide\askpass.exe'; $env:SSH_ASKPASS = 'C:\ssh\askpass.exe'
        $savedToken = $env:ACL_UPDATE_GATE_TOKEN; $env:ACL_UPDATE_GATE_TOKEN = $null
        try {
            $token = Get-AclGitHubToken 'Invoke-FakeGit' 'C:\acl-target'
            Assert ($token -eq 'stored-token') 'stored credential must be used'
            Assert (($script:gitArgs -join ' ') -eq '-C C:\acl-target -c core.askPass= credential fill') "git args: $($script:gitArgs -join ' ')"
            Assert ($script:askpassSeen -eq '||0|never') "askpass must be off during the lookup: $script:askpassSeen"
            Assert ($env:GIT_ASKPASS -eq 'C:\ide\askpass.exe' -and $env:SSH_ASKPASS -eq 'C:\ssh\askpass.exe') 'environment must be restored'
        } finally {
            Remove-Item Env:GIT_ASKPASS, Env:SSH_ASKPASS -ErrorAction SilentlyContinue
            $env:ACL_UPDATE_GATE_TOKEN = $savedToken
        }
    }

    & $git -C $temp -c user.name=Test -c user.email=test@example.invalid commit --allow-empty --quiet -m divergence
    & { function Invoke-AclGitHubApi { @{ Code = 200; Body = (New-FakeRun @{}) } }
        Expect-Failure { Get-AclRelease $git $temp $false } 'Git failed' }
} finally { Remove-Item -LiteralPath $temp -Recurse -Force }
Write-Host 'Bootstrap scenarios passed'
