# Behavioural checks for the one-click submit entry (no ssh, no worker, no CAD).
# The real entry uses one ssh session with -R; here ssh/HTTP are functions.
$ErrorActionPreference = 'Stop'
$scriptPath = Join-Path $PSScriptRoot '../../src/description_pipeline/sources/solidworks/deploy/submit.ps1'
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
$chinese = -join ([char[]]@(26356,26032,32,77,105,99,114,111,98,97,110,32,20505,36873,65288,20013,25991,27979,35797,65289))
function Rejects([scriptblock]$Body) {
    try { & $Body | Out-Null; return $false } catch { return $true }
}
function Rejection([scriptblock]$Body) {
    try { & $Body | Out-Null; return '' } catch { return $_.Exception.Message }
}

# --- the shipped template is local-first and holds no personal host -----------
$example = Resolve-Path (Join-Path $PSScriptRoot '../../src/description_pipeline/sources/solidworks/deploy/submit-host.example.json')
$template = Get-Content -LiteralPath $example -Raw | ConvertFrom-Json
Assert-True ($null -eq $template.build_host) 'the shipped template is local-first: no build host'
Assert-True ($null -eq $template.remote_python) 'the shipped template carries no build-host python'
Assert-True ($template.model_root -like 'C:\Users\*') 'template model_root is a Windows path'
Assert-True ($template.profile -eq 'kinematics') 'template profile is generic'
Assert-True ([bool]$template.message) 'template carries a default message'
Assert-True ($null -eq $template.remote_command) 'there is no free-form remote command any more'
$local = Read-HostConfig $example
Assert-True (-not (Test-RemoteMode $local)) 'the shipped template selects local mode'
Assert-True ((Resolve-Plan $local $null $null).model_root -eq $template.model_root) 'the local model root passes through'
Assert-True (Rejects { Resolve-Plan $local '/srv/description/models/myrobot' $null }) 'a POSIX model root is refused in local mode'

# --- a legacy remote config still validates and stays remote ------------------
$temp = Join-Path ([IO.Path]::GetTempPath()) ('oneclick-' + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Force -Path $temp | Out-Null
$unicodePath = Join-Path $temp 'unicode.json'
$unicodeRoot = 'C:\models\' + $chinese
[IO.File]::WriteAllText($unicodePath, (@{
    model_root = $unicodeRoot; profile = 'kinematics'; message = $chinese
} | ConvertTo-Json), [Text.UTF8Encoding]::new($false))
$unicodeConfig = Read-HostConfig $unicodePath
Assert-True ($unicodeConfig.model_root -eq $unicodeRoot) 'BOM-less UTF-8 preserves the model path'
Assert-True ((Get-Message $unicodeConfig '') -eq $chinese) 'BOM-less UTF-8 preserves the commit message'
$remotePath = Join-Path $temp 'remote.json'
@{
    build_host    = 'description-build'
    identity_file = ''
    remote_python = '/opt/description/venv/bin/python'
    model_root    = '/srv/description/models/myrobot'
    profile       = 'kinematics'
    message       = 'Update model candidate'
    worker_port   = 8765
    remote_port   = 8765
} | ConvertTo-Json | Set-Content -Encoding ASCII $remotePath
$config = Read-HostConfig $remotePath
Assert-True (Test-RemoteMode $config) 'a non-empty build_host selects remote mode'
Assert-True ($config.build_host -eq 'description-build') 'the legacy ssh alias is used as-is'
Assert-True ($config.remote_python -eq '/opt/description/venv/bin/python') 'the legacy build-host python is kept'
Assert-True ($config.model_root -eq '/srv/description/models/myrobot') 'the legacy remote model root is a build-host path'
Assert-True ($config.profile -eq 'kinematics') 'template profile is generic'
Assert-True ($null -eq $config.remote_command) 'there is no free-form remote command any more'

# --- quoting / path rules ----------------------------------------------------
Assert-True ((Quote-PosixArg 'x' '/srv/a b/robot') -eq "'/srv/a b/robot'") 'a path with a space is single-quoted'
foreach ($value in @("a'b", "a`nb")) {
    Assert-True (Rejects { Quote-PosixArg 'x' $value }) "unsafe value '$value' must be refused"
}
Assert-True (Rejects { Assert-AbsolutePosixPath 'model_root' 'C:\models\robot' }) 'a Windows path must be refused as a build-host path'
Assert-True ((Assert-AbsolutePosixPath 'model_root' '/srv/description/models/myrobot') -eq '/srv/description/models/myrobot') 'a build-host path passes'

# --- host / profile validation ----------------------------------------------
Assert-True ((Assert-SshHostName 'build_host' 'description-build') -eq 'description-build') 'a plain alias passes'
foreach ($hostValue in @('-oProxyCommand=calc.exe', '-F', 'a b', 'host;rm -rf /', '', '-')) {
    Assert-True (Rejects { Assert-SshHostName 'build_host' $hostValue }) "option-like or unsafe host '$hostValue' must be refused"
}
Assert-True ((Assert-ProfileName 'kinematics') -eq 'kinematics') 'a lowercase profile passes'
foreach ($profileValue in @('Kinematics', '../etc/passwd', 'a b', '', '-x', ('p' * 65))) {
    Assert-True (Rejects { Assert-ProfileName $profileValue }) "invalid profile '$profileValue' must be refused"
}

# --- command-line overrides go through the same validation -------------------
Assert-True ((Resolve-Plan $config $null $null).profile -eq 'kinematics') 'the configured profile is the default'
Assert-True ((Resolve-Plan $config '/srv/description/models/other' 'simulation').model_root -eq '/srv/description/models/other') 'a build-host model root override passes'
Assert-True (Rejects { Resolve-Plan $config 'C:\models\robot' $null }) 'a Windows model root override must be refused'
Assert-True (Rejects { Resolve-Plan $config $null 'Kinematics' }) 'an invalid profile override must be refused'
Assert-True ((Rejection { Resolve-Plan $config $null '../escape' }) -like '*profile*') 'the profile override failure names the profile'
$badHost = Join-Path $temp 'bad-host.json'
(Get-Content -LiteralPath $remotePath -Raw | ConvertFrom-Json |
    Add-Member -NotePropertyName build_host -NotePropertyValue '-oProxyCommand=calc.exe' -Force -PassThru) |
    ConvertTo-Json | Set-Content -Encoding ASCII $badHost
Assert-True (Rejects { Read-HostConfig $badHost }) 'an option-like build_host in the config must be refused'
$badRoot = Join-Path $temp 'bad-root.json'
($template.PSObject.Copy() |
    Add-Member -NotePropertyName model_root -NotePropertyValue 'models\myrobot' -Force -PassThru) |
    ConvertTo-Json | Set-Content -Encoding ASCII $badRoot
Assert-True (Rejects { Read-HostConfig $badRoot }) 'a relative Windows model root must be refused in local mode'
Assert-True ((Rejection { Read-HostConfig $badRoot }) -like '*absolute Windows path*') 'the local path refusal names the rule'

# --- remote command: one python -m call, every argument quoted ---------------
$remote = Build-RemoteCommand $config $config.model_root $config.profile
Assert-True ($remote.StartsWith("'/opt/description/venv/bin/python' -m description_pipeline model update")) 'remote command calls the module with the pinned python'
Assert-True ($remote -like "*--root '/srv/description/models/myrobot'*") 'model root is quoted'
Assert-True ($remote -like "*--profile 'kinematics'*") 'profile is quoted'
Assert-True ($remote -like '*--message-file - *') 'the message arrives on stdin'
Assert-True ($remote -like "*--expect-worker-url 'http://127.0.0.1:8765'*") 'the tunnel url is bound'
$withReference = Build-RemoteCommand $config $config.model_root $config.profile '/srv/approved refs/mechanism.json'
Assert-True ($withReference -like "*--mechanical-reference '/srv/approved refs/mechanism.json'*") 'the reference is quoted on the execution host'
Assert-True (Rejects { Resolve-Plan $config $null $null 'C:\approved\mechanism.json' }) 'remote reference paths belong to the build host'

# --- message defaulting ------------------------------------------------------
Assert-True ((Get-Message ([pscustomobject]@{ message = 'from config' }) '') -eq 'from config') 'configured message is the default'
Assert-True ((Get-Message ([pscustomobject]@{ message = 'x' }) 'from flag') -eq 'from flag') 'the flag wins'
Assert-True (Rejects { Get-Message ([pscustomobject]@{}) '' }) 'no message anywhere must be refused'

# --- worker health gate ------------------------------------------------------
function Test-Health($health) {
    $script:health = $health
    try { Test-WorkerHealth ([pscustomobject]@{ worker_port = 8765 }) | Out-Null; return 'ok' }
    catch { return $_.Exception.Message }
}
function Invoke-RestMethod { param($Method, $Uri, $TimeoutSec) return $script:health }
$ready = [pscustomobject]@{ status = 'ok'; maintenance = $false; cad_recovery_required = $false;
    runner = @{ current = $null; queued = @(); alive = $true }; jobs = @{ queued = 0; running = 0 } }
Assert-True ((Test-Health $ready) -eq 'ok') 'an idle worker passes'
$busy = $ready.PSObject.Copy(); $busy.runner = @{ current = 'job-1'; queued = @(); alive = $true }
Assert-True ((Test-Health $busy) -like '*active_or_queued_work*') 'a running job must block'
$recovered = $ready.PSObject.Copy(); $recovered.jobs = @{ queued = 1; running = 0 }
Assert-True ((Test-Health $recovered) -like '*active_or_queued_work*') 'a recovered queued job must block'
$maintenance = $ready.PSObject.Copy(); $maintenance.maintenance = $true
Assert-True ((Test-Health $maintenance) -like '*maintenance*') 'maintenance must block'
$recovery = $ready.PSObject.Copy(); $recovery.cad_recovery_required = $true
Assert-True ((Test-Health $recovery) -like '*cad_recovery_required*') 'pending CAD recovery must block'
$dead = $ready.PSObject.Copy(); $dead.runner = @{ current = $null; queued = @(); alive = $false }
Assert-True ((Test-Health $dead) -like '*runner_alive=false*') 'a dead runner must block'

# --- record parsing and the "no record means unknown" rule --------------------
$bare = ConvertFrom-Record '{"ok":true,"pull_request":"https://example/pr/9"}'
Assert-True ($bare.pull_request -eq 'https://example/pr/9') 'stdout starting with { must parse'
Assert-True ($null -eq (ConvertFrom-Record 'not json at all')) 'garbage must not parse'
$mixed = "prepared the workspace`n{`"ok`":true,`"pull_request`":`"https://example/pr/2`"}`nwarn: written after the record"
Assert-True ((ConvertFrom-Record $mixed).pull_request -eq 'https://example/pr/2') 'text before and after the record must not hide it'
$nested = '{"detail":{"note":"a } brace in a string"},"ok":true,"pull_request":"https://example/pr/3"}'
Assert-True ((ConvertFrom-Record $nested).pull_request -eq 'https://example/pr/3') 'a brace inside a string must not end the record'
$realistic = 'pushed the candidate' + "`n" +
    '{"ok":true,"pull_request":"https://github.com/x/y/pull/1","central_validation":{"state":"dispatched"}}' + "`n" +
    'warn: written after the record'
$parsed = ConvertFrom-Record $realistic
Assert-True ($parsed.ok -eq $true) 'the top-level record must win over its nested payload'
Assert-True ($parsed.pull_request -eq 'https://github.com/x/y/pull/1') 'the url comes from the top-level record'
Assert-True ($parsed.central_validation.state -eq 'dispatched') 'the nested payload stays reachable inside the record'

# --- success needs a true flag, a real url and exit 0 ------------------------
$ready = '{"ok":true,"pull_request":"https://example/pr/1","central_validation":{"state":"dispatched"}}'
Assert-True ((Rejection { Show-RemoteResult -StdOut $ready -StdErr '' -ExitCode 0 }) -eq '') 'a complete record with exit 0 is success'
$localReady = '{"ok":true,"pull_request":"https://example/pr/1","central_validation":{"state":"not_requested"}}'
$localOutput = & { Show-RemoteResult -StdOut $localReady -StdErr '' -ExitCode 0 } 6>&1 | Out-String
Assert-True ($localOutput -notmatch 'central\s*:') 'local submission must not display pending CI'
foreach ($record in @('', 'not json at all', '{}', '{"ok":true}', '{"passed":true}',
        '{"ok":true,"pull_request":""}', '{"ok":false,"pull_request":"https://example/pr/1"}')) {
    Assert-True (Rejects { Show-RemoteResult -StdOut $record -StdErr '' -ExitCode 0 }) "record '$record' must not be reported as success"
}
Assert-True (Rejects { Show-RemoteResult -StdOut $ready -StdErr '' -ExitCode 7 }) 'a non-zero exit must fail even with a good record'
$refusal = '{"passed":false,"error":"PipelineError","message":"workspace has uncommitted changes"}'
Assert-True ((Rejection { Show-RemoteResult -StdOut '' -StdErr $refusal -ExitCode 1 }) -like '*uncommitted*') 'a refusal on stderr must surface its message'
Assert-True (Rejects { Show-RemoteResult -StdOut '' -StdErr 'plain stderr noise' -ExitCode 0 }) 'stderr without a record is not success'

# --- real native stdin regression: PS5 must send UTF-8 bytes on the pipe ------
# Explicit discovery only: no personal or stale interpreter paths.
$python = $null
foreach ($candidate in @('python', 'python3')) {
    $found = Get-Command $candidate -ErrorAction SilentlyContinue
    if ($found) { $python = $found.Source; break }
}
if (-not $python) {
    $launcher = Get-Command py -ErrorAction SilentlyContinue
    if ($launcher) {
        $resolved = & $launcher.Source -3 -c 'import sys; print(sys.executable)' 2>$null
        if ($LASTEXITCODE -eq 0 -and $resolved) { $python = ([string]$resolved).Trim() }
    }
}
Assert-True $python 'the native stdin regression needs python on PATH (python, python3 or the py launcher)'
$probe = 'import json,sys;b=sys.stdin.buffer.read();print(len(b));print(json.dumps(dict(ok=True,pull_request=''native'')));sys.stderr.write(''warn\n'')'
$encodingBefore = $OutputEncoding.WebName
$native = Invoke-NativeCommand -FilePath $python -Arguments @('-c', $probe) -InputText $chinese
$expected = [Text.Encoding]::UTF8.GetByteCount($chinese)
$received = 0
foreach ($line in ($native.stdout -split "`n")) { if ([int]::TryParse($line.Trim(), [ref]$received) -and $received -gt 1) { break } }
Assert-True ($native.exit_code -eq 0) 'the native probe must exit 0'
Assert-True ($received -ge $expected -and $received -le $expected + 2) "native stdin must carry UTF-8 bytes (expected ~$expected, got $received)"
Assert-True ($native.stderr -like '*warn*') 'native stderr must be captured apart from stdout'
Assert-True ((ConvertFrom-Record $native.stdout).pull_request -eq 'native') 'the native stdout record must parse'
Assert-True ((ConvertFrom-Record ($native.stdout + "`n" + $native.stderr)).pull_request -eq 'native') 'a record with stderr text after it must still parse'
Assert-True ($OutputEncoding.WebName -eq $encodingBefore) 'the helper must restore $OutputEncoding'
$failing = 'import json,sys;print(json.dumps(dict(ok=True,pull_request=''https://example/pr/7'')));sys.stderr.write(''failing on purpose\n'');sys.exit(7)'
$failed = Invoke-NativeCommand -FilePath $python -Arguments @('-c', $failing) -InputText ''
Assert-True ($failed.exit_code -eq 7) "a child that fails must report its own exit code (got $($failed.exit_code))"
Assert-True ((ConvertFrom-Record $failed.stdout).pull_request -eq 'https://example/pr/7') 'the record of a failed child must stay readable'
Assert-True (Rejects { Show-RemoteResult -StdOut $failed.stdout -StdErr $failed.stderr -ExitCode $failed.exit_code }) 'a failed child must never be reported as success'

# --- local mode: Windows paths, installed runtime and the public arguments ----
Assert-True ((Assert-AbsoluteWindowsPath 'model_root' 'C:\models\myrobot') -eq 'C:\models\myrobot') 'a drive-letter path passes'
Assert-True ((Assert-AbsoluteWindowsPath 'model_root' 'C:/Users/Mi/models/myrobot') -eq 'C:/Users/Mi/models/myrobot') 'a forward-slash drive path passes'
Assert-True ((Assert-AbsoluteWindowsPath 'model_root' '\\srv\share\models\myrobot') -eq '\\srv\share\models\myrobot') 'a UNC path passes'
foreach ($badPath in @('models\myrobot', '/srv/description/models/myrobot', '', 'C:')) {
    Assert-True (Rejects { Assert-AbsoluteWindowsPath 'model_root' $badPath }) "local mode refuses '$badPath'"
}
$localArguments = Build-LocalArguments 'C:\models\myrobot' 'kinematics'
$localLine = ($localArguments -join ' ')
Assert-True ($localLine -eq '-m description_pipeline model update --root C:\models\myrobot --profile kinematics --message-file -') 'local mode runs the public update command'
Assert-True ($localLine -notlike '*--expect-worker-url*') 'local mode does not pin a tunnel url'
Assert-True ($localLine -notlike '*--worker-host*') 'local mode never selects a worker host'
Assert-True ($localLine -notlike '*--reuse-source*') 'local mode leaves the source mode to the model'
$referencePath = 'C:\approved refs\mechanism.json'
$withReference = Build-LocalArguments 'C:\models\myrobot' 'kinematics' $referencePath
Assert-True ($withReference[-2] -eq '--mechanical-reference' -and $withReference[-1] -eq $referencePath) 'a local reference path stays a single native argument'
$configuredReference = $local.PSObject.Copy()
$configuredReference | Add-Member -NotePropertyName mechanical_reference -NotePropertyValue $referencePath
Assert-True ((Resolve-Plan $configuredReference $null $null).mechanical_reference -eq $referencePath) 'the reference may be selected in the operator host config'
Assert-True ((Resolve-Plan $configuredReference $null $null 'C:\approved\other.json').mechanical_reference -eq 'C:\approved\other.json') 'an explicit reference flag overrides the operator config'
Assert-True (Rejects { Resolve-Plan $local $null $null 'relative-reference.json' }) 'local reference paths must be absolute'
# runtime discovery: explicit override, installed worker venv, and the refusal text
Assert-True ((Resolve-LocalRuntime ([pscustomobject]@{ python = $python })) -eq $python) 'an explicit python override is used as-is'
$install = Join-Path $temp 'install'
$versionDir = Join-Path $install 'versions\9.9.9'
New-Item -ItemType Directory -Force -Path (Join-Path $versionDir 'venv\Scripts') | Out-Null
Set-Content -LiteralPath (Join-Path $versionDir 'venv\Scripts\python.exe') -Value 'stub' -Encoding ASCII
Set-Content -LiteralPath (Join-Path $install 'current.json') -Value '{"version":"9.9.9"}' -Encoding ASCII
$resolved = Resolve-LocalRuntime ([pscustomobject]@{ install_root = $install })
Assert-True ($resolved -eq (Join-Path $versionDir 'venv\Scripts\python.exe')) 'the installed worker venv is the default runtime'
$emptyRoot = Join-Path $temp 'empty-install'
New-Item -ItemType Directory -Force -Path $emptyRoot | Out-Null
Assert-True ((Rejection { Resolve-LocalRuntime ([pscustomobject]@{ install_root = $emptyRoot }) }) -like '*worker.ps1 -Action Install*') 'a missing runtime names the installer action'
Assert-True ((Find-Executable 'python' @()) -ne $null) 'Find-Executable sees PATH entries'
Assert-True ($null -eq (Find-Executable 'definitely-not-a-tool-xyz' @('%TEMP%\nope.exe'))) 'Find-Executable returns nothing when the tool is absent'

$script:seenMessage = $null
$script:seenArguments = $null
function ssh {
    $script:seenArguments = $args
    $script:seenMessage = [string]($input | Out-String)
    $global:LASTEXITCODE = 0
    Write-Output '{"ok":true,"pull_request":"https://example/pr/1","central_validation":{"state":"dispatched"}}'
}
$result = Invoke-RemoteUpdate $config $remote $chinese
Assert-True ($script:seenMessage.Trim() -eq $chinese) 'the commit message must survive the pipeline unchanged'
Assert-True (($script:seenArguments -join ' ') -like '*-T *-R 127.0.0.1:8765:127.0.0.1:8765*') 'one ssh session carries -R and the remote command'
Assert-True (($script:seenArguments -join ' ') -like '*description-build*') 'the ssh alias is used as-is'
Assert-True ($result.exit_code -eq 0) 'a successful ssh call reports exit 0'
$record = ConvertFrom-Record $result.stdout
Assert-True ($record.pull_request -eq 'https://example/pr/1') 'the JSON record is parsed from the tail of the output'
Assert-True ([Console]::OutputEncoding.WebName -ne 'us-ascii') 'console encoding must not be left as ascii'

Remove-Item -Recurse -Force $temp
'PowerShell submit-entry checks passed: parser, local-first template, remote compatibility, POSIX quoting, single-ssh -R call, UTF-8 message, health gates, local runtime discovery'
