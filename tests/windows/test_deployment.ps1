# Execute deployment state transitions with isolated process/task/HTTP doubles.
# No Windows process, scheduled task, CAD file or real worker is touched.
$ErrorActionPreference = 'Stop'
$scriptPath = Join-Path $PSScriptRoot '../../src/description_pipeline/sources/solidworks/deploy/worker.ps1'
$tokens = $null
$parseErrors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
    (Resolve-Path $scriptPath), [ref]$tokens, [ref]$parseErrors)
if ($parseErrors.Count) { throw ($parseErrors | Out-String) }
$functions = $ast.FindAll({ param($node)
    $node -is [System.Management.Automation.Language.FunctionDefinitionAst]
}, $false)
foreach ($definition in $functions) { Invoke-Expression $definition.Extent.Text }

function Assert-True($Value, [string]$Message) { if (-not $Value) { throw $Message } }

# --- the bundle digest is required, with no way to skip it --------------------
$bundleFixture = Join-Path ([IO.Path]::GetTempPath()) ('description-bundle-' + [Guid]::NewGuid().ToString('N') + '.zip')
Set-Content -LiteralPath $bundleFixture -Value 'not a real bundle' -Encoding ASCII
$bundleDigest = (Get-FileHash -LiteralPath $bundleFixture -Algorithm SHA256).Hash.ToLowerInvariant()
foreach ($case in @(
        @{ name = 'missing'; digest = '' },
        @{ name = 'placeholder'; digest = '<64 hex characters printed by the bundle build>' },
        @{ name = 'short'; digest = $bundleDigest.Substring(0, 32) },
        @{ name = 'wrong'; digest = ('0' * 64) }
    )) {
    $rejected = $false
    try { Assert-Bundle $bundleFixture $case.digest | Out-Null } catch { $rejected = $true }
    Assert-True $rejected ("Assert-Bundle accepted a $($case.name) digest")
}
$accepted = Assert-Bundle $bundleFixture $bundleDigest.ToUpperInvariant()
Assert-True ($accepted -eq $bundleDigest) 'Assert-Bundle did not accept the correct digest'
$failedInstall = Join-Path ([IO.Path]::GetTempPath()) ('description-install-' + [Guid]::NewGuid().ToString('N'))
try {
    foreach ($attempt in 1..2) {
        $failure = $null
        try { Install-Version $bundleFixture $failedInstall '1.2.3' $bundleFixture | Out-Null } catch { $failure = $_ }
        Assert-True ($null -ne $failure) 'corrupt archive installed'
        Assert-True (-not (Test-Path (Join-Path $failedInstall 'versions/1.2.3'))) 'failed extraction left an installed version'
        $leftovers = @(Get-ChildItem (Join-Path $failedInstall 'versions') -Force)
        Assert-True ($leftovers.Count -eq 0) 'failed extraction left staging files'
    }
} finally { Remove-Item -LiteralPath $failedInstall -Recurse -Force }
Remove-Item -LiteralPath $bundleFixture -Force

# A failed activation leaves the new version installed; retrying the same bundle has to work,
# and a *different* bundle reusing that version number has to be refused by name.
$reuseRoot = Join-Path ([IO.Path]::GetTempPath()) ('description-reuse-' + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $reuseRoot | Out-Null
try {
    $payload = Join-Path $reuseRoot 'payload.txt'
    Set-Content -LiteralPath $payload -Value 'worker payload' -Encoding ASCII
    $goodZip = Join-Path $reuseRoot 'good-bundle.zip'
    Compress-Archive -LiteralPath $payload -DestinationPath $goodZip
    $goodDigest = (Get-FileHash -LiteralPath $goodZip -Algorithm SHA256).Hash.ToLowerInvariant()
    $goodInstall = Join-Path $reuseRoot 'install'
    $goodConfig = Join-Path $goodInstall 'worker-host.json'
    $first = Install-Version $goodZip $goodInstall '2.0.0' $goodConfig $goodDigest
    $recorded = (Get-Content -LiteralPath (Join-Path $first 'bundle.sha256') -Raw -Encoding UTF8).Trim()
    Assert-True ($recorded -eq $goodDigest) 'the install did not record the bundle digest it came from'
    $second = Install-Version $goodZip $goodInstall '2.0.0' $goodConfig $goodDigest
    Assert-True ($second -eq $first) 'retrying the same bundle did not reuse the installation'

    Set-Content -LiteralPath $payload -Value 'different worker payload' -Encoding ASCII
    $otherZip = Join-Path $reuseRoot 'other-bundle.zip'
    Compress-Archive -LiteralPath $payload -DestinationPath $otherZip
    $otherDigest = (Get-FileHash -LiteralPath $otherZip -Algorithm SHA256).Hash.ToLowerInvariant()
    $refused = $false
    $message = ''
    try { Install-Version $otherZip $goodInstall '2.0.0' $goodConfig $otherDigest | Out-Null } catch {
        $refused = $true; $message = $_.Exception.Message
    }
    Assert-True $refused 'a different bundle reusing a version number was installed over it'
    Assert-True ($message -like "*$goodDigest*" -and $message -like "*$otherDigest*") `
        'the refusal did not name both bundle digests'

    # An installation from an older script carries no digest: refuse, and say how to recover.
    $legacyRoot = Join-Path $reuseRoot 'legacy'
    New-Item -ItemType Directory -Force -Path (Join-Path $legacyRoot 'versions\2.1.0') | Out-Null
    $refused = $false
    $message = ''
    try { Install-Version $goodZip $legacyRoot '2.1.0' (Join-Path $legacyRoot 'worker-host.json') $goodDigest | Out-Null } catch {
        $refused = $true; $message = $_.Exception.Message
    }
    Assert-True $refused 'an installation with no recorded digest was reused blindly'
    Assert-True ($message -like '*remove that directory*') 'the refusal did not say how to recover'

    # Reuse requires the whole runtime, not just an interpreter: an install interrupted before
    # Publish-VersionRuntime must be completed, not skipped into a version with no description.cmd.
    $partialRoot = Join-Path $reuseRoot 'partial'
    $partialScripts = Join-Path $partialRoot 'venv\Scripts'
    New-Item -ItemType Directory -Force -Path $partialScripts | Out-Null
    Assert-True (-not (Test-VersionRuntimeReady $partialRoot)) 'a version with no runtime at all was reusable'
    Set-Content -LiteralPath (Join-Path $partialScripts 'python.exe') -Value 'stub' -Encoding ASCII
    Assert-True (-not (Test-VersionRuntimeReady $partialRoot)) `
        'a version with an interpreter but no runtime entry was reusable'
    Set-Content -LiteralPath (Join-Path $partialScripts 'description.cmd') -Value '@echo off' -Encoding ASCII
    Assert-True (Test-VersionRuntimeReady $partialRoot) 'a complete runtime was not reusable'
} finally { Remove-Item -LiteralPath $reuseRoot -Recurse -Force }

# Real filesystem regression: operators normally keep the config in install_root.
$configInstall = Join-Path ([IO.Path]::GetTempPath()) ('description-config-' + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $configInstall | Out-Null
try {
    $unicodeRoot = 'C:\workers\' + (-join ([char[]]@(20013,25991)))
    $unicodeConfigPath = Join-Path $configInstall 'unicode-host.json'
    [IO.File]::WriteAllText($unicodeConfigPath, (@{
        install_root = $unicodeRoot; user = 'test-user'
    } | ConvertTo-Json), [Text.UTF8Encoding]::new($false))
    $unicodeConfig = Get-HostConfig $unicodeConfigPath
    Assert-True ($unicodeConfig.install_root -eq $unicodeRoot) 'BOM-less UTF-8 preserves the worker path'
    $payload = Join-Path $configInstall 'payload.txt'
    Set-Content -LiteralPath $payload -Value 'package' -Encoding ASCII
    $archive = Join-Path $configInstall 'package.zip'
    Compress-Archive -LiteralPath $payload -DestinationPath $archive
    $hostConfig = Join-Path $configInstall 'worker-host.json'
    Set-Content -LiteralPath $hostConfig -Value '{"fixture":true}' -Encoding ASCII
    $originalConfig = (Get-FileHash -LiteralPath $hostConfig).Hash
    $installed = Install-Version $archive $configInstall '1.0.0' (Join-Path $configInstall './worker-host.json')
    Assert-True (Test-Path (Join-Path $installed 'payload.txt')) 'in-place host config prevented installation'
    Assert-True ((Get-FileHash -LiteralPath $hostConfig).Hash -eq $originalConfig) 'in-place host config changed'
    $rejected = $false
    try { Install-Version $archive $configInstall '1.0.1' (Join-Path $configInstall 'missing.json') | Out-Null } catch { $rejected = $true }
    Assert-True $rejected 'missing host configuration was accepted'
    Assert-True (-not (Test-Path (Join-Path $configInstall 'versions/1.0.1'))) 'config failure left an installed version'
    Assert-True (@(Get-ChildItem (Join-Path $configInstall 'versions') -Filter '*.partial' -Force).Count -eq 0) 'config failure left staging files'
} finally { Remove-Item -LiteralPath $configInstall -Recurse -Force }

$script:events = [System.Collections.Generic.List[string]]::new()
$script:failNew = $false
$config = [pscustomobject]@{
    install_root = (Join-Path ([IO.Path]::GetTempPath()) 'description-deployment-fixture')
    task_name = 'fixture-worker'
}
function Get-CurrentVersion($Root) { '0.1.0' }
function Stop-Worker($ConfigHost) { $script:events.Add('stop') }
function New-Runner($Root, $Version, $ConfigHost) { "runner-$Version" }
function Register-WorkerTask($Name, $Runner, $ConfigHost) {
    $script:events.Add("register-$Runner")
    'scheduled_task'
}
function Start-Worker($Name, $ConfigHost, $Version, $Mode) {
    $script:events.Add("health-$Version")
    if ($script:failNew -and $Version -eq '0.2.0') { throw 'new worker failed health' }
}
function Set-CurrentVersion($Root, $Version, $Mode, $Previous) {
    $script:events.Add("commit-$Version-from-$Previous")
}
Activate-Version $config '0.2.0'
Assert-True (($script:events -join ',') -eq
    'stop,register-runner-0.2.0,health-0.2.0,commit-0.2.0-from-0.1.0') 'version committed before health'

$script:events.Clear()
$script:failNew = $true
$failed = $false
try { Activate-Version $config '0.2.0' } catch { $failed = $true }
Assert-True $failed 'failed activation did not propagate an error'
Assert-True (($script:events -join ',') -eq
    'stop,register-runner-0.2.0,health-0.2.0,stop,register-runner-0.1.0,health-0.1.0') 'failed update did not restore previous runtime'

# Restore the real stopping implementation; its process operations remain doubles.
Invoke-Expression ($functions | Where-Object Name -eq 'Stop-Worker').Extent.Text
$script:busy = $true
$script:recordsQueued = 0
$script:paused = $false
$script:alive = $true
$script:stopped = @()
function Invoke-WorkerEndpoint($ConfigHost, $Path, $Method) {
    if ($Path -eq '/maintenance') { $script:paused = $true; return @{ maintenance = $true } }
    if (-not $script:alive) { return $null }
    return [pscustomobject]@{ pid = 12345; worker_version = '0.2.0';
        runner = @{ current = $(if ($script:busy) { 'active-job' } else { $null }); queued = @() };
        jobs = @{ queued = $script:recordsQueued; running = 0 } }
}
$script:portOwner = 12345
$script:ownerExecutable = $null
$script:ownerCommandLine = $null
$script:keepsAnswering = $false
function Get-CimInstance($ClassName, $Filter) {
    $id = if ("$Filter" -match 'ProcessId=(\d+)') { [int]$Matches[1] } else { 12345 }
    if ($id -eq 12345) {
        return [pscustomobject]@{
            ProcessId = $id
            ExecutablePath = (Join-Path $config.install_root 'versions\0.2.0\venv\Scripts\python.exe')
            CommandLine = ('-m description_pipeline.sources.solidworks.worker --jobs-root "' + $config.jobs_root + '"')
        }
    }
    return [pscustomobject]@{ ProcessId = $id; ExecutablePath = $script:ownerExecutable; CommandLine = $script:ownerCommandLine }
}
function Get-NetTCPConnection($LocalPort, $State) { [pscustomobject]@{ OwningProcess = $script:portOwner } }
function Get-CurrentMode($Root) { 'startup_folder' }
function Get-Process($Id) { [pscustomobject]@{ Id = $Id } }
function Stop-Process($Id) {
    Assert-True $script:paused 'worker stopped before admission was paused'
    $script:stopped += $Id
    if (-not $script:keepsAnswering) { $script:alive = $false }
}
function Start-Sleep($Milliseconds) { }
$failed = $false
try { Stop-Worker $config } catch { $failed = $true }
Assert-True ($failed -and $script:stopped.Count -eq 0) 'active worker was stopped'
$script:busy = $false

# a job recovered by a previous process is queued in the records but never in the
# runner queue: /health alone used to look idle
$script:recordsQueued = 1
$failed = $false
try { Stop-Worker $config } catch { $failed = $true }
Assert-True ($failed -and $script:stopped.Count -eq 0) 'worker with recovered queued work was stopped'
Assert-True (Test-WorkerBusy ([pscustomobject]@{ runner = @{ current = $null; queued = @() }; jobs = @{ queued = 1; running = 0 } })) 'recovered queued job did not count as busy'
Assert-True (Test-WorkerBusy ([pscustomobject]@{ runner = @{ current = $null; queued = @() }; jobs = @{ queued = 0; running = 2 } })) 'recorded running job did not count as busy'
Assert-True (Test-WorkerBusy ([pscustomobject]@{ cad_operation_active = $true })) 'incomplete COM operation counted as idle'
Assert-True (Test-WorkerBusy @{ cad_operation_active = $true }) 'dictionary COM state counted as idle'
Assert-True (-not (Test-WorkerBusy ([pscustomobject]@{
    cad_operation_active = $true; cad_recovery_required = $true
    runner = @{ current = $null; queued = @() }; jobs = @{ queued = 0; running = 0 }
}))) 'terminal stuck operation has no worker restart path'
Assert-True (Test-WorkerBusy ([pscustomobject]@{
    cad_operation_active = $true; cad_recovery_required = $true
    runner = @{ current = $null; queued = @() }; jobs = @{ queued = 1; running = 0 }
})) 'recovery allowed with queued work'
$script:recordsQueued = 0
Assert-True (-not (Test-WorkerBusy ([pscustomobject]@{ runner = @{ current = $null; queued = @() }; jobs = @{ queued = 0; running = 0 } }))) 'idle worker counted as busy'

Stop-Worker $config
Assert-True ($script:stopped.Count -eq 1 -and $script:stopped[0] -eq 12345) 'did not stop only the verified worker PID'

# The process that holds the port is the authority.  A stranger listening on that port is
# reported, never killed - and the message has to name it, or the operator cannot act.
$script:portOwner = 99999
$script:ownerExecutable = 'C:\Windows\System32\notepad.exe'
$script:ownerCommandLine = 'notepad.exe'
$script:stopped = @()
$script:alive = $true
$failed = $false
$message = ''
try { Stop-Worker $config } catch { $failed = $true; $message = $_.Exception.Message }
Assert-True ($failed -and $script:stopped.Count -eq 0) 'a foreign port owner was stopped'
Assert-True ($message -like '*99999*') 'the refusal did not name the foreign pid'
Assert-True ($message -like '*notepad.exe*') 'the refusal did not name the foreign process'

# A verified worker that keeps answering is reported with the process still holding the port.
$script:portOwner = 12345
$script:alive = $true
$script:keepsAnswering = $true
$script:stopped = @()
$failed = $false
$message = ''
try { Stop-Worker $config } catch { $failed = $true; $message = $_.Exception.Message }
Assert-True ($failed -and $script:stopped.Count -ge 1) 'the verified worker was never even attempted'
Assert-True ($message -like '*12345*') 'the failure did not name the process that still answers'
Assert-True ($message -like '*desktop session*') 'the failure did not say how to recover'
$script:keepsAnswering = $false
$script:alive = $true

# --- Setup writes the host configuration, so a first-time operator edits no JSON ------------
$setupRoot = Join-Path ([IO.Path]::GetTempPath()) ('description-setup-' + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $setupRoot | Out-Null
try {
    $setupBundle = Join-Path $setupRoot 'description-worker-9.9.9-windows-x86_64.zip'
    Set-Content -LiteralPath $setupBundle -Value 'bundle' -Encoding ASCII
    $setupDigest = (Get-FileHash -LiteralPath $setupBundle -Algorithm SHA256).Hash.ToLowerInvariant()
    Set-Content -LiteralPath (Join-Path $setupRoot 'SHA256SUMS') `
        -Value ("{0}  {1}" -f $setupDigest, (Split-Path -Leaf $setupBundle)) -Encoding ASCII
    $setupConfig = Join-Path $setupRoot 'worker-host.json'
    $setupPython = (Get-Command python -ErrorAction SilentlyContinue).Source
    Assert-True ($null -ne $setupPython) 'the deployment suite needs CPython on PATH for the Setup checks'
    $written = New-HostConfig -Path $setupConfig -Bundle $setupBundle -InstallRoot (Join-Path $setupRoot 'worker') `
        -Python $setupPython -Assembly 'D:\models\robot.SLDASM' -AssemblyConfiguration Default -Force
    Assert-True ($written.bundle_sha256 -eq $setupDigest) 'Setup did not verify the digest against SHA256SUMS'
    Assert-True ($written.assembly -eq 'D:\models\robot.SLDASM') 'Setup dropped the assembly path'
    Assert-True ($written.configuration -eq 'Default') 'Setup dropped the assembly configuration'
    Assert-True ($written.port -eq 8765 -and $written.task_name -eq 'description-pipeline-worker') 'Setup did not apply the defaults'
    Assert-True ((Get-Content -LiteralPath $setupConfig -Raw | ConvertFrom-Json).install_root -eq (Join-Path $setupRoot 'worker')) 'Setup wrote an unreadable config'

    $refused = $false
    try { New-HostConfig -Path $setupConfig -Bundle $setupBundle -Python $setupPython } catch { $refused = $true }
    Assert-True $refused 'Setup overwrote an existing host config with other options and no -Force'
    # Repeating the very same command must not need -Force: a first-time operator retries it.
    $repeated = New-HostConfig -Path $setupConfig -Bundle $setupBundle -InstallRoot (Join-Path $setupRoot 'worker') `
        -Python $setupPython -Assembly 'D:\models\robot.SLDASM' -AssemblyConfiguration Default
    Assert-True ($repeated.bundle_sha256 -eq $setupDigest) 'repeating Setup with the same options changed the config'

    $missingPythonRefused = $false
    try { New-HostConfig -Path (Join-Path $setupRoot 'other.json') -Bundle $setupBundle -Python 'C:\nope\python.exe' -Force } catch { $missingPythonRefused = $true }
    Assert-True $missingPythonRefused 'Setup accepted a python path that does not exist'

    $wrongRefused = $false
    try { Resolve-BundleDigest $setupBundle ('0' * 64) } catch { $wrongRefused = $true }
    Assert-True $wrongRefused 'Setup accepted an explicit digest that does not match the bundle'
    # Without a SHA256SUMS file the digest is pinned from the file itself, so the install is at least
    # bound to the bytes the operator has.
    Move-Item -LiteralPath (Join-Path $setupRoot 'SHA256SUMS') -Destination (Join-Path $setupRoot 'SHA256SUMS.bak')
    Assert-True ((Resolve-BundleDigest $setupBundle '') -eq $setupDigest) 'Setup did not pin the provided file digest'
} finally { Remove-Item -LiteralPath $setupRoot -Recurse -Force }

# --- a worker that runs with other settings is named, not just refused -----------------------
$hintRoot = Join-Path ([IO.Path]::GetTempPath()) ('description-endpoint-' + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $hintRoot | Out-Null
try {
    $hintInstall = Join-Path $hintRoot 'worker'
    $installedPath = Join-Path $hintRoot 'worker-host.json'
    [IO.File]::WriteAllText($installedPath, (@{
                install_root = $hintInstall; jobs_root = (Join-Path $hintInstall 'jobs'); user = 'test-user'
                host = '127.0.0.1'; port = 8799; task_name = 'description-pipeline-worker'
                python = 'C:\Python312\python.exe'; keep_versions = 3; bundle_sha256 = ('a' * 64)
                assembly = ''; configuration = ''
            } | ConvertTo-Json), [Text.UTF8Encoding]::new($false))
    $movedHost = @{ install_root = $hintInstall; host = '127.0.0.1'; port = 8801 }
    $sameHost = @{ install_root = $hintInstall; host = '127.0.0.1'; port = 8799 }

    Invoke-Expression 'function Invoke-WorkerEndpoint($ConfigHost, $Path, $Method) { return $null }'
    $quiet = (& { Write-InstalledEndpoint $installedPath $movedHost } 6>&1 | Out-String)
    Assert-True ($quiet.Trim() -eq '') "Setup warned about an endpoint with no worker on it: $quiet"
    $quietSame = (& { Write-InstalledEndpoint $installedPath $sameHost } 6>&1 | Out-String)
    Assert-True ($quietSame.Trim() -eq '') "Setup warned although only the assembly changed: $quietSame"

    Invoke-Expression 'function Invoke-WorkerEndpoint($ConfigHost, $Path, $Method) { return [pscustomobject]@{ pid = 1 } }'
    # -Width 4096 keeps Out-String from wrapping the paths inside the hint.
    $hint = (& { Write-InstalledEndpoint $installedPath $movedHost } 6>&1 | Out-String -Width 4096)
    Assert-True ($hint -match 'still listens on 127\.0\.0\.1:8799') "endpoint hint missed the running worker: $hint"
    Assert-True ($hint.Contains("-Action Stop -Config `"$installedPath`"")) "endpoint hint missed the stop command: $hint"
    Assert-True ($hint -match 'then run this command again') "endpoint hint missed the follow-up: $hint"
    # The hint is only worth anything where Setup runs it, before it hands over to Install.
    $scriptText = Get-Content -LiteralPath $scriptPath -Raw -Encoding UTF8
    Assert-True ($scriptText -match '(?m)^\s*Write-InstalledEndpoint \$installedConfig \$setup\s*$') `
        'Setup does not call Write-InstalledEndpoint'
} finally { Remove-Item -LiteralPath $hintRoot -Recurse -Force }

# --- Setup runs on a machine that has no configuration yet, and hands over to Install ----------
$flowRoot = Join-Path ([IO.Path]::GetTempPath()) ('description-setup-flow-' + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $flowRoot | Out-Null
try {
    $flowBundle = Join-Path $flowRoot 'description-worker-9.9.9-windows-x86_64.zip'
    Set-Content -LiteralPath $flowBundle -Value 'not a real bundle' -Encoding ASCII
    $flowDigest = (Get-FileHash -LiteralPath $flowBundle -Algorithm SHA256).Hash.ToLowerInvariant()
    Set-Content -LiteralPath (Join-Path $flowRoot 'SHA256SUMS') `
        -Value ("{0}  {1}" -f $flowDigest, (Split-Path -Leaf $flowBundle)) -Encoding ASCII
    $flowConfig = Join-Path $flowRoot 'worker-host.json'
    $flowInstall = Join-Path $flowRoot 'worker'
    $flowPython = (Get-Command python -ErrorAction SilentlyContinue).Source
    $flowShell = (Get-Command powershell.exe -ErrorAction SilentlyContinue).Source
    Assert-True ($null -ne $flowShell) 'the deployment suite needs powershell.exe for the Setup flow check'

    # A first-time operator has no worker-host.json at all: Setup must still run and write one.
    $ErrorActionPreference = 'Continue'
    $prepared = & $flowShell -NoProfile -ExecutionPolicy Bypass -File $scriptPath -Action Setup -Bundle $flowBundle `
        -Config $flowConfig -Python $flowPython -InstallRoot $flowInstall -NoInstall 2>&1 | Out-String
    $preparedCode = $LASTEXITCODE
    $ErrorActionPreference = 'Stop'
    Assert-True ($preparedCode -eq 0) "Setup -NoInstall failed without an existing config: $prepared"
    Assert-True ($prepared -match 'worker-host\.json') 'Setup did not report the configuration it wrote'
    Assert-True ((Get-Content -LiteralPath $flowConfig -Raw | ConvertFrom-Json).bundle_sha256 -eq $flowDigest) `
        'Setup did not record the digest it verified'

    # Without -NoInstall Setup installs what it just configured.  This archive is corrupt on
    # purpose: the run must reach the install step and report it, not stop at the config or the
    # deployment lock that the other actions take.
    $ErrorActionPreference = 'Continue'
    $installed = & $flowShell -NoProfile -ExecutionPolicy Bypass -File $scriptPath -Action Setup -Bundle $flowBundle `
        -Config $flowConfig -Python $flowPython -InstallRoot $flowInstall -Force 2>&1 | Out-String
    $installedCode = $LASTEXITCODE
    $ErrorActionPreference = 'Stop'
    Assert-True ($installedCode -ne 0) 'Setup reported success for a corrupt bundle'
    Assert-True ($installed -notmatch 'deployment\.lock|host config not found') "Setup stuck before installing: $installed"
    Assert-True ($installed -match 'not a readable ZIP') "Setup did not explain the corrupt bundle: $installed"
    Assert-True ($installed -match 'install failed with exit code') "Setup did not report the failed install: $installed"

    # A machine that already runs this bundle with these settings has nothing to install, so the
    # third run of the same command must succeed without touching the archive at all.
    New-Item -ItemType Directory -Force -Path (Join-Path $flowInstall 'versions\9.9.9') | Out-Null
    Copy-Item -LiteralPath $flowConfig -Destination (Join-Path $flowInstall 'worker-host.json') -Force
    [IO.File]::WriteAllText((Join-Path $flowInstall 'current.json'),
        (@{ version = '9.9.9' } | ConvertTo-Json), [Text.UTF8Encoding]::new($false))
    $ErrorActionPreference = 'Continue'
    $again = & $flowShell -NoProfile -ExecutionPolicy Bypass -File $scriptPath -Action Setup -Bundle $flowBundle `
        -Config $flowConfig -Python $flowPython -InstallRoot $flowInstall 2>&1 | Out-String
    $againCode = $LASTEXITCODE
    $ErrorActionPreference = 'Stop'
    Assert-True ($againCode -eq 0) "Setup failed on an installed machine: $again"
    Assert-True ($again -match 'already installed with these settings') "Setup reinstalled: $again"
    Assert-True ($again -notmatch 'not a readable ZIP') 'Setup read the archive although nothing was to install'
} finally { Remove-Item -LiteralPath $flowRoot -Recurse -Force }

# --- Doctor shows the notices the worker sends --------------------------------
# `/doctor` carries `advisories` (today `cad_save_flag_set`: the documents SolidWorks would
# prompt to save).  The Python CLI prints them and the Windows entry has to as well, because
# the first-use guide tells the operator that Doctor reports them.
function Get-CurrentVersion($Root) { '1.2.3' }
function Test-LocalPipeline($Venv) { @{ status = 'ok'; detail = 'mujoco 3.0' } }
function Test-SolidWorksProcess { @{ running = $true; id = 7; window = 'SolidWorks' } }
function Get-TaskState($TaskName) { '0/Ready' }
function Invoke-WorkerEndpoint($ConfigHost, $Path, $Method, [int]$Timeout) {
    if ($Path -like '/doctor*') {
        return [pscustomobject]@{
            installed = $true; worker_alive = $true; solidworks_reachable = $true; cad_collectable = $true
            advisories = @([pscustomobject]@{
                code = 'cad_save_flag_set'
                message = 'SolidWorks reports unsaved changes for a document opened read-only'
                documents = @('C:\models\robot.SLDASM')
                count = 1
            })
        }
    }
    return [pscustomobject]@{ worker_version = '1.2.3' }
}
$script:printed = New-Object System.Collections.Generic.List[string]
function Write-Step([string]$Text) { $script:printed.Add('== ' + $Text) }
function Write-Info([string]$Text) { $script:printed.Add([string]$Text) }
$doctorHost = @{ install_root = 'C:\probe\worker'; task_name = 'fixture-worker'; configuration = 'Default' }
$doctorReport = Invoke-Doctor $doctorHost 'C:\models\robot.SLDASM'
Assert-True ($doctorReport.cad_collectable) 'Doctor stopped calling a collection with a notice collectable'
Assert-True (@($doctorReport.advisories).Count -eq 1) 'Doctor dropped the advisory from its summary'
Assert-True (@($script:printed | Where-Object { $_ -like 'notice*unsaved changes*' }).Count -eq 1) `
    'Doctor did not print the save-flag notice'
Assert-True (@($script:printed | Where-Object { $_ -like '*robot.SLDASM*' }).Count -eq 1) `
    'Doctor did not name the document in the notice'

# An older worker answers without the field and must still work.
function Invoke-WorkerEndpoint($ConfigHost, $Path, $Method, [int]$Timeout) {
    if ($Path -like '/doctor*') {
        return [pscustomobject]@{
            installed = $true; worker_alive = $true; solidworks_reachable = $true; cad_collectable = $true
        }
    }
    return [pscustomobject]@{ worker_version = '1.2.3' }
}
$script:printed.Clear()
$quietReport = Invoke-Doctor $doctorHost 'C:\models\robot.SLDASM'
Assert-True (@($quietReport.advisories).Count -eq 0) 'a report without advisories produced one'
Assert-True (@($script:printed | Where-Object { $_ -like 'notice*' }).Count -eq 0) `
    'a report without advisories printed a notice'
Assert-True ($quietReport.cad_collectable) 'a report without advisories lost collectability'

'PowerShell deployment checks passed: parser, required bundle digest, activation, rollback, busy refusal (runner and recovered jobs), idempotent bundle reuse, idle process isolation, endpoint-owner identity, a worker that keeps answering, guided setup, guided setup without a host config, doctor advisories'
