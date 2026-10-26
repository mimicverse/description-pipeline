# Settle-semantics tests for deploy/windows/instance-lifecycle.ps1 (offline; pwsh on Linux).
#
#   pwsh -NoProfile -File tests/deploy/test_instance_lifecycle.ps1 [-ScriptPath PATH] [-StaticOnly]
#
# Stub inventory: the OS/network boundary (Get-Process / cmd / the ScheduledTask cmdlets /
# Invoke-RestMethod), the settle timer (Wait-SettleRetryDelay) and the health probe are local
# stubs; the script's own interpreters - Get-ProcessFacts, Test-LockProbeHeld,
# Get-OwnershipRefusalFacts, Start-OrRefuse, Restore-PreviousEndpoint - run for real.  The
# outer-promote cases additionally run the REAL Invoke-Main in a child pwsh via
# tests/deploy/invoke-main-harness.ps1.  Running with -ScriptPath pointing at a pre-change copy
# plus -StaticOnly is the negative-evidence mode: the old wording and the missing
# observer/ownership gates must make that run fail.
[CmdletBinding()]
param(
  [string]$ScriptPath = '',
  [switch]$StaticOnly
)
$ErrorActionPreference = 'Stop'
$repositoryRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
if (-not $ScriptPath) {
  # Windows PowerShell 5.1 does not populate $PSScriptRoot while parameter defaults evaluate and
  # has no multi-child Join-Path (PowerShell 6+ -AdditionalChildPath): the repository-relative
  # default resolves here, after the parameter block.
  $ScriptPath = Join-Path (Join-Path (Join-Path $repositoryRoot 'deploy') 'windows') 'instance-lifecycle.ps1'
}
$bootstrapScript = Join-Path (Join-Path (Join-Path $repositoryRoot 'deploy') 'windows') 'bootstrap-managed-root.ps1'
$script:checks = 0
$script:failures = [System.Collections.Generic.List[string]]::new()
function Check([string]$name, [bool]$condition, [string]$detail = '') {
  $script:checks++
  if ($condition) {
    Write-Host ("PASS {0}" -f $name)
  } else {
    $script:failures.Add($name)
    Write-Host ("FAIL {0} {1}" -f $name, $detail)
  }
}
function Compare-CanonicalAclRule($left, $right) {
  $comparison = [string]::CompareOrdinal(
    ([string]$left.sid).ToLowerInvariant(),
    ([string]$right.sid).ToLowerInvariant())
  if ($comparison -ne 0) { return $comparison }
  foreach ($property in @('rights', 'access_type', 'inheritance', 'propagation', 'inherited')) {
    $leftValue = if ($property -eq 'access_type') {
      [string]$left.$property
    } else {
      [long]$left.$property
    }
    $rightValue = if ($property -eq 'access_type') {
      [string]$right.$property
    } else {
      [long]$right.$property
    }
    $comparison = if ($property -eq 'access_type') {
      [string]::CompareOrdinal($leftValue, $rightValue)
    } elseif ($leftValue -lt $rightValue) {
      -1
    } elseif ($leftValue -gt $rightValue) {
      1
    } else {
      0
    }
    if ($comparison -ne 0) { return $comparison }
  }
  return 0
}
function Test-CanonicalAclRuleOrder($rules) {
  $rows = @($rules)
  for ($index = 1; $index -lt $rows.Count; $index++) {
    if ((Compare-CanonicalAclRule $rows[$index - 1] $rows[$index]) -gt 0) {
      return $false
    }
  }
  return $true
}

# ---- parse + wording gates --------------------------------------------------
$tokens = $null
$parseErrors = $null
$null = [System.Management.Automation.Language.Parser]::ParseFile($ScriptPath, [ref]$tokens, [ref]$parseErrors)
Check 'parse.clean' ($parseErrors.Count -eq 0) (($parseErrors | Out-String).Trim())
$bootstrapTokens = $null
$bootstrapParseErrors = $null
$null = [System.Management.Automation.Language.Parser]::ParseFile(
  $bootstrapScript, [ref]$bootstrapTokens, [ref]$bootstrapParseErrors)
Check 'parse.bootstrap_clean' ($bootstrapParseErrors.Count -eq 0) (($bootstrapParseErrors | Out-String).Trim())
$source = Get-Content -LiteralPath $ScriptPath -Raw
$bootstrapSource = Get-Content -LiteralPath $bootstrapScript -Raw
# Design-lead settle rules: no blind wait/retry, positive owner proof first, three-valued
# observers, and a recorded safe restore on the failure path.
Check 'wording.blind_retry_removed' (-not $source.Contains('waited out for')) 'the pre-change blind-retry wording is still present'
Check 'wording.no_blind_sleep' (-not $source.Contains('Start-Sleep -Seconds 60')) 'an unconditional 60s sleep is still present'
Check 'wording.owner_proof_gate' ($source.Contains('lock_refused_unproven') -and $source.Contains('owner_ended'))
Check 'wording.observer_tristate' ($source.Contains('observer_failed') -and $source.Contains('PROBE_IMPORT_FAILED'))
Check 'wording.restore_path' ($source.Contains('Restore-PreviousEndpoint') -and $source.Contains('restore=$restore'))
Check 'wording.stage_candidate_generation' ($source.Contains('Get-EndpointConfigCandidateProgram') -and
  $source.Contains('endpoint_config_unknown_keys') -and $source.Contains('candidate-identity.json') -and
  $source.Contains('candidate_config_mismatch'))
Check 'wording.launcher_parser_removed' (-not $source.Contains('Action-Config') -and
  -not $source.Contains('Get-RecoveryIdentity')) 'a second task-action parser is still present'
Check 'wording.compat_tolerance_removed' ((-not $source.Contains('REMOVED_KEYS')) -and (-not $source.Contains('removed_keys'))) 'the removed-key tolerance channel is still present'
Check 'wording.transform_receipt_removed' ((-not $source.Contains('config-transformation.json')) -and (-not $source.Contains('stage-config-transform'))) 'the obsolete transform receipt is still present'
Check 'wording.no_lock_or_kill' (-not $source.Contains('Remove-Item -LiteralPath $lock')) 'never delete the ownership lock'
Check 'wording.no_system_text_json' (-not $source.Contains('System.Text.Json')) 'a Core-only JSON type the Desktop PowerShell 5.1 lifecycle cannot load is still present'
Check 'wording.strict_scan_on_bound_python' ($source.Contains('Get-EndpointConfigScanPython') -and
  $source.Contains('Get-EndpointConfigStrictPrelude') -and $source.Contains('object_pairs_hook'))
Check 'wording.python_module_task_action' ($source.Contains('function Runtime-LauncherArguments') -and
  $source.Contains('function Test-Runtime-Launcher') -and
  $source.Contains('-I -B -m description_pipeline.orchestration.windows serve --config') -and
  $source.Contains('launch_contract=(Python-Module-LaunchContract)') -and
  $source.Contains('-Argument (Runtime-LauncherArguments $StageDir)'))
Check 'wording.remote_python_bytecode_disabled' (-not @(($source -split "`r?`n") |
  Where-Object { $_ -match '\s-I\s' -and $_ -notmatch '\s-I\s+-B' }))
Check 'wording.history_special_cases_removed' (-not $source.Contains('55cdf8da178e37239d3ac199cd27fc50cf56540d') -and
  -not $source.Contains('140d39e51be5f0c39c32d7cfea9ca25fe642c273') -and
  -not $source.Contains('legacy_pip_launcher_v0'))
Check 'wording.source_action_config_binding' (
  $source.Contains('the raw source action does not carry its managed configuration path') -and
  $source.Contains('Assert-ListenerMatch $sourceListener $preStopListener'))
Check 'wording.status_exposes_raw_task_xml' (
  $source.Contains('task_xml=$null') -and
  $source.Contains('$live.task_xml = [string]$projection.xml'))
Check 'wording.archive_is_read_only' ($source.Contains('function Protect-ManagedArchive') -and
  $source.Contains('function Assert-ManagedArchive') -and
  $source.Contains('Set-ItemProperty -LiteralPath $Path -Name IsReadOnly -Value $true') -and
  $source.Contains([System.Security.AccessControl.FileSystemRights]::ReadAndExecute.ToString()))
Check 'wording.archive_outside_recovery_state' (
  $source.Contains('canonical release archive is immutable lifecycle input'))
Check 'wording.path_identity_normalization' ($source.Contains('Normalize-PathIdentity') -and
  $source.Contains("Replace('\', '/')")) 'path identity comparisons must normalize the two Windows separators'
Check 'wording.user_domain_pins' ($source.Contains("GetEnvironmentVariable(`$Name, 'User')") -and
  $source.Contains("SetEnvironmentVariable(`$Name, `$Value, 'User')"))
Check 'wording.windows_recovery_schema' ($source.Contains('recovery-snapshot') -and
  $source.Contains('recovery-restore') -and $source.Contains('recovery-verify') -and
  $source.Contains('solidworks-to-urdf.windows-recovery/v2'))
$snapshotStateStart = $source.IndexOf('$state = [ordered]@{')
$snapshotStateEnd = $source.IndexOf('files = $files', $snapshotStateStart)
Check 'wording.recovery_snapshot_is_opaque_source_state' ($snapshotStateStart -ge 0 -and
  $snapshotStateEnd -gt $snapshotStateStart -and
  -not $source.Substring($snapshotStateStart, $snapshotStateEnd - $snapshotStateStart).Contains(
    'runtime_launch_contract'))
$lookupStart = $source.IndexOf('function Find-GenerationLaunchContract')
$lookupEnd = $source.IndexOf('function Get-GenerationLaunchContract', $lookupStart)
Check 'wording.launch_contract_lookup_is_non_exiting' ($lookupStart -ge 0 -and
  $lookupEnd -gt $lookupStart -and
  -not $source.Substring($lookupStart, $lookupEnd - $lookupStart).Contains('Die '))
Check 'wording.windows_recovery_acl' ($source.Contains('Get-AclProjection') -and
  $source.Contains('Set-AclProjection') -and $source.Contains('AreAccessRulesProtected'))
Check 'wording.windows_acl_canonical_sort' (
  $source.Contains('} | Sort-Object -Property @(') -and
  -not $source.Contains('} | Sort-Object sid, rights, access_type, inheritance, propagation, inherited'))
Check 'wording.bootstrap_acl_canonical_sort' (
  $bootstrapSource.Contains('} | Sort-Object -Property @(') -and
  -not $bootstrapSource.Contains('} | Sort-Object sid, rights, access_type, inheritance, propagation, inherited'))
$canonicalAclRules = @(
  [ordered]@{
    sid='S-1-5-18'; rights=2032127; access_type='Allow'
    inheritance=3; propagation=0; inherited=$false
  },
  [ordered]@{
    sid='S-1-5-21-1-2-3-1001'; rights=2032127; access_type='Allow'
    inheritance=3; propagation=0; inherited=$false
  },
  [ordered]@{
    sid='S-1-5-32-544'; rights=2032127; access_type='Allow'
    inheritance=3; propagation=0; inherited=$false
  }
)
Check 'windows_acl.canonical_order_fixture' (
  (Test-CanonicalAclRuleOrder $canonicalAclRules) -and
  -not (Test-CanonicalAclRuleOrder @($canonicalAclRules[2], $canonicalAclRules[0], $canonicalAclRules[1])))
Check 'wording.windows_managed_acl' ($source.Contains('Set-ManagedEndpointAcls') -and
  $source.Contains('Assert-ManagedEndpointAcls') -and $source.Contains('windows_managed_acl_invalid') -and
  $source.Contains('windows_managed_acl_unsafe') -and $source.Contains('Get-ManagedCommonParent') -and
  $source.Contains('windows_managed_acl_boundary_invalid') -and
  $source.Contains('Get-ManagedServiceAuthorization') -and
  $source.Contains('user-approved-administrator-runtime') -and
  $source.Contains('windows_managed_acl_marker_missing') -and
  $source.Contains('solidworks-to-urdf.managed-root/v1'))
$harnessPath = Join-Path $PSScriptRoot 'invoke-main-harness.ps1'
$harnessSource = Get-Content -LiteralPath $harnessPath -Raw
Check 'wording.harness_acl_signature' (
  $harnessSource.Contains('[string]$ServiceSid') -and
  -not $harnessSource.Contains('[switch]$AllowServiceOwner') -and
  -not $harnessSource.Contains('[string]$ServiceRights') -and
  -not $harnessSource.Contains('[string]$ServiceSid, [string]$TaskFile'))
Check 'wording.windows_recovery_identity' ($source.Contains('config_digest') -and
  $source.Contains('release_identity_sha256') -and $source.Contains('install_id') -and
  $source.Contains('script_sha256'))
Check 'wording.windows_recovery_no_cad_inference' ($source.Contains('cad_session_facts') -and
  $source.Contains('external_session_detected'))
$restoreStart = $source.IndexOf('function Invoke-RecoveryRestore')
$resumeStart = $source.IndexOf('function Invoke-RecoveryResume')
Check 'wording.windows_restore_never_starts' ($restoreStart -ge 0 -and $resumeStart -gt $restoreStart -and
  (-not $source.Substring($restoreStart, $resumeStart - $restoreStart).Contains('Start-ScheduledTask')))
if ($StaticOnly) {
  Write-Host ("checks={0} failures={1}" -f $script:checks, $script:failures.Count)
  exit ([int]($script:failures.Count -gt 0))
}

# ---- load the script without running its main flow --------------------------
. $ScriptPath -Verb health -Binding @{ task_name = 'test-endpoint' }

$startupRoot = Join-Path ([System.IO.Path]::GetTempPath()) (
  'il-startup-' + [guid]::NewGuid().ToString('N'))
$null = New-Item -ItemType Directory -Path $startupRoot
$startupConfig = Join-Path $startupRoot 'endpoint.json'
$startupToken = Join-Path $startupRoot 'token'
[System.IO.File]::WriteAllText(
  $startupConfig,
  (@{port=8765; token_file=$startupToken} | ConvertTo-Json -Compress))
[System.IO.File]::WriteAllText($startupToken, 'token')
$script:startupStates = @('Ready','Ready','Running')
$script:startupNow = [datetime]'2026-10-22T00:00:00Z'
function Get-ScheduledTask {
  [CmdletBinding()] param([string]$TaskName, [string]$TaskPath)
  $state = $script:startupStates[0]
  if ($script:startupStates.Count -gt 1) {
    $script:startupStates = @($script:startupStates | Select-Object -Skip 1)
  }
  return [pscustomobject]@{ State=$state }
}
function Get-ListenerProcess { param([int]$port) return 4242 }
function Invoke-RestMethod {
  param([string]$Uri, [hashtable]$Headers, [int]$TimeoutSec)
  return [pscustomobject]@{
    schema_version='solidworks-to-urdf.health/v1'
    pipeline_id='solidworks-to-urdf'
    ready=$true
    native_platform=$true
    release=[pscustomobject]@{
      version='1.0.0'; source_commit=('a' * 40);
      source_sha256=('b' * 64); role='native'}
    endpoint=[pscustomobject]@{port=8765}}
}
function Get-Date { return $script:startupNow }
function Start-Sleep {
  param([int]$Milliseconds)
  $script:startupNow = $script:startupNow.AddMilliseconds($Milliseconds)
}
$startupBinding = @{
  task_name='test-endpoint'; port=8765; version='1.0.0';
  source_commit=('a' * 40); source_sha256=('b' * 64)}
$startupResult = Wait-StartupHealthy $startupConfig $startupBinding
Check 'settle.startup_ready_then_running' ($startupResult.listener_pid -eq 4242)
function Get-ScheduledTask {
  [CmdletBinding()] param([string]$TaskName)
  return [pscustomobject]@{ State='Ready' }
}
$script:startupNow = [datetime]'2026-10-22T00:00:00Z'
try {
  $null = Wait-StartupHealthy $startupConfig $startupBinding
  $startupTimeout = ''
} catch {
  $startupTimeout = $_.Exception.Message
}
Check 'settle.startup_ready_timeout_refused' (
  $startupTimeout -match 'did not answer /health within the startup window')
$commonHealth = [pscustomobject]@{
  schema_version='solidworks-to-urdf.health/v1'
  pipeline_id='solidworks-to-urdf'
  ready=$true
  native_platform=$true
  release=[pscustomobject]@{
    version='1.0.0'; source_commit=('a' * 40);
    source_sha256=('b' * 64); role='native'}
  endpoint=[pscustomobject]@{port=8765}
  install=[pscustomobject]@{
    install_id='source-install'; config_digest=('c' * 64)}
}
Check 'health.common_legacy_extras_ignored' (
  Test-MaintenanceHealthShape $commonHealth 8765)
foreach ($mutation in @(
    @('schema', 'schema_version', 'solidworks-to-urdf.health/v2'),
    @('pipeline', 'pipeline_id', 'foreign-pipeline'),
    @('ready', 'ready', $false),
    @('native', 'native_platform', $false),
    @('port', 'port', 8766),
    @('release', 'source_commit', ('d' * 40)))) {
  $changedHealth = ($commonHealth | ConvertTo-Json -Depth 5 | ConvertFrom-Json)
  if ($mutation[0] -eq 'port') {
    $changedHealth.endpoint.port = $mutation[2]
  } elseif ($mutation[0] -eq 'release') {
    $changedHealth.release.source_commit = $mutation[2]
  } else {
    $changedHealth.($mutation[1]) = $mutation[2]
  }
  $healthAccepted = $true
  try {
    Assert-MaintenanceHealth $changedHealth '1.0.0' ('a' * 40) ('b' * 64) 8765
  } catch {
    $healthAccepted = $false
  }
  Check ('health.common_' + $mutation[0] + '_refused') (-not $healthAccepted)
}
Remove-Item Function:\Get-ScheduledTask, Function:\Get-ListenerProcess, Function:\Invoke-RestMethod,
  Function:\Get-Date, Function:\Start-Sleep -ErrorAction SilentlyContinue
Remove-Item -LiteralPath $startupRoot -Recurse -Force -ErrorAction SilentlyContinue

$seams = 'Get-OwnershipRefusalFacts', 'Start-OrRefuse', 'Wait-SettleRetryDelay', 'Read-LockOwner',
  'Get-ProcessFacts', 'Test-LockProbeHeld', 'Restore-PreviousEndpoint', 'Write-LockRefusalReceipt',
  'Get-TaskGeneration', 'Get-ListenerEvidence', 'Get-ListenerConnections',
  'Get-ListenerProcesses', 'Get-ScheduleService', 'Get-ProcessOwnerSid',
  'Get-EndpointIdentity',
  'Read-EndpointRuntimeProbe',
  'New-EndpointSourceState', 'Assert-EndpointSourceMatch', 'Assert-HealthSourceMatch',
  'Get-EndpointConfigEarlyProblems', 'Get-EndpointConfigCandidateProgram',
  'Get-ServedStateRoot', 'Get-EndpointConfigScanPython', 'Get-EndpointConfigStrictPrelude',
  'Get-EndpointConfigEarlyScanProgram', 'Write-PythonProgram', 'Normalize-PathIdentity', 'Get-WriterResidue'
$seams += 'Get-AclProjection', 'Set-AclProjection', 'Get-TaskProjection', 'Get-RecoveryCurrentState',
  'Invoke-RecoverySnapshot', 'Invoke-RecoveryRestore', 'Invoke-RecoveryResume', 'Test-NetTcpNoMatchError'
$seams += 'Get-ManagedServiceSid', 'Set-ManagedAcl', 'Assert-ManagedAcl',
  'Set-ManagedEndpointAcls', 'Assert-ManagedEndpointAcls', 'Get-ManagedEndpointPaths',
  'Get-ManagedCommonParent', 'Test-ManagedPathWithin', 'Assert-ManagedOperatorIsAdministrator',
  'Get-ManagedServiceAuthorization', 'New-ManagedAccessRule',
  'Get-ManagedAclProjectionForRole'
Check 'seams.present' (@($seams | Where-Object { -not (Get-Command $_ -ErrorAction SilentlyContinue) }).Count -eq 0)
Check 'windows_recovery.net_tcp_no_match' (Test-NetTcpNoMatchError ([pscustomobject]@{
  CategoryInfo=[pscustomobject]@{Category='ObjectNotFound'}
  FullyQualifiedErrorId='CmdletizationQuery_NotFound_LocalPort,Get-NetTCPConnection'
}))
Check 'windows_recovery.net_tcp_no_match_without_property' (Test-NetTcpNoMatchError ([pscustomobject]@{
  CategoryInfo=[pscustomobject]@{Category='ObjectNotFound'}
  FullyQualifiedErrorId='CmdletizationQuery_NotFound,Get-NetTCPConnection'
}))
foreach ($invalid in @(
  @('PermissionDenied', 'CmdletizationQuery_NotFound,Get-NetTCPConnection'),
  @('InvalidOperation', 'CmdletizationQuery_NotFound_LocalPort,Get-NetTCPConnection'),
  @('ObjectNotFound', 'CmdletizationQuery_NotFound,Get-NetUDPEndpoint'),
  @('ObjectNotFound', 'CmdletizationQuery_NotFound_,Get-NetTCPConnection'),
  @('ObjectNotFound', 'ProviderFailure,Get-NetTCPConnection'),
  @('ObjectNotFound', 'PrefixCmdletizationQuery_NotFound,Get-NetTCPConnection'),
  @('ObjectNotFound', 'CmdletizationQuery_NotFound,Get-NetTCPConnectionSuffix')
)) {
  Check ('windows_recovery.net_tcp_invalid_' + $invalid[0] + '_' + $invalid[1]) (-not (
    Test-NetTcpNoMatchError ([pscustomobject]@{
      CategoryInfo=[pscustomobject]@{Category=$invalid[0]}
      FullyQualifiedErrorId=$invalid[1]
    })
  ))
}
Check 'windows_recovery.net_tcp_not_found_text_is_not_enough' (-not (Test-NetTcpNoMatchError ([pscustomobject]@{
  CategoryInfo=[pscustomobject]@{Category='InvalidOperation'}
  FullyQualifiedErrorId='ProviderFailure'
})))
$releaseHash = Get-ReleaseIdentitySha256 @{
  version='1.2.3'
  source_commit='a' * 40
  source_sha256='b' * 64
  role='native'
}
Check 'windows_recovery.release_hash' ($releaseHash -eq 'd429e65c8f58a11d51d12ab6a83219568c3505e0698a34a52afd9668f47691c5')
$recoveryRoot = Join-Path ([System.IO.Path]::GetTempPath()) ('windows-recovery-' + [guid]::NewGuid().ToString('N'))
$recoverySource = Join-Path $recoveryRoot 'source'
$null = New-Item -ItemType Directory -Path $recoverySource -Force
$taskText = '<Task />'
[System.IO.File]::WriteAllText((Join-Path $recoverySource 'task.xml'), $taskText, (New-Object System.Text.UTF8Encoding($false)))
$taskBytes = [System.Text.Encoding]::UTF8.GetBytes($taskText)
$recoveryState = [ordered]@{
  schema_version='solidworks-to-urdf.windows-recovery/v2'
  members=@([ordered]@{member='task.xml';bytes=$taskBytes.Length;sha256=(Get-BytesSha256 $taskBytes)})
}
[System.IO.File]::WriteAllText(
  (Join-Path $recoverySource 'windows-state.json'),
  ($recoveryState | ConvertTo-Json -Depth 8 -Compress),
  (New-Object System.Text.UTF8Encoding($false)))
$recoveryBundle = Join-Path $recoveryRoot 'recovery.zip'
Compress-Archive -Path (Join-Path $recoverySource '*') -DestinationPath $recoveryBundle
$recoveryRead = Read-RecoveryBundle $recoveryBundle
Check 'windows_recovery.bundle_schema' ($recoveryRead.schema_version -eq 'solidworks-to-urdf.windows-recovery/v2')
$recoveryExpanded = Expand-RecoveryBundle $recoveryBundle $recoveryRoot
Check 'windows_recovery.bundle_member' ((Get-FileHash -LiteralPath (Join-Path $recoveryExpanded.root 'task.xml') -Algorithm SHA256).Hash.ToLower() -eq
  [string]$recoveryRead.members[0].sha256)
Remove-Item -LiteralPath $recoveryRoot -Recurse -Force -ErrorAction SilentlyContinue
$readbackExpected = [ordered]@{
  schema_version='solidworks-to-urdf.windows-recovery/v2'; ssh_host='fixture-host';
  task=[ordered]@{
    task_path='\'; task_name='fixture'; state='Disabled';
    xml_sha256=('a' * 64); semantic_sha256=('b' * 64);
    principal=@{sid='S-1-5-21-1-2-3-500'};
    action=@{execute='C:/generation/runtime/Scripts/python.exe';
      arguments='opaque source action'; working_directory='C:/state'};
    settings=@{multiple_instances='IgnoreNew'};
    triggers_sha256=('c' * 64); security_descriptor_sha256=('d' * 64)};
  identity=@{config_digest=('e' * 64); install_id='win-' + ('f' * 32)};
  files=@(
    @{role='task_xml'; member='task.xml'; sha256=('a' * 64)},
    @{role='endpoint_config'; member='endpoint.json'; sha256=('1' * 64)});
  acl=@{task_file=@{projection_sha256=('2' * 64)}};
  cad=@{lease_held=$false};
  members=@(
    @{member='task.xml'; sha256=('a' * 64)},
    @{member='endpoint.json'; sha256=('1' * 64)})
}
$readbackCurrent = ($readbackExpected | ConvertTo-Json -Depth 12 | ConvertFrom-Json)
$readbackCurrent.task.xml_sha256 = '9' * 64
$readbackCurrent.files[0].sha256 = '9' * 64
$readbackCurrent.members[0].sha256 = '9' * 64
Check 'windows_recovery.readback_ignores_export_format_only' (
  (Get-JsonSha256 (Get-RecoveryComparableState $readbackCurrent)) -eq
  (Get-JsonSha256 (Get-RecoveryComparableState $readbackExpected)))
$readbackCurrent.task.action.arguments = 'changed action'
Check 'windows_recovery.readback_refuses_semantic_action_change' (
  (Get-JsonSha256 (Get-RecoveryComparableState $readbackCurrent)) -ne
  (Get-JsonSha256 (Get-RecoveryComparableState $readbackExpected)))

# ---- strict JSON boundary on the bound CPython 3.12 (PowerShell 5.1-compatible path) --------
$scanPython = Get-EndpointConfigScanPython
Check 'json.scan_python_312' ($null -ne $scanPython) 'no CPython 3.12 interpreter is available for the strict scan'
$jsonRoot = Join-Path ([System.IO.Path]::GetTempPath()) ('il-json-' + [guid]::NewGuid().ToString('N'))
$null = New-Item -ItemType Directory -Path $jsonRoot -Force
function Write-IJson([string]$name, [string]$text) {
  $path = Join-Path $jsonRoot $name
  [System.IO.File]::WriteAllText($path, $text, (New-Object System.Text.UTF8Encoding($false)))
  return $path
}
$validPath = Write-IJson 'valid.json' '{"schema_version":"solidworks-to-urdf.endpoint/v1","package_root":"C:/pkg","output_root":"C:/out","state_root":"C:/state","token_file":"C:/state/token","host":"127.0.0.1","port":8765}'
$problems = Get-EndpointConfigEarlyProblems $validPath
Check 'json.valid_clean' (($problems.unreadable -eq '') -and (@($problems.duplicates).Count -eq 0) -and (@($problems.unknown).Count -eq 0))
$duplicatePath = Write-IJson 'duplicate.json' '{"schema_version":"solidworks-to-urdf.endpoint/v1","port":8765,"port":8766}'
$problems = Get-EndpointConfigEarlyProblems $duplicatePath
Check 'json.duplicate_detected' (($problems.unreadable -eq '') -and (@($problems.duplicates) -contains 'port'))
$unknownPath = Write-IJson 'unknown.json' '{"schema_version":"solidworks-to-urdf.endpoint/v1","bogus_key":1}'
$problems = Get-EndpointConfigEarlyProblems $unknownPath
Check 'json.unknown_detected' (($problems.unreadable -eq '') -and (@($problems.unknown) -contains 'bogus_key'))
$malformedPath = Write-IJson 'malformed.json' '{"port": '
$problems = Get-EndpointConfigEarlyProblems $malformedPath
Check 'json.malformed_unreadable' ($problems.unreadable -ne '')
$constantPath = Write-IJson 'constant.json' '{"port": NaN}'
$problems = Get-EndpointConfigEarlyProblems $constantPath
Check 'json.nonfinite_unreadable' ($problems.unreadable -ne '')
$arrayPath = Write-IJson 'array.json' '[1, 2]'
$problems = Get-EndpointConfigEarlyProblems $arrayPath
Check 'json.nonobject_unreadable' ($problems.unreadable -ne '')
$retiredPath = Write-IJson 'retired.json' '{"schema_version":"solidworks-to-urdf.endpoint/v1","targets":{}}'
$problems = Get-EndpointConfigEarlyProblems $retiredPath
Check 'json.retired_key_refused' (($problems.unreadable -eq '') -and (@($problems.unknown) -contains 'targets') -and (@($problems.duplicates).Count -eq 0))
$problems = Get-EndpointConfigEarlyProblems (Join-Path $jsonRoot 'missing.json')
Check 'json.missing_silent' (($problems.unreadable -eq '') -and (@($problems.duplicates).Count -eq 0) -and (@($problems.unknown).Count -eq 0))
Remove-Item -LiteralPath $jsonRoot -Recurse -Force -ErrorAction SilentlyContinue

# In-process refusal cases need the script's top-level Die to throw instead of exiting the
# harness; every existing check exercises throwing interpreters, never Die itself.
function Die { param([string]$code, [string]$message) throw ($code + ': ' + $message) }

function Test-SharedAclPolicyProjections {
  $fixturePath = Join-Path (Join-Path $repositoryRoot 'tests/fixtures') 'windows_acl_policy.json'
  $fixture = Get-Content -LiteralPath $fixturePath -Raw | ConvertFrom-Json
  Check 'windows_acl.golden_case_count' (@($fixture.cases).Count -eq 16)
  $parentPath = [System.IO.Path]::GetFullPath((Join-Path ([System.IO.Path]::GetTempPath()) 'acl-golden-state'))
  $leafPath = Join-Path $parentPath 'cad-ownership.json'
  function Resolve-Sid([string]$Identity) { return $Identity }
  function Get-AclProjection([string]$Path) {
    if ($Path -eq $parentPath) { return $activeCase.parent }
    return $activeCase.leaf
  }
  function Get-Item {
    [CmdletBinding()] param([string]$LiteralPath, [switch]$Force)
    $directory = ($LiteralPath -eq $parentPath) -or [bool]$activeCase.directory
    $attributes = if ($directory) {
      [System.IO.FileAttributes]::Directory
    } else {
      [System.IO.FileAttributes]::Normal
    }
    return [pscustomobject]@{PSIsContainer=$directory; Attributes=$attributes}
  }
  foreach ($activeCase in $fixture.cases) {
    $passed = $true
    try {
      if ($activeCase.mutable) {
        Assert-ManagedRuntimeLeafAcl $leafPath $parentPath $fixture.service_sid
      } else {
        Assert-ManagedAcl $leafPath $true $fixture.service_sid
      }
    } catch {
      if ($_.Exception.Message -notmatch 'windows_managed_acl_invalid') { throw }
      $passed = $false
    }
    Check ('windows_acl.golden.' + $activeCase.name) ($passed -eq [bool]$activeCase.expected)
  }
}
Test-SharedAclPolicyProjections

# ---- real Windows ACL boundary: inherited broad access is replaced ----------
if ([System.Environment]::OSVersion.Platform -eq [System.PlatformID]::Win32NT) {
  $driveRoot = ([System.IO.Path]::GetPathRoot($env:SystemRoot)).TrimEnd('\')
  $aclRoot = Join-Path $driveRoot ('description-managed-acl-' + [guid]::NewGuid().ToString('N'))
  $siblingRoot = Join-Path $driveRoot ('description-unmanaged-sibling-' + [guid]::NewGuid().ToString('N'))
  $null = New-Item -ItemType Directory -Path $aclRoot -Force
  $null = New-Item -ItemType Directory -Path $siblingRoot -Force
  $packageRoot = Join-Path $aclRoot 'packages'
  $outputRoot = Join-Path $aclRoot 'output'
  $stateRoot = Join-Path $aclRoot 'state'
  $secretRoot = Join-Path $aclRoot 'secrets'
  $endpointRoot = Join-Path $aclRoot 'generation'
  $lifecycleRoot = Join-Path $stateRoot 'lifecycle'
  foreach ($path in @($packageRoot, $outputRoot, $stateRoot, $secretRoot, $endpointRoot, $lifecycleRoot)) {
    $null = New-Item -ItemType Directory -Path $path -Force
  }
  $configPath = Join-Path $endpointRoot 'endpoint.json'
  $tokenPath = Join-Path $secretRoot 'endpoint.token'
  $installIdPath = Join-Path $stateRoot 'install-id'
  $ownershipPath = Join-Path $stateRoot 'cad-ownership.json'
  $leasePath = Join-Path $stateRoot 'cad-ownership.json.lease'
  $taskFile = Join-Path $aclRoot 'endpoint-task.xml'
  $packageChild = Join-Path $packageRoot 'cached-package.bin'
  foreach ($path in @(
      $configPath, $tokenPath, $installIdPath, $ownershipPath, $leasePath,
      $taskFile, $packageChild)) {
    [System.IO.File]::WriteAllText($path, 'fixture', (New-Object System.Text.UTF8Encoding($false)))
  }
  $operatorIdentity = [System.Security.Principal.WindowsIdentity]::GetCurrent()
  $serviceSid = [string]$operatorIdentity.User.Value
  $bootstrapOutput = & $bootstrapScript -ManagedRoot $aclRoot -ServiceAccount $serviceSid
  $bootstrapReceipt = ($bootstrapOutput | Select-Object -Last 1) | ConvertFrom-Json
  $rootAclRules = @((Get-AclProjection $aclRoot).rules)
  Check 'windows_acl.lifecycle_projection_canonical_order' (
    Test-CanonicalAclRuleOrder $rootAclRules)
  Check 'windows_acl.bootstrap_root_canonical_order' (
    Test-CanonicalAclRuleOrder @($bootstrapReceipt.root_acl.rules))
  Check 'windows_acl.bootstrap_marker_canonical_order' (
    Test-CanonicalAclRuleOrder @($bootstrapReceipt.marker_acl.rules))
  Check 'windows_acl.bootstrap_marker' ($bootstrapReceipt.passed -eq $true -and
    $bootstrapReceipt.service_sid -eq $serviceSid -and
    $bootstrapReceipt.managed_runtime.sid -eq $serviceSid -and
    $bootstrapReceipt.managed_runtime.administrator -eq $true -and
    $bootstrapReceipt.managed_runtime.privilege_qualification -eq 'user-approved-administrator-runtime' -and
    $bootstrapReceipt.managed_runtime.acl_ace -eq $true -and
    (Test-Path -LiteralPath (Join-Path $aclRoot '.description-managed-root.json') -PathType Leaf))
  Check 'windows_acl.bootstrap_marker_acl' (@($bootstrapReceipt.marker_acl.rules | Where-Object {
    $_.sid -in @('S-1-5-18', 'S-1-5-32-544', $serviceSid) -and
    $_.rights -eq [long][System.Security.AccessControl.FileSystemRights]::FullControl -and
    -not $_.inherited
  }).Count -eq 3 -and @($bootstrapReceipt.marker_acl.rules | Where-Object {
    $_.sid -in @('S-1-5-11', 'S-1-1-0', 'S-1-5-32-545', 'S-1-5-4')
  }).Count -eq 0)
  Check 'windows_acl.bootstrap_existing_service_access' (@(
      (Get-AclProjection $packageChild).rules | Where-Object {
        $_.sid -eq $serviceSid -and
        $_.rights -eq [long][System.Security.AccessControl.FileSystemRights]::FullControl
      }).Count -eq 1)
  $packageRead = $false
  try {
    $stream = [System.IO.File]::OpenRead($packageChild)
    $stream.Dispose()
    $packageRead = $true
  } catch {
    $packageRead = $false
  }
  Check 'windows_acl.bootstrap_existing_service_read' $packageRead
  $inheritedProbe = Join-Path $aclRoot 'bootstrap-inherited-probe'
  $null = New-Item -ItemType Directory -Path $inheritedProbe -ErrorAction Stop
  $inheritedFile = Join-Path $inheritedProbe 'payload.txt'
  [System.IO.File]::WriteAllText(
    $inheritedFile,
    'service-access',
    (New-Object System.Text.UTF8Encoding($false)))
  Check 'windows_acl.bootstrap_new_child_inherits_service' (@(
      (Get-AclProjection $inheritedProbe).rules | Where-Object {
        $_.sid -eq $serviceSid -and
        $_.inherited -and
        $_.rights -eq [long][System.Security.AccessControl.FileSystemRights]::FullControl
      }).Count -eq 1)
  Check 'windows_acl.bootstrap_new_file_inherits_service' (@(
      (Get-AclProjection $inheritedFile).rules | Where-Object {
        $_.sid -eq $serviceSid -and
        $_.inherited -and
        $_.rights -eq [long][System.Security.AccessControl.FileSystemRights]::FullControl
      }).Count -eq 1)
  Remove-Item -LiteralPath $inheritedProbe -Recurse -Force -ErrorAction SilentlyContinue
  $operatorAuthorization = Get-ManagedServiceAuthorization $serviceSid
  Check 'windows_acl.administrator_runtime_allowed' (
    $operatorAuthorization.administrator -eq $true)
  $config = [pscustomobject]@{
    package_root=$packageRoot
    output_root=$outputRoot
    state_root=$stateRoot
    token_file=$tokenPath
  }
  $managedPaths = @(Get-ManagedEndpointPaths $config $endpointRoot $configPath $installIdPath)
  $commonParent = Get-ManagedCommonParent $managedPaths
  Check 'windows_acl.common_parent_from_config' ((Normalize-PathIdentity $commonParent) -eq
    (Normalize-PathIdentity $aclRoot))
  $aclRows = @(Get-ManagedEndpointAclRows $config $endpointRoot $configPath $installIdPath $taskFile)
  Check 'windows_acl.paths_deduplicated' (@($aclRows.path | Select-Object -Unique).Count -eq $aclRows.Count)
  $commonRows = @($aclRows | Where-Object {
    (Normalize-PathIdentity ([string]$_.path)) -eq (Normalize-PathIdentity $commonParent)
  })
  Check 'windows_acl.common_parent_inheriting' ($commonRows.Count -eq 1 -and
    (-not $commonRows[0].recurse) -and (-not $commonRows[0].non_inheriting))
  $authenticated = New-Object System.Security.Principal.SecurityIdentifier -ArgumentList 'S-1-5-11'
  $broadAcl = Get-Acl -LiteralPath $aclRoot
  $broadRule = New-Object System.Security.AccessControl.FileSystemAccessRule -ArgumentList @(
    $authenticated,
    [System.Security.AccessControl.FileSystemRights]::Modify,
    ([System.Security.AccessControl.InheritanceFlags]::ContainerInherit -bor
      [System.Security.AccessControl.InheritanceFlags]::ObjectInherit),
    [System.Security.AccessControl.PropagationFlags]::None,
    [System.Security.AccessControl.AccessControlType]::Allow)
  [void]$broadAcl.AddAccessRule($broadRule)
  Set-Acl -LiteralPath $aclRoot -AclObject $broadAcl
  $markerPath = Join-Path $aclRoot '.description-managed-root.json'
  $siblingBefore = Get-AclProjection $siblingRoot
  Set-ManagedEndpointAcls $config $endpointRoot $configPath $installIdPath $taskFile
  Assert-ManagedEndpointAcls $config $endpointRoot $configPath $installIdPath $taskFile
  $broadFound = @()
  foreach ($row in $aclRows) {
    $broadFound += @((Get-AclProjection $row.path).rules | Where-Object {
      $_.sid -in @('S-1-5-11', 'S-1-1-0', 'S-1-5-32-545', 'S-1-5-4')
    })
  }
  Check 'windows_acl.broad_principals_removed' ($broadFound.Count -eq 0)
  $operatorSid = $serviceSid
  Check 'windows_acl.administrator_owns_boundary' (
    (Get-AclProjection $aclRoot).owner_sid -in @('S-1-5-32-544', $operatorSid))
  $managedAclChecks = @(
    $packageChild,
    $tokenPath,
    $markerPath,
    $ownershipPath,
    $endpointRoot
  ) | ForEach-Object {
    $rules = @((Get-AclProjection $_).rules)
    @($rules).Count -eq 3 -and
      @($rules | Where-Object {
        $_.sid -in @('S-1-5-18', 'S-1-5-32-544', $serviceSid) -and
        $_.rights -eq [long][System.Security.AccessControl.FileSystemRights]::FullControl
      }).Count -eq 3
  }
  Check 'windows_acl.managed_principals_only' (@($managedAclChecks | Where-Object { -not $_ }).Count -eq 0)
  Assert-ManagedRuntimeLeafAcl $ownershipPath $stateRoot $serviceSid
  Check 'windows_acl.runtime_self_protected' $true
  $testPython = $env:DESCRIPTION_TEST_PYTHON
  if (-not $testPython) { $testPython = (Get-Command python.exe -ErrorAction Stop).Source }
  & $testPython -I -B -c 'import os,sys,tempfile; p=sys.argv[1]; fd,t=tempfile.mkstemp(dir=os.path.dirname(p)); os.write(fd,bytes([123,125])); os.close(fd); os.replace(t,p)' $ownershipPath
  Check 'windows_acl.atomic_replace_exit' ($LASTEXITCODE -eq 0)
  $atomicAcl = Get-AclProjection $ownershipPath
  Assert-ManagedRuntimeLeafAcl $ownershipPath $stateRoot $serviceSid
  Assert-ManagedEndpointAcls $config $endpointRoot $configPath $installIdPath $taskFile
  Check 'windows_acl.atomic_replace_inherited' (-not $atomicAcl.protected -and
    @($atomicAcl.rules | Where-Object { $_.inherited }).Count -eq 3)
  function Assert-RuntimeLeafRejected([string]$Name, [string]$Path = $ownershipPath) {
    $rejected = $false
    try { Assert-ManagedRuntimeLeafAcl $Path $stateRoot $serviceSid } catch {
      $rejected = $_.Exception.Message -match 'windows_managed_acl_(invalid|unsafe)|windows_recovery_target_unsafe'
    }
    Check ('windows_acl.runtime_refuses_' + $Name) $rejected
  }
  $stateAclBefore = Get-Acl -LiteralPath $stateRoot
  $unprotectedState = Get-Acl -LiteralPath $stateRoot
  $unprotectedState.SetAccessRuleProtection($false, $true)
  Set-Acl -LiteralPath $stateRoot -AclObject $unprotectedState
  Assert-RuntimeLeafRejected 'unprotected_parent'
  Set-Acl -LiteralPath $stateRoot -AclObject $stateAclBefore
  $extraInheritedState = Get-Acl -LiteralPath $stateRoot
  [void]$extraInheritedState.AddAccessRule((New-ManagedAccessRule 'S-1-5-11' `
    ([System.Security.AccessControl.FileSystemRights]::Read) `
    ([System.Security.AccessControl.InheritanceFlags]::ObjectInherit)))
  Set-Acl -LiteralPath $stateRoot -AclObject $extraInheritedState
  Assert-RuntimeLeafRejected 'extra_inherited'
  Set-Acl -LiteralPath $stateRoot -AclObject $stateAclBefore
  $leafAclBefore = Get-Acl -LiteralPath $ownershipPath
  $extraLeaf = Get-Acl -LiteralPath $ownershipPath
  [void]$extraLeaf.AddAccessRule((New-ManagedAccessRule 'S-1-5-11' `
    ([System.Security.AccessControl.FileSystemRights]::Read) `
    ([System.Security.AccessControl.InheritanceFlags]::None)))
  Set-Acl -LiteralPath $ownershipPath -AclObject $extraLeaf
  Assert-RuntimeLeafRejected 'extra_explicit'
  Set-Acl -LiteralPath $ownershipPath -AclObject $leafAclBefore
  $denyLeaf = Get-Acl -LiteralPath $ownershipPath
  [void]$denyLeaf.AddAccessRule((New-Object System.Security.AccessControl.FileSystemAccessRule -ArgumentList @(
    (New-Object System.Security.Principal.SecurityIdentifier -ArgumentList $serviceSid),
    [System.Security.AccessControl.FileSystemRights]::Read,
    [System.Security.AccessControl.AccessControlType]::Deny)))
  Set-Acl -LiteralPath $ownershipPath -AclObject $denyLeaf
  Assert-RuntimeLeafRejected 'deny'
  Set-Acl -LiteralPath $ownershipPath -AclObject $leafAclBefore
  $serviceOwned = Get-Acl -LiteralPath $ownershipPath
  $serviceOwned.SetOwner((New-Object System.Security.Principal.SecurityIdentifier -ArgumentList $serviceSid))
  Set-Acl -LiteralPath $ownershipPath -AclObject $serviceOwned
  Assert-RuntimeLeafRejected 'service_owner'
  Set-Acl -LiteralPath $ownershipPath -AclObject $leafAclBefore
  $linkPath = Join-Path $stateRoot 'cad-ownership-link.json'
  $null = New-Item -ItemType SymbolicLink -Path $linkPath -Target $ownershipPath -ErrorAction Stop
  Assert-RuntimeLeafRejected 'reparse' $linkPath
  Remove-Item -LiteralPath $linkPath -Force
  Set-ManagedAcl $ownershipPath $true $serviceSid
  $outsideState = Join-Path $siblingRoot 'escaped-state'
  $null = New-Item -ItemType Directory -Path $outsideState
  $outsideSentinel = Join-Path $outsideState 'sentinel.txt'
  [System.IO.File]::WriteAllText($outsideSentinel, 'outside-must-not-change')
  $outsideAclBefore = Get-AclProjection $outsideState
  $outsideFileAclBefore = Get-AclProjection $outsideSentinel
  $outsideHashBefore = Sha256 $outsideSentinel
  $junctionRoot = Join-Path $aclRoot 'outside-junction'
  $null = New-Item -ItemType Junction -Path $junctionRoot -Target $siblingRoot -ErrorAction Stop
  $junctionConfig = [pscustomobject]@{
    package_root=$packageRoot
    output_root=$outputRoot
    state_root=(Join-Path $junctionRoot 'escaped-state')
    token_file=$tokenPath
  }
  $junctionRejected = $false
  try { Set-ManagedEndpointAcls $junctionConfig $endpointRoot $configPath $installIdPath $taskFile } catch {
    $junctionRejected = $_.Exception.Message -match 'windows_recovery_target_unsafe'
  }
  Check 'windows_acl.ancestor_junction_refused_before_write' ($junctionRejected -and
    (Get-AclProjection $outsideState).projection_sha256 -eq $outsideAclBefore.projection_sha256 -and
    (Get-AclProjection $outsideSentinel).projection_sha256 -eq $outsideFileAclBefore.projection_sha256 -and
    (Sha256 $outsideSentinel) -eq $outsideHashBefore)
  [System.IO.Directory]::Delete($junctionRoot)
  Check 'windows_acl.bootstrap_transaction_source' (
    $bootstrapSource.Contains('PostflightScript') -and
    $bootstrapSource.Contains('Get-ManagedTreeSnapshot') -and
    $bootstrapSource.Contains('Assert-ServiceInherited') -and
    $bootstrapSource.Contains('Set-ManagedAcl $root $serviceSid') -and
    $bootstrapSource.Contains('managed_root_transaction_failed'))
  $powershellExe = [System.Diagnostics.Process]::GetCurrentProcess().MainModule.FileName
  $otherLocalUser = Get-LocalUser | Where-Object {
    [string]$_.SID.Value -ne $serviceSid
  } | Select-Object -First 1
  if ($null -ne $otherLocalUser) {
    $mismatchRoot = Join-Path $driveRoot (
      'description-managed-mismatch-' + [guid]::NewGuid().ToString('N'))
    $null = New-Item -ItemType Directory -Path $mismatchRoot -Force
    $mismatchBefore = Get-AclProjection $mismatchRoot
    $mismatchOutput = @(& $powershellExe -NoProfile -File $bootstrapScript `
      -ManagedRoot $mismatchRoot -ServiceAccount ([string]$otherLocalUser.SID.Value))
    $mismatchExit = $LASTEXITCODE
    $mismatchReceipt = ($mismatchOutput | Select-Object -Last 1) | ConvertFrom-Json
    Check 'windows_acl.bootstrap_operator_runtime_mismatch' (
      $mismatchExit -ne 0 -and
      $mismatchReceipt.code -eq 'managed_root_operator_runtime_mismatch' -and
      -not (Test-Path -LiteralPath (
        Join-Path $mismatchRoot '.description-managed-root.json')) -and
      (Get-AclProjection $mismatchRoot).projection_sha256 -eq
        $mismatchBefore.projection_sha256)
    Remove-Item -LiteralPath $mismatchRoot -Recurse -Force -ErrorAction SilentlyContinue
  }
  $rollbackRoot = Join-Path $driveRoot ('description-managed-rollback-' + [guid]::NewGuid().ToString('N'))
  $rollbackChild = Join-Path $rollbackRoot 'endpoint-state/lifecycle/gen/runtime/site-packages'
  $null = New-Item -ItemType Directory -Path $rollbackChild -Force
  $rollbackFile = Join-Path $rollbackChild 'description_pipeline.py'
  [System.IO.File]::WriteAllText($rollbackFile, 'before', (New-Object System.Text.UTF8Encoding($false)))
  $rollbackBefore = Get-AclProjection $rollbackRoot
  $rollbackChildRestoreAcl = Get-Acl -LiteralPath $rollbackChild
  $rollbackFileBefore = Get-AclProjection $rollbackFile
  $rollbackDeepAcl = New-Object System.Security.AccessControl.DirectorySecurity
  $rollbackDeepAcl.SetOwner(
    (New-Object System.Security.Principal.SecurityIdentifier -ArgumentList 'S-1-5-32-544'))
  $rollbackDeepAcl.SetAccessRuleProtection($true, $false)
  Set-Acl -LiteralPath $rollbackChild -AclObject $rollbackDeepAcl
  $rollbackChildBefore = Get-AclProjection $rollbackChild
  $failureScript = Join-Path ([System.IO.Path]::GetTempPath()) (
    'bootstrap-failing-postflight-' + [guid]::NewGuid().ToString('N') + '.ps1')
  [System.IO.File]::WriteAllText(
    $failureScript,
    "exit 7`n",
    (New-Object System.Text.UTF8Encoding($false)))
  $rollbackOutput = @(& $powershellExe -NoProfile -File $bootstrapScript `
    -ManagedRoot $rollbackRoot -ServiceAccount $serviceSid -PostflightScript $failureScript)
  $rollbackExit = $LASTEXITCODE
  $rollbackReceipt = ($rollbackOutput | Select-Object -Last 1) | ConvertFrom-Json
  Check 'windows_acl.bootstrap_postflight_failure_rolls_back' (
    $rollbackExit -ne 0 -and
    -not (Test-Path -LiteralPath (
      Join-Path $rollbackRoot '.description-managed-root.json')) -and
    (Get-AclProjection $rollbackRoot).projection_sha256 -eq $rollbackBefore.projection_sha256 -and
    (Get-AclProjection $rollbackChild).projection_sha256 -eq $rollbackChildBefore.projection_sha256 -and
    $rollbackReceipt.rollback.passed -eq $true) (
      'exit=' + [string]$rollbackExit +
      ' receipt=' + ($rollbackReceipt | ConvertTo-Json -Depth 8 -Compress))
  Set-Acl -LiteralPath $rollbackChild -AclObject $rollbackChildRestoreAcl
  Check 'windows_acl.bootstrap_deep_file_acl_rolls_back' (
    (Get-AclProjection $rollbackFile).projection_sha256 -eq $rollbackFileBefore.projection_sha256)
  $emptyRoot = Join-Path $driveRoot ('description-managed-empty-' + [guid]::NewGuid().ToString('N'))
  $emptyChild = Join-Path $emptyRoot 'endpoint-state/lifecycle/gen/runtime/site-packages'
  $null = New-Item -ItemType Directory -Path $emptyChild -Force
  $emptyFile = Join-Path $emptyChild 'description_pipeline.py'
  [System.IO.File]::WriteAllText($emptyFile, 'package', (New-Object System.Text.UTF8Encoding($false)))
  $emptyChildBefore = Get-Acl -LiteralPath $emptyChild
  $emptyAcl = New-Object System.Security.AccessControl.DirectorySecurity
  $emptyAcl.SetOwner(
    (New-Object System.Security.Principal.SecurityIdentifier -ArgumentList 'S-1-5-32-544'))
  $emptyAcl.SetAccessRuleProtection($true, $false)
  Set-Acl -LiteralPath $emptyChild -AclObject $emptyAcl
  $emptyOutput = @(& $powershellExe -NoProfile -File $bootstrapScript `
    -ManagedRoot $emptyRoot -ServiceAccount $serviceSid)
  $emptyExit = $LASTEXITCODE
  $emptyReceipt = ($emptyOutput | Select-Object -Last 1) | ConvertFrom-Json
  $emptyRead = $false
  try {
    $stream = [System.IO.File]::OpenRead($emptyFile)
    $stream.Dispose()
    $emptyRead = $true
  } catch {
    $emptyRead = $false
  }
  Check 'windows_acl.bootstrap_deep_empty_dacl_repaired' (
    $emptyExit -eq 0 -and
    $emptyReceipt.passed -eq $true -and
    $emptyRead -and
    @((Get-AclProjection $emptyChild).rules | Where-Object {
      $_.sid -eq $serviceSid -and
      $_.rights -eq [long][System.Security.AccessControl.FileSystemRights]::FullControl
    }).Count -eq 1) (
      'exit=' + [string]$emptyExit +
      ' receipt=' + ($emptyReceipt | ConvertTo-Json -Depth 8 -Compress))
  Set-Acl -LiteralPath $emptyChild -AclObject $emptyChildBefore
  Remove-Item -LiteralPath $emptyRoot -Recurse -Force -ErrorAction SilentlyContinue
  Remove-Item -LiteralPath $failureScript -Force -ErrorAction SilentlyContinue
  Remove-Item -LiteralPath $rollbackRoot -Recurse -Force -ErrorAction SilentlyContinue
  $badTokenAcl = Get-Acl -LiteralPath $tokenPath
  [void]$badTokenAcl.AddAccessRule((New-Object System.Security.AccessControl.FileSystemAccessRule -ArgumentList @(
    $authenticated,
    [System.Security.AccessControl.FileSystemRights]::Read,
    [System.Security.AccessControl.InheritanceFlags]::None,
    [System.Security.AccessControl.PropagationFlags]::None,
    [System.Security.AccessControl.AccessControlType]::Allow)))
  Set-Acl -LiteralPath $tokenPath -AclObject $badTokenAcl
  $badBefore = Get-AclProjection $tokenPath
  $assertRejected = $false
  try {
    Assert-ManagedEndpointAcls $config $endpointRoot $configPath $installIdPath $taskFile
  } catch {
    $assertRejected = $_.Exception.Message -match 'windows_managed_acl_invalid'
  }
  $badAfter = Get-AclProjection $tokenPath
  Check 'windows_acl.snapshot_assert_is_read_only' ($assertRejected -and
    $badBefore.projection_sha256 -eq $badAfter.projection_sha256)
  Check 'windows_acl.sibling_untouched' (
    (Get-AclProjection $siblingRoot).projection_sha256 -eq $siblingBefore.projection_sha256)
  Remove-Item -LiteralPath $aclRoot -Recurse -Force -ErrorAction SilentlyContinue
  Remove-Item -LiteralPath $siblingRoot -Recurse -Force -ErrorAction SilentlyContinue
} else {
  Write-Host 'SKIP windows_acl.runtime (not a Windows host)'
}

# ---- harness: stub only the OS boundary -------------------------------------
$script:TaskMissing = $false
$script:TaskState = 'Ready'
$script:ProcScenario = 'missing'
$script:AccountEnv = @{}
$script:ServedStateRoot = ''
$script:ProbeScenario = 'refused'
$script:SettleOutcomes = @()
$script:CurrentAction = $null
$script:Starts = 0
$script:Sleeps = [System.Collections.Generic.List[int]]::new()
$script:CallLog = [System.Collections.Generic.List[string]]::new()
$script:OldHealthOk = $true
$script:HealthProbes = [System.Collections.Generic.List[string]]::new()
function Get-ScheduledTask {
  [CmdletBinding()] param([string]$TaskName, [string]$TaskPath)
  if ($script:TaskMissing) { return $null }
  $actions = if ($null -eq $script:CurrentAction) { @() } else { @($script:CurrentAction) }
  return [pscustomobject]@{ State = $script:TaskState; Actions = $actions }
}
function Start-ScheduledTask { [CmdletBinding()] param([string]$TaskName, [string]$TaskPath) $script:Starts++; $script:CallLog.Add('start') }
function Stop-ScheduledTask { [CmdletBinding()] param([string]$TaskName, [string]$TaskPath) $script:CallLog.Add('stop') }
function Register-ScheduledTask {
  [CmdletBinding()] param([string]$TaskName, [string]$TaskPath, [string]$Xml, [switch]$Force)
  if ($PSBoundParameters.ContainsKey('TaskPath') -and -not $TaskPath) {
    throw 'root task registration must omit an empty TaskPath'
  }
  $script:CallLog.Add('register')
  return $null
}
$rootTaskRegistration = $true
try { Register-ManagedTask -TaskName 'root-fixture' -TaskPath '' -TaskXml '<Task />' } catch { $rootTaskRegistration = $false }
Check 'windows.recovery.root_task_registration_omits_empty_path' $rootTaskRegistration
$script:CallLog.Clear()
function Get-Process {
  [CmdletBinding()] param([int]$Id)
  switch ($script:ProcScenario) {
    'missing' {
      Write-Error ('Cannot find a process with the process ID: {0}.' -f $Id) -Category ObjectNotFound
      return
    }
    'denied' {
      Write-Error 'process query denied by injected observer failure' -Category PermissionDenied
      return
    }
    'alive' {
      return [pscustomobject]@{
        Id = $Id; Path = 'C:\gen\runtime\Scripts\description.exe'; StartTime = [datetime]'2026-10-15T00:00:00Z' }
    }
    default { return $null }
  }
}
function cmd {
  switch ($script:ProbeScenario) {
    'refused' { $global:LASTEXITCODE = 3; return 'LOCK_REFUSED' }
    'free' { $global:LASTEXITCODE = 0; return '' }
    default { $global:LASTEXITCODE = 7; return '' }
  }
}
function Wait-Settled {
  param([string]$configPath, [object]$binding)
  if ($script:SettleOutcomes.Count -eq 0) { throw 'no settle outcome queued' }
  $next = $script:SettleOutcomes[0]
  $script:SettleOutcomes = @($script:SettleOutcomes | Select-Object -Skip 1)
  if ($next -eq 'throw') { throw 'the settle window observed a refusal' }
  return @{ health = @{ install = @{ config_digest = 'digest' } }; settled_seconds = 184; listener_pid = 4242 }
}
function Wait-SettleRetryDelay { param([int]$Seconds = 60) $script:Sleeps.Add($Seconds) }
function Health-Probe {
  param([string]$configPath, [object]$binding)
  $script:HealthProbes.Add([string]$binding.version)
  if (-not $script:OldHealthOk) { throw 'the previous endpoint did not answer /health' }
  return @{ ready = $true }
}
function Write-Receipt {
  param([string]$path, $payload)
  $parent = Split-Path -Parent $path
  if (-not (Test-Path -LiteralPath $parent)) { New-Item -ItemType Directory -Path $parent | Out-Null }
  [System.IO.File]::WriteAllText($path, ($payload | ConvertTo-Json -Depth 8 -Compress))
}
function Get-AccountEnvironment { param([string]$Name) return [string]$script:AccountEnv[$Name] }
function Set-AccountEnvironment { param([string]$Name, [string]$Value) $script:AccountEnv[$Name] = $Value }
function Get-ServedStateRoot { param([string]$configPath) return [string]$script:ServedStateRoot }

$root = Join-Path ([System.IO.Path]::GetTempPath()) ("il-settle-" + [guid]::NewGuid().ToString('N'))
$generation = Join-Path $root 'generation'
$promotions = Join-Path $root 'instance-promotions'
$previous = Join-Path $promotions 'previous-test'
$null = New-Item -ItemType Directory -Path $generation -Force
$null = New-Item -ItemType Directory -Path $previous -Force
[System.IO.File]::WriteAllText((Join-Path $previous 'task.xml'), '<Task />')
$configPath = Join-Path $generation 'endpoint.json'
[System.IO.File]::WriteAllText($configPath, ('{{"state_root":"{0}"}}' -f ($root -replace '\\', '/')))
[System.IO.File]::WriteAllText(
  (Join-Path $generation 'stage-receipt.json'),
  (@{passed=$true; verb='stage'; generation=(Normalize-PathIdentity $generation);
     launch_contract='python_module_v1'; version='1.5.0';
     source_commit=('b' * 40); source_sha256=('b' * 64)} | ConvertTo-Json -Compress))
$lockPath = Join-Path $root '.endpoint.lock'
$binding = @{ task_name = 'test-endpoint'; port = 8765; state_root = $root; generation = $generation }
Check 'launch.python_entrypoint' ((Normalize-PathIdentity (Runtime-Launcher $generation)) -eq
  (Normalize-PathIdentity (Join-Path $generation 'runtime\Scripts\python.exe')))
Check 'launch.module_arguments' ((Runtime-LauncherArguments $generation) -eq
  ('-I -B -m description_pipeline.orchestration.windows serve --config "' + (Config-Path $generation) + '"'))
Check 'launch.candidate_action_accepted' (Test-Runtime-Launcher (Runtime-Launcher $generation) `
  (Runtime-LauncherArguments $generation) $generation)
Check 'launch.bytecode_flag_required' (-not (Test-Runtime-Launcher (Runtime-Launcher $generation) `
  ((Runtime-LauncherArguments $generation) -replace ' -B', '') $generation))
Check 'launch.interactive_flag_refused' (-not (Test-Runtime-Launcher (Runtime-Launcher $generation) `
  ((Runtime-LauncherArguments $generation) -replace '^-I ', '-i ') $generation))
Check 'launch.module_case_refused' (-not (Test-Runtime-Launcher (Runtime-Launcher $generation) `
  ((Runtime-LauncherArguments $generation) -replace 'description_pipeline', 'Description_Pipeline') $generation))
Check 'launch.argument_suffix_refused' (-not (Test-Runtime-Launcher (Runtime-Launcher $generation) `
  ((Runtime-LauncherArguments $generation) + ' --foreign') $generation))
$windowsConfigPath = 'C:/root/old/generation/endpoint.json'
$windowsArguments = (
  '-I -B -m description_pipeline serve --config "C:\root\old\generation\endpoint.json"')
Check 'task.action_config_path_is_separator_neutral' (
  Test-ActionConfigArgument $windowsArguments $windowsConfigPath)
$textualWindowsConfigPath = 'C:/root/name --config text/generation/endpoint.json'
Check 'task.action_config_text_inside_quoted_value_is_not_an_option' (
  Test-ActionConfigArgument (
    '-I -B -m description_pipeline serve --config ' +
      '"C:\root\name --config text\generation\endpoint.json"'
  ) $textualWindowsConfigPath)
Check 'task.action_config_path_suffix_refused' (
  -not (Test-ActionConfigArgument (
    '-I -B -m description_pipeline serve --config "C:\root\old\generation\endpoint.json.bak"'
  ) $windowsConfigPath))
Check 'task.action_config_path_missing_refused' (
  -not (Test-ActionConfigArgument '-I -B -m description_pipeline serve' $windowsConfigPath))
Check 'task.action_config_path_duplicate_refused' (
  -not (Test-ActionConfigArgument (
    $windowsArguments + ' --config "C:\root\old\generation\endpoint.json"'
  ) $windowsConfigPath))
Check 'task.action_config_path_unquoted_duplicate_refused' (
  -not (Test-ActionConfigArgument (
    $windowsArguments + ' --config C:\foreign\generation\endpoint.json'
  ) $windowsConfigPath))
Check 'task.action_config_path_equals_duplicate_refused' (
  -not (Test-ActionConfigArgument (
    $windowsArguments + ' --config=C:\foreign\generation\endpoint.json'
  ) $windowsConfigPath))
Check 'task.action_config_path_missing_value_duplicate_refused' (
  -not (Test-ActionConfigArgument (
    $windowsArguments + ' --config'
  ) $windowsConfigPath))
Check 'task.action_config_path_equals_form_refused' (
  -not (Test-ActionConfigArgument (
    '-I -B -m description_pipeline serve --config=C:\root\old\generation\endpoint.json'
  ) $windowsConfigPath))
Check 'task.action_config_path_trailing_option_refused' (
  -not (Test-ActionConfigArgument (
    $windowsArguments + ' --conf C:\foreign\generation\endpoint.json'
  ) $windowsConfigPath))
$sourceGeneration = Join-Path $root 'lifecycle/source-generation'
$null = New-Item -ItemType Directory -Path $sourceGeneration
$opaqueTask = [pscustomobject]@{
  Actions=@([pscustomobject]@{
    Execute=(Runtime-Python $sourceGeneration)
    Arguments='-I -B -m description_pipeline serve --config "legacy-shaped.json"'
    WorkingDirectory=$root
  })
}
Check 'task.source_generation_from_execute' (
  (Normalize-PathIdentity (Get-TaskGeneration $opaqueTask)) -eq
  (Normalize-PathIdentity $sourceGeneration))
$foreignExecuteTask = [pscustomobject]@{
  Actions=@([pscustomobject]@{
    Execute=(Join-Path $sourceGeneration 'runtime/Scripts/description.exe')
    Arguments='serve --config "ignored.json"'
    WorkingDirectory=$root
  })
}
$message = ''
try { $null = Get-TaskGeneration $foreignExecuteTask } catch { $message = $_.Exception.Message }
Check 'task.foreign_source_execute_refused' ($message -match 'managed generation runtime')
$typedGeneration = Join-Path $root 'typed-contract-generation'
$null = New-Item -ItemType Directory -Path $typedGeneration
$typedReceipt = Join-Path $typedGeneration 'stage-receipt.json'
[System.IO.File]::WriteAllText(
  $typedReceipt,
  (@{passed=1; verb='stage'; generation=(Normalize-PathIdentity $typedGeneration);
     launch_contract='python_module_v1'; version='1.5.0';
     source_commit=('b' * 40); source_sha256=('b' * 64)} | ConvertTo-Json -Compress))
$lookup = Find-GenerationLaunchContract $typedGeneration
Check 'launch.numeric_passed_refused' (
  (-not $lookup.passed) -and ($lookup.message -match 'JSON boolean true'))
[System.IO.File]::WriteAllText(
  $typedReceipt,
  (@{passed=$true; verb='stage'; generation=(Normalize-PathIdentity $typedGeneration);
     launch_contract=@('python_module_v1'); version='1.5.0';
     source_commit=('b' * 40); source_sha256=('b' * 64)} | ConvertTo-Json -Compress))
$lookup = Find-GenerationLaunchContract $typedGeneration
Check 'launch.array_contract_refused' (
  (-not $lookup.passed) -and ($lookup.message -match 'launch_contract is not a JSON string'))
[System.IO.File]::WriteAllText(
  $typedReceipt,
  (@{passed=$true; verb='stage'; generation=123;
     launch_contract='python_module_v1'; version='1.5.0';
     source_commit=('b' * 40); source_sha256=('b' * 64)} | ConvertTo-Json -Compress))
$lookup = Find-GenerationLaunchContract $typedGeneration
Check 'launch.numeric_generation_refused' (
  (-not $lookup.passed) -and ($lookup.message -match 'generation is not an exact string'))
$unsupportedGeneration = Join-Path $root 'unsupported-generation'
$null = New-Item -ItemType Directory -Path $unsupportedGeneration
[System.IO.File]::WriteAllText(
  (Join-Path $unsupportedGeneration 'stage-receipt.json'),
  '{"passed":true,"verb":"stage","launch_contract":"foreign_launcher_v1"}')
$message = ''
try { $null = Test-Runtime-Launcher (Runtime-Launcher $unsupportedGeneration) `
  (Runtime-LauncherArguments $unsupportedGeneration) $unsupportedGeneration } catch { $message = $_.Exception.Message }
Check 'launch.unsupported_contract_refused' ($message -match 'stage_launch_contract_unsupported')
$emptyGeneration = Join-Path $root 'empty-contract-generation'
$null = New-Item -ItemType Directory -Path $emptyGeneration
[System.IO.File]::WriteAllText((Join-Path $emptyGeneration 'stage-receipt.json'), '{}')
$message = ''
try { $null = Test-Runtime-Launcher (Runtime-Launcher $emptyGeneration) `
  (Runtime-LauncherArguments $emptyGeneration) $emptyGeneration } catch { $message = $_.Exception.Message }
Check 'launch.empty_contract_refused' ($message -match 'stage_launch_contract_unsupported')
$singleActionTask = [pscustomobject]@{Actions=@([pscustomobject]@{Execute='first'})}
Check 'task.exact_one_action_accepted' (
  $null -eq (Assert-Single-TaskAction $singleActionTask))
$multiActionTask = [pscustomobject]@{Actions=@(
  [pscustomobject]@{Execute='first'},
  [pscustomobject]@{Execute='second'}
)}
$message = ''
try { Assert-Single-TaskAction $multiActionTask } catch { $message = $_.Exception.Message }
Check 'task.multi_action_refused' ($message -match 'task_ambiguous')
function Resolve-Sid { param([string]$Identity) return $Identity }
$listenerFixtureRoot = Join-Path ([System.IO.Path]::GetTempPath()) (
  'il-listener-' + [guid]::NewGuid().ToString('N'))
$listenerGeneration = Join-Path $listenerFixtureRoot 'managed/generation'
$listenerRuntime = Join-Path $listenerGeneration 'runtime'
$listenerScripts = Join-Path $listenerRuntime 'Scripts'
$listenerBaseHome = Join-Path $listenerFixtureRoot 'Python312'
$listenerSystemRoot = Join-Path $listenerFixtureRoot 'Windows'
$listenerSchedulerRoot = Join-Path $listenerSystemRoot 'System32'
foreach ($directory in @(
    $listenerGeneration, $listenerScripts, $listenerBaseHome, $listenerSchedulerRoot)) {
  $null = New-Item -ItemType Directory -Path $directory -Force
}
$listenerLauncher = Join-Path $listenerScripts 'python.exe'
$listenerBase = Join-Path $listenerBaseHome 'python.exe'
$listenerScheduler = Join-Path $listenerSchedulerRoot 'svchost.exe'
$listenerForeignSchedulerRoot = Join-Path $listenerSystemRoot 'Foreign/System32'
$null = New-Item -ItemType Directory -Path $listenerForeignSchedulerRoot -Force
$listenerForeignScheduler = Join-Path $listenerForeignSchedulerRoot 'svchost.exe'
foreach ($path in @($listenerLauncher, $listenerBase, $listenerScheduler)) {
  Write-Utf8NoBom $path ('fixture:' + $path)
}
Write-Utf8NoBom $listenerForeignScheduler ('fixture:' + $listenerForeignScheduler)
$listenerConfig = Join-Path $listenerGeneration 'endpoint.json'
Write-Utf8NoBom $listenerConfig '{"schema_version":"solidworks-to-urdf.endpoint/v1"}'
$listenerArguments = '-I -B -m description_pipeline serve --config "' +
  $listenerConfig + '"'
$listenerTaskSid = 'S-1-5-21-1-2-3-500'
$listenerProjection = [pscustomobject]@{
  principal=[pscustomobject]@{sid=$listenerTaskSid}
  action=[pscustomobject]@{
    execute=$listenerLauncher
    arguments=$listenerArguments
  }
}
$script:OriginalListenerSystemRoot = $env:SystemRoot
$env:SystemRoot = $listenerSystemRoot
function Write-ListenerPyvenv([string]$CommandRoot = '') {
  if (-not $CommandRoot) { $CommandRoot = $listenerRuntime }
  $text = @(
    ('home = ' + $listenerBaseHome)
    'include-system-site-packages = false'
    'version = 3.12.10'
    ('executable = ' + $listenerBase)
    ('command = "' + $listenerBase + '" -m venv "' + $CommandRoot + '"')
  ) -join "`n"
  Write-Utf8NoBom (Join-Path $listenerRuntime 'pyvenv.cfg') $text
}
function New-ListenerProcess(
    [int]$ProcessId,
    [int]$ParentProcessId,
    [string]$Image,
    [string]$Arguments,
    [datetime]$Created,
    [string]$Sid,
    [int]$SessionId) {
  return [pscustomobject]@{
    ProcessId=$ProcessId
    ParentProcessId=$ParentProcessId
    ExecutablePath=$Image
    CommandLine=('"' + $Image + '" ' + $Arguments).Trim()
    CreationDate=$Created
    OwnerSid=$Sid
    SessionId=$SessionId
  }
}
function Reset-ListenerProcesses([string]$Scenario = 'valid') {
  foreach ($path in @($listenerLauncher, $listenerBase, $listenerScheduler)) {
    Write-Utf8NoBom $path ('fixture:' + $path)
  }
  Write-ListenerPyvenv
  $scheduler = New-ListenerProcess 2512 900 $listenerScheduler `
    '-k netsvcs -p -s Schedule' ([datetime]'2026-10-27T00:00:00Z') `
    'S-1-5-18' 0
  $launcher = New-ListenerProcess 19344 2512 $listenerLauncher `
    $listenerArguments ([datetime]'2026-10-27T00:00:01Z') `
    $listenerTaskSid 1
  $listener = New-ListenerProcess 15124 19344 $listenerBase `
    $listenerArguments ([datetime]'2026-10-27T00:00:02Z') `
    $listenerTaskSid 1
  $script:SchedulerService = [pscustomobject]@{
    Name='Schedule'
    State='Running'
    ProcessId=2512
  }
  $script:ListenerPorts = @([pscustomobject]@{OwningProcess=15124})
  switch ($Scenario) {
    'multiple_listeners' {
      $script:ListenerPorts += [pscustomobject]@{OwningProcess=15125}
    }
    'multiple_rows_same_listener' {
      $script:ListenerPorts += [pscustomobject]@{OwningProcess=15124}
    }
    'missing_listener' {
      $script:ListenerPorts = @([pscustomobject]@{OwningProcess=15125})
    }
    'non_direct_listener' {
      $listener.ParentProcessId = 999
    }
    'foreign_launcher_parent' {
      $launcher.ParentProcessId = 999
    }
    'same_process' {
      $script:ListenerPorts[0].OwningProcess = 19344
    }
    'multiple_launchers' {
      $extraLauncher = New-ListenerProcess 19345 2512 $listenerLauncher `
        $listenerArguments ([datetime]'2026-10-27T00:00:01.500Z') `
        $listenerTaskSid 1
    }
    'launcher_arguments' {
      $launcher.CommandLine = '"' + $listenerLauncher + '" foreign-action'
    }
    'listener_arguments' {
      $listener.CommandLine = '"' + $listenerBase + '" foreign-action'
    }
    'launcher_sid' {
      $launcher.OwnerSid = 'S-1-5-21-1-2-3-501'
    }
    'listener_sid' {
      $listener.OwnerSid = 'S-1-5-21-1-2-3-501'
    }
    'launcher_session' {
      $launcher.SessionId = 2
    }
    'listener_session' {
      $listener.SessionId = 2
    }
    'scheduler_image' {
      $scheduler.ExecutablePath = $listenerForeignScheduler
      $scheduler.CommandLine = '"' + $listenerForeignScheduler + '" -k netsvcs -p -s Schedule'
    }
    'scheduler_service_missing' {
      $script:SchedulerService = $null
    }
    'scheduler_service_name' {
      $script:SchedulerService.Name = 'Foreign'
    }
    'scheduler_service_state' {
      $script:SchedulerService.State = 'Stopped'
    }
    'scheduler_service_pid' {
      $script:SchedulerService.ProcessId = 2513
    }
    'scheduler_sid' {
      $scheduler.OwnerSid = $listenerTaskSid
    }
    'scheduler_session' {
      $scheduler.SessionId = 1
    }
    'creation_order' {
      $listener.CreationDate = [datetime]'2026-10-27T00:00:00.500Z'
    }
    'foreign_pyvenv_command' {
      Write-ListenerPyvenv (Join-Path $listenerFixtureRoot 'foreign-runtime')
    }
    'missing_base' {
      Remove-Item -LiteralPath $listenerBase -Force
    }
  }
  $script:ListenerProcesses = @($scheduler, $launcher, $listener) +
    @(if ($extraLauncher) { $extraLauncher })
}
function Get-ListenerConnections {
  param([int]$Port)
  return @($script:ListenerPorts)
}
function Get-ListenerProcesses {
  return @($script:ListenerProcesses)
}
function Get-ScheduleService {
  return @($script:SchedulerService)
}
function Get-ProcessOwnerSid {
  param($Process)
  return [string]$Process.OwnerSid
}
function Get-CurrentListenerFacts {
  return @{
    listeners=@($script:ListenerPorts)
    processes=@($script:ListenerProcesses)
    scheduler_services=@($script:SchedulerService)
  }
}
Reset-ListenerProcesses
$evidence = Get-ListenerEvidence @{port=8765} $listenerProjection (Get-CurrentListenerFacts)
Check 'listener.direct_venv_child_bound' (
  ($evidence.schema_version -eq 'solidworks-to-urdf.windows-listener/v2') -and
  ($evidence.relation -eq 'direct_venv_child') -and
  ($evidence.scheduler_pid -eq 2512) -and
  ($evidence.launcher_pid -eq 19344) -and
  ($evidence.launcher_parent_pid -eq 2512) -and
  ($evidence.listener_pid -eq 15124) -and
  ($evidence.listener_parent_pid -eq 19344) -and
  ($evidence.process_arguments_sha256 -eq (Get-TextSha256 $listenerArguments)) -and
  ($evidence.base_interpreter_path -eq (Normalize-PathIdentity $listenerBase)))
$listenerSourceContext = [ordered]@{
  schema_version='solidworks-to-urdf.windows-source-state/v2'
  endpoint_root=(Normalize-PathIdentity $listenerGeneration)
  config_path=(Normalize-PathIdentity $listenerConfig)
  execute=(Normalize-PathIdentity $listenerLauncher)
  arguments=$listenerArguments
  principal_sid=$listenerTaskSid
  principal=[pscustomobject]@{sid=$listenerTaskSid}
  listener=$evidence
}
$staticBindingPassed = $true
try { Assert-ListenerSourceMatch $evidence $listenerSourceContext } catch {
  $staticBindingPassed = $false
}
Check 'listener.static_source_binding_passes' $staticBindingPassed
Reset-ListenerProcesses 'multiple_rows_same_listener'
$dualStackEvidence = Get-ListenerEvidence @{port=8765} $listenerProjection `
  (Get-CurrentListenerFacts)
Check 'listener.dual_stack_rows_same_pid_pass' (
  $dualStackEvidence.listener_pid -eq 15124)
foreach ($replacement in @(
    @('launcher', $listenerLauncher, 'replaced-launcher'),
    @('base', $listenerBase, 'replaced-base'),
    @('pyvenv', (Join-Path $listenerRuntime 'pyvenv.cfg'), 'replaced-pyvenv'))) {
  $originalBytes = [System.IO.File]::ReadAllBytes([string]$replacement[1])
  Write-Utf8NoBom ([string]$replacement[1]) ([string]$replacement[2])
  $message = ''
  try {
    Assert-ListenerSourceMatch $evidence $listenerSourceContext
  } catch { $message = $_.Exception.Message }
  Check ('listener.' + [string]$replacement[0] + '_replacement_refused') (
    $message -match 'differs|unreadable')
  [System.IO.File]::WriteAllBytes([string]$replacement[1], $originalBytes)
}
$changedListener = ($evidence | ConvertTo-Json -Compress | ConvertFrom-Json)
$changedListener.listener_start_utc = '2026-10-27T00:00:03Z'
$message = ''
try { Assert-ListenerMatch $evidence $changedListener } catch { $message = $_.Exception.Message }
Check 'listener.mid_switch_change_refused' ($message -match 'windows_listener_changed')
$sameProcessEvidence = ($evidence | ConvertTo-Json -Compress | ConvertFrom-Json)
$sameProcessEvidence.relation = 'same_process'
$message = ''
try { Assert-ListenerDocument $sameProcessEvidence } catch { $message = $_.Exception.Message }
Check 'listener.same_process_refused' ($message -match 'direct venv child lineage')
foreach ($case in @(
    @('multiple_listeners', 'windows_listener_ambiguous'),
    @('missing_listener', 'endpoint listener process is missing'),
    @('non_direct_listener', 'launcher child'),
    @('foreign_launcher_parent', 'exact scheduler parent'),
    @('same_process', 'not the task-action launcher child|ambiguous'),
    @('multiple_launchers', 'windows_listener_ambiguous'),
    @('launcher_arguments', 'ambiguous'),
    @('listener_arguments', 'projected task arguments'),
    @('launcher_sid', 'account differs'),
    @('listener_sid', 'account differs'),
    @('launcher_session', 'different sessions'),
    @('listener_session', 'different sessions'),
    @('scheduler_image', 'Task Scheduler host'),
    @('scheduler_service_missing', 'service process'),
    @('scheduler_service_name', 'service process'),
    @('scheduler_service_state', 'service process'),
    @('scheduler_service_pid', 'service process'),
    @('scheduler_sid', 'Task Scheduler host'),
    @('scheduler_session', 'Task Scheduler host'),
    @('creation_order', 'creation order'),
    @('foreign_pyvenv_command', 'another virtual environment'),
    @('missing_base', 'base interpreter is missing'))) {
  Reset-ListenerProcesses $case[0]
  $message = ''
  try {
    $null = Get-ListenerEvidence @{port=8765} $listenerProjection `
      (Get-CurrentListenerFacts)
  } catch {
    $message = $_.Exception.Message
  }
  Check ('listener.' + $case[0] + '_refused') ($message -match $case[1]) $message
}
Reset-ListenerProcesses
$foreignConfigArguments = '-I -B -m description_pipeline serve --config "' +
  (Join-Path $listenerGeneration 'foreign.json') + '"'
$message = ''
try {
  $null = Get-ListenerEvidence @{port=8765} ([pscustomobject]@{
      principal=$listenerProjection.principal
      action=[pscustomobject]@{
        execute=$listenerLauncher
        arguments=$foreignConfigArguments
      }
    }) (Get-CurrentListenerFacts)
} catch { $message = $_.Exception.Message }
Check 'listener.task_action_config_refused' ($message -match 'another configuration') $message
$message = ''
try {
  $null = Get-ListenerEvidence @{port=8765} $listenerProjection {
    throw 'injected process inventory failure'
  }
} catch {
  $message = $_.Exception.Message
}
Check 'listener.process_inventory_failure_is_not_identity_mismatch' (
  ($message -match 'injected process inventory failure') -and
  ($message -notmatch 'windows_listener_identity_mismatch')) $message
$env:SystemRoot = $script:OriginalListenerSystemRoot
Remove-Item -LiteralPath $listenerFixtureRoot -Recurse -Force -ErrorAction SilentlyContinue
function Get-NetTCPConnection {
  [CmdletBinding()] param([int]$LocalPort, [string]$State)
  return $null
}
function Get-CimInstance {
  [CmdletBinding()] param([string]$ClassName, [string]$Filter)
  return $null
}
function Get-Process {
  [CmdletBinding()] param([int]$Id)
  switch ($script:ProcScenario) {
    'missing' {
      Write-Error ('Cannot find a process with the process ID: {0}.' -f $Id) -Category ObjectNotFound
      return
    }
    'denied' {
      Write-Error 'process query denied by injected observer failure' -Category PermissionDenied
      return
    }
    'alive' {
      return [pscustomobject]@{
        Id=$Id; Path='C:\gen\runtime\Scripts\python.exe';
        StartTime=[datetime]'2026-10-15T00:00:00Z'}
    }
    default { return $null }
  }
}
$readyTask = [pscustomobject]@{State='Ready'; Actions=@([pscustomobject]@{Execute='first'})}
$message = ''
try { Assert-RunningTask $readyTask } catch { $message = $_.Exception.Message }
Check 'task.ready_health_refused' ($message -match 'task_not_running')
function Reset-State {
  $script:TaskMissing = $false
  $script:TaskState = 'Ready'
  $script:ProcScenario = 'missing'
  $script:ProbeScenario = 'refused'
  $script:SettleOutcomes = @()
  $script:Starts = 0
  $script:Sleeps.Clear()
  $script:CallLog.Clear()
  $script:AccountEnv.Clear()
  $script:ServedStateRoot = $root
  $script:OldHealthOk = $true
  $script:HealthProbes.Clear()
  if (Test-Path -LiteralPath $lockPath) { Remove-Item -LiteralPath $lockPath -Force }
  $receipt = Join-Path $promotions 'lock-refusal.json'
  if (Test-Path -LiteralPath $receipt) { Remove-Item -LiteralPath $receipt -Force }
}

# ---- ownership-fact units (real interpreters, stubbed OS boundary) ----------
Reset-State
$cleanConfig = Join-Path $generation 'clean-endpoint.json'
[System.IO.File]::WriteAllText($cleanConfig, '{"schema_version":"solidworks-to-urdf.endpoint/v1","port":8765}')
$retiredConfig = Join-Path $generation 'retired-endpoint.json'
[System.IO.File]::WriteAllText($retiredConfig, '{"schema_version":"solidworks-to-urdf.endpoint/v1","targets":{}}')
$unknownConfig = Join-Path $generation 'unknown-endpoint.json'
[System.IO.File]::WriteAllText($unknownConfig, '{"schema_version":"solidworks-to-urdf.endpoint/v1","bogus_key":true}')
$duplicateConfig = Join-Path $generation 'duplicate-endpoint.json'
[System.IO.File]::WriteAllText($duplicateConfig, '{"schema_version":"solidworks-to-urdf.endpoint/v1","port":8765,"port":8766}')
$retiredProblems = Get-EndpointConfigEarlyProblems $retiredConfig
Check 'early.retired_key_refused' (((@($retiredProblems.unknown) -contains 'targets')) -and (@($retiredProblems.duplicates).Count -eq 0))
Check 'early.clean_config' ((@((Get-EndpointConfigEarlyProblems $cleanConfig).unknown).Count -eq 0))
Check 'early.unknown_reported' ((@((Get-EndpointConfigEarlyProblems $unknownConfig).unknown) -contains 'bogus_key'))
Check 'early.duplicate_reported' ((@((Get-EndpointConfigEarlyProblems $duplicateConfig).duplicates) -contains 'port'))

# ---- account domain pins: restate-only, set on switch, survive restart ------
Reset-State
$canonical = Join-Path $root 'cad-ownership.json'
$pins = Assert-DomainPins $root $generation
Check 'pins.unbound_plan' (($pins.canonical -eq $canonical) -and ($pins.previous_ownership -eq '') -and
  ($pins.previous_root -eq ''))
Set-DomainPins $root $generation
Check 'pins.set_canonical' (($script:AccountEnv['DESCRIPTION_ENDPOINT_ROOT'] -eq $generation) -and
  ($script:AccountEnv['DESCRIPTION_CAD_OWNERSHIP_FILE'] -eq $canonical))
# An equal pin only restates the bound domain.
$pins = Assert-DomainPins $root $generation
Check 'pins.equal_restates' ($pins.canonical -eq $canonical)
# A divergent ownership pin is refused: a second domain is never created silently.
$script:AccountEnv['DESCRIPTION_CAD_OWNERSHIP_FILE'] = (Join-Path $root 'other/cad-ownership.json')
$message = ''
try { $null = Assert-DomainPins $root $generation } catch { $message = $_.Exception.Message }
Check 'pins.divergent_ownership_refused' ($message -match 'cad_domain_override_refused')
# A changed state root cannot slip in as a new domain: the served configuration refuses it first.
$script:AccountEnv['DESCRIPTION_CAD_OWNERSHIP_FILE'] = $canonical
$message = ''
try { $null = Assert-DomainPins (Join-Path $root 'other-state') $generation } catch { $message = $_.Exception.Message }
Check 'pins.state_root_change_refused' ($message -match 'state_root_mismatch')
# A divergent endpoint-root pin is refused: the served generation may only be restated.
$script:AccountEnv['DESCRIPTION_ENDPOINT_ROOT'] = (Join-Path $root 'somewhere-else')
$message = ''
try { $null = Assert-DomainPins $root $generation } catch { $message = $_.Exception.Message }
Check 'pins.divergent_root_refused' ($message -match 'cad_domain_override_refused')
# The binding survives a service restart: the pins are read again, unchanged.
$script:AccountEnv['DESCRIPTION_ENDPOINT_ROOT'] = $generation
Stop-ScheduledTask -TaskName $binding.task_name | Out-Null
Start-ScheduledTask -TaskName $binding.task_name | Out-Null
Check 'pins.survive_restart' (($script:AccountEnv['DESCRIPTION_ENDPOINT_ROOT'] -eq $generation) -and
  ($script:AccountEnv['DESCRIPTION_CAD_OWNERSHIP_FILE'] -eq $canonical))
# An independent default call (no override) resolves the pinned domain.
Check 'pins.independent_default_call' ((Get-AccountEnvironment 'DESCRIPTION_CAD_OWNERSHIP_FILE') -eq $canonical)
# The binding's state_root never self-grants: a served configuration declaring another root refuses.
$script:ServedStateRoot = (Join-Path $root 'other-state')
$message = ''
try { $null = Assert-DomainPins $root $generation } catch { $message = $_.Exception.Message }
Check 'pins.served_state_root_mismatch_refused' ($message -match 'state_root_mismatch')
$script:ServedStateRoot = $root
# Representation-insensitive: the official reader reports backslashes on Windows while the
# binding and platform.json declare forward slashes; the same root in either spelling passes.
$script:ServedStateRoot = $root.Replace('/', '\')
$message = ''
$pins = $null
try { $pins = Assert-DomainPins $root $generation } catch { $message = $_.Exception.Message }
Check 'pins.representation_insensitive_pass' (($null -ne $pins) -and ($message -eq ''))
$script:ServedStateRoot = $root
$message = ''
$pins = $null
try { $pins = Assert-DomainPins ($root.Replace('/', '\')) $generation } catch { $message = $_.Exception.Message }
Check 'pins.representation_variant_state_root_pass' (($null -ne $pins) -and ($message -eq ''))
# A genuinely different path still refuses after normalization (a near miss, not a separator).
$script:ServedStateRoot = ($root + '-other')
$message = ''
try { $null = Assert-DomainPins $root $generation } catch { $message = $_.Exception.Message }
Check 'pins.true_mismatch_after_normalization_refused' ($message -match 'state_root_mismatch')
$script:ServedStateRoot = $root
# The account pins may carry either spelling too: the same generation restated with backslashes
# passes; a different generation still refuses.
$script:AccountEnv['DESCRIPTION_ENDPOINT_ROOT'] = $generation.Replace('/', '\')
$script:AccountEnv['DESCRIPTION_CAD_OWNERSHIP_FILE'] = $canonical.Replace('/', '\')
$message = ''
$pins = $null
try { $pins = Assert-DomainPins $root $generation } catch { $message = $_.Exception.Message }
Check 'pins.pin_representation_restates_pass' (($null -ne $pins) -and ($message -eq ''))
$script:AccountEnv['DESCRIPTION_ENDPOINT_ROOT'] = ($generation + '-other')
$message = ''
try { $null = Assert-DomainPins $root $generation } catch { $message = $_.Exception.Message }
Check 'pins.representation_true_root_change_refused' ($message -match 'cad_domain_override_refused')
$script:AccountEnv['DESCRIPTION_ENDPOINT_ROOT'] = $generation
$script:AccountEnv['DESCRIPTION_CAD_OWNERSHIP_FILE'] = $canonical

# ---- writer residue: the stop gate scans path identity, not path spelling ----
# Win32_Process reports backslash images while the binding's roots travel slash-normalized; a
# writer that keeps its image under the root in either spelling must count as residue, while a
# neighbouring directory never may (the separator stays a boundary).
$script:WriterImages = @()
function Get-CimInstance {
  param($ClassName, $ErrorAction)
  foreach ($image in $script:WriterImages) { [pscustomobject]@{ ExecutablePath = $image } }
}
$residueRoot = 'C:\description-v1-review\endpoint-state'
$residueGeneration = $residueRoot + '\lifecycle\gen140\generation'
$residueBinding = @{ state_root = $residueRoot; generation = $residueGeneration }
$residueImage = $residueGeneration + '\runtime\Scripts\python.exe'
$script:WriterImages = @($residueImage)
Check 'writer_residue.same_spelling_counts' ((@(Get-WriterResidue $residueBinding)).Count -eq 1)
# The regression counterexample: a slash-normalized binding must count the backslash image too.
$residueForward = @{ state_root = $residueRoot.Replace('\', '/'); generation = $residueGeneration.Replace('\', '/') }
Check 'writer_residue.forward_binding_backslash_process_counts' ((@(Get-WriterResidue $residueForward)).Count -eq 1)
# And the reverse spelling agrees.
$script:WriterImages = @($residueImage.Replace('\', '/'))
Check 'writer_residue.backslash_binding_forward_process_counts' ((@(Get-WriterResidue $residueBinding)).Count -eq 1)
# A process under a different root is somebody else's writer.
$script:WriterImages = @('C:\another-root\runtime\Scripts\python.exe')
Check 'writer_residue.different_root_ignored' ((@(Get-WriterResidue $residueBinding)).Count -eq 0)
# A near-neighbour prefix is not inside the root: the separator stays the boundary.
$script:WriterImages = @('C:\description-v1-review\endpoint-state-other\python.exe')
Check 'writer_residue.near_neighbour_ignored' ((@(Get-WriterResidue $residueBinding)).Count -eq 0)
# The generation prefix counts its writers even outside the state root.
$script:WriterImages = @($residueImage)
$residueGenerationOnly = @{ state_root = 'C:\description-v1-review\other-state'; generation = $residueGeneration }
Check 'writer_residue.generation_prefix_counts' ((@(Get-WriterResidue $residueGenerationOnly)).Count -eq 1)
# An empty sweep is clean.
$script:WriterImages = @()
Check 'writer_residue.empty_sweep_clean' ((@(Get-WriterResidue $residueBinding)).Count -eq 0)

Reset-State
$facts = Get-OwnershipRefusalFacts $generation $binding
Check 'facts.no_lock_file' ((-not $facts.refused) -and ($facts.state -eq 'no_lock_file'))
[System.IO.File]::WriteAllText($lockPath, '999999')
$script:ProbeScenario = 'free'
$facts = Get-OwnershipRefusalFacts $generation $binding
Check 'facts.lock_free' ((-not $facts.refused) -and ($facts.state -eq 'lock_free'))
$script:ProbeScenario = 'refused'
$facts = Get-OwnershipRefusalFacts $generation $binding
Check 'facts.owner_ended' ($facts.refused -and ($facts.owner_state -eq 'ended') -and ($facts.owner_ended -eq $true) -and
  ($facts.owner_process_alive -eq $false))
$script:ProcScenario = 'alive'
$facts = Get-OwnershipRefusalFacts $generation $binding
Check 'facts.owner_alive' ($facts.refused -and ($facts.owner_state -eq 'alive') -and ($facts.owner_ended -eq $false) -and
  ($facts.owner_process_alive -eq $true))
$script:ProcScenario = 'denied'
$facts = Get-OwnershipRefusalFacts $generation $binding
Check 'facts.owner_query_unknown' ($facts.refused -and ($facts.owner_state -eq 'unknown') -and ($facts.owner_ended -eq $false) -and
  ($null -eq $facts.owner_process_alive) -and ($facts.owner_process_error -match 'denied'))
$script:ProcScenario = 'missing'
[System.IO.File]::WriteAllText($lockPath, 'not-a-pid')
$facts = Get-OwnershipRefusalFacts $generation $binding
Check 'facts.owner_unreadable' ($facts.refused -and ($facts.reason -eq 'owner_unreadable') -and ($facts.owner_state -eq 'unknown') -and
  ($facts.owner_ended -eq $false))
[System.IO.File]::WriteAllText($lockPath, '999999')
$script:ProbeScenario = 'observer'
$facts = Get-OwnershipRefusalFacts $generation $binding
Check 'facts.observer_failed' ((-not $facts.refused) -and ($facts.state -eq 'observer_failed'))
$script:ProbeScenario = 'refused'
$script:TaskState = 'Running'
$facts = Get-OwnershipRefusalFacts $generation $binding
Check 'facts.task_running' ((-not $facts.refused) -and ($facts.state -eq 'task_running'))

# ---- settle flows: retry only on proven-ended; unknown/observer stop ---------
Reset-State
[System.IO.File]::WriteAllText($lockPath, '999999')
$script:SettleOutcomes = @('throw', 'ok')
$result = Start-OrRefuse $configPath $binding $generation $promotions
Check 'flow.retry_once_after_60s' (($script:Starts -eq 2) -and ($result.retried_after_s -eq 60) -and
  ($script:Sleeps.Count -eq 1) -and ($script:Sleeps[0] -ge 60))
Check 'flow.refusal_owner_ended' ($result.lock_refusal.owner_ended -eq $true)
$receipt = Get-Content -LiteralPath (Join-Path $promotions 'lock-refusal.json') -Raw | ConvertFrom-Json
Check 'flow.receipt_records_proof' (($receipt.owner_ended -eq $true) -and ($receipt.retryable -eq $true) -and
  ($receipt.retry_after_s -ge 60) -and ($receipt.owner_state -eq 'ended'))

Reset-State
[System.IO.File]::WriteAllText($lockPath, '999999')
$script:ProcScenario = 'alive'
$script:SettleOutcomes = @('throw')
$message = ''
try { $null = Start-OrRefuse $configPath $binding $generation $promotions } catch { $message = $_.Exception.Message }
Check 'flow.unproven_alive_stops' ($message.StartsWith('lock_refused_unproven:') -and ($message -match 'owner_state=alive') -and
  ($script:Starts -eq 1) -and ($script:Sleeps.Count -eq 0))

Reset-State
[System.IO.File]::WriteAllText($lockPath, '999999')
$script:ProcScenario = 'denied'
$script:SettleOutcomes = @('throw')
$message = ''
try { $null = Start-OrRefuse $configPath $binding $generation $promotions } catch { $message = $_.Exception.Message }
$receipt = Get-Content -LiteralPath (Join-Path $promotions 'lock-refusal.json') -Raw | ConvertFrom-Json
Check 'flow.unproven_unknown_stops' ($message.StartsWith('lock_refused_unproven:') -and ($message -match 'owner_state=unknown') -and
  ($script:Starts -eq 1) -and ($script:Sleeps.Count -eq 0) -and ($receipt.owner_state -eq 'unknown') -and
  ($receipt.retryable -eq $false))

Reset-State
[System.IO.File]::WriteAllText($lockPath, '999999')
$script:ProbeScenario = 'observer'
$script:SettleOutcomes = @('throw')
$message = ''
try { $null = Start-OrRefuse $configPath $binding $generation $promotions } catch { $message = $_.Exception.Message }
$receipt = Get-Content -LiteralPath (Join-Path $promotions 'lock-refusal.json') -Raw | ConvertFrom-Json
Check 'flow.observer_failed_stops' ($message.StartsWith('lock_refused_unproven:') -and ($message -match 'observer_failed') -and
  ($script:Starts -eq 1) -and ($script:Sleeps.Count -eq 0) -and ($receipt.reason -eq 'observer_failed'))

Reset-State
[System.IO.File]::WriteAllText($lockPath, '999999')
$script:SettleOutcomes = @('throw', 'throw')
$message = ''
try { $null = Start-OrRefuse $configPath $binding $generation $promotions } catch { $message = $_.Exception.Message }
Check 'flow.second_refusal_stops' ($message.StartsWith('lock_refused:') -and ($script:Starts -eq 2) -and
  ($script:Sleeps.Count -eq 1))

Reset-State
$script:ProbeScenario = 'free'
$script:SettleOutcomes = @('throw')
$message = ''
try { $null = Start-OrRefuse $configPath $binding $generation $promotions } catch { $message = $_.Exception.Message }
Check 'flow.non_refusal_rethrows' (($message -match 'settle window') -and ($script:Starts -eq 1) -and
  ($script:Sleeps.Count -eq 0))

# ---- failure-path restore: exact opaque source snapshot ---------------------
$sourceLauncher = Runtime-Python $sourceGeneration
$sourceArguments = '-I -B -m description_pipeline serve --config "' +
  (Join-Path $sourceGeneration 'endpoint.json') + '"'
$sourceWorkingDirectory = $root
$sourceRuntime = Join-Path $sourceGeneration 'runtime'
$sourceScripts = Join-Path $sourceRuntime 'Scripts'
$sourceBaseHome = Join-Path $root 'source-python'
$sourceSystemRoot = Join-Path $root 'windows'
$sourceScheduler = Join-Path $sourceSystemRoot 'System32/svchost.exe'
$sourceBase = Join-Path $sourceBaseHome 'python.exe'
foreach ($directory in @(
    $sourceScripts, $sourceBaseHome, (Split-Path -Parent $sourceScheduler))) {
  $null = New-Item -ItemType Directory -Path $directory -Force
}
foreach ($path in @($sourceLauncher, $sourceBase, $sourceScheduler)) {
  Write-Utf8NoBom $path ('source-fixture:' + $path)
}
$sourcePyvenvPath = Join-Path $sourceRuntime 'pyvenv.cfg'
Write-Utf8NoBom $sourcePyvenvPath (@(
    ('home = ' + $sourceBaseHome)
    'include-system-site-packages = false'
    'version = 3.12.10'
    ('executable = ' + $sourceBase)
    ('command = "' + $sourceBase + '" -m venv "' + $sourceRuntime + '"')
  ) -join "`n")
$sourceConfigPath = Join-Path $sourceGeneration 'endpoint.json'
$sourceConfigText = ('{{"state_root":"{0}","port":8765}}' -f ($root -replace '\\', '/'))
Write-Utf8NoBom $sourceConfigPath $sourceConfigText
$sourceTaskText = '<Task><Actions><Exec><Command>' + $sourceLauncher +
  '</Command><Arguments>' + $sourceArguments +
  '</Arguments><WorkingDirectory>' + $sourceWorkingDirectory +
  '</WorkingDirectory></Exec></Actions></Task>'
[System.IO.File]::WriteAllText((Join-Path $previous 'task.xml'), $sourceTaskText)
[System.IO.File]::WriteAllText((Join-Path $previous 'endpoint-before.json'), $sourceConfigText)
$sourceProjection = [pscustomobject]@{
  task_path='\'; task_name='test-endpoint'; state='Running';
  xml=$sourceTaskText; xml_sha256=(Get-TextSha256 $sourceTaskText);
  semantic_sha256='a' * 64; security_descriptor_sha256='b' * 64;
  triggers_sha256='c' * 64;
  principal=[pscustomobject]@{sid='S-1-5-21-1'; administrator=$true};
  action=[pscustomobject]@{
    execute=$sourceLauncher; arguments=$sourceArguments; working_directory=$sourceWorkingDirectory}
  settings=[pscustomobject]@{multiple_instances='IgnoreNew'}}
$sourceProjection.semantic_sha256 = Get-JsonSha256 (
  New-TaskSemanticDocument $sourceProjection.task_path $sourceProjection.task_name `
    $sourceProjection.principal $sourceProjection.action $sourceProjection.settings `
    $sourceProjection.triggers_sha256 $sourceProjection.security_descriptor_sha256)
$sourceIdentity = [pscustomobject]@{
  endpoint_root=(Normalize-PathIdentity $sourceGeneration);
  config_path=(Normalize-PathIdentity (Join-Path $sourceGeneration 'endpoint.json'));
  config_sha256=(Get-TextSha256 $sourceConfigText); config_digest='c' * 64;
  package_root='C:/packages'; output_root='C:/results';
  state_root=(Normalize-PathIdentity $root); port=8765;
  install_id_path=(Join-Path $root 'install-id'); install_id='win-' + ('d' * 32);
  release_identity_sha256='';
  release=[pscustomobject]@{version='1.5.0'; source_commit='4ed0f03d'; source_sha256='f' * 64; role='native'}}
$sourceIdentity.release_identity_sha256 = Get-ReleaseIdentitySha256 $sourceIdentity.release
[System.IO.File]::WriteAllText($sourceIdentity.install_id_path, $sourceIdentity.install_id)
$script:SourceListenerSystemRoot = $env:SystemRoot
$env:SystemRoot = $sourceSystemRoot
$sourceListener = [ordered]@{
  schema_version='solidworks-to-urdf.windows-listener/v2'
  relation='direct_venv_child'
  process_arguments_sha256=(Get-TextSha256 $sourceArguments)
  scheduler_service_name='Schedule'
  scheduler_service_state='Running'
  scheduler_service_process_id=2512
  scheduler_pid=2512
  scheduler_image=(Normalize-PathIdentity $sourceScheduler)
  scheduler_start_utc='2026-10-27T00:00:00.0000000Z'
  scheduler_user_sid='S-1-5-18'
  scheduler_session_id=0
  launcher_pid=19344
  launcher_parent_pid=2512
  launcher_image=(Normalize-PathIdentity $sourceLauncher)
  launcher_image_sha256=(Sha256 $sourceLauncher)
  launcher_start_utc='2026-10-27T00:00:01.0000000Z'
  launcher_user_sid=$sourceProjection.principal.sid
  launcher_session_id=1
  listener_pid=15124
  listener_parent_pid=19344
  listener_start_utc='2026-10-27T00:00:02.0000000Z'
  listener_user_sid=$sourceProjection.principal.sid
  listener_session_id=1
  venv_config_path=(Normalize-PathIdentity $sourcePyvenvPath)
  venv_config_sha256=(Sha256 $sourcePyvenvPath)
  venv_home=(Normalize-PathIdentity $sourceBaseHome)
  venv_version='3.12.10'
  base_interpreter_path=(Normalize-PathIdentity $sourceBase)
  base_interpreter_sha256=(Sha256 $sourceBase)
}
$sourceState = New-EndpointSourceState ([pscustomobject]@{}) $sourceProjection `
  $sourceIdentity $sourceListener
Write-Receipt (Join-Path $previous 'source-state.json') $sourceState
$sourceStateSha256 = Get-SourceStateAuthorityHash (Join-Path $previous 'source-state.json')
function Get-TaskProjection {
  param($Task)
  $projection = $sourceProjection.PSObject.Copy()
  $projection.action = [pscustomobject]@{
    execute=$sourceProjection.action.execute
    arguments=$script:RestoreActionArguments
    working_directory=$sourceProjection.action.working_directory
  }
  return $projection
}
function Get-EndpointIdentity { param($Task, $TaskProjection, [string]$SshHost) return $sourceIdentity }
function Read-EndpointRuntimeProbe {
  param([string]$Generation, [string]$ConfigPath)
  return [pscustomobject]@{
    config_digest=$sourceIdentity.config_digest
    package_root=$sourceIdentity.package_root
    output_root=$sourceIdentity.output_root
    state_root=$sourceIdentity.state_root
    port=$sourceIdentity.port
    release=[pscustomobject]@{
      version=$sourceIdentity.release.version
      source_sha256=$sourceIdentity.release.source_sha256
      release=[pscustomobject]@{source_commit=$sourceIdentity.release.source_commit}
    }
  }
}
function Get-ListenerEvidence {
  param([object]$binding, $TaskProjection)
  $restoredListener = (
    $sourceListener | ConvertTo-Json -Compress | ConvertFrom-Json)
  $restoredListener.listener_pid = 15125
  $restoredListener.listener_start_utc = '2026-10-27T00:00:03.0000000Z'
  if ($script:RestoreListenerScenario -eq 'bad_sid') {
    $restoredListener.listener_user_sid = 'S-1-5-21-1-2-3-501'
  }
  return $restoredListener
}
function Read-EndpointHealth {
  param([string]$configPath)
  if (-not $script:OldHealthOk) { throw 'the previous endpoint did not answer /health' }
  return [pscustomobject]@{
    schema_version='solidworks-to-urdf.health/v1'
    pipeline_id='solidworks-to-urdf'; ready=$true; native_platform=$true;
    release=$sourceIdentity.release;
    endpoint=[pscustomobject]@{port=8765};
    install=[pscustomobject]@{
      install_id=$sourceIdentity.install_id
      config_digest=$sourceIdentity.config_digest}}
}
function Register-ScheduledTask {
  [CmdletBinding()]
  param([string]$TaskName, [string]$TaskPath, [string]$Xml, [switch]$Force)
  $script:CurrentAction = [pscustomobject]@{
    Execute=$sourceLauncher
    Arguments=$script:RestoreActionArguments
    WorkingDirectory=$sourceWorkingDirectory
  }
  $script:CallLog.Add('register')
}
$script:RestoreActionArguments = $sourceArguments
Reset-State
$script:RestoreActionArguments = $sourceArguments
$restore = Restore-PreviousEndpoint -binding $binding -previous $previous `
  -stageDir $generation -expectedTaskPath $sourceProjection.task_path `
  -ExpectedSourceStateSha256 $sourceStateSha256
Check 'restore.success_exact_source_identity' ($restore.stopped -and $restore.restored -and $restore.startable -and $restore.started -and
  ($restore.healthy -eq $true) -and ($restore.previous_generation -eq (Normalize-PathIdentity $sourceGeneration)) -and
  ($restore.listener.listener_pid -eq 15125) -and
  ($sourceState.listener.listener_pid -eq 15124) -and
  (($script:CallLog -join ',') -eq 'stop,register,start')) `
  ($restore | ConvertTo-Json -Depth 8 -Compress)

Reset-State
$script:RestoreActionArguments = $sourceArguments
[System.IO.File]::WriteAllText($lockPath, '999999')
$script:ProcScenario = 'denied'
$restore = Restore-PreviousEndpoint -binding $binding -previous $previous `
  -stageDir $generation -expectedTaskPath $sourceProjection.task_path `
  -ExpectedSourceStateSha256 $sourceStateSha256
Check 'restore.gate_unknown_no_start' ($restore.stopped -and $restore.restored -and (-not $restore.startable) -and (-not $restore.started) -and
  ($restore.start_gate -eq 'refused') -and ($restore.error -match 'start gate is refused') -and
  (($script:CallLog -join ',') -eq 'stop,register')) `
  ($restore | ConvertTo-Json -Depth 8 -Compress)

Reset-State
$script:RestoreActionArguments = $sourceArguments
$script:OldHealthOk = $false
$restore = Restore-PreviousEndpoint -binding $binding -previous $previous `
  -stageDir $generation -expectedTaskPath $sourceProjection.task_path `
  -ExpectedSourceStateSha256 $sourceStateSha256
Check 'restore.health_failure_is_not_success' ($restore.stopped -and $restore.restored -and $restore.started -and (-not $restore.healthy) -and
  ($restore.error -match 'did not answer')) ($restore | ConvertTo-Json -Depth 8 -Compress)

Reset-State
$script:RestoreActionArguments = $sourceArguments
$script:RestoreListenerScenario = 'bad_sid'
$restore = Restore-PreviousEndpoint -binding $binding -previous $previous `
  -stageDir $generation -expectedTaskPath $sourceProjection.task_path `
  -ExpectedSourceStateSha256 $sourceStateSha256
Check 'restore.static_listener_mismatch_fails_closed' (
  $restore.stopped -and $restore.restored -and $restore.started -and
  (-not $restore.healthy) -and
  ($restore.error -match 'differs from the task')) `
  ($restore | ConvertTo-Json -Depth 8 -Compress)
$script:RestoreListenerScenario = $null

Reset-State
$script:RestoreActionArguments = $sourceArguments
Remove-Item -LiteralPath (Join-Path $previous 'source-state.json') -Force
$restore = Restore-PreviousEndpoint -binding $binding -previous $previous `
  -stageDir $generation -expectedTaskPath $sourceProjection.task_path `
  -ExpectedSourceStateSha256 $sourceStateSha256
Check 'restore.missing_source_state_fails_closed' ((-not $restore.stopped) -and (-not $restore.started) -and
  ($null -eq $restore.healthy) -and ($restore.error -match 'source-state.json'))
Write-Receipt (Join-Path $previous 'source-state.json') $sourceState
$sourceStateSha256 = Get-SourceStateAuthorityHash (Join-Path $previous 'source-state.json')

Reset-State
$script:RestoreActionArguments = $sourceArguments + ' --foreign'
$restore = Restore-PreviousEndpoint -binding $binding -previous $previous `
  -stageDir $generation -expectedTaskPath $sourceProjection.task_path `
  -ExpectedSourceStateSha256 $sourceStateSha256
Check 'restore.changed_opaque_action_fails_closed' ((-not $restore.started) -and ($null -eq $restore.healthy) -and
  ($restore.error -match 'arguments') -and (($script:CallLog -join ',') -eq 'stop,register'))
$script:RestoreActionArguments = $sourceArguments
$env:SystemRoot = $script:SourceListenerSystemRoot

# ---- real outer promote flow (child pwsh; real Invoke-Main) -----------------
$harness = Join-Path $PSScriptRoot 'invoke-main-harness.ps1'
$pwshExe = [System.Diagnostics.Process]::GetCurrentProcess().MainModule.FileName
Check 'outer.path_predicates_are_host_neutral' (
  ((Normalize-PathIdentity 'C:\root\new\runtime\Scripts\python.exe') -like
    '*/new/runtime/Scripts/python.exe') -and
  ((Normalize-PathIdentity 'C:\root\bound-previous') -like '*/bound-previous') -and
  ((Normalize-PathIdentity 'start:C:\root\old\runtime\Scripts\python.exe') -like
    'start:*/old/*') -and
  ((Normalize-PathIdentity (
      '-I -B -m description_pipeline serve --config "C:\root\old\generation\endpoint.json"'
    )).IndexOf(
      (Normalize-PathIdentity 'C:/root/old/generation/endpoint.json'),
      [System.StringComparison]::OrdinalIgnoreCase) -ge 0))
function Invoke-Outer([string]$scenario) {
  $outerRoot = Join-Path ([System.IO.Path]::GetTempPath()) ('il-outer-' + [guid]::NewGuid().ToString('N'))
  $null = New-Item -ItemType Directory -Path $outerRoot -Force
  $json = & $pwshExe -NoProfile -File $harness -ScriptPath $ScriptPath -Scenario $scenario -Root $outerRoot
  $result = ($json | Select-Object -Last 1) | ConvertFrom-Json
  $lastFile = Join-Path $outerRoot 'instance-promotions/last-promotion.json'
  $lastPromotion = if (Test-Path -LiteralPath $lastFile) {
    Get-Content -LiteralPath $lastFile -Raw | ConvertFrom-Json
  } else {
    $null
  }
  $pendingFile = Join-Path $outerRoot 'instance-promotions/pending-promotion.json'
  $pendingPromotion = if (Test-Path -LiteralPath $pendingFile) {
    Get-Content -LiteralPath $pendingFile -Raw | ConvertFrom-Json
  } else {
    $null
  }
  $actualSourceAuthority = if ($lastPromotion -and
      (Test-Path -LiteralPath (Join-Path ([string]$lastPromotion.previous) 'source-state.json'))) {
    Get-SourceStateAuthorityHash (Join-Path ([string]$lastPromotion.previous) 'source-state.json')
  } else {
    $null
  }
  $rollbackFile = Get-ChildItem -LiteralPath (Join-Path $outerRoot 'instance-promotions') `
    -Filter 'rollback-*.json' -ErrorAction SilentlyContinue | Select-Object -First 1
  $rollbackReceipt = if ($rollbackFile) {
    Get-Content -LiteralPath $rollbackFile.FullName -Raw | ConvertFrom-Json
  } else {
    $null
  }
  $result | Add-Member -NotePropertyName last_promotion -NotePropertyValue $lastPromotion -Force
  $result | Add-Member -NotePropertyName pending_promotion `
    -NotePropertyValue $pendingPromotion -Force
  $result | Add-Member -NotePropertyName source_state_authority `
    -NotePropertyValue $actualSourceAuthority -Force
  $result | Add-Member -NotePropertyName rollback_receipt `
    -NotePropertyValue $rollbackReceipt -Force
  Remove-Item -LiteralPath $outerRoot -Recurse -Force -ErrorAction SilentlyContinue
  return $result
}
$outer = Invoke-Outer 'unknown_owner'
Check 'outer.unknown_owner_no_restart' ((@($outer.calls | Where-Object { $_ -like 'start:*old*' }).Count -eq 0) -and
  ($outer.receipt.restore.startable -eq $false) -and ($outer.receipt.restore.started -eq $false) -and
  ($outer.receipt.restore.start_gate -eq 'refused') -and
  ($outer.message -match 'promote_restore_failed'))
Check 'outer.unknown_owner_candidate_started_once' ((@($outer.calls | Where-Object { $_ -like 'start:*new*' }).Count -eq 1))

$outer = Invoke-Outer 'promote_prestop_identity_mismatch'
Check 'outer.prestop_failure_has_no_mutation' (
  ($outer.message -match 'promote_preflight_failed') -and
  (@($outer.calls | Where-Object {
    $_ -like 'stop*' -or $_ -like 'register*' -or
    $_ -like 'start:*' -or $_ -like 'set:*' -or
    $_ -like 'managed-endpoint-acl:*'
  }).Count -eq 0) -and
  (@($outer.setenv).Count -eq 0) -and
  ($outer.receipt.restore.stopped -eq $false) -and
  ($outer.receipt.restore.restored -eq $false) -and
  ($outer.receipt.restore.started -eq $false)) `
  ($outer | ConvertTo-Json -Depth 12 -Compress)

$outer = Invoke-Outer 'startable_second_refusal'
Check 'outer.startable_starts_old_with_pinned_health' ((@($outer.calls | Where-Object { $_ -like 'start:*old*' }).Count -eq 1) -and
  ($outer.receipt.restore.startable -eq $true) -and ($outer.receipt.restore.started -eq $true) -and
  ($outer.receipt.restore.healthy -eq $true) -and (@($outer.sleeps) -contains 60))

$outer = Invoke-Outer 'promote_missing_stage_receipt'
Check 'outer.missing_stage_receipt_fails_closed' ((@($outer.calls).Count -eq 0) -and
  ($outer.message -match 'stage_receipt_missing'))

$outer = Invoke-Outer 'restore_multi_action'
Check 'outer.restore_multi_action_fails_closed' (
  (@($outer.calls | Where-Object { $_ -like 'start:*old*' }).Count -eq 0) -and
  ($outer.receipt.restore.started -eq $false) -and
  ($outer.receipt.restore.error -match 'task_ambiguous'))

$outer = Invoke-Outer 'restore_foreign_one_action'
Check 'outer.restore_foreign_one_action_fails_closed' (
  (@($outer.calls | Where-Object { $_ -like 'start:*old*' }).Count -eq 0) -and
  (@($outer.calls | Where-Object { $_ -like 'start:*foreign*' }).Count -eq 0) -and
  ($outer.receipt.restore.started -eq $false) -and
  ($outer.receipt.restore.error -match 'managed generation runtime'))

$outer = Invoke-Outer 'stage_unknown_key_refused'
Check 'outer.stage_unknown_key_refused' (($outer.message -match 'endpoint_config_unknown_keys') -and
  ($outer.message -match 'bogus_key') -and (@($outer.calls).Count -eq 0))

$outer = Invoke-Outer 'stage_legacy_targets_refused'
Check 'outer.stage_legacy_targets_refused' (($outer.message -match 'endpoint_config_unknown_keys') -and
  ($outer.message -match 'targets') -and (@($outer.calls).Count -eq 0))

$outer = Invoke-Outer 'stage_duplicate_key_refused'
Check 'outer.stage_duplicate_key_refused' (($outer.message -match 'endpoint_config_duplicate_keys') -and
  (@($outer.calls).Count -eq 0))

$outer = Invoke-Outer 'stage_acl_boundary'
$stageAclCalls = @($outer.calls | Where-Object {
  $_ -like 'managed-acl:*staged:protected=True:service-sid=*:non-inheriting=False:recurse=True'
})
Check 'outer.stage_acl_boundary_signature' (
  ($outer.message -match 'injected_stage_acl_boundary') -and
  $stageAclCalls.Count -eq 1)

$outer = Invoke-Outer 'promote_candidate_mismatch'
Check 'outer.promote_candidate_mismatch' (($outer.message -match 'candidate_config_mismatch') -and
  (@($outer.calls).Count -eq 0))

$outer = Invoke-Outer 'promote_source_mismatch'
Check 'outer.promote_source_mismatch' (($outer.message -match 'candidate_source_mismatch') -and
  (@($outer.calls).Count -eq 0))

$outer = Invoke-Outer 'status_pending_rollback'
Check 'outer.status_exposes_pending_rollback_intent' (
  ($outer.passed -eq $true) -and
  ($outer.pending_rollback.state -eq 'present') -and
  ($outer.pending_rollback.source_state_sha256 -eq ('a' * 64)) -and
  ((Normalize-PathIdentity $outer.pending_rollback.previous) -like
    '*/bound-previous') -and
  ((Normalize-PathIdentity $outer.pending_rollback.target_generation) -like
    '*/lifecycle/new/generation') -and
  ((Normalize-PathIdentity $outer.pending_rollback.restore_to) -like
    '*/lifecycle/old/generation') -and
  ((Normalize-PathIdentity $outer.pending_rollback.current_snapshot) -like
    '*/instance-promotions/rollback-before-fixture') -and
  ($outer.live_task.state -eq 'Running')) `
  ($outer | ConvertTo-Json -Depth 12 -Compress)

$outer = Invoke-Outer 'status_pending_rollback_invalid'
Check 'outer.status_preserves_health_with_unknown_rollback_intent' (
  ($outer.passed -eq $true) -and
  ($outer.pending_rollback.state -eq 'unknown') -and
  ($outer.pending_rollback.code -eq 'rollback_intent_unreadable') -and
  ($outer.live_task.state -eq 'Running')) `
  ($outer | ConvertTo-Json -Depth 12 -Compress)

$outer = Invoke-Outer 'status_pending_rollback_directory'
Check 'outer.status_reports_non_file_rollback_intent_as_unknown' (
  ($outer.passed -eq $true) -and
  ($outer.pending_rollback.state -eq 'unknown') -and
  ($outer.pending_rollback.code -eq 'rollback_intent_unreadable') -and
  ($outer.pending_rollback.error -match 'not a regular file') -and
  ($outer.live_task.state -eq 'Running')) `
  ($outer | ConvertTo-Json -Depth 12 -Compress)

$outer = Invoke-Outer 'promote_4ed_success'
Check 'outer.current_owned_shape_promotes_to_target' (
  ($outer.passed -eq $true) -and
  ($outer.last_promotion.source.release.source_commit -eq
    '4ed0f03db9462c084d63cc7106125a794906ba30') -and
  ($outer.last_promotion.source_state_sha256 -match '^[0-9a-f]{64}$') -and
  ($outer.pending_promotion.intent -eq $true) -and
  ($outer.pending_promotion.source_state_sha256 -ceq
    $outer.last_promotion.source_state_sha256) -and
  ($outer.source_state_authority -ceq $outer.last_promotion.source_state_sha256) -and
  ($outer.last_promotion.source.arguments -match
    '^-I -B -m description_pipeline serve --config "') -and
  ((Normalize-PathIdentity $outer.last_promotion.target.action.execute) -like
    '*/new/generation/runtime/Scripts/python.exe')) `
  ($outer | ConvertTo-Json -Depth 12 -Compress)

$outer = Invoke-Outer 'rollback_success'
Check 'outer.rollback_restores_exact_source_xml' (
  ($outer.passed -eq $true) -and
  ($outer.verb -eq 'rollback') -and
  ((Normalize-PathIdentity $outer.restored_from) -like '*/bound-previous') -and
  ($outer.rollback_receipt.source_state_sha256 -match '^[0-9a-f]{64}$') -and
  ($outer.source_state_authority -ceq $outer.rollback_receipt.source_state_sha256)) `
  ($outer | ConvertTo-Json -Depth 12 -Compress)

$outer = Invoke-Outer 'rollback_bad_snapshot'
Check 'outer.rollback_bad_snapshot_has_no_mutation' (
  ($outer.message -match 'previous_invalid') -and
  (@($outer.setenv).Count -eq 0) -and
  (@($outer.calls | Where-Object {
    $_ -like 'stop*' -or $_ -like 'register*' -or
    $_ -like 'start:*' -or $_ -like 'set:*' -or $_ -like 'disable*'
  }).Count -eq 0) -and
  ($outer.task_state -eq 'Running') -and
  ($outer.receipt.code -eq 'previous_invalid')) `
  ($outer | ConvertTo-Json -Depth 12 -Compress)

$outer = Invoke-Outer 'rollback_task_path_mismatch'
Check 'outer.rollback_foreign_task_path_has_no_mutation' (
  ($outer.message -match 'previous_invalid') -and
  (@($outer.setenv).Count -eq 0) -and
  (@($outer.calls | Where-Object {
    $_ -like 'stop*' -or $_ -like 'register*' -or
    $_ -like 'start:*' -or $_ -like 'set:*' -or $_ -like 'disable*'
  }).Count -eq 0) -and
  ($outer.task_state -eq 'Running') -and
  ($outer.receipt.code -eq 'previous_invalid')) `
  ($outer | ConvertTo-Json -Depth 12 -Compress)

$outer = Invoke-Outer 'rollback_source_state_tamper'
Check 'outer.rollback_source_state_tamper_has_no_mutation' (
  ($outer.message -match 'previous_invalid') -and
  (@($outer.setenv).Count -eq 0) -and
  (@($outer.calls | Where-Object {
    $_ -like 'stop*' -or $_ -like 'register*' -or
    $_ -like 'start:*' -or $_ -like 'set:*' -or $_ -like 'disable*'
  }).Count -eq 0) -and
  ($outer.task_state -eq 'Running') -and
  ($outer.receipt.code -eq 'previous_invalid')) `
  ($outer | ConvertTo-Json -Depth 12 -Compress)

$outer = Invoke-Outer 'rollback_stop_refused'
Check 'outer.rollback_stop_refusal_does_not_register_or_disable' (
  ($outer.message -match 'rollback_stop_failed') -and
  (@($outer.calls | Where-Object {
    $_ -like 'register*' -or $_ -like 'start:*' -or
    $_ -like 'set:*' -or $_ -like 'disable*'
  }).Count -eq 0) -and
  ($outer.task_state -eq 'Running') -and
  (@($outer.setenv).Count -eq 4) -and
  ($outer.receipt.code -eq 'rollback_stop_failed')) `
  ($outer | ConvertTo-Json -Depth 12 -Compress)

$outer = Invoke-Outer 'rollback_stop_residue'
Check 'outer.rollback_stop_timeout_does_not_register_or_disable' (
  ($outer.message -match 'rollback_stop_failed') -and
  (@($outer.calls | Where-Object {
    $_ -like 'register*' -or $_ -like 'start:*' -or
    $_ -like 'set:*' -or $_ -like 'disable*'
  }).Count -eq 0) -and
  ($outer.calls -contains 'stop') -and
  ($outer.task_state -eq 'Ready') -and
  (@($outer.setenv).Count -eq 4) -and
  ($outer.receipt.code -eq 'rollback_stop_failed')) `
  ($outer | ConvertTo-Json -Depth 12 -Compress)

$outer = Invoke-Outer 'rollback_restore_failure'
Check 'outer.rollback_restore_failure_is_not_success' (
  ($outer.message -match 'rollback_failed') -and
  ($outer.message -match 'the current endpoint was restored') -and
  (@($outer.calls | Where-Object {
    (Normalize-PathIdentity $_) -like 'start:*/old/*'
  }).Count -eq 1) -and
  (@($outer.calls | Where-Object {
    (Normalize-PathIdentity $_) -like 'start:*/new/*'
  }).Count -eq 1) -and
  ($outer.task_state -eq 'Running') -and
  ($outer.receipt.code -eq 'rollback_failed_current_restored') -and
  ($outer.receipt.current_recovery.healthy -eq $true)) `
  ($outer | ConvertTo-Json -Depth 12 -Compress)

$outer = Invoke-Outer 'rollback_receipt_missing'
Check 'outer.rollback_missing_operation_binding_has_no_mutation' (
  ($outer.message -match 'previous_invalid') -and
  (@($outer.calls).Count -eq 0) -and
  (@($outer.setenv).Count -eq 0) -and
  ($outer.task_state -eq 'Running')) `
  ($outer | ConvertTo-Json -Depth 12 -Compress)

$outer = Invoke-Outer 'rollback_receipt_conflict'
Check 'outer.rollback_conflicting_operation_binding_has_no_mutation' (
  ($outer.message -match 'previous_invalid') -and
  ($outer.message -match 'different source-state authorities') -and
  (@($outer.calls).Count -eq 0) -and
  (@($outer.setenv).Count -eq 0) -and
  ($outer.task_state -eq 'Running')) `
  ($outer | ConvertTo-Json -Depth 12 -Compress)

$outer = Invoke-Outer 'rollback_target_mismatch'
Check 'outer.rollback_foreign_receipt_target_has_no_mutation' (
  ($outer.message -match 'previous_invalid') -and
  ($outer.message -match 'valid source-state authority') -and
  (@($outer.calls).Count -eq 0) -and
  (@($outer.setenv).Count -eq 0) -and
  ($outer.task_state -eq 'Running')) `
  ($outer | ConvertTo-Json -Depth 12 -Compress)

$outer = Invoke-Outer 'rollback_task_name_ambiguous'
Check 'outer.rollback_ambiguous_task_name_has_no_mutation' (
  ($outer.message -match 'windows_task_ambiguous') -and
  (@($outer.calls).Count -eq 0) -and
  (@($outer.setenv).Count -eq 0) -and
  ($outer.task_state -eq 'Running')) `
  ($outer | ConvertTo-Json -Depth 12 -Compress)

# The actual exception boundary runs in a child process so exit semantics are not mocked.
$exceptionHarness = Join-Path $PSScriptRoot 'lifecycle-exception-harness.ps1'
$exceptionCases = @('stage', 'promote', 'rollback', 'recovery-snapshot', 'noisy', 'paths',
  'fallback', 'sensitive', 'die', 'die-unknown')
if ([System.Environment]::OSVersion.Platform -eq [System.PlatformID]::Win32NT) {
  $exceptionCases += @('set', 'assert')
}
foreach ($scenario in $exceptionCases) {
  $errorFile = Join-Path ([System.IO.Path]::GetTempPath()) ('il-error-' + [guid]::NewGuid().ToString('N'))
  $outputFile = $errorFile + '.stdout'
  # Desktop PowerShell deserializes native stderr beginning with CLIXML. Capture
  # at the OS process boundary instead so the deliberately malformed noise stays raw.
  $arguments = @('-NoProfile', '-File', ('"{0}"' -f $exceptionHarness),
    '-ScriptPath', ('"{0}"' -f $ScriptPath), '-Scenario', $scenario,
    '-Root', ('"{0}"' -f $PSScriptRoot))
  $child = Start-Process -FilePath $pwshExe -ArgumentList $arguments `
    -RedirectStandardOutput $outputFile -RedirectStandardError $errorFile -Wait -PassThru
  $rc = $child.ExitCode
  $lines = @(Get-Content -LiteralPath $outputFile -Encoding UTF8)
  $result = ($lines | Select-Object -Last 1) | ConvertFrom-Json
  $expectedRc = if ($scenario -in @('promote', 'rollback', 'recovery-snapshot', 'die-unknown')) { 2 } else { 1 }
  Check ('exception.' + $scenario + '.exit') ($rc -eq $expectedRc)
  Check ('exception.' + $scenario + '.refused') ($result.passed -eq $false)
  Check ('exception.' + $scenario + '.single_json') (@($lines | Where-Object { $_ -match '^\{' }).Count -eq 1)
  if ($scenario -eq 'noisy') {
    Check 'exception.noisy.raw_stderr' (([System.IO.File]::ReadAllText($errorFile)).Contains('#< CLIXML'))
    Check 'exception.noisy.stdout_preserved' ($lines[0] -eq 'pip succeeded')
  }
  if ($scenario -eq 'die') {
    Check 'exception.die_preserved' ($result.code -eq 'original_refusal')
  } elseif ($scenario -eq 'die-unknown') {
    Check 'exception.unknown_preserved' ($result.code -eq 'original_unknown')
  } else {
    Check ('exception.' + $scenario + '.code') ($result.code -eq 'instance_lifecycle_exception')
  }
  if ($scenario -in @('set', 'assert')) {
    Check ('exception.' + $scenario + '.context') ($result.role -eq 'endpoint_config' -and
      $result.operation -eq $scenario -and $result.path -eq '[path]' -and
      $result.path_sha256 -match '^[0-9a-f]{64}$' -and $null -ne $result.base_hresult)
  }
  if ($scenario -eq 'stage') {
    Check 'exception.base_preserved' ($result.base_exception_type -eq 'System.IO.IOException' -and
      $result.base_hresult -eq -2146232800 -and $result.error_id -and $result.line -gt 0)
  }
  if ($scenario -in @('sensitive', 'paths')) {
    Check 'exception.secret_redacted' (($lines -join '') -notmatch 'SECRET_' -and
      [System.Text.Encoding]::UTF8.GetByteCount($result.message) -le 512 -and $result.message_redacted)
  }
  Remove-Item -LiteralPath $errorFile -Force -ErrorAction SilentlyContinue
  Remove-Item -LiteralPath $outputFile -Force -ErrorAction SilentlyContinue
}

Write-Host ("checks={0} failures={1}" -f $script:checks, $script:failures.Count)
exit ([int]($script:failures.Count -gt 0))
