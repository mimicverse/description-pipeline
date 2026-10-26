# Runs the candidate script's REAL Invoke-Main for one promote scenario (offline).
#
#   invoke-main-harness.ps1 -ScriptPath <lifecycle.ps1> -Scenario <name> -Root <temp dir>
#
# Only the OS/network boundaries and the settle retry timer are stubbed over synthetic temp
# files; the candidate's own Invoke-Main, Start-OrRefuse, refusal interpreters, restore path and
# health probe run for real (Wait-Settled runs real and fails fast on the stub task state).
# Scenarios: unknown_owner | startable_second_refusal | promote_missing_stage_receipt |
# stage_unknown_key_refused | stage_legacy_targets_refused | stage_duplicate_key_refused |
# stage_acl_boundary | promote_prestop_identity_mismatch | rollback_task_path_mismatch |
# rollback_bad_snapshot | rollback_source_state_tamper | rollback_stop_refused |
# rollback_stop_residue | rollback_restore_failure | rollback_receipt_missing |
# rollback_receipt_conflict | rollback_target_mismatch | rollback_task_name_ambiguous |
# status_pending_rollback | status_pending_rollback_invalid |
# status_pending_rollback_directory |
# promote_candidate_mismatch | promote_source_mismatch.
# Prints one JSON object (scenario, message, calls, sleeps, receipt) and exits 0.
param(
  [Parameter(Mandatory=$true)][string]$ScriptPath,
  [Parameter(Mandatory=$true)][string]$Scenario,
  [Parameter(Mandatory=$true)][string]$Root
)
$ErrorActionPreference = 'Stop'
. $ScriptPath -Verb promote -Binding 'unused'

$newGeneration = Join-Path $Root 'lifecycle/new/generation'
$oldGeneration = Join-Path $Root 'lifecycle/old/generation'
$null = New-Item -ItemType Directory -Path $newGeneration -Force
$null = New-Item -ItemType Directory -Path $oldGeneration -Force
$harnessSystemRoot = Join-Path $Root 'windows'
$harnessScheduler = Join-Path $harnessSystemRoot 'System32/svchost.exe'
$null = New-Item -ItemType Directory -Path (Split-Path -Parent $harnessScheduler) -Force
Write-Utf8NoBom $harnessScheduler 'harness-svchost'
$env:SystemRoot = $harnessSystemRoot
function Initialize-HarnessRuntime([string]$Generation, [string]$Name) {
  $runtime = Join-Path $Generation 'runtime'
  $scripts = Join-Path $runtime 'Scripts'
  $baseHome = Join-Path $Root ($Name + '-python')
  $base = Join-Path $baseHome 'python.exe'
  foreach ($directory in @($scripts, $baseHome)) {
    $null = New-Item -ItemType Directory -Path $directory -Force
  }
  Write-Utf8NoBom (Join-Path $scripts 'python.exe') ('launcher:' + $Generation)
  Write-Utf8NoBom $base ('base:' + $Name)
  Write-Utf8NoBom (Join-Path $runtime 'pyvenv.cfg') (@(
      ('home = ' + $baseHome)
      'include-system-site-packages = false'
      'version = 3.12.10'
      ('executable = ' + $base)
      ('command = "' + $base + '" -m venv "' + $runtime + '"')
    ) -join "`n")
}
Initialize-HarnessRuntime $oldGeneration 'old'
Initialize-HarnessRuntime $newGeneration 'new'
$oldConfig = Join-Path $oldGeneration 'endpoint.json'
$newConfig = Join-Path $newGeneration 'endpoint.json'
$token = Join-Path $Root 'local-test-token'
[System.IO.File]::WriteAllText($token, 'synthetic-offline-token')
$script:FixtureInstallId = 'win-' + ('1' * 32)
[System.IO.File]::WriteAllText((Join-Path $Root 'install-id'), $script:FixtureInstallId)
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
Copy-Item -LiteralPath $bindingPath `
  -Destination (Join-Path $Root 'lifecycle/old/windows-binding.json')
Copy-Item -LiteralPath $bindingPath `
  -Destination (Join-Path $Root 'lifecycle/new/windows-binding.json')
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
$script:CurrentAction = if ($Scenario -like 'rollback_*' -or $Scenario -like 'status_*') {
  $script:NewAction
} else {
  $script:OldAction
}
$script:RegisteredActions = @($script:CurrentAction)
$script:TaskState = 'Running'
$script:Calls = [System.Collections.Generic.List[string]]::new()
$script:SetEnvCalls = [System.Collections.Generic.List[string]]::new()
$script:Sleeps = [System.Collections.Generic.List[int]]::new()
$script:AccountEnv = @{}
$script:ListenerCallCount = 0
$script:StopTestNow = [datetime]'2026-10-27T00:00:00Z'

function Get-AccountEnvironment { param([string]$Name) return [string]$script:AccountEnv[$Name] }
function Set-AccountEnvironment {
  param([string]$Name, [string]$Value)
  $script:SetEnvCalls.Add($Name + '=' + $Value)
  $script:AccountEnv[$Name] = $Value
}
function Get-ServedStateRoot { param([string]$configPath) return [string]$Root }
function Get-ScheduledTask {
  [CmdletBinding()] param([string]$TaskName, [string]$TaskPath)
  if ($TaskPath -and $TaskPath -cne '\') {
    Write-Error 'the requested task path is not registered' -Category ObjectNotFound
    return
  }
  $task = [pscustomobject]@{
    TaskName=$TaskName
    TaskPath='\'
    State=$script:TaskState
    Principal=[pscustomobject]@{UserId='fixture-user'}
    Settings=[pscustomobject]@{MultipleInstances='IgnoreNew'}
    Actions=@($script:RegisteredActions)
  }
  if (-not $TaskPath -and $Scenario -eq 'rollback_task_name_ambiguous') {
    return @($task, $task)
  }
  return $task
}
function Start-ScheduledTask {
  [CmdletBinding()] param([string]$TaskName, [string]$TaskPath)
  $script:TaskState = if (
    $Scenario -eq 'promote_4ed_success' -or
    (Normalize-PathIdentity $script:CurrentAction.Execute) -like
      ((Normalize-PathIdentity $oldGeneration) + '/*') -or
    ($Scenario -like 'rollback_*' -and
      (Normalize-PathIdentity $script:CurrentAction.Execute) -like
        ((Normalize-PathIdentity $newGeneration) + '/*'))
  ) { 'Running' } else { 'Ready' }
  $script:Calls.Add('start:' + $script:CurrentAction.Execute)
}
function Stop-ScheduledTask {
  [CmdletBinding()] param([string]$TaskName, [string]$TaskPath)
  if ($Scenario -eq 'rollback_stop_refused') {
    $script:Calls.Add('stop-refused')
    throw 'the scheduled task stop was refused'
  }
  $script:TaskState = 'Ready'
  $script:Calls.Add('stop')
}
function Disable-ScheduledTask {
  [CmdletBinding()] param([string]$TaskName)
  $script:TaskState = 'Disabled'
  $script:Calls.Add('disable')
}
function Get-NetTCPConnection {
  [CmdletBinding()] param([int]$LocalPort, [string]$State)
  if ($Scenario -eq 'rollback_stop_residue') {
    return [pscustomobject]@{OwningProcess=4242}
  }
  return $null
}
function Get-CimInstance {
  [CmdletBinding()] param([string]$ClassName, [string]$Filter)
  return $null
}
function Export-ScheduledTask { param([string]$TaskName) '<Task/>' }
function Get-Date {
  if ($Scenario -eq 'rollback_stop_residue') { return $script:StopTestNow }
  return [datetime]::UtcNow
}
function Start-Sleep {
  param([int]$Milliseconds, [int]$Seconds)
  if ($Scenario -eq 'rollback_stop_residue') {
    $script:StopTestNow = $script:StopTestNow.AddMilliseconds(500)
  }
}
function New-ScheduledTaskAction {
  param([string]$Execute, [string]$Argument, [string]$WorkingDirectory)
  [pscustomobject]@{ Execute=$Execute; Arguments=$Argument; WorkingDirectory=$WorkingDirectory }
}
function Set-ScheduledTask {
  param([string]$TaskName, [string]$TaskPath, [object]$Action)
  $Action | Add-Member -NotePropertyName WorkingDirectory `
    -NotePropertyValue $binding.state_root -Force
  $script:CurrentAction=$Action
  $script:RegisteredActions=@($Action)
  $script:Calls.Add('set:' + $Action.Execute)
}
function Register-ScheduledTask {
  [CmdletBinding()] param([string]$TaskName, [string]$TaskPath, [string]$Xml, [switch]$Force)
  $taskXml = [xml]$Xml
  $execute = @($taskXml.SelectNodes('//*[local-name()="Exec"]/*[local-name()="Command"]'))
  $arguments = @($taskXml.SelectNodes('//*[local-name()="Exec"]/*[local-name()="Arguments"]'))
  $working = @($taskXml.SelectNodes('//*[local-name()="Exec"]/*[local-name()="WorkingDirectory"]'))
  if ($execute.Count -ne 1 -or $arguments.Count -ne 1 -or $working.Count -ne 1) {
    throw 'the registered source task XML does not carry exactly one action'
  }
  $script:CurrentAction=[pscustomobject]@{
    Execute=[string]$execute[0].InnerText
    Arguments=[string]$arguments[0].InnerText
    WorkingDirectory=[string]$working[0].InnerText
  }
  $script:RegisteredActions=@($script:CurrentAction)
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
  }
  $script:Calls.Add('register:old')
}
function Get-TaskProjection {
  param($Task)
  Assert-Single-TaskAction $Task
  $action = $Task.Actions[0]
  $xmlText = '<Task><Actions><Exec><Command>' + [string]$action.Execute +
    '</Command><Arguments>' + [string]$action.Arguments +
    '</Arguments><WorkingDirectory>' + [string]$action.WorkingDirectory +
    '</WorkingDirectory></Exec></Actions></Task>'
  $principal = [pscustomobject]@{
    user='fixture-user'; sid='S-1-5-21-offline-harness';
    logon_type='Interactive'; run_level='HighestAvailable';
    group_id=''; administrator=$true}
  $projectedAction = [pscustomobject]@{
    execute=[string]$action.Execute
    arguments=[string]$action.Arguments
    working_directory=[string]$action.WorkingDirectory}
  $settings = [pscustomobject]@{multiple_instances='IgnoreNew'}
  $semantic = New-TaskSemanticDocument ([string]$Task.TaskPath) ([string]$Task.TaskName) `
    $principal $projectedAction $settings ('b' * 64) ('a' * 64)
  return [pscustomobject]@{
    task_path=[string]$Task.TaskPath
    task_name=[string]$Task.TaskName
    state=[string]$Task.State
    xml=$xmlText
    xml_sha256=(Get-TextSha256 $xmlText)
    semantic_sha256=(Get-JsonSha256 $semantic)
    security_descriptor_sha256=('a' * 64)
    triggers_sha256=('b' * 64)
    principal=$principal
    action=$projectedAction
    settings=$settings
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
    install_id_path=(Join-Path $Root 'install-id')
    install_id=$script:FixtureInstallId
    release_identity_sha256=(Get-ReleaseIdentitySha256 $release)
    release=$release
  }
}
function Read-EndpointRuntimeProbe {
  param([string]$Generation, [string]$ConfigPath)
  $config = Read-Json $ConfigPath
  $source = (Normalize-PathIdentity $Generation) -eq (Normalize-PathIdentity $oldGeneration)
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
    config_digest=if ($source) { '1' * 64 } else { '2' * 64 }
    package_root=(Normalize-PathIdentity ([string]$config.package_root))
    output_root=(Normalize-PathIdentity ([string]$config.output_root))
    state_root=(Normalize-PathIdentity ([string]$config.state_root))
    port=[int]$config.port
    release=[pscustomobject]@{
      version=$release.version
      source_sha256=$release.source_sha256
      release=[pscustomobject]@{source_commit=$release.source_commit}
    }
  }
}
function Get-ListenerEvidence {
  param([object]$binding, $TaskProjection)
  $script:ListenerCallCount++
  $generation = Get-ActionGeneration $TaskProjection.action
  $runtime = Read-PyvenvRuntime (Join-Path $generation 'runtime')
  $listenerPid = if (
    $Scenario -eq 'promote_prestop_identity_mismatch' -and
    $script:ListenerCallCount -ge 2
  ) { 4243 } else { 4242 }
  $evidence = [ordered]@{
    schema_version='solidworks-to-urdf.windows-listener/v2'
    relation='direct_venv_child'
    process_arguments_sha256=(Get-TextSha256 ([string]$TaskProjection.action.arguments))
    scheduler_service_name='Schedule'
    scheduler_service_state='Running'
    scheduler_service_process_id=2512
    scheduler_pid=2512
    scheduler_image=(Normalize-PathIdentity $harnessScheduler)
    scheduler_start_utc='2026-10-27T00:00:00.0000000Z'
    scheduler_user_sid='S-1-5-18'
    scheduler_session_id=0
    launcher_pid=19344
    launcher_parent_pid=2512
    launcher_image=(Normalize-PathIdentity ([string]$TaskProjection.action.execute))
    launcher_image_sha256=(Sha256 ([string]$TaskProjection.action.execute))
    launcher_start_utc='2026-10-27T00:00:01.0000000Z'
    launcher_user_sid=$TaskProjection.principal.sid
    launcher_session_id=1
    listener_pid=$listenerPid
    listener_parent_pid=19344
    listener_start_utc=if ($listenerPid -eq 4243) {
      '2026-10-27T00:00:02.5000000Z'
    } else {
      '2026-10-27T00:00:02.0000000Z'
    }
    listener_user_sid=$TaskProjection.principal.sid
    listener_session_id=1
    venv_config_path=[string]$runtime.path
    venv_config_sha256=[string]$runtime.sha256
    venv_home=[string]$runtime.home
    venv_version=[string]$runtime.version
    base_interpreter_path=[string]$runtime.base_interpreter
    base_interpreter_sha256=[string]$runtime.base_interpreter_sha256
  }
  Assert-ListenerDocument $evidence
  return $evidence
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
$rollbackListener = Get-ListenerEvidence $binding $rollbackProjection
$rollbackState = New-EndpointSourceState $rollbackTask $rollbackProjection `
  $rollbackIdentity $rollbackListener
Write-Utf8NoBom (Join-Path $script:RollbackPrevious 'task.xml') ([string]$rollbackProjection.xml)
Copy-Item -LiteralPath $oldConfig `
  -Destination (Join-Path $script:RollbackPrevious 'endpoint-before.json')
Write-Receipt (Join-Path $script:RollbackPrevious 'source-state.json') $rollbackState
$rollbackStateAuthority = Get-SourceStateAuthorityHash (
  Join-Path $script:RollbackPrevious 'source-state.json')
$rollbackReceiptAuthority = $rollbackStateAuthority
if ($Scenario -eq 'rollback_bad_snapshot') {
  Write-Utf8NoBom (Join-Path $script:RollbackPrevious 'endpoint-before.json') '{"corrupt":true}'
} elseif ($Scenario -eq 'rollback_task_path_mismatch') {
  $rollbackState.task_path = '\Foreign\'
  Write-Receipt (Join-Path $script:RollbackPrevious 'source-state.json') $rollbackState
  $rollbackReceiptAuthority = Get-SourceStateAuthorityHash (
    Join-Path $script:RollbackPrevious 'source-state.json')
} elseif ($Scenario -eq 'rollback_source_state_tamper') {
  $rollbackState.task_semantic_sha256 = '0' * 64
  Write-Receipt (Join-Path $script:RollbackPrevious 'source-state.json') $rollbackState
}
$promotionRecords = Join-Path $Root 'instance-promotions'
$null = New-Item -ItemType Directory -Path $promotionRecords -Force
if ($Scenario -ne 'rollback_receipt_missing') {
  $receiptSource = Read-Json (Join-Path $script:RollbackPrevious 'source-state.json')
  $receiptTarget = if ($Scenario -eq 'rollback_target_mismatch') {
    Normalize-PathIdentity (Join-Path $Root 'lifecycle/foreign/generation')
  } else {
    Normalize-PathIdentity $newGeneration
  }
  Write-Receipt (Join-Path $promotionRecords 'pending-promotion.json') @{
    intent=$true; verb='promote'; generation=(Normalize-PathIdentity $newGeneration);
    previous=(Normalize-PathIdentity $script:RollbackPrevious);
    source=$receiptSource; source_state_sha256=$rollbackReceiptAuthority;
    target=@{endpoint_root=$receiptTarget}}
  $successAuthority = if ($Scenario -eq 'rollback_receipt_conflict') {
    '0' * 64
  } else {
    $rollbackReceiptAuthority
  }
  Write-Receipt (Join-Path $promotionRecords 'last-promotion.json') @{
    passed=$true; verb='promote'; generation=(Normalize-PathIdentity $newGeneration);
    previous=(Normalize-PathIdentity $script:RollbackPrevious);
    source=$receiptSource; source_state_sha256=$successAuthority;
    target=@{endpoint_root=$receiptTarget}}
}
if ($Scenario -in @(
    'status_pending_rollback',
    'status_pending_rollback_invalid',
    'status_pending_rollback_directory')) {
  $rollbackIntentPath = Join-Path $promotionRecords 'pending-rollback.json'
  if ($Scenario -eq 'status_pending_rollback_invalid') {
    Write-Utf8NoBom $rollbackIntentPath '{"verb":"rollback","intent":true}'
  } elseif ($Scenario -eq 'status_pending_rollback_directory') {
    $null = New-Item -ItemType Directory -Path $rollbackIntentPath
  } else {
    Write-Receipt $rollbackIntentPath @{
      intent=$true; verb='rollback'; previous=$script:RollbackPrevious;
      rollback_generation=(Normalize-PathIdentity $newGeneration);
      restore_to=(Normalize-PathIdentity $oldGeneration);
      current_snapshot=(Join-Path $promotionRecords 'rollback-before-fixture');
      source_state_sha256=('a' * 64); source=$rollbackState
    }
  }
}
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
} elseif ($Scenario -like 'status_*') {
  $Script:Verb = 'status'
  $Script:Binding = $bindingPath
} else {
  $Script:Verb = 'promote'
  $Script:Binding = $bindingPath
  $Script:StageDir = $newGeneration
}
$script:ListenerCallCount = 0
$message = ''
$stack = ''
try { Invoke-Main } catch { $message = $_.Exception.Message; $stack = $_.ScriptStackTrace }
$failedFile = Get-ChildItem -LiteralPath (Join-Path $Root 'instance-promotions') -Recurse `
  -Filter 'failed-*.json' -ErrorAction SilentlyContinue | Select-Object -First 1
$receipt = if ($failedFile) { Get-Content -LiteralPath $failedFile.FullName -Raw | ConvertFrom-Json } else { $null }
$lastFile = Join-Path $Root 'instance-promotions/last-promotion.json'
$lastPromotion = if (Test-Path -LiteralPath $lastFile) {
  Get-Content -LiteralPath $lastFile -Raw | ConvertFrom-Json
} else {
  $null
}
@{scenario=$Scenario; message=$message; stack=$stack; calls=@($script:Calls);
  setenv=@($script:SetEnvCalls); sleeps=@($script:Sleeps);
  receipt=$receipt; last_promotion=$lastPromotion; task_state=$script:TaskState} |
  ConvertTo-Json -Depth 12 -Compress
exit 0
