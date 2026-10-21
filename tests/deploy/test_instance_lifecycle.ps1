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
Check 'wording.old_copy_removed' (-not $source.Contains('Copy-Item -LiteralPath (Action-Config $task)')) 'the byte-copy stage path is still present'
Check 'wording.compat_tolerance_removed' ((-not $source.Contains('REMOVED_KEYS')) -and (-not $source.Contains('removed_keys'))) 'the removed-key tolerance channel is still present'
Check 'wording.transform_receipt_removed' ((-not $source.Contains('config-transformation.json')) -and (-not $source.Contains('stage-config-transform'))) 'the obsolete transform receipt is still present'
Check 'wording.no_lock_or_kill' (-not $source.Contains('Remove-Item -LiteralPath $lock')) 'never delete the ownership lock'
Check 'wording.no_system_text_json' (-not $source.Contains('System.Text.Json')) 'a Core-only JSON type the Desktop PowerShell 5.1 lifecycle cannot load is still present'
Check 'wording.strict_scan_on_bound_python' ($source.Contains('Get-EndpointConfigScanPython') -and
  $source.Contains('Get-EndpointConfigStrictPrelude') -and $source.Contains('object_pairs_hook'))
Check 'wording.path_identity_normalization' ($source.Contains('Normalize-PathIdentity') -and
  $source.Contains("Replace('\', '/')")) 'path identity comparisons must normalize the two Windows separators'
Check 'wording.machine_domain_pins' ($source.Contains("GetEnvironmentVariable(`$Name, 'Machine')") -and
  $source.Contains("SetEnvironmentVariable(`$Name, `$Value, 'Machine')"))
Check 'wording.windows_recovery_schema' ($source.Contains('recovery-snapshot') -and
  $source.Contains('recovery-restore') -and $source.Contains('recovery-verify') -and
  $source.Contains('solidworks-to-urdf.windows-recovery/v1'))
Check 'wording.windows_recovery_acl' ($source.Contains('Get-AclProjection') -and
  $source.Contains('Set-AclProjection') -and $source.Contains('AreAccessRulesProtected'))
Check 'wording.windows_managed_acl' ($source.Contains('Set-ManagedEndpointAcls') -and
  $source.Contains('Assert-ManagedEndpointAcls') -and $source.Contains('windows_managed_acl_invalid') -and
  $source.Contains('windows_managed_acl_unsafe') -and $source.Contains('Get-ManagedCommonParent') -and
  $source.Contains('windows_managed_acl_boundary_invalid') -and
  $source.Contains('Assert-ManagedServiceIsNonAdministrator') -and
  $source.Contains('windows_service_identity_elevated') -and
  $source.Contains('windows_service_environment_override') -and
  $source.Contains('windows_managed_acl_marker_missing') -and
  $source.Contains('solidworks-to-urdf.managed-root/v1'))
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
$seams = 'Get-OwnershipRefusalFacts', 'Start-OrRefuse', 'Wait-SettleRetryDelay', 'Read-LockOwner',
  'Get-ProcessFacts', 'Test-LockProbeHeld', 'Restore-PreviousEndpoint', 'Write-LockRefusalReceipt',
  'Get-PreviousGenerationBinding', 'Get-EndpointConfigEarlyProblems', 'Get-EndpointConfigCandidateProgram',
  'Get-ServedStateRoot', 'Get-EndpointConfigScanPython', 'Get-EndpointConfigStrictPrelude',
  'Get-EndpointConfigEarlyScanProgram', 'Write-PythonProgram', 'Normalize-PathIdentity', 'Get-WriterResidue'
$seams += 'Get-AclProjection', 'Set-AclProjection', 'Get-TaskProjection', 'Get-RecoveryCurrentState',
  'Invoke-RecoverySnapshot', 'Invoke-RecoveryRestore', 'Invoke-RecoveryResume', 'Test-NetTcpNoMatchError'
$seams += 'Get-ManagedServiceSid', 'Set-ManagedAcl', 'Assert-ManagedAcl',
  'Set-ManagedEndpointAcls', 'Assert-ManagedEndpointAcls', 'Get-ManagedEndpointPaths',
  'Get-ManagedCommonParent', 'Test-ManagedPathWithin', 'Assert-ManagedOperatorIsAdministrator',
  'Assert-ManagedServiceIsNonAdministrator', 'New-ManagedAccessRule',
  'Get-ManagedAclProjectionForRole', 'Assert-ManagedServiceEnvironmentUnpinned',
  'Get-ManagedComparableRights'
Check 'seams.present' (@($seams | Where-Object { -not (Get-Command $_ -ErrorAction SilentlyContinue) }).Count -eq 0)
foreach ($rights in @(
    [System.Security.AccessControl.FileSystemRights]::Modify,
    [System.Security.AccessControl.FileSystemRights]::Read,
    [System.Security.AccessControl.FileSystemRights]::ReadAndExecute)) {
  Check ('windows_acl.synchronize_' + $rights) (
    (Get-ManagedComparableRights ([long]($rights -bor [System.Security.AccessControl.FileSystemRights]::Synchronize))) -eq
      (Get-ManagedComparableRights ([long]$rights)))
}
Check 'windows_acl.regular_file_read_execute_is_read' (
  (Get-ManagedComparableRights (
    [long][System.Security.AccessControl.FileSystemRights]::ReadAndExecute) -RegularFile) -eq
  (Get-ManagedComparableRights (
    [long][System.Security.AccessControl.FileSystemRights]::Read) -RegularFile))
Check 'windows_recovery.net_tcp_no_match' (Test-NetTcpNoMatchError ([pscustomobject]@{
  CategoryInfo=[pscustomobject]@{Category='ObjectNotFound'}
  FullyQualifiedErrorId='CmdletizationQuery_NotFound_LocalPort,Get-NetTCPConnection'
}))
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
  schema_version='solidworks-to-urdf.windows-recovery/v1'
  members=@([ordered]@{member='task.xml';bytes=$taskBytes.Length;sha256=(Get-BytesSha256 $taskBytes)})
}
[System.IO.File]::WriteAllText(
  (Join-Path $recoverySource 'windows-state.json'),
  ($recoveryState | ConvertTo-Json -Depth 8 -Compress),
  (New-Object System.Text.UTF8Encoding($false)))
$recoveryBundle = Join-Path $recoveryRoot 'recovery.zip'
Compress-Archive -Path (Join-Path $recoverySource '*') -DestinationPath $recoveryBundle
$recoveryRead = Read-RecoveryBundle $recoveryBundle
Check 'windows_recovery.bundle_schema' ($recoveryRead.schema_version -eq 'solidworks-to-urdf.windows-recovery/v1')
$recoveryExpanded = Expand-RecoveryBundle $recoveryBundle $recoveryRoot
Check 'windows_recovery.bundle_member' ((Get-FileHash -LiteralPath (Join-Path $recoveryExpanded.root 'task.xml') -Algorithm SHA256).Hash.ToLower() -eq
  [string]$recoveryRead.members[0].sha256)
Remove-Item -LiteralPath $recoveryRoot -Recurse -Force -ErrorAction SilentlyContinue

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
  $localUserName = 'descsvc' + [guid]::NewGuid().ToString('N').Substring(0, 12)
  $localPassword = ConvertTo-SecureString -String ('Aa1!' + [guid]::NewGuid().ToString('N')) -AsPlainText -Force
  $localUser = New-LocalUser -Name $localUserName -Password $localPassword `
    -AccountNeverExpires -PasswordNeverExpires
  $bootstrapOutput = & $bootstrapScript -ManagedRoot $aclRoot -ServiceAccount $localUserName
  $bootstrapReceipt = ($bootstrapOutput | Select-Object -Last 1) | ConvertFrom-Json
  Check 'windows_acl.bootstrap_marker' ($bootstrapReceipt.passed -eq $true -and
    $bootstrapReceipt.service_sid -eq $localUser.SID.Value -and
    (Test-Path -LiteralPath (Join-Path $aclRoot '.description-managed-root.json') -PathType Leaf))
  Check 'windows_acl.bootstrap_marker_acl' (@($bootstrapReceipt.marker_acl.rules | Where-Object {
    $_.sid -eq $localUser.SID.Value -and
    (Get-ManagedComparableRights ([long]$_.rights) -RegularFile) -eq
      (Get-ManagedComparableRights ([long][System.Security.AccessControl.FileSystemRights]::Read) -RegularFile) -and
    -not $_.inherited
  }).Count -eq 1 -and @($bootstrapReceipt.marker_acl.rules | Where-Object {
    $_.sid -in @('S-1-5-11', 'S-1-1-0', 'S-1-5-32-545', 'S-1-5-4')
  }).Count -eq 0)
  $serviceSid = Assert-ManagedServiceIsNonAdministrator $localUser.SID.Value
  Check 'windows_acl.service_identity_non_admin' ($serviceSid -eq $localUser.SID.Value)
  $adminRejected = $false
  try {
    $null = Assert-ManagedServiceIsNonAdministrator (
      [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value)
  } catch {
    $adminRejected = $_.Exception.Message -match 'windows_service_operator_not_separated'
  }
  Check 'windows_acl.admin_service_identity_refused' $adminRejected
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
  $aclRows = @(Get-ManagedEndpointAclRows $config $endpointRoot $configPath $installIdPath $taskFile $serviceSid)
  Check 'windows_acl.paths_deduplicated' (@($aclRows.path | Select-Object -Unique).Count -eq $aclRows.Count)
  $commonRows = @($aclRows | Where-Object {
    (Normalize-PathIdentity ([string]$_.path)) -eq (Normalize-PathIdentity $commonParent)
  })
  Check 'windows_acl.common_parent_non_inheriting' ($commonRows.Count -eq 1 -and
    (-not $commonRows[0].recurse) -and $commonRows[0].non_inheriting)
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
  Set-ManagedEndpointAcls $config $endpointRoot $configPath $installIdPath $serviceSid $taskFile
  Assert-ManagedEndpointAcls $config $endpointRoot $configPath $installIdPath $serviceSid $taskFile
  $broadFound = @()
  foreach ($row in $aclRows) {
    $broadFound += @((Get-AclProjection $row.path).rules | Where-Object {
      $_.sid -in @('S-1-5-11', 'S-1-1-0', 'S-1-5-32-545', 'S-1-5-4')
    })
  }
  Check 'windows_acl.broad_principals_removed' ($broadFound.Count -eq 0)
  $operatorSid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
  Check 'windows_acl.administrator_owns_boundary' (
    (Get-AclProjection $aclRoot).owner_sid -in @('S-1-5-32-544', $operatorSid))
  $packageRules = @((Get-AclProjection $packageChild).rules)
  Check 'windows_acl.package_service_modify' (@($packageRules | Where-Object {
    $_.sid -eq $serviceSid -and
    $_.rights -eq [long][System.Security.AccessControl.FileSystemRights]::Modify
  }).Count -eq 1)
  $commonServiceRules = @((Get-AclProjection $aclRoot).rules | Where-Object {
    $_.sid -eq $serviceSid
  })
  Check 'windows_acl.common_service_read_execute_only' ($commonServiceRules.Count -eq 1 -and
    $commonServiceRules[0].rights -eq [long][System.Security.AccessControl.FileSystemRights]::ReadAndExecute -and
    ($commonServiceRules[0].rights -band [long][System.Security.AccessControl.FileSystemRights]::DeleteSubdirectoriesAndFiles) -eq 0)
  Check 'windows_acl.token_service_read' (@((Get-AclProjection $tokenPath).rules | Where-Object {
    $_.sid -eq $serviceSid -and
    $_.rights -eq [long][System.Security.AccessControl.FileSystemRights]::Read
  }).Count -eq 1)
  Check 'windows_acl.marker_service_read' (@((Get-AclProjection $markerPath).rules | Where-Object {
    $_.sid -eq $serviceSid -and
    $_.rights -eq [long][System.Security.AccessControl.FileSystemRights]::Read
  }).Count -eq 1)
  Check 'windows_acl.cad_ledger_service_modify' (@((Get-AclProjection $ownershipPath).rules | Where-Object {
    $_.sid -eq $serviceSid -and
    $_.rights -eq [long][System.Security.AccessControl.FileSystemRights]::Modify
  }).Count -eq 1)
  Check 'windows_acl.endpoint_service_read_execute' (@((Get-AclProjection $endpointRoot).rules | Where-Object {
    $_.sid -eq $serviceSid -and
    $_.rights -eq [long][System.Security.AccessControl.FileSystemRights]::ReadAndExecute
  }).Count -eq 1)
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
    Assert-ManagedEndpointAcls $config $endpointRoot $configPath $installIdPath $serviceSid $taskFile
  } catch {
    $assertRejected = $_.Exception.Message -match 'windows_managed_acl_invalid'
  }
  $badAfter = Get-AclProjection $tokenPath
  Check 'windows_acl.snapshot_assert_is_read_only' ($assertRejected -and
    $badBefore.projection_sha256 -eq $badAfter.projection_sha256)
  Check 'windows_acl.sibling_untouched' (
    (Get-AclProjection $siblingRoot).projection_sha256 -eq $siblingBefore.projection_sha256)
  Remove-LocalUser -Name $localUserName -ErrorAction SilentlyContinue
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
$script:Starts = 0
$script:Sleeps = [System.Collections.Generic.List[int]]::new()
$script:CallLog = [System.Collections.Generic.List[string]]::new()
$script:OldHealthOk = $true
$script:HealthProbes = [System.Collections.Generic.List[string]]::new()
function Get-ScheduledTask {
  [CmdletBinding()] param([string]$TaskName)
  if ($script:TaskMissing) { return $null }
  return [pscustomobject]@{ State = $script:TaskState }
}
function Start-ScheduledTask { [CmdletBinding()] param([string]$TaskName) $script:Starts++; $script:CallLog.Add('start') }
function Stop-ScheduledTask { [CmdletBinding()] param([string]$TaskName) $script:CallLog.Add('stop') }
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
  return @{ health = @{ install = @{ config_digest = 'digest' } }; settled_seconds = 180; listener_pid = 4242 }
}
function Wait-SettleRetryDelay { param([int]$Seconds = 60) $script:Sleeps.Add($Seconds) }
function Health-Probe {
  param([string]$configPath, [object]$binding)
  $script:HealthProbes.Add([string]$binding.version)
  if (-not $script:OldHealthOk) { throw 'the previous endpoint did not answer /health' }
  return @{ ready = $true }
}
function Write-Receipt {
  param([string]$path, [hashtable]$payload)
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
[System.IO.File]::WriteAllText(
  (Join-Path $previous 'stage-receipt.json'),
  '{"passed":true,"verb":"stage","version":"1.4.3","source_commit":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","source_sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}')
$configPath = Join-Path $generation 'endpoint.json'
[System.IO.File]::WriteAllText($configPath, ('{{"state_root":"{0}"}}' -f ($root -replace '\\', '/')))
$lockPath = Join-Path $root '.endpoint.lock'
$binding = @{ task_name = 'test-endpoint'; port = 8765; state_root = $root; generation = $generation }
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
  [System.IO.File]::WriteAllText(
    (Join-Path $previous 'stage-receipt.json'),
    '{"passed":true,"verb":"stage","version":"1.4.3","source_commit":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","source_sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}')
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

# ---- failure-path restore: gated start + pinned previous identity -----------
$sourceLauncher = Join-Path $previous 'runtime/Scripts/description.exe'
Reset-State
$restore = Restore-PreviousEndpoint $binding $previous $configPath $generation $sourceLauncher
Check 'restore.success_pinned_health' ($restore.stopped -and $restore.restored -and $restore.startable -and $restore.started -and
  ($restore.healthy -eq $true) -and ($restore.previous_generation -eq $previous) -and
  ($script:HealthProbes.Count -eq 1) -and ($script:HealthProbes[0] -eq '1.4.3') -and
  (($script:CallLog -join ',') -eq 'stop,register,start'))

Reset-State
[System.IO.File]::WriteAllText($lockPath, '999999')
$script:ProcScenario = 'denied'
$restore = Restore-PreviousEndpoint $binding $previous $configPath $generation $sourceLauncher
Check 'restore.gate_unknown_no_start' ($restore.stopped -and $restore.restored -and (-not $restore.startable) -and (-not $restore.started) -and
  ($restore.start_gate -eq 'refused') -and ($restore.error -match 'start gate is refused') -and
  (($script:CallLog -join ',') -eq 'stop,register'))

Reset-State
$script:OldHealthOk = $false
$restore = Restore-PreviousEndpoint $binding $previous $configPath $generation $sourceLauncher
Check 'restore.health_failure_pinned' ($restore.stopped -and $restore.restored -and $restore.started -and (-not $restore.healthy) -and
  ($restore.error -match 'did not answer') -and ($script:HealthProbes.Count -eq 1) -and ($script:HealthProbes[0] -eq '1.4.3'))

Reset-State
Remove-Item -LiteralPath (Join-Path $previous 'stage-receipt.json') -Force
$restore = Restore-PreviousEndpoint $binding $previous $configPath $generation $sourceLauncher
Check 'restore.missing_receipt_unjudged' ($restore.started -and ($null -eq $restore.healthy) -and
  ($restore.error -match 'receipt is unavailable') -and ($script:HealthProbes.Count -eq 0))

# ---- real outer promote flow (child pwsh; real Invoke-Main) -----------------
$harness = Join-Path $PSScriptRoot 'invoke-main-harness.ps1'
$pwshExe = [System.Diagnostics.Process]::GetCurrentProcess().MainModule.FileName
function Invoke-Outer([string]$scenario) {
  $outerRoot = Join-Path ([System.IO.Path]::GetTempPath()) ('il-outer-' + [guid]::NewGuid().ToString('N'))
  $null = New-Item -ItemType Directory -Path $outerRoot -Force
  $json = & $pwshExe -NoProfile -File $harness -ScriptPath $ScriptPath -Scenario $scenario -Root $outerRoot
  $result = ($json | Select-Object -Last 1) | ConvertFrom-Json
  Remove-Item -LiteralPath $outerRoot -Recurse -Force -ErrorAction SilentlyContinue
  return $result
}
$outer = Invoke-Outer 'unknown_owner'
Check 'outer.unknown_owner_no_restart' ((@($outer.calls | Where-Object { $_ -like 'start:*old*' }).Count -eq 0) -and
  ($outer.receipt.restore.startable -eq $false) -and ($outer.receipt.restore.started -eq $false) -and
  ($outer.receipt.restore.start_gate -eq 'refused') -and ($outer.message -match 'lock_refused_unproven'))
Check 'outer.unknown_owner_candidate_started_once' ((@($outer.calls | Where-Object { $_ -like 'start:*new*' }).Count -eq 1))

$outer = Invoke-Outer 'startable_second_refusal'
Check 'outer.startable_starts_old_with_pinned_health' ((@($outer.calls | Where-Object { $_ -like 'start:*old*' }).Count -eq 1) -and
  ($outer.receipt.restore.startable -eq $true) -and ($outer.receipt.restore.started -eq $true) -and
  ($outer.receipt.restore.healthy -eq $true) -and (@($outer.sleeps) -contains 60))

$outer = Invoke-Outer 'startable_missing_receipt'
Check 'outer.missing_receipt_health_unjudged' ((@($outer.calls | Where-Object { $_ -like 'start:*old*' }).Count -eq 1) -and
  ($outer.receipt.restore.started -eq $true) -and ($null -eq $outer.receipt.restore.healthy) -and
  ($outer.receipt.restore.error -match 'receipt is unavailable'))

$outer = Invoke-Outer 'stage_unknown_key_refused'
Check 'outer.stage_unknown_key_refused' (($outer.message -match 'endpoint_config_unknown_keys') -and
  ($outer.message -match 'bogus_key') -and (@($outer.calls).Count -eq 0))

$outer = Invoke-Outer 'stage_legacy_targets_refused'
Check 'outer.stage_legacy_targets_refused' (($outer.message -match 'endpoint_config_unknown_keys') -and
  ($outer.message -match 'targets') -and (@($outer.calls).Count -eq 0))

$outer = Invoke-Outer 'stage_duplicate_key_refused'
Check 'outer.stage_duplicate_key_refused' (($outer.message -match 'endpoint_config_duplicate_keys') -and
  (@($outer.calls).Count -eq 0))

$outer = Invoke-Outer 'promote_candidate_mismatch'
Check 'outer.promote_candidate_mismatch' (($outer.message -match 'candidate_config_mismatch') -and
  (@($outer.calls).Count -eq 0))

$outer = Invoke-Outer 'promote_source_mismatch'
Check 'outer.promote_source_mismatch' (($outer.message -match 'candidate_source_mismatch') -and
  (@($outer.calls).Count -eq 0))

Write-Host ("checks={0} failures={1}" -f $script:checks, $script:failures.Count)
exit ([int]($script:failures.Count -gt 0))
