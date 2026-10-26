# Runs the candidate script's REAL Invoke-Main for one promote scenario (offline).
#
#   invoke-main-harness.ps1 -ScriptPath <lifecycle.ps1> -Scenario <name> -Root <temp dir>
#
# Only the OS/network boundaries and the settle retry timer are stubbed over synthetic temp
# files; the candidate's own Invoke-Main, Start-OrRefuse, refusal interpreters, restore path and
# health probe run for real (Wait-Settled runs real and fails fast on the stub task state).
# Scenarios: unknown_owner | startable_second_refusal | promote_missing_stage_receipt |
# stage_unknown_key_refused | stage_legacy_targets_refused | stage_duplicate_key_refused |
# stage_acl_boundary | promote_prestop_identity_mismatch |
# promote_candidate_mismatch | promote_source_mismatch.
# Prints one JSON object (scenario, message, calls, sleeps, receipt) and exits 0.
param(
  [Parameter(Mandatory=$true)][string]$ScriptPath,
  [Parameter(Mandatory=$true)][string]$Scenario,
  [Parameter(Mandatory=$true)][string]$Root
)
$ErrorActionPreference = 'Stop'
. $ScriptPath -Verb promote -Binding 'unused'

$newGeneration = Join-Path $Root 'new'
$oldGeneration = Join-Path $Root 'old'
$null = New-Item -ItemType Directory -Path $newGeneration -Force
$null = New-Item -ItemType Directory -Path $oldGeneration -Force
$oldConfig = Join-Path $oldGeneration 'endpoint.json'
$newConfig = Join-Path $newGeneration 'endpoint.json'
$token = Join-Path $Root 'local-test-token'
[System.IO.File]::WriteAllText($token, 'synthetic-offline-token')
$configuration = @{
  schema_version='solidworks-to-urdf.endpoint/v1';
  state_root=$Root; port=8765; token_file=$token;
  package_root=(Join-Path $Root 'packages'); output_root=(Join-Path $Root 'results')}
if ($Scenario -eq 'stage_unknown_key_refused') {
  # A key the endpoint schema does not define stops the stage.
  $configuration['bogus_key'] = $true
}
if ($Scenario -eq 'stage_legacy_targets_refused') {
  # A retired publication-routing key is no longer a supported configuration shape.
  $configuration['targets'] = @{nd_fixture = @{repository_slug = 'a/b'; base = 'feature/arm'}}
}
if ($Scenario -eq 'stage_duplicate_key_refused') {
  $configurationText = '{"schema_version":"solidworks-to-urdf.endpoint/v1","port":8765,' +
    '"token_file":"' + ($token -replace '\\', '/') + '","port":8766}'
} else {
  $configurationText = $configuration | ConvertTo-Json -Depth 4
}
[System.IO.File]::WriteAllText($oldConfig, $configurationText)
[System.IO.File]::WriteAllText($newConfig, $configurationText)
[System.IO.File]::WriteAllText(
  (Join-Path $newGeneration 'stage-receipt.json'),
  (@{passed=$true; verb='stage'; generation=(Normalize-PathIdentity $newGeneration);
     launch_contract='python_module_v1'; version='1.5.0';
     source_commit=('b' * 40); source_sha256=('b' * 64)} | ConvertTo-Json -Compress))
[System.IO.File]::WriteAllText((Join-Path $Root '.endpoint.lock'), '4242')
if ($Scenario -eq 'promote_missing_stage_receipt') {
  Remove-Item -LiteralPath (Join-Path $newGeneration 'stage-receipt.json') -Force
}
$bindingPath = Join-Path $Root 'binding.json'
$archiveName = 'synthetic.zip'
$archiveSha = 'synthetic'
if ($Scenario -eq 'stage_acl_boundary') {
  $archiveMember = Join-Path $Root 'archive-member.txt'
  [System.IO.File]::WriteAllText($archiveMember, 'synthetic archive member')
  Compress-Archive -LiteralPath $archiveMember -DestinationPath (Join-Path $Root $archiveName)
  $archiveSha = (Get-FileHash -LiteralPath (Join-Path $Root $archiveName) -Algorithm SHA256).Hash.ToLower()
}
@{task_name='offline-endpoint'; state_root=$Root; generation=$newGeneration; dir=$Root;
  archive=$archiveName; artifact_sha256=$archiveSha; port=8765; package_root=$Root; output_root=$Root;
  version='1.5.0'; source_commit=('b' * 40); source_sha256=('b' * 64)} |
  ConvertTo-Json | Set-Content -LiteralPath $bindingPath
Copy-Item -LiteralPath $bindingPath -Destination (Join-Path $Root 'windows-binding.json')
if ($Scenario -notlike 'stage_*') {
  $candidateSha = (Get-FileHash -LiteralPath $newConfig -Algorithm SHA256).Hash.ToLower()
  $sourceSha = (Get-FileHash -LiteralPath $oldConfig -Algorithm SHA256).Hash.ToLower()
  if ($Scenario -eq 'promote_candidate_mismatch') { $candidateSha = ('0' * 64) }
  if ($Scenario -eq 'promote_source_mismatch') { $sourceSha = ('1' * 64) }
  [System.IO.File]::WriteAllText(
    (Join-Path $newGeneration 'candidate-identity.json'),
    (@{expected=$true; config_digest=('2' * 64); config_sha256=$candidateSha; source_config=$oldConfig;
       source_config_sha256=$sourceSha; version='1.5.0'; source_commit=('b' * 40);
       source_sha256=('b' * 64)} | ConvertTo-Json -Compress))
}

$script:OldAction = [pscustomobject]@{
  Execute = (Join-Path $oldGeneration 'runtime/Scripts/python.exe')
  Arguments = ('-I -B -m description_pipeline serve --config "' + $oldConfig + '"')
  WorkingDirectory = $Root}
$script:NewAction = [pscustomobject]@{
  Execute = (Join-Path $newGeneration 'runtime/Scripts/python.exe')
  Arguments = (Runtime-LauncherArguments $newGeneration)
  WorkingDirectory = $Root}
$script:CurrentAction = if ($Scenario -like 'rollback_*') {
  $script:NewAction
} else {
  $script:OldAction
}
$script:RegisteredActions = @($script:CurrentAction)
$script:TaskState = 'Running'
$script:Calls = [System.Collections.Generic.List[string]]::new()
$script:Sleeps = [System.Collections.Generic.List[int]]::new()
$script:AccountEnv = @{}
$script:ListenerCallCount = 0

function Get-AccountEnvironment { param([string]$Name) return [string]$script:AccountEnv[$Name] }
function Set-AccountEnvironment { param([string]$Name, [string]$Value) $script:AccountEnv[$Name] = $Value }
function Get-ServedStateRoot { param([string]$configPath) return [string]$Root }
function Get-ScheduledTask {
  [CmdletBinding()] param([string]$TaskName)
  [pscustomobject]@{
    TaskName=$TaskName
    TaskPath='\'
    State=$script:TaskState
    Principal=[pscustomobject]@{UserId='fixture-user'}
    Settings=[pscustomobject]@{MultipleInstances='IgnoreNew'}
    Actions=@($script:RegisteredActions)
  }
}
function Start-ScheduledTask {
  [CmdletBinding()] param([string]$TaskName)
  $script:TaskState = if (
    $Scenario -eq 'promote_4ed_success' -or
    (Normalize-PathIdentity $script:CurrentAction.Execute) -like
      ((Normalize-PathIdentity $oldGeneration) + '/*')
  ) { 'Running' } else { 'Ready' }
  $script:Calls.Add('start:' + $script:CurrentAction.Execute)
}
function Stop-ScheduledTask {
  [CmdletBinding()] param([string]$TaskName)
  $script:TaskState = 'Ready'
  $script:Calls.Add('stop')
}
function Get-NetTCPConnection { [CmdletBinding()] param([int]$LocalPort, [string]$State) return $null }
function Get-CimInstance {
  [CmdletBinding()] param([string]$ClassName, [string]$Filter)
  return $null
}
function Export-ScheduledTask { param([string]$TaskName) '<Task/>' }
function New-ScheduledTaskAction {
  param([string]$Execute, [string]$Argument, [string]$WorkingDirectory)
  [pscustomobject]@{ Execute=$Execute; Arguments=$Argument; WorkingDirectory=$WorkingDirectory }
}
function Set-ScheduledTask {
  param([string]$TaskName, [object]$Action)
  $Action | Add-Member -NotePropertyName WorkingDirectory `
    -NotePropertyValue $binding.state_root -Force
  $script:CurrentAction=$Action
  $script:RegisteredActions=@($Action)
  $script:Calls.Add('set:' + $Action.Execute)
}
function Register-ScheduledTask {
  [CmdletBinding()] param([string]$TaskName, [string]$TaskPath, [string]$Xml, [switch]$Force)
  $script:CurrentAction=$script:OldAction
  if ($Scenario -eq 'restore_foreign_one_action') {
    $script:ForeignAction=[pscustomobject]@{
      Execute=(Join-Path $Root 'foreign.exe')
      Arguments='foreign-action'
      WorkingDirectory=$Root
    }
    $script:CurrentAction=$script:ForeignAction
    $script:RegisteredActions=@($script:ForeignAction)
  } elseif ($Scenario -eq 'restore_multi_action') {
    $script:RegisteredActions=@(
      $script:OldAction,
      [pscustomobject]@{
        Execute=(Join-Path $Root 'foreign.exe')
        Arguments='foreign-action'
        WorkingDirectory=$Root
      })
  } else {
    $script:RegisteredActions=@($script:OldAction)
  }
  $script:Calls.Add('register:old')
}
function Get-TaskProjection {
  param($Task)
  Assert-Single-TaskAction $Task
  $action = $Task.Actions[0]
  $xmlText = '<Task>' + [string]$action.Execute + '|' + [string]$action.Arguments + '</Task>'
  return [pscustomobject]@{
    task_path=[string]$Task.TaskPath
    task_name=[string]$Task.TaskName
    state=[string]$Task.State
    xml=$xmlText
    xml_sha256=(Get-TextSha256 $xmlText)
    semantic_sha256=(Get-TextSha256 ('semantic|' + [string]$action.Execute + '|' +
      [string]$action.Arguments + '|' + [string]$action.WorkingDirectory))
    security_descriptor_sha256=('a' * 64)
    triggers_sha256=('b' * 64)
    principal=[pscustomobject]@{
      user='fixture-user'; sid='S-1-5-21-offline-harness';
      logon_type='Interactive'; run_level='HighestAvailable';
      group_id=''; administrator=$true}
    action=[pscustomobject]@{
      execute=[string]$action.Execute
      arguments=[string]$action.Arguments
      working_directory=[string]$action.WorkingDirectory}
    settings=[pscustomobject]@{multiple_instances='IgnoreNew'}
  }
}
function Get-EndpointIdentity {
  param($Task, $TaskProjection, [string]$SshHost)
  $generation = Get-TaskGeneration $Task
  $configPath = Join-Path $generation 'endpoint.json'
  $config = Read-Json $configPath
  $source = (Normalize-PathIdentity $generation) -eq (Normalize-PathIdentity $oldGeneration)
  $release = if ($source) {
    [pscustomobject]@{
      version='1.5.0';
      source_commit='4ed0f03db9462c084d63cc7106125a794906ba30';
      source_sha256='d199dd48015f3908115bf6d181ac4deb590a15c9532e1e83145b684d9a5da13f';
      role='native'}
  } else {
    [pscustomobject]@{
      version='1.5.0'; source_commit=('b' * 40);
      source_sha256=('b' * 64); role='native'}
  }
  return [pscustomobject]@{
    endpoint_root=(Normalize-PathIdentity $generation)
    config_path=(Normalize-PathIdentity $configPath)
    config_sha256=(Sha256 $configPath)
    config_digest=if ($source) { '1' * 64 } else { '2' * 64 }
    package_root=(Normalize-PathIdentity ([string]$config.package_root))
    output_root=(Normalize-PathIdentity ([string]$config.output_root))
    state_root=(Normalize-PathIdentity ([string]$config.state_root))
    port=[int]$config.port
    install_id_path='C:/state/install-id'
    install_id=if ($source) { 'win-' + ('1' * 32) } else { 'win-' + ('2' * 32) }
    release_identity_sha256=(Get-ReleaseIdentitySha256 $release)
    release=$release
  }
}
function Get-ListenerEvidence {
  param([object]$binding, $TaskProjection)
  $script:ListenerCallCount++
  $listenerPid = if (
    $Scenario -eq 'promote_prestop_identity_mismatch' -and
    $script:ListenerCallCount -ge 2
  ) { 4243 } else { 4242 }
  return [pscustomobject]@{
    listener_pid=$listenerPid
    process_start_utc='2026-10-27T00:00:00Z'
    process_command_sha256=('c' * 64)
    process_user_sid=$TaskProjection.principal.sid}
}
function Read-EndpointHealth {
  param([string]$configPath)
  $generation = Split-Path -Parent $configPath
  if ($Scenario -eq 'rollback_restore_failure' -and
      (Normalize-PathIdentity $generation) -eq (Normalize-PathIdentity $oldGeneration)) {
    throw 'the restored source endpoint did not answer /health'
  }
  $identity = Get-EndpointIdentity ([pscustomobject]@{
    TaskName='offline-endpoint'; TaskPath='\'; Principal=[pscustomobject]@{UserId='fixture-user'};
    Actions=@([pscustomobject]@{
      Execute=(Join-Path $generation 'runtime/Scripts/python.exe')
      Arguments='opaque'
      WorkingDirectory=$Root})
  }) $null ''
  return [pscustomobject]@{
    schema_version='solidworks-to-urdf.health/v1'; ready=$true; native_platform=$true;
    install_id=$identity.install_id; config_digest=$identity.config_digest;
    release=$identity.release; target_identity=[pscustomobject]@{target_id='fixture-target'}}
}
$script:RollbackPrevious = Join-Path $Root 'bound-previous'
$null = New-Item -ItemType Directory -Path $script:RollbackPrevious -Force
$rollbackTask = [pscustomobject]@{
  TaskName='offline-endpoint'; TaskPath='\'; State='Running';
  Principal=[pscustomobject]@{UserId='fixture-user'};
  Settings=[pscustomobject]@{MultipleInstances='IgnoreNew'};
  Actions=@($script:OldAction)
}
$rollbackProjection = Get-TaskProjection $rollbackTask
$rollbackIdentity = Get-EndpointIdentity $rollbackTask $rollbackProjection ''
$rollbackState = New-EndpointSourceState $rollbackTask $rollbackProjection $rollbackIdentity
Write-Utf8NoBom (Join-Path $script:RollbackPrevious 'task.xml') ([string]$rollbackProjection.xml)
Copy-Item -LiteralPath $oldConfig `
  -Destination (Join-Path $script:RollbackPrevious 'endpoint-before.json')
Write-Receipt (Join-Path $script:RollbackPrevious 'source-state.json') $rollbackState
function Get-ManagedServiceSid { param($Task) return 'S-1-5-21-offline-harness' }
function Set-ManagedAcl {
  param(
    [string]$Path,
    [bool]$Protected,
    [string]$ServiceSid,
    [switch]$NonInheriting,
    [switch]$Recurse)
  $script:Calls.Add(
    'managed-acl:' + $Path +
    ':protected=' + [string]$Protected +
    ':service-sid=' + $ServiceSid +
    ':non-inheriting=' + [string][bool]$NonInheriting +
    ':recurse=' + [string][bool]$Recurse)
}
function Assert-ManagedArchive {
  param([string]$Path, [string]$ExpectedSha256, [string]$ServiceSid)
  $script:Calls.Add(
    'managed-archive:' + $Path +
    ':sha256=' + $ExpectedSha256 +
    ':service-sid=' + $ServiceSid)
}
function Protect-ManagedArchive {
  param([string]$Path, [string]$ExpectedSha256, [string]$ServiceSid)
  $script:Calls.Add(
    'protect-archive:' + $Path + ':sha256=' + $ExpectedSha256 +
    ':service-sid=' + $ServiceSid)
}
function Set-ManagedEndpointAcls {
  param($Config, [string]$EndpointRoot, [string]$ConfigPath, [string]$InstallIdPath, [string]$TaskFile)
  $script:Calls.Add('managed-endpoint-acl:' + $EndpointRoot)
}
function Assert-ManagedEndpointAcls {
  param($Config, [string]$EndpointRoot, [string]$ConfigPath, [string]$InstallIdPath, [string]$TaskFile)
}
function Assert-DomainPins { param([string]$StateRoot, [string]$ServedGeneration) return @() }
function Wait-Settled {
  param([string]$configPath, [object]$binding)
  if ($Scenario -eq 'promote_4ed_success') {
    $health = Read-EndpointHealth $configPath
    return @{
      health=$health
      listener_pid=4242
      settled_seconds=184
    }
  }
  throw 'the offline harness did not enter the sustained settle window'
}
if ($Scenario -eq 'stage_acl_boundary') {
  function Expand-Archive {
    param([string]$LiteralPath, [string]$DestinationPath)
    throw 'DIE:injected_stage_acl_boundary:the archive expansion is intentionally stopped'
  }
}
function Get-TaskFilePath { param($Task) return (Join-Path $Root 'task.xml') }
function Get-Process {
  [CmdletBinding()] param([int]$Id)
  if ($Scenario -eq 'unknown_owner') {
    Write-Error 'injected permission denied' -Category PermissionDenied
    return
  }
  Write-Error ('Cannot find a process with the process ID: {0}.' -f $Id) -Category ObjectNotFound
}
function cmd { $global:LASTEXITCODE = 3; 'LOCK_REFUSED' }
function Wait-SettleRetryDelay { param([int]$Seconds = 60) $script:Sleeps.Add($Seconds) }
function Die { param([string]$code, [string]$message) throw ('DIE:' + $code + ':' + $message) }

if ($Scenario -like 'stage_*') {
  $Script:Verb = 'stage'
  $Script:Binding = $bindingPath
  $Script:StageDir = Join-Path $Root 'staged'
} elseif ($Scenario -like 'rollback_*') {
  $Script:Verb = 'rollback'
  $Script:Binding = $bindingPath
  $Script:Previous = $script:RollbackPrevious
} else {
  $Script:Verb = 'promote'
  $Script:Binding = $bindingPath
  $Script:StageDir = $newGeneration
}
$message = ''
$stack = ''
try { Invoke-Main } catch { $message = $_.Exception.Message; $stack = $_.ScriptStackTrace }
$failedFile = Get-ChildItem -LiteralPath (Join-Path $Root 'instance-promotions') -Recurse `
  -Filter 'failed-promotion.json' -ErrorAction SilentlyContinue | Select-Object -First 1
$receipt = if ($failedFile) { Get-Content -LiteralPath $failedFile.FullName -Raw | ConvertFrom-Json } else { $null }
$lastFile = Join-Path $Root 'instance-promotions/last-promotion.json'
$lastPromotion = if (Test-Path -LiteralPath $lastFile) {
  Get-Content -LiteralPath $lastFile -Raw | ConvertFrom-Json
} else {
  $null
}
@{scenario=$Scenario; message=$message; stack=$stack; calls=@($script:Calls); sleeps=@($script:Sleeps);
  receipt=$receipt; last_promotion=$lastPromotion} |
  ConvertTo-Json -Depth 12 -Compress
exit 0
