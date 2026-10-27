# Offline contract tests for deploy/windows/instance-lifecycle.ps1.
#
# Set DESCRIPTION_TEST_PYTHON to the real CPython 3.12 executable before running:
#   pwsh -NoProfile -File tests/deploy/test_instance_lifecycle.ps1
#
# The suite is cross-platform.  Windows-only ACL, Scheduled Task and process observations are
# replaced with narrow stubs; strict JSON/release/archive parsing and the install state machine
# execute for real.
[CmdletBinding()]
param(
  [string]$ScriptPath = '',
  [switch]$StaticOnly
)
$ErrorActionPreference = 'Stop'
$repositoryRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
if (-not $ScriptPath) {
  $ScriptPath = Join-Path (Join-Path (Join-Path $repositoryRoot 'deploy') 'windows') 'instance-lifecycle.ps1'
}
$bootstrapPath = Join-Path (Split-Path -Parent $ScriptPath) 'bootstrap-managed-root.ps1'
$script:checks = 0
$script:failures = [System.Collections.Generic.List[string]]::new()

function Check([string]$Name, [bool]$Condition, [string]$Detail = '') {
  $script:checks++
  if ($Condition) {
    Write-Host ('PASS ' + $Name)
  } else {
    $script:failures.Add($Name)
    Write-Host ('FAIL ' + $Name + ' ' + $Detail)
  }
}

function Invoke-Refusal([scriptblock]$Action) {
  try {
    $null = & $Action
    return @{threw=$false; code=''; message=''}
  } catch {
    $code = ''
    try { $code = [string]$_.Exception.Data['code'] } catch { }
    return @{
      threw=$true
      code=$code
      message=[string]$_.Exception.Message
    }
  }
}

function New-TestBindingPath(
    [string]$Root,
    [string]$EndpointConfig,
    [string]$ReleaseFile,
    [string]$PythonExecutable,
    [hashtable]$Extra = @{},
    [string]$Remove = '') {
  $document = [ordered]@{
    endpoint_config=$EndpointConfig
    release_file=$ReleaseFile
    task_name='urdf-endpoint'
    task_path='\'
    service_account='fixture-user'
    python_executable=$PythonExecutable
  }
  foreach ($key in $Extra.Keys) { $document[$key] = $Extra[$key] }
  if ($Remove) { $document.Remove($Remove) }
  $path = Join-Path $Root ('binding-' + [guid]::NewGuid().ToString('N') + '.json')
  [System.IO.File]::WriteAllText(
    $path,
    ($document | ConvertTo-Json -Depth 4 -Compress),
    (New-Object System.Text.UTF8Encoding($false)))
  return $path
}

function Get-TestPython {
  $candidate = $env:DESCRIPTION_TEST_PYTHON
  if (-not $candidate -or -not (Test-Path -LiteralPath $candidate -PathType Leaf)) {
    throw 'DESCRIPTION_TEST_PYTHON must name the bound CPython 3.12 executable'
  }
  $version = (& $candidate -c 'import platform, sys; print(platform.python_implementation(), *sys.version_info[:2])') -join ''
  if ($LASTEXITCODE -ne 0 -or $version -cne 'CPython 3 12') {
    throw 'DESCRIPTION_TEST_PYTHON must be CPython 3.12'
  }
  return [string]$candidate
}

function Write-TestConfig(
    [string]$Path,
    [string]$Root,
    [string]$Token,
    [hashtable]$Mutation = @{},
    [string]$Raw = '') {
  if ($Raw) {
    [System.IO.File]::WriteAllText($Path, $Raw, (New-Object System.Text.UTF8Encoding($false)))
    return
  }
  $document = [ordered]@{
    schema_version='solidworks-to-urdf.endpoint/v1'
    package_root=(Join-Path $Root 'packages')
    output_root=(Join-Path $Root 'results')
    state_root=(Join-Path $Root 'state')
    token_file=$Token
    host='127.0.0.1'
    port=8765
  }
  foreach ($key in $Mutation.Keys) { $document[$key] = $Mutation[$key] }
  [System.IO.File]::WriteAllText(
    $Path,
    ($document | ConvertTo-Json -Depth 5 -Compress),
    (New-Object System.Text.UTF8Encoding($false)))
}

function Write-TestRelease(
    [string]$Path,
    [string]$ArtifactName,
    [string]$ArtifactSha,
    [array]$MoreArtifacts = @()) {
  $artifacts = [ordered]@{}
  $artifacts[$ArtifactName] = $ArtifactSha
  foreach ($row in $MoreArtifacts) { $artifacts[[string]$row.name] = [string]$row.sha256 }
  $document = [ordered]@{
    schema_version='solidworks-to-urdf.release/v1'
    version='1.5.0'
    source_commit=('a' * 40)
    source_tree=('b' * 40)
    source_sha256=('c' * 64)
    artifacts=$artifacts
  }
  [System.IO.File]::WriteAllText(
    $Path,
    ($document | ConvertTo-Json -Depth 6 -Compress),
    (New-Object System.Text.UTF8Encoding($false)))
}

function Get-TestPosixRelativePath([string]$Root, [string]$Path) {
  $relative = $Path.Substring($Root.Length).TrimStart('\', '/')
  return $relative.Replace('\', '/')
}

function New-TestZipFromDirectory([string]$Source, [string]$Destination) {
  Add-Type -AssemblyName System.IO.Compression
  Add-Type -AssemblyName System.IO.Compression.FileSystem
  $zip = [System.IO.Compression.ZipFile]::Open(
    $Destination, [System.IO.Compression.ZipArchiveMode]::Create)
  try {
    foreach ($file in @(Get-ChildItem -LiteralPath $Source -Recurse -File | Sort-Object FullName)) {
      $entryName = Get-TestPosixRelativePath $Source $file.FullName
      if (-not $entryName -or $entryName.Contains('\')) {
        throw 'the test archive entry is not POSIX-separated'
      }
      $entry = $zip.CreateEntry(
        $entryName, [System.IO.Compression.CompressionLevel]::Optimal)
      $inputStream = [System.IO.File]::OpenRead($file.FullName)
      try {
        $outputStream = $entry.Open()
        try {
          $inputStream.CopyTo($outputStream)
        } finally {
          $outputStream.Dispose()
        }
      } finally {
        $inputStream.Dispose()
      }
    }
  } finally {
    $zip.Dispose()
  }
}

# ---- static boundaries ------------------------------------------------------
$tokens = $null
$parseErrors = $null
$null = [System.Management.Automation.Language.Parser]::ParseFile(
  $ScriptPath, [ref]$tokens, [ref]$parseErrors)
Check 'parse.clean' ($parseErrors.Count -eq 0) (($parseErrors | Out-String).Trim())
$bootstrapTokens = $null
$bootstrapErrors = $null
$null = [System.Management.Automation.Language.Parser]::ParseFile(
  $bootstrapPath, [ref]$bootstrapTokens, [ref]$bootstrapErrors)
Check 'parse.bootstrap_clean' ($bootstrapErrors.Count -eq 0) (($bootstrapErrors | Out-String).Trim())
$source = [System.IO.File]::ReadAllText($ScriptPath)
$bootstrapSource = [System.IO.File]::ReadAllText($bootstrapPath)
Check 'interface.public_verbs' (
  $source.Contains("`$allowed = @('install', 'start', 'drain', 'stop', 'status', 'health')"))
Check 'interface.exact_binding_fields' (
  $source.Contains("'endpoint_config'") -and
  $source.Contains("'release_file'") -and
  $source.Contains("'task_name'") -and
  $source.Contains("'task_path'") -and
  $source.Contains("'service_account'") -and
  $source.Contains("'python_executable'"))
foreach ($legacy in @(
    "switch (`$script:CurrentVerb) {`n      'stage'",
    "'promote'",
    "'rollback'",
    'recovery-snapshot',
    'recovery-restore',
    'recovery-verify',
    '-StageDir',
    '-Previous',
    'stage-receipt.json',
    'windows-install/v1')) {
  Check ('static.legacy_absent.' + ($legacy -replace '[^A-Za-z0-9]+', '_').Trim('_')) (
    -not $source.Contains($legacy))
}
Check 'static.no_second_public_installer' (
  -not (Test-Path -LiteralPath (Join-Path $PSScriptRoot 'invoke-main-harness.ps1')))
Check 'static.single_managed_bootstrap' (
  $source.Contains('Invoke-ManagedRootBootstrap') -and
  $bootstrapSource.Contains('PostflightScript') -and
  $bootstrapSource.Contains('managed_root_transaction_failed'))
Check 'static.launch_contract' (
  $source.Contains('-I -B -m description_pipeline.orchestration.windows serve --config') -and
  $source.Contains("`$script:PythonLaunchContract = 'python_module_v1'"))
Check 'static.fixed_control_paths' (
  $source.Contains('function Get-ControlPaths') -and
  $source.Contains("'windows-binding.json'") -and
  $source.Contains("'instance-lifecycle.ps1'") -and
  $source.Contains("'bootstrap-managed-root.ps1'") -and
  $source.Contains('binding_sha256'))
Check 'static.no_direct_cad_launch' (
  -not $source.Contains('New-Object -ComObject') -and
  -not $source.Contains('CreateObject'))
if ([System.Environment]::OSVersion.Platform -eq [System.PlatformID]::Win32NT) {
  $settingsParameters = (Get-Command New-ScheduledTaskSettingsSet).Parameters
  Check 'windows.scheduled_task_settings_contract' (
    $settingsParameters.ContainsKey('ExecutionTimeLimit') -and
    $settingsParameters.ContainsKey('MultipleInstances') -and
    $settingsParameters.ContainsKey('AllowStartIfOnBatteries') -and
    $settingsParameters.ContainsKey('DontStopIfGoingOnBatteries'))
}
$installStart = $source.IndexOf('function Invoke-Install')
$installEnd = $source.IndexOf('function Invoke-Start', $installStart)
$installSource = if ($installStart -ge 0 -and $installEnd -gt $installStart) {
  $source.Substring($installStart, $installEnd - $installStart)
} else { '' }
Check 'install.registers_disabled_only' (
  $installSource.Contains('Register-DisabledTask') -and
  $installSource.Contains("projection.state -cne 'Disabled'") -and
  -not $installSource.Contains('Start-ScheduledTask'))
Check 'install.result_has_no_fake_health' (
  $installSource.Contains('listener = $null') -and
  $installSource.Contains('health = $null'))
$installPairCheck = $installSource.IndexOf('Assert-InstallStatePair')
$installBootstrap = $installSource.IndexOf('Invoke-ManagedRootBootstrap')
Check 'install.state_pair_checked_before_bootstrap' (
  $installPairCheck -ge 0 -and
  $installBootstrap -gt $installPairCheck)
$readJsonStart = $source.IndexOf('function Read-Json')
$readJsonEnd = $source.IndexOf('function Sha256', $readJsonStart)
$readJsonSource = if ($readJsonStart -ge 0 -and $readJsonEnd -gt $readJsonStart) {
  $source.Substring($readJsonStart, $readJsonEnd - $readJsonStart)
} else { '' }
$aclProjectionStart = $source.IndexOf('function Get-AclProjection')
$aclProjectionEnd = $source.IndexOf('function New-ManagedAccessRule', $aclProjectionStart)
$aclProjectionSource = if (
    $aclProjectionStart -ge 0 -and
    $aclProjectionEnd -gt $aclProjectionStart) {
  $source.Substring($aclProjectionStart, $aclProjectionEnd - $aclProjectionStart)
} else { '' }
Check 'acl.projection_owner_uses_resolved_sid' (
  $aclProjectionSource.Contains('owner_sid = Resolve-Sid ([string]$acl.Owner)') -and
  -not $aclProjectionSource.Contains(
    '[System.Security.Principal.SecurityIdentifier]$acl.Owner'))
$assertAclStart = $source.IndexOf('function Assert-ProtectedAcl')
$assertAclEnd = $source.IndexOf('function Set-TaskFileAcl', $assertAclStart)
$assertAclSource = if (
    $assertAclStart -ge 0 -and
    $assertAclEnd -gt $assertAclStart) {
  $source.Substring($assertAclStart, $assertAclEnd - $assertAclStart)
} else { '' }
Check 'acl.assert_uses_actual_full_control' (
  $assertAclSource.Contains('$rights = [long]$row.rights') -and
  -not $assertAclSource.Contains('Synchronize'))
Check 'json.single_strict_read_path' (
  -not $source.Contains('$script:JsonPython') -and
  $readJsonSource.Contains('Assert-StrictJsonText $text') -and
  $readJsonSource.Contains('ConvertFrom-Json'))
$taskReadStart = $source.IndexOf('function Get-BoundTask')
$taskReadEnd = $source.IndexOf('function Get-RequiredTask', $taskReadStart)
$taskReadSource = if ($taskReadStart -ge 0 -and $taskReadEnd -gt $taskReadStart) {
  $source.Substring($taskReadStart, $taskReadEnd - $taskReadStart)
} else { '' }
Check 'task_observer.strict_enumeration' (
  $taskReadSource.Contains('Get-ScheduledTask -ErrorAction Stop') -and
  -not $taskReadSource.Contains('SilentlyContinue'))

if ($StaticOnly) {
  Write-Host ('checks={0} failures={1}' -f $script:checks, $script:failures.Count)
  exit ([int]($script:failures.Count -gt 0))
}

# ---- load the script without running main ----------------------------------
. $ScriptPath -Verb status -Binding @{}

# ---- listener observation uses successful empty CIM data -------------------
& {
  $script:ListenerProbe = [pscustomobject]@{
    namespace = $null
    class_name = $null
    filter = $null
    error = $null
    values = @()
  }
  function Get-CimInstance {
    [CmdletBinding()]
    param(
      [string]$Namespace,
      [string]$ClassName,
      [string]$Filter
    )
    $script:ListenerProbe.namespace = $Namespace
    $script:ListenerProbe.class_name = $ClassName
    $script:ListenerProbe.filter = $Filter
    if ($script:ListenerProbe.error) { throw $script:ListenerProbe.error }
    return $script:ListenerProbe.values
  }

  $absent = @(Get-ListenerConnections 8765)
  Check 'listener.cim_empty_is_absent' (
    $absent.Count -eq 0 -and
    $script:ListenerProbe.namespace -ceq 'root/StandardCimv2' -and
    $script:ListenerProbe.class_name -ceq 'MSFT_NetTCPConnection' -and
    $script:ListenerProbe.filter -ceq 'LocalPort = 8765 AND State = 2')

  $script:ListenerProbe.values = @([pscustomobject]@{OwningProcess=4242})
  $present = @(Get-ListenerConnections 8765)
  Check 'listener.cim_present_preserves_owner' (
    $present.Count -eq 1 -and [int]$present[0].OwningProcess -eq 4242)

  $script:ListenerProbe.error = 'CIM observer failed'
  $failed = Invoke-Refusal { Get-ListenerConnections 8765 }
  Check 'listener.cim_failure_propagates' $failed.threw
}

# SID and ACL APIs require Windows; the Linux matrix keeps its existing stubs.
if ($env:OS -eq 'Windows_NT') {
  # Exercise the production Resolve-Sid before install fixtures override it below.
  function New-AclProjectionFixture([string]$Owner) {
    $acl = [pscustomobject]@{
      Owner = $Owner
      AreAccessRulesProtected = $true
    }
    $acl | Add-Member -MemberType ScriptMethod -Name GetAccessRules -Value {
      param($Explicit, $Inherited, $TargetType)
      return @()
    }
    $acl | Add-Member -MemberType ScriptMethod -Name GetSecurityDescriptorSddlForm -Value {
      param($Sections)
      return 'O:BAG:SY'
    }
    return $acl
  }

  function Get-Acl {
    [CmdletBinding()]
    param([string]$LiteralPath)
    return $script:FixtureAcl
  }

  $script:FixtureAcl = $null
  try {
    $script:FixtureAcl = New-AclProjectionFixture 'S-1-5-32-544'
    $sidOwnerProjection = Get-AclProjection 'acl-projection-fixture'
    Check 'acl.owner_sid_resolves' (
      $sidOwnerProjection.owner_sid -ceq 'S-1-5-32-544' -and
      $sidOwnerProjection.protected -eq $true -and
      @($sidOwnerProjection.rules).Count -eq 0)

    if ($PSVersionTable.PSEdition -eq 'Desktop' -and
        $PSVersionTable.PSVersion.Major -eq 5) {
      $administratorsName = (
        [System.Security.Principal.SecurityIdentifier]'S-1-5-32-544'
      ).Translate([System.Security.Principal.NTAccount]).Value
      $script:FixtureAcl = New-AclProjectionFixture $administratorsName
      $nameOwnerProjection = Get-AclProjection 'acl-projection-fixture'
      Check 'acl.owner_desktop_name_resolves' (
        $administratorsName -notmatch '^S-1-' -and
        $nameOwnerProjection.owner_sid -ceq 'S-1-5-32-544')
    }

    $unresolvedOwnerName = 'description-unresolvable-' + [guid]::NewGuid().ToString('N')
    $script:FixtureAcl = New-AclProjectionFixture $unresolvedOwnerName
    $unresolvedOwner = Invoke-Refusal {
      Get-AclProjection 'acl-projection-fixture'
    }
    Check 'acl.owner_unresolved_fails_closed' (
      $unresolvedOwner.threw -and
      $unresolvedOwner.code -ceq 'service_account_invalid')
  } finally {
    Remove-Item Function:\Get-Acl -Force
    $script:FixtureAcl = $null
  }

  # Exercise the production ACL functions before install fixtures override them below.
  $aclRoundtripRoot = Join-Path ([System.IO.Path]::GetTempPath()) (
    'description-acl-roundtrip-' + [guid]::NewGuid().ToString('N'))
  $aclServiceSid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
  try {
    $null = New-Item -ItemType Directory -Path $aclRoundtripRoot
    $roundtripFile = Join-Path $aclRoundtripRoot 'roundtrip.txt'
    [System.IO.File]::WriteAllText($roundtripFile, 'acl-roundtrip')
    $roundtripRefusal = Invoke-Refusal {
      Set-ProtectedAcl $aclRoundtripRoot $aclServiceSid
      Assert-ProtectedAcl $aclRoundtripRoot $aclServiceSid
      Set-ProtectedAcl $roundtripFile $aclServiceSid
      Assert-ProtectedAcl $roundtripFile $aclServiceSid
    }
    $roundtripPassed = -not $roundtripRefusal.threw
    Check 'acl.production_roundtrip_directory_and_file' $roundtripPassed $roundtripRefusal.code

    $incompleteFile = Join-Path $aclRoundtripRoot 'incomplete.txt'
    [System.IO.File]::WriteAllText($incompleteFile, 'incomplete')
    Set-ProtectedAcl $incompleteFile $aclServiceSid
    $incompleteAcl = Get-Acl -LiteralPath $incompleteFile -ErrorAction Stop
    $reducedRule = New-Object System.Security.AccessControl.FileSystemAccessRule -ArgumentList @(
      (New-Object System.Security.Principal.SecurityIdentifier -ArgumentList $aclServiceSid),
      [System.Security.AccessControl.FileSystemRights]::ReadAndExecute,
      [System.Security.AccessControl.InheritanceFlags]::None,
      [System.Security.AccessControl.PropagationFlags]::None,
      [System.Security.AccessControl.AccessControlType]::Allow)
    $incompleteAcl.SetAccessRule($reducedRule)
    Set-Acl -LiteralPath $incompleteFile -AclObject $incompleteAcl -ErrorAction Stop
    $incompleteRefusal = Invoke-Refusal {
      Assert-ProtectedAcl $incompleteFile $aclServiceSid
    }
    Check 'acl.incomplete_rights_refused' (
      $incompleteRefusal.threw -and
      $incompleteRefusal.code -ceq 'windows_managed_acl_invalid')

    $extraFile = Join-Path $aclRoundtripRoot 'extra.txt'
    [System.IO.File]::WriteAllText($extraFile, 'extra')
    Set-ProtectedAcl $extraFile $aclServiceSid
    $extraAcl = Get-Acl -LiteralPath $extraFile -ErrorAction Stop
    $extraRule = New-Object System.Security.AccessControl.FileSystemAccessRule -ArgumentList @(
      (New-Object System.Security.Principal.SecurityIdentifier -ArgumentList 'S-1-1-0'),
      [System.Security.AccessControl.FileSystemRights]::Read,
      [System.Security.AccessControl.InheritanceFlags]::None,
      [System.Security.AccessControl.PropagationFlags]::None,
      [System.Security.AccessControl.AccessControlType]::Allow)
    $extraAcl.AddAccessRule($extraRule)
    Set-Acl -LiteralPath $extraFile -AclObject $extraAcl -ErrorAction Stop
    $extraRefusal = Invoke-Refusal {
      Assert-ProtectedAcl $extraFile $aclServiceSid
    }
    Check 'acl.extra_principal_refused' (
      $extraRefusal.threw -and
      $extraRefusal.code -ceq 'windows_managed_acl_invalid')
  } finally {
    if (Test-Path -LiteralPath $aclRoundtripRoot) {
      Remove-Item -LiteralPath $aclRoundtripRoot -Recurse -Force
    }
  }
}

$python = Get-TestPython
$processArguments = @('', 'space value', 'embedded"quote', 'C:\space path\', 'slash\"quote', "line`nbreak")
$processProbe = @'
import json, sys
print(json.dumps(sys.argv[1:]))
sys.stderr.write("diagnostic " + "x" * 131072)
sys.exit(7)
'@
$capture = Invoke-CapturedProcess $python (@('-I', '-B', '-c', $processProbe) + $processArguments)
$observedArguments = $capture.lines[0] | ConvertFrom-Json
$observedArgumentRows = @($observedArguments)
$expectedArgumentRows = @($processArguments)
$argumentsMatch = $observedArgumentRows.Count -eq $expectedArgumentRows.Count
if ($argumentsMatch) {
  for ($index = 0; $index -lt $expectedArgumentRows.Count; $index++) {
    if (-not [string]::Equals(
        [string]$observedArgumentRows[$index],
        [string]$expectedArgumentRows[$index],
        [System.StringComparison]::Ordinal)) {
      $argumentsMatch = $false
      break
    }
  }
}
$argumentDetail = (
  'expected=' + ($expectedArgumentRows | ConvertTo-Json -Compress) +
  ' observed=' + ($observedArgumentRows | ConvertTo-Json -Compress))
Check 'process.argv_preserved' (
  $argumentsMatch) $argumentDetail
Check 'process.stderr_and_exit_preserved' (
  $capture.exit_code -eq 7 -and $capture.text.Contains('diagnostic ' + ('x' * 131072)))
$root = Join-Path ([System.IO.Path]::GetTempPath()) (
  'lifecycle-contract-' + [guid]::NewGuid().ToString('N'))
$null = New-Item -ItemType Directory -Path $root -Force
$token = Join-Path $root 'endpoint.token'
[System.IO.File]::WriteAllText($token, ('t' * 40), (New-Object System.Text.UTF8Encoding($false)))
$configPath = Join-Path $root 'endpoint.json'
Write-TestConfig $configPath $root $token
$archiveName = 'description-1.5.0-windows-cp312-x86_64.zip'
$archivePath = Join-Path $root $archiveName
[System.IO.File]::WriteAllText($archivePath, 'fixture-archive')
$releasePath = Join-Path $root 'release.json'
Write-TestRelease $releasePath $archiveName ((Get-FileHash $archivePath -Algorithm SHA256).Hash.ToLowerInvariant())
$bindingPath = New-TestBindingPath $root $configPath $releasePath $python
$binding = Read-Binding $bindingPath
Check 'binding.valid' (
  @($binding.Keys).Count -eq 6 -and $binding.task_name -eq 'urdf-endpoint')
$unknownBinding = New-TestBindingPath $root $configPath $releasePath $python @{extra='no'}
$missingBinding = New-TestBindingPath $root $configPath $releasePath $python @{} 'service_account'
Check 'binding.unknown_field_refused' (
  (Invoke-Refusal { Read-Binding $unknownBinding }).code -eq 'binding_fields_invalid')
Check 'binding.missing_field_refused' (
  (Invoke-Refusal { Read-Binding $missingBinding }).code -eq 'binding_fields_invalid')
$duplicateBindingText = (Read-Utf8Text $bindingPath).TrimEnd()
$duplicateBindingText = $duplicateBindingText.Substring(0, $duplicateBindingText.Length - 1) +
  ',"task_name":"duplicate"}'
$duplicateBindingPath = Join-Path $root 'binding-duplicate.json'
[System.IO.File]::WriteAllText(
  $duplicateBindingPath,
  $duplicateBindingText,
  (New-Object System.Text.UTF8Encoding($false)))
$duplicateBindingBytes = [Convert]::ToBase64String(
  [System.IO.File]::ReadAllBytes($duplicateBindingPath))
$duplicateBindingRefusal = Invoke-Refusal { Read-Binding $duplicateBindingPath }
Check 'binding.duplicate_key_refused_preserved' (
  $duplicateBindingRefusal.code -eq 'json_duplicate_keys' -and
  [Convert]::ToBase64String(
    [System.IO.File]::ReadAllBytes($duplicateBindingPath)) -ceq $duplicateBindingBytes)
$escapedDuplicateText = (Read-Utf8Text $bindingPath).TrimEnd()
$escapedDuplicateText = $escapedDuplicateText.Substring(0, $escapedDuplicateText.Length - 1) +
  ',"task\u005fname":"duplicate"}'
$escapedDuplicatePath = Join-Path $root 'binding-escaped-duplicate.json'
[System.IO.File]::WriteAllText(
  $escapedDuplicatePath,
  $escapedDuplicateText,
  (New-Object System.Text.UTF8Encoding($false)))
Check 'binding.escaped_duplicate_key_refused' (
  (Invoke-Refusal { Read-Binding $escapedDuplicatePath }).code -eq 'json_duplicate_keys')

$pairRoot = Join-Path $root 'state-pair'
$null = New-Item -ItemType Directory -Path $pairRoot -Force
$pairId = Join-Path $pairRoot 'install-id'
$pairReceipt = Join-Path $pairRoot 'install-receipt.json'
Check 'install_state.absent_pair_accepted' (
  -not (Invoke-Refusal { Assert-InstallStatePair $pairId $pairReceipt }).threw)
[System.IO.File]::WriteAllText($pairId, ('win-' + ('a' * 32)))
$idOnly = Invoke-Refusal { Assert-InstallStatePair $pairId $pairReceipt }
Check 'install_state.id_without_receipt_refused_preserved' (
  $idOnly.code -eq 'install_state_incomplete' -and
  (Read-Utf8Text $pairId).Trim() -eq ('win-' + ('a' * 32)) -and
  -not (Test-Path -LiteralPath $pairReceipt))
Remove-Item -LiteralPath $pairId -Force
[System.IO.File]::WriteAllText($pairReceipt, '{"sentinel":true}')
$receiptOnly = Invoke-Refusal { Assert-InstallStatePair $pairId $pairReceipt }
Check 'install_state.receipt_without_id_refused_preserved' (
  $receiptOnly.code -eq 'install_state_incomplete' -and
  -not (Test-Path -LiteralPath $pairId) -and
  (Read-Utf8Text $pairReceipt) -eq '{"sentinel":true}')
[System.IO.File]::WriteAllText($pairId, ('win-' + ('b' * 32)))
Check 'install_state.complete_pair_admitted' (
  -not (Invoke-Refusal { Assert-InstallStatePair $pairId $pairReceipt }).threw)

$release = Read-ReleaseManifest $binding
Check 'release.unique_windows_archive' (
  $release.archive_name -eq $archiveName -and
  $release.archive_sha256 -eq (Get-FileHash $archivePath -Algorithm SHA256).Hash.ToLowerInvariant())
$duplicateReleaseText = (Read-Utf8Text $releasePath).TrimEnd()
$duplicateReleaseText = $duplicateReleaseText.Substring(0, $duplicateReleaseText.Length - 1) +
  ',"version":"duplicate"}'
$duplicateReleasePath = Join-Path $root 'release-duplicate.json'
[System.IO.File]::WriteAllText(
  $duplicateReleasePath,
  $duplicateReleaseText,
  (New-Object System.Text.UTF8Encoding($false)))
$duplicateReleaseBytes = [Convert]::ToBase64String(
  [System.IO.File]::ReadAllBytes($duplicateReleasePath))
$duplicateReleaseBindingPath = New-TestBindingPath $root $configPath $duplicateReleasePath $python
$duplicateReleaseBinding = Read-Binding $duplicateReleaseBindingPath
Check 'release.duplicate_key_refused' (
  (Invoke-Refusal { Read-ReleaseManifest $duplicateReleaseBinding }).code -eq
    'json_duplicate_keys' -and
  [Convert]::ToBase64String(
    [System.IO.File]::ReadAllBytes($duplicateReleasePath)) -ceq $duplicateReleaseBytes)
$nestedDuplicateReleasePath = Join-Path $root 'release-nested-duplicate.json'
[System.IO.File]::WriteAllText(
  $nestedDuplicateReleasePath,
  '{"artifacts":{"description-1.5.0-windows-cp312-x86_64.zip":"' +
    ('a' * 64) + '","description-1.5.0-windows-cp312-x86_64.zip":"' +
    ('b' * 64) + '"}}',
  (New-Object System.Text.UTF8Encoding($false)))
$nestedDuplicateBindingPath = New-TestBindingPath `
  $root $configPath $nestedDuplicateReleasePath $python
$nestedDuplicateBinding = Read-Binding $nestedDuplicateBindingPath
Check 'release.nested_duplicate_key_refused' (
  (Invoke-Refusal { Read-ReleaseManifest $nestedDuplicateBinding }).code -eq
    'json_duplicate_keys')
$duplicateReceiptPath = Join-Path $root 'install-receipt-duplicate.json'
[System.IO.File]::WriteAllText(
  $duplicateReceiptPath,
  '{"schema_version":"solidworks-to-urdf.windows-install-receipt/v1",' +
    '"passed":true,"passed":false}',
  (New-Object System.Text.UTF8Encoding($false)))
$duplicateReceiptBytes = [Convert]::ToBase64String(
  [System.IO.File]::ReadAllBytes($duplicateReceiptPath))
Check 'receipt.duplicate_key_refused_preserved' (
  (Invoke-Refusal { Read-InstallReceipt $duplicateReceiptPath }).code -eq
    'json_duplicate_keys' -and
  [Convert]::ToBase64String(
    [System.IO.File]::ReadAllBytes($duplicateReceiptPath)) -ceq $duplicateReceiptBytes)
$invalidJsonPath = Join-Path $root 'invalid.json'
[System.IO.File]::WriteAllText(
  $invalidJsonPath,
  '{"value":}',
  (New-Object System.Text.UTF8Encoding($false)))
$invalidJsonBytes = [Convert]::ToBase64String(
  [System.IO.File]::ReadAllBytes($invalidJsonPath))
Check 'json.invalid_syntax_refused_preserved' (
  (Invoke-Refusal { Read-Json $invalidJsonPath }).code -eq 'json_unreadable' -and
  [Convert]::ToBase64String(
    [System.IO.File]::ReadAllBytes($invalidJsonPath)) -ceq $invalidJsonBytes)
$duplicateInventoryRoot = Join-Path $root 'duplicate-inventory'
$null = New-Item -ItemType Directory -Path $duplicateInventoryRoot -Force
$duplicateInventory = Join-Path $duplicateInventoryRoot 'files.json'
[System.IO.File]::WriteAllText(
  $duplicateInventory,
  '{"README.md":"' + ('a' * 64) + '","README.md":"' + ('b' * 64) + '"}',
  (New-Object System.Text.UTF8Encoding($false)))
$duplicateInventoryBytes = [Convert]::ToBase64String(
  [System.IO.File]::ReadAllBytes($duplicateInventory))
Check 'files_json.duplicate_key_refused_preserved' (
  (Invoke-Refusal { Assert-ArchiveInventory $duplicateInventoryRoot }).code -eq
    'json_duplicate_keys' -and
  [Convert]::ToBase64String(
    [System.IO.File]::ReadAllBytes($duplicateInventory)) -ceq $duplicateInventoryBytes)
$second = 'description-1.5.0-windows-foreign.zip'
[System.IO.File]::WriteAllText((Join-Path $root $second), 'second')
$ambiguousRelease = Join-Path $root 'release-ambiguous.json'
Write-TestRelease $ambiguousRelease $archiveName $release.archive_sha256 @(
  @{name=$second; sha256=(Get-FileHash (Join-Path $root $second) -Algorithm SHA256).Hash.ToLowerInvariant()}
)
$ambiguousBindingPath = New-TestBindingPath $root $configPath $ambiguousRelease $python
$ambiguousBinding = Read-Binding $ambiguousBindingPath
Check 'release.ambiguous_windows_archive_refused' (
  (Invoke-Refusal { Read-ReleaseManifest $ambiguousBinding }).code -eq 'windows_archive_ambiguous')
[System.IO.File]::WriteAllText($archivePath, 'tampered')
Check 'release.archive_digest_refused' (
  (Invoke-Refusal { Read-ReleaseManifest $binding }).code -eq 'windows_archive_mismatch')
[System.IO.File]::WriteAllText($archivePath, 'fixture-archive')

$config = Read-EndpointConfigStrict $binding
Check 'endpoint_config.strict_valid' (
  $config.port -eq 8765 -and $config.host -eq '127.0.0.1')
$duplicatePath = Join-Path $root 'duplicate.json'
Write-TestConfig $duplicatePath $root $token @{} (
  '{"schema_version":"solidworks-to-urdf.endpoint/v1","package_root":"' +
  (($root + '/packages').Replace('\', '/')) +
  '","output_root":"' + (($root + '/results').Replace('\', '/')) +
  '","state_root":"' + (($root + '/state').Replace('\', '/')) +
  '","token_file":"' + ($token.Replace('\', '/')) +
  '","host":"127.0.0.1","port":8765,"port":8766}')
$duplicateBindingPath = New-TestBindingPath $root $duplicatePath $releasePath $python
$duplicateBinding = Read-Binding $duplicateBindingPath
Check 'endpoint_config.duplicate_refused' (
  (Invoke-Refusal { Read-EndpointConfigStrict $duplicateBinding }).code -eq 'endpoint_config_invalid')
$unknownConfig = Join-Path $root 'unknown-config.json'
Write-TestConfig $unknownConfig $root $token @{targets=@{}}
$unknownConfigBindingPath = New-TestBindingPath $root $unknownConfig $releasePath $python
$unknownConfigBinding = Read-Binding $unknownConfigBindingPath
Check 'endpoint_config.unknown_key_refused' (
  (Invoke-Refusal { Read-EndpointConfigStrict $unknownConfigBinding }).code -eq 'endpoint_config_invalid')
$badHost = Join-Path $root 'bad-host.json'
Write-TestConfig $badHost $root $token @{host='0.0.0.0'}
$badHostBindingPath = New-TestBindingPath $root $badHost $releasePath $python
$badHostBinding = Read-Binding $badHostBindingPath
Check 'endpoint_config.non_loopback_refused' (
  (Invoke-Refusal { Read-EndpointConfigStrict $badHostBinding }).code -eq 'endpoint_config_invalid')
$nestedConfig = Join-Path $root 'nested-config.json'
Write-TestConfig $nestedConfig $root $token @{
  state_root=(Join-Path $root 'packages/state')
}
$nestedConfigBindingPath = New-TestBindingPath $root $nestedConfig $releasePath $python
$nestedConfigBinding = Read-Binding $nestedConfigBindingPath
$nestedParsed = Read-EndpointConfigStrict $nestedConfigBinding
Check 'endpoint_config.nested_roots_refused' (
  (Invoke-Refusal { Assert-EndpointRootsSeparate $nestedParsed }).code -eq
    'endpoint_config_invalid')

# ---- archive inventory and traversal ---------------------------------------
$archiveSource = Join-Path $root 'archive-source'
$null = New-Item -ItemType Directory -Path (Join-Path $archiveSource 'wheels') -Force
foreach ($path in @(
    (Join-Path $archiveSource 'README.md'),
    (Join-Path $archiveSource 'requirements.lock'),
    (Join-Path $archiveSource 'wheels/fixture.whl'))) {
  [System.IO.File]::WriteAllText($path, ('payload:' + [System.IO.Path]::GetFileName($path)))
}
$inventory = [ordered]@{}
foreach ($file in @(Get-ChildItem -LiteralPath $archiveSource -Recurse -File)) {
  $relative = Get-TestPosixRelativePath $archiveSource $file.FullName
  $inventory[$relative] = (Get-FileHash $file.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
}
[System.IO.File]::WriteAllText(
  (Join-Path $archiveSource 'files.json'),
  ($inventory | ConvertTo-Json -Compress),
  (New-Object System.Text.UTF8Encoding($false)))
$serviceArchive = Join-Path $root 'service.zip'
New-TestZipFromDirectory $archiveSource $serviceArchive
$serviceZip = [System.IO.Compression.ZipFile]::OpenRead($serviceArchive)
try {
  $serviceEntryNames = @($serviceZip.Entries | ForEach-Object { [string]$_.FullName })
} finally {
  $serviceZip.Dispose()
}
Check 'archive.fixture_posix_entries' (
  @($serviceEntryNames | Where-Object { $_.Contains('\') }).Count -eq 0)
$expanded = Join-Path $root 'expanded'
Expand-ArchiveSafely $serviceArchive $expanded
$inventoryRefusal = Invoke-Refusal { Assert-ArchiveInventory $expanded }
Check 'archive.inventory_valid' (-not $inventoryRefusal.threw) $inventoryRefusal.message
Add-Content -LiteralPath (Join-Path $expanded 'README.md') -Value 'tampered'
Check 'archive.inventory_tamper_refused' (
  (Invoke-Refusal { Assert-ArchiveInventory $expanded }).code -eq 'archive_inventory_mismatch')

$unsafeArchive = Join-Path $root 'unsafe.zip'
Add-Type -AssemblyName System.IO.Compression
$zip = [System.IO.Compression.ZipFile]::Open(
  $unsafeArchive, [System.IO.Compression.ZipArchiveMode]::Create)
try {
  $entry = $zip.CreateEntry('../escaped.txt')
  $writer = New-Object System.IO.StreamWriter($entry.Open())
  try { $writer.Write('escape') } finally { $writer.Dispose() }
} finally {
  $zip.Dispose()
}
$unsafeDestination = Join-Path $root 'unsafe-expanded'
Check 'archive.traversal_refused' (
  (Invoke-Refusal { Expand-ArchiveSafely $unsafeArchive $unsafeDestination }).code -eq
    'archive_member_unsafe')
Check 'archive.traversal_wrote_nothing' (-not (Test-Path -LiteralPath (Join-Path $root 'escaped.txt')))

# ---- listener process sessions ---------------------------------------------
Check 'listener.session1_interactive_lineage' (
  Test-EndpointSessionIdentity 0 1 1)
Check 'listener.session0_launcher_refused' (
  -not (Test-EndpointSessionIdentity 0 0 0))
Check 'listener.session0_listener_refused' (
  -not (Test-EndpointSessionIdentity 0 1 0))
Check 'listener.session_mismatch_refused' (
  -not (Test-EndpointSessionIdentity 0 1 2))
Check 'listener.scheduler_session_nonzero_refused' (
  -not (Test-EndpointSessionIdentity 1 1 1))
$schedulerStart = '2026-10-27T01:00:00.0000000Z'
$launcherStart = '2026-10-27T01:00:01.0000000Z'
$listenerStart = '2026-10-27T01:00:02.0000000Z'
Check 'listener.creation_order_valid' (
  Test-ProcessCreationOrder $schedulerStart $launcherStart $listenerStart)
Check 'listener.scheduler_after_launcher_refused' (
  -not (Test-ProcessCreationOrder `
    '2026-10-27T01:00:02.0000000Z' $launcherStart $listenerStart))
Check 'listener.launcher_after_listener_refused' (
  -not (Test-ProcessCreationOrder $schedulerStart $listenerStart $launcherStart))
Check 'listener.creation_order_invalid_time_refused' (
  -not (Test-ProcessCreationOrder $schedulerStart 'unknown' $listenerStart))
Check 'listener.creation_order_missing_zone_refused' (
  -not (Test-ProcessCreationOrder `
    '2026-10-27T01:00:00' $launcherStart $listenerStart))
$listenerEvidenceStart = $source.IndexOf('function Get-ListenerEvidence')
$listenerEvidenceEnd = $source.IndexOf('function Read-EndpointHealth', $listenerEvidenceStart)
$listenerEvidenceSource = if (
    $listenerEvidenceStart -ge 0 -and
    $listenerEvidenceEnd -gt $listenerEvidenceStart) {
  $source.Substring(
    $listenerEvidenceStart, $listenerEvidenceEnd - $listenerEvidenceStart)
} else {
  ''
}
Check 'listener.creation_order_required_by_identity' (
  $listenerEvidenceSource.Contains('Test-ProcessCreationOrder') -and
  $listenerEvidenceSource.Contains('$scheduler.start_utc') -and
  $listenerEvidenceSource.Contains('$launcher.start_utc') -and
  $listenerEvidenceSource.Contains('$listener.start_utc'))

# ---- drain is a read-only three-way safety gate ----------------------------
$safeHealth = [pscustomobject]@{
  jobs=[pscustomobject]@{active=0; queued=0; unresolved=0}
  cad=[pscustomobject]@{
    external_session_detected=$false; owned_sessions=0; lease_held=$false}
}
$safeState = [pscustomobject]@{projection=[pscustomobject]@{state='Running'}}
$safeRunning = [pscustomobject]@{
  health=$safeHealth
  listener=[pscustomobject]@{listener_pid=4242}
}
function Get-SolidWorksProcesses { return @() }
$drain = Assert-DrainSafety $safeState $safeRunning
Check 'drain.idle_passes' (
  $drain.jobs.active -eq 0 -and $drain.cad.solidworks_processes -eq 0)
$busy = $safeHealth | ConvertTo-Json -Depth 5 | ConvertFrom-Json
$busy.jobs.queued = 1
Check 'drain.jobs_refused' (
  (Invoke-Refusal {
    Assert-DrainSafety $safeState ([pscustomobject]@{
      health=$busy; listener=$safeRunning.listener})
  }).code -eq 'drain_jobs_present')
$leased = $safeHealth | ConvertTo-Json -Depth 5 | ConvertFrom-Json
$leased.cad.lease_held = $true
Check 'drain.lease_refused' (
  (Invoke-Refusal {
    Assert-DrainSafety $safeState ([pscustomobject]@{
      health=$leased; listener=$safeRunning.listener})
  }).code -eq 'drain_cad_not_idle')

# ---- install transaction: Disabled task, no start, no CAD -------------------
$installRoot = Join-Path $root 'install'
$null = New-Item -ItemType Directory -Path $installRoot -Force
$installToken = Join-Path $installRoot 'endpoint.token'
[System.IO.File]::WriteAllText($installToken, ('u' * 40))
$installConfigPath = Join-Path $installRoot 'endpoint.json'
Write-TestConfig $installConfigPath $installRoot $installToken
$installReleasePath = Join-Path $installRoot 'release.json'
$installArchive = Join-Path $installRoot $archiveName
[System.IO.File]::WriteAllText($installArchive, 'install-archive')
Write-TestRelease $installReleasePath $archiveName ((Get-FileHash $installArchive -Algorithm SHA256).Hash.ToLowerInvariant())
$null = New-Item -ItemType Directory -Path (Join-Path $installRoot 'state') -Force
$installBindingSeed = New-TestBindingPath $installRoot $installConfigPath $installReleasePath $python
$installBindingPath = Join-Path $installRoot 'state/windows-binding.json'
Copy-Item -LiteralPath $installBindingSeed -Destination $installBindingPath
$installBinding = Read-Binding $installBindingPath
$script:FixtureTask = $null
$script:StartCalls = 0
$script:SolidWorksOpenCalls = 0
$script:FixtureServiceSid = 'S-1-5-21-1-2-3-500'
$script:FailTaskObserverAfterRemove = $false
$script:TaskObserverFailed = $false

function Read-ReleaseManifest($IgnoredBinding) {
  return [ordered]@{
    path=Normalize-PathIdentity $installReleasePath
    sha256=Sha256 $installReleasePath
    archive_name=$archiveName
    archive_path=Normalize-PathIdentity $installArchive
    archive_sha256=Sha256 $installArchive
    version='1.5.0'
    source_commit=('a' * 40)
    source_tree=('b' * 40)
    source_sha256=('c' * 64)
  }
}
function Read-EndpointConfigStrict($IgnoredBinding) {
  return [ordered]@{
    schema_version='solidworks-to-urdf.endpoint/v1'
    package_root=Normalize-PathIdentity (Join-Path $installRoot 'packages')
    output_root=Normalize-PathIdentity (Join-Path $installRoot 'results')
    state_root=Normalize-PathIdentity (Join-Path $installRoot 'state')
    token_file=Normalize-PathIdentity $installToken
    host='127.0.0.1'
    port=8765
    source_path=Normalize-PathIdentity $installConfigPath
    source_sha256=Sha256 $installConfigPath
  }
}
function Get-BasePythonIdentity($IgnoredBinding) { return [ordered]@{version='3.12.10'} }
function Get-ManagedCommonParent($IgnoredConfig, $IgnoredBinding) { return $installRoot }
function Invoke-ManagedRootBootstrap([string]$ManagedRoot, [string]$ServiceAccount) {
  [System.IO.File]::WriteAllText(
    (Join-Path $ManagedRoot '.description-managed-root.json'),
    '{"schema_version":"solidworks-to-urdf.managed-root/v1"}')
  return [pscustomobject]@{passed=$true; service_sid=$script:FixtureServiceSid}
}
function Resolve-Sid([string]$Identity) { return $script:FixtureServiceSid }
function Get-OperatorSid { return $script:FixtureServiceSid }
function Expand-ArchiveSafely([string]$Archive, [string]$Destination) {
  $null = New-Item -ItemType Directory -Path $Destination
  $null = New-Item -ItemType Directory -Path (Join-Path $Destination 'wheels')
  [System.IO.File]::WriteAllText((Join-Path $Destination 'requirements.lock'), 'fixture')
  [System.IO.File]::WriteAllText((Join-Path $Destination 'files.json'), '{}')
}
function Assert-ArchiveInventory([string]$Root) { }
function New-EndpointVirtualEnvironment([string]$BasePython, [string]$Generation) {
  $scripts = Join-Path $Generation 'runtime/Scripts'
  $null = New-Item -ItemType Directory -Path $scripts -Force
  [System.IO.File]::WriteAllText((Join-Path $scripts 'python.exe'), 'fixture-python')
}
function Install-EndpointRequirements([string]$Generation) { }
function Get-RuntimeDoctor([string]$Generation) {
  return [pscustomobject]@{
    tool=[pscustomobject]@{
      version='1.5.0'
      source_sha256=('c' * 64)
      release=[pscustomobject]@{source_commit=('a' * 40); source_sha256=('c' * 64)}
      runtime=[pscustomobject]@{
        role='native'; system='Windows'; python='3.12.10'; packages=@{packaging='26.3'}}
    }
    doctor=[pscustomobject]@{passed=$true}
  }
}
function Read-EndpointConfigOfficial([string]$Generation) {
  return [pscustomobject]@{
    config_digest=('d' * 64)
    state_root=(Join-Path $installRoot 'state')
    package_root=(Join-Path $installRoot 'packages')
    output_root=(Join-Path $installRoot 'results')
    token_file=$installToken
    port=8765
  }
}
$script:ProtectedAclSetPaths = New-Object 'System.Collections.Generic.List[string]'
$script:ProtectedAclAssertPaths = New-Object 'System.Collections.Generic.List[string]'
$script:ProtectedAclEvents = New-Object 'System.Collections.Generic.List[System.Object]'
function Add-ProtectedAclObservation([string]$Kind, [string]$Path) {
  $identity = Normalize-PathIdentity ([System.IO.Path]::GetFullPath($Path))
  $script:ProtectedAclEvents.Add([pscustomobject]@{kind=$Kind; path=$identity})
  if ($Kind -ceq 'set') {
    $script:ProtectedAclSetPaths.Add($identity)
  } else {
    $script:ProtectedAclAssertPaths.Add($identity)
  }
}
function Set-ProtectedAcl([string]$Path, [string]$ServiceSid) {
  Add-ProtectedAclObservation 'set' $Path
}
function Assert-ProtectedAcl([string]$Path, [string]$ServiceSid) {
  Add-ProtectedAclObservation 'assert' $Path
}
function Get-ScheduledTask {
  [CmdletBinding()]
  param()
  if ($script:TaskObserverFailed) {
    Write-Error 'injected non-terminating task observer failure'
    return
  }
  if ($null -eq $script:FixtureTask) { return @() }
  return @($script:FixtureTask)
}
function Register-DisabledTask($IgnoredBinding, [string]$Generation) {
  $script:FixtureTask = [pscustomobject]@{
    TaskName=$installBinding.task_name
    TaskPath=$installBinding.task_path
    State='Disabled'
  }
}
function Start-ScheduledTask { [CmdletBinding()] param([string]$TaskName, [string]$TaskPath)
  $script:StartCalls++
  if ($null -ne $script:FixtureTask) { $script:FixtureTask.State = 'Running' }
}
function Get-TaskFilePath($Task) { return (Join-Path $installRoot 'task.xml') }
function Set-TaskFileAcl([string]$TaskFile, [string]$ServiceSid) {
  [System.IO.File]::WriteAllText($TaskFile, '<Task/>')
}
function Get-TaskProjection($Task) {
  $generation = Join-Path $installRoot 'state/lifecycle/generation'
  return [ordered]@{
    task_name=$installBinding.task_name
    task_path=$installBinding.task_path
    state='Disabled'
    xml='<Task/>'
    xml_sha256=('1' * 64)
    semantic_sha256=('2' * 64)
    security_descriptor_sha256=('3' * 64)
    triggers_sha256=('4' * 64)
    principal=[ordered]@{
      user=$installBinding.service_account
      sid=$script:FixtureServiceSid
      logon_type='Interactive'
      run_level='Highest'
    }
    action=[ordered]@{
      execute=Normalize-PathIdentity (Runtime-Python $generation)
      arguments=Runtime-LauncherArguments $generation
      working_directory=Normalize-PathIdentity (Join-Path $installRoot 'state')
    }
    settings=[ordered]@{multiple_instances='IgnoreNew'}
  }
}
function Get-DomainPins([string]$StateRoot, [string]$EndpointRoot) {
  return [ordered]@{
    DESCRIPTION_ENDPOINT_ROOT=$EndpointRoot
    DESCRIPTION_CAD_OWNERSHIP_FILE=(Join-Path $StateRoot 'cad-ownership.json')
  }
}
function Assert-DomainPins($Pins) { }
function Set-DomainPins($Pins) { return @{'previous'=$true} }
function Restore-DomainPins($Pins) { }
function Remove-ManagedTask($IgnoredBinding) {
  $script:FixtureTask=$null
  if ($script:FailTaskObserverAfterRemove) {
    $script:TaskObserverFailed = $true
  }
}
function Invoke-RestMethod {
  param([string]$Uri, [hashtable]$Headers, [int]$TimeoutSec)
  throw 'install must not probe health'
}

$installResult = Invoke-Install $installBinding
$installReceipt = Read-InstallReceipt (
  Join-Path $installRoot 'state/install-receipt.json')
Check 'install.result_disabled' (
  $installResult.passed -eq $true -and
  $installResult.verb -eq 'install' -and
  $installResult.task.state -eq 'Disabled' -and
  $null -eq $installResult.listener -and
  $null -eq $installResult.health)
Check 'install.no_start_or_cad' (
  $script:StartCalls -eq 0 -and $script:SolidWorksOpenCalls -eq 0)
Check 'install.identity_from_real_config' (
  $installResult.identity.install_id -match '^win-[0-9a-f]{32}$' -and
  $installResult.identity.config_digest -eq ('d' * 64) -and
  $installReceipt.config_digest -eq ('d' * 64))
Check 'install.generation_and_receipt' (
  (Test-Path -LiteralPath (
    Join-Path $installRoot 'state/lifecycle/generation/endpoint.json') -PathType Leaf) -and
  (Test-Path -LiteralPath (
    Join-Path $installRoot 'state/install-receipt.json') -PathType Leaf))
$fixedBindingPath = Join-Path $installRoot 'state/windows-binding.json'
Check 'install.fixed_control_files' (
  (Test-Path -LiteralPath $fixedBindingPath -PathType Leaf) -and
  (Test-Path -LiteralPath (
    Join-Path $installRoot 'state/instance-lifecycle.ps1') -PathType Leaf) -and
  (Test-Path -LiteralPath (
    Join-Path $installRoot 'state/bootstrap-managed-root.ps1') -PathType Leaf) -and
  $installResult.control.binding -eq (Normalize-PathIdentity $fixedBindingPath))
Check 'install.control_bytes_exact' (
  [Convert]::ToBase64String([System.IO.File]::ReadAllBytes($fixedBindingPath)) -ceq
    [Convert]::ToBase64String([System.IO.File]::ReadAllBytes($installBindingSeed)) -and
  [Convert]::ToBase64String([System.IO.File]::ReadAllBytes(
    (Join-Path $installRoot 'state/instance-lifecycle.ps1'))) -ceq
    [Convert]::ToBase64String([System.IO.File]::ReadAllBytes($ScriptPath)) -and
  [Convert]::ToBase64String([System.IO.File]::ReadAllBytes(
    (Join-Path $installRoot 'state/bootstrap-managed-root.ps1'))) -ceq
    [Convert]::ToBase64String([System.IO.File]::ReadAllBytes($bootstrapPath)))
$installGeneration = Join-Path $installRoot 'state/lifecycle/generation'
$installReceiptPath = Join-Path $installRoot 'state/install-receipt.json'
$installFixtureConfig = Read-EndpointConfigStrict $installBinding
$stableManagedPaths = @(Get-StableManagedPaths `
  $installFixtureConfig $installBinding $installGeneration $installReceiptPath)
$requiredStablePaths = @(
  $installRoot,
  (Join-Path $installRoot '.description-managed-root.json'),
  (Join-Path $installRoot 'packages'),
  (Join-Path $installRoot 'results'),
  (Join-Path $installRoot 'state'),
  (Join-Path $installRoot 'state/lifecycle'),
  $installGeneration,
  (Join-Path $installGeneration 'endpoint.json'),
  $installConfigPath,
  $installToken,
  (Join-Path $installRoot 'state/install-id'),
  $installReceiptPath,
  (Join-Path $installRoot 'state/windows-binding.json'),
  (Join-Path $installRoot 'state/instance-lifecycle.ps1'),
  (Join-Path $installRoot 'state/bootstrap-managed-root.ps1')
) | ForEach-Object { Normalize-PathIdentity $_ }
$missingStablePaths = @(
  $requiredStablePaths | Where-Object { $_ -cnotin $stableManagedPaths })
Check 'acl.stable_path_roles' (
  $missingStablePaths.Count -eq 0) ($missingStablePaths -join ', ')
$stableManagedPathKeys = @($stableManagedPaths | ForEach-Object { $_.ToLowerInvariant() })
Check 'acl.stable_paths_case_insensitive_unique' (
  @($stableManagedPathKeys | Sort-Object -Unique).Count -eq
    $stableManagedPaths.Count)
$receiptIdentity = Normalize-PathIdentity $installReceiptPath
$installAclSets = @($script:ProtectedAclSetPaths)
$installAclAsserts = @($script:ProtectedAclAssertPaths)
$missingSetPaths = @(
  $stableManagedPaths |
    Where-Object { $_ -cne $receiptIdentity -and $_ -cnotin $installAclSets })
$missingAssertPaths = @(
  $stableManagedPaths | Where-Object { $_ -cnotin $installAclAsserts })
Check 'acl.install_sets_stable_paths' (
  $missingSetPaths.Count -eq 0) ($missingSetPaths -join ', ')
Check 'acl.install_asserts_stable_paths' (
  $missingAssertPaths.Count -eq 0) ($missingAssertPaths -join ', ')
$receiptPartialSet = @($script:ProtectedAclEvents | Where-Object {
  $_.kind -ceq 'set' -and
  $_.path -match '/\.install-receipt\.json\.[0-9a-f]+\.partial$'})
$receiptPartialAssert = @($script:ProtectedAclEvents | Where-Object {
  $_.kind -ceq 'assert' -and
  $_.path -match '/\.install-receipt\.json\.[0-9a-f]+\.partial$'})
$receiptFinalAssert = @($script:ProtectedAclEvents | Where-Object {
  $_.kind -ceq 'assert' -and $_.path -ceq $receiptIdentity})
Check 'acl.receipt_protected_before_atomic_commit' (
  $receiptPartialSet.Count -gt 0 -and
  $receiptPartialAssert.Count -gt 0 -and
  $receiptFinalAssert.Count -gt 0)
$stateIdentity = Normalize-PathIdentity (Join-Path $installRoot 'state')
$mutableAclPaths = @(
  (Join-Path $installRoot 'state/cad-ownership.json'),
  (Join-Path $installRoot 'state/cad-ownership.json.lease')
) | ForEach-Object { (Normalize-PathIdentity $_).ToLowerInvariant() }
$mutableAclViolations = @($stableManagedPaths | Where-Object {
  $identity = $_.ToLowerInvariant()
  $identity -in $mutableAclPaths -or
    $identity.StartsWith(
      (($stateIdentity + '/native-operations/').ToLowerInvariant()),
      [System.StringComparison]::Ordinal)
})
Check 'acl.mutable_runtime_paths_excluded' (
  $mutableAclViolations.Count -eq 0) ($mutableAclViolations -join ', ')
$fixedBinding = Read-Binding $fixedBindingPath
$script:LifecycleScriptPath = Join-Path $installRoot 'state/instance-lifecycle.ps1'

function Get-ListenerConnections([int]$Port) { return @() }
$script:ProtectedAclSetPaths.Clear()
$script:ProtectedAclAssertPaths.Clear()
$script:ProtectedAclEvents.Clear()
$statusResult = Invoke-Status $fixedBinding
$statusAclAsserts = @($script:ProtectedAclAssertPaths)
$statusMissingAclPaths = @(
  $stableManagedPaths | Where-Object { $_ -cnotin $statusAclAsserts })
Check 'status.acl_asserts_stable_paths_read_only' (
  $script:ProtectedAclSetPaths.Count -eq 0 -and
  $statusMissingAclPaths.Count -eq 0) ($statusMissingAclPaths -join ', ')
Check 'status.disabled_is_read_only_and_consistent' (
  $statusResult.passed -eq $true -and
  $statusResult.task.state -eq 'Disabled' -and
  $statusResult.status.consistent -eq $true -and
  $null -eq $statusResult.listener -and
  $null -eq $statusResult.health) (
    $statusResult.status.error | ConvertTo-Json -Depth 5 -Compress)

# ---- start/health/stop orchestration ---------------------------------------
$fakeState = [pscustomobject]@{
  task=$script:FixtureTask
  projection=(Get-TaskProjection $script:FixtureTask)
  release=[ordered]@{
    version='1.5.0'; source_commit=('a' * 40); source_sha256=('c' * 64); role='native'}
  release_manifest=[ordered]@{}
  source_config=[ordered]@{
    state_root=(Join-Path $installRoot 'state')
    package_root=(Join-Path $installRoot 'packages')
    output_root=(Join-Path $installRoot 'results')
    token_file=$installToken
  }
  config=[ordered]@{config_digest=('d' * 64)}
  generation=Normalize-PathIdentity (Join-Path $installRoot 'state/lifecycle/generation')
  config_path=Normalize-PathIdentity (Join-Path $installRoot 'state/lifecycle/generation/endpoint.json')
  config_sha256=(Sha256 (Join-Path $installRoot 'state/lifecycle/generation/endpoint.json'))
  install_id=('win-' + ('e' * 32))
  install_id_path=Normalize-PathIdentity (Join-Path $installRoot 'state/install-id')
  receipt=$installReceipt
  receipt_path=Normalize-PathIdentity (Join-Path $installRoot 'state/install-receipt.json')
  token_sha256=(Sha256 $installToken)
  port=8765
  pins=[ordered]@{}
}
$fakeIdentity = [ordered]@{
  install_id=$fakeState.install_id
  endpoint_root=$fakeState.generation
  config_path=$fakeState.config_path
  config_sha256=$fakeState.config_sha256
  config_digest=$fakeState.config.config_digest
  package_root=$fakeState.source_config.package_root
  output_root=$fakeState.source_config.output_root
  state_root=$fakeState.source_config.state_root
  token_path=$fakeState.source_config.token_file
  token_sha256=$fakeState.token_sha256
  port=8765
  release=$fakeState.release
}
$fakeHealth = [pscustomobject]@{
  schema_version='solidworks-to-urdf.health/v1'
  pipeline_id='solidworks-to-urdf'
  native_platform=$true
  ready=$true
  release=[pscustomobject]@{
    version='1.5.0'; source_commit=('a' * 40); source_sha256=('c' * 64); role='native'}
  install_id=$fakeState.install_id
  config_digest=('d' * 64)
  target_identity=[pscustomobject]@{
    install_id=$fakeState.install_id; pipeline_id='solidworks-to-urdf'}
  endpoint=[pscustomobject]@{port=8765}
  cad=[pscustomobject]@{
    external_session_detected=$false; owned_sessions=0; lease_held=$false}
  jobs=[pscustomobject]@{active=0; queued=0; unresolved=0}
}
$fakeRunning = [pscustomobject]@{
  identity=$fakeIdentity
  listener=[pscustomobject]@{listener_pid=4242}
  health=$fakeHealth
}
function Get-InstalledState($IgnoredBinding, [switch]$SkipDoctor) { return $fakeState }
function Get-RunningEvidence($State, [switch]$AllowUnready) { return $fakeRunning }
function Enable-ScheduledTask {
  [CmdletBinding()] param([string]$TaskName, [string]$TaskPath)
  $script:FixtureTask.State = 'Ready'
}
function Stop-ScheduledTask {
  [CmdletBinding()] param([string]$TaskName, [string]$TaskPath)
  if ($null -ne $script:FixtureTask -and $script:FixtureTask.State -ne 'Disabled') {
    $script:FixtureTask.State = 'Ready'
  }
}
function Disable-ScheduledTask {
  [CmdletBinding()] param([string]$TaskName, [string]$TaskPath)
  $script:FixtureTask.State = 'Disabled'
}
function Get-WriterResidue($State) { return @() }
function Get-TaskProjection($Task) {
  $generation = Join-Path $installRoot 'state/lifecycle/generation'
  return [ordered]@{
    task_name=$installBinding.task_name
    task_path=$installBinding.task_path
    state=[string]$Task.State
    xml='<Task/>'
    xml_sha256=('1' * 64)
    semantic_sha256=('2' * 64)
    security_descriptor_sha256=('3' * 64)
    triggers_sha256=('4' * 64)
    principal=[ordered]@{
      user=$installBinding.service_account
      sid=$script:FixtureServiceSid
      logon_type='Interactive'
      run_level='Highest'
    }
    action=[ordered]@{
      execute=Normalize-PathIdentity (Runtime-Python $generation)
      arguments=Runtime-LauncherArguments $generation
      working_directory=Normalize-PathIdentity (Join-Path $installRoot 'state')
    }
    settings=[ordered]@{multiple_instances='IgnoreNew'}
  }
}

$startResult = Invoke-Start $fixedBinding
Check 'start.real_health_identity_required' (
  $startResult.passed -eq $true -and
  $startResult.verb -eq 'start' -and
  $startResult.task.state -eq 'Running' -and
  $startResult.identity.install_id -eq $fakeState.install_id -and
  $startResult.health.ready -eq $true)
$healthResult = Invoke-Health $fixedBinding
Check 'health.running_identity' (
  $healthResult.passed -eq $true -and
  $healthResult.task.state -eq 'Running' -and
  $healthResult.listener.listener_pid -eq 4242)
$stopResult = Invoke-Stop $fixedBinding
Check 'stop.drains_then_proves_exit' (
  $stopResult.passed -eq $true -and
  $stopResult.task.state -eq 'Disabled' -and
  $null -eq $stopResult.listener -and
  $stopResult.safety.jobs.active -eq 0 -and
  $stopResult.stopped.listener_count -eq 0)

# ---- install cleanup reports attempted work, not assumed success -----------
$savedTaskName = [string]$script:FixtureTask.TaskName
$savedTaskPath = [string]$script:FixtureTask.TaskPath
$script:FixtureTask.TaskName = $savedTaskName + '-foreign'
$taskNameMismatch = @(Get-BoundTask $installBinding).Count
$script:FixtureTask.TaskName = $savedTaskName
$script:FixtureTask.TaskPath = '\Foreign\'
$taskPathMismatch = @(Get-BoundTask $installBinding).Count
$script:FixtureTask.TaskPath = $savedTaskPath
Check 'task_observer.exact_name_and_path_filter' (
  $taskNameMismatch -eq 0 -and
  $taskPathMismatch -eq 0 -and
  @(Get-BoundTask $installBinding).Count -eq 1)

$script:TaskObserverFailed = $true
$observerProbe = Invoke-Refusal { Get-BoundTask $installBinding }
$script:TaskObserverFailed = $false
Check 'task_observer.nonterminating_error_is_not_empty' (
  $observerProbe.threw -and
  $observerProbe.message -match 'injected non-terminating task observer failure')

$cleanupRoot = Join-Path $root 'cleanup-failure'
$null = New-Item -ItemType Directory -Path (Join-Path $cleanupRoot 'state') -Force
$cleanupToken = Join-Path $cleanupRoot 'endpoint.token'
[System.IO.File]::WriteAllText($cleanupToken, ('v' * 40))
$cleanupConfig = Join-Path $cleanupRoot 'endpoint.json'
Write-TestConfig $cleanupConfig $cleanupRoot $cleanupToken
$cleanupArchive = Join-Path $cleanupRoot $archiveName
[System.IO.File]::WriteAllText($cleanupArchive, 'cleanup-archive')
$cleanupRelease = Join-Path $cleanupRoot 'release.json'
Write-TestRelease $cleanupRelease $archiveName (
  (Get-FileHash $cleanupArchive -Algorithm SHA256).Hash.ToLowerInvariant())
$cleanupSeed = New-TestBindingPath $cleanupRoot $cleanupConfig $cleanupRelease $python
$cleanupBindingPath = Join-Path $cleanupRoot 'state/windows-binding.json'
Copy-Item -LiteralPath $cleanupSeed -Destination $cleanupBindingPath
$installRoot = $cleanupRoot
$installToken = $cleanupToken
$installConfigPath = $cleanupConfig
$installArchive = $cleanupArchive
$installReleasePath = $cleanupRelease
$installBinding = Read-Binding $cleanupBindingPath
$script:FixtureTask = $null
$failedGeneration = Join-Path $cleanupRoot 'state/lifecycle/generation'
$script:FailCleanupGeneration = Normalize-PathIdentity $failedGeneration

function Remove-Item {
  [CmdletBinding()]
  param(
    [string]$LiteralPath,
    [switch]$Recurse,
    [switch]$Force
  )
  if ((Normalize-PathIdentity $LiteralPath) -eq $script:FailCleanupGeneration) {
    throw 'injected cleanup removal failure'
  }
  Microsoft.PowerShell.Management\Remove-Item @PSBoundParameters
}
$originalWriteJsonAtomic = ${function:Write-JsonAtomic}
function Write-JsonAtomic([string]$Path, $Document, [string]$ServiceSid = '') {
  throw 'injected receipt commit failure'
}
$cleanupDetail = $null
try {
  $null = Invoke-Install $installBinding
} catch {
  $cleanupDetail = $_.Exception.Data['detail']
}
Check 'install.cleanup_failure_reported' (
  $null -ne $cleanupDetail -and
  $cleanupDetail.cleanup.passed -eq $false -and
  $cleanupDetail.cleanup.steps.generation.attempted -eq $true -and
  $cleanupDetail.cleanup.steps.generation.passed -eq $false -and
  $cleanupDetail.cleanup.steps.generation.remaining -eq $true -and
  @($cleanupDetail.cleanup.errors).Count -gt 0 -and
  (Test-Path -LiteralPath $failedGeneration -PathType Container) -and
  $null -eq $script:FixtureTask)
Check 'install.cleanup_confirmed_empty_allows_dependent_cleanup' (
  $null -ne $cleanupDetail -and
  $cleanupDetail.cleanup.steps.task.observed -eq $true -and
  $cleanupDetail.cleanup.steps.task.remaining -eq $false -and
  $cleanupDetail.cleanup.steps.generation.attempted -eq $true)
Check 'install.cleanup_preserves_primary_failure' (
  $null -ne $cleanupDetail -and
  $cleanupDetail.phase -eq 'install-control')

Remove-Item Function:\Remove-Item -ErrorAction SilentlyContinue
Microsoft.PowerShell.Management\Remove-Item -LiteralPath $cleanupRoot -Recurse -Force

$observerRoot = Join-Path $root 'cleanup-observer-failure'
$null = New-Item -ItemType Directory -Path (Join-Path $observerRoot 'state') -Force
$observerToken = Join-Path $observerRoot 'endpoint.token'
[System.IO.File]::WriteAllText($observerToken, ('w' * 40))
$observerConfig = Join-Path $observerRoot 'endpoint.json'
Write-TestConfig $observerConfig $observerRoot $observerToken
$observerArchive = Join-Path $observerRoot $archiveName
[System.IO.File]::WriteAllText($observerArchive, 'observer-archive')
$observerRelease = Join-Path $observerRoot 'release.json'
Write-TestRelease $observerRelease $archiveName (
  (Get-FileHash $observerArchive -Algorithm SHA256).Hash.ToLowerInvariant())
$observerSeed = New-TestBindingPath $observerRoot $observerConfig $observerRelease $python
$observerBindingPath = Join-Path $observerRoot 'state/windows-binding.json'
Copy-Item -LiteralPath $observerSeed -Destination $observerBindingPath
$installRoot = $observerRoot
$installToken = $observerToken
$installConfigPath = $observerConfig
$installArchive = $observerArchive
$installReleasePath = $observerRelease
$installBinding = Read-Binding $observerBindingPath
$script:FixtureTask = $null
$script:TaskObserverFailed = $false
$script:FailTaskObserverAfterRemove = $true
function Write-JsonAtomic([string]$Path, $Document, [string]$ServiceSid = '') {
  throw 'injected receipt commit failure'
}
$observerDetail = $null
try {
  $null = Invoke-Install $installBinding
} catch {
  $observerDetail = $_.Exception.Data['detail']
}
$observerGeneration = Join-Path $observerRoot 'state/lifecycle/generation'
$observerInstallId = Join-Path $observerRoot 'state/install-id'
Check 'install.cleanup_observer_failure_preserves_unknown' (
  $null -ne $observerDetail -and
  $observerDetail.cleanup.passed -eq $false -and
  $observerDetail.cleanup.steps.task.observed -eq $false -and
  $null -eq $observerDetail.cleanup.steps.task.remaining -and
  $observerDetail.cleanup.steps.generation.attempted -eq $false -and
  $observerDetail.cleanup.steps.generation.remaining -eq $true -and
  $observerDetail.cleanup.steps.install_id.attempted -eq $false -and
  $observerDetail.cleanup.steps.install_id.remaining -eq $true -and
  (Test-Path -LiteralPath $observerGeneration -PathType Container) -and
  (Test-Path -LiteralPath $observerInstallId -PathType Leaf) -and
  (Test-Path -LiteralPath (
    Join-Path $observerRoot 'state/instance-lifecycle.ps1') -PathType Leaf))

Set-Item Function:\Write-JsonAtomic -Value $originalWriteJsonAtomic
$script:FailTaskObserverAfterRemove = $false
$script:TaskObserverFailed = $false
Microsoft.PowerShell.Management\Remove-Item -LiteralPath $observerRoot -Recurse -Force

# ---- one-line error envelope and exit classes -------------------------------
$pwshPath = [System.Diagnostics.Process]::GetCurrentProcess().MainModule.FileName
$invalidOutput = @(& $pwshPath -NoProfile -File $ScriptPath -Verb invalid -Binding 'unused')
$invalidExit = $LASTEXITCODE
$invalidJson = $invalidOutput | Select-Object -Last 1 | ConvertFrom-Json
Check 'cli.invalid_verb_one_json' (
  $invalidExit -eq 1 -and
  @($invalidOutput).Count -eq 1 -and
  $invalidJson.passed -eq $false -and
  $invalidJson.code -eq 'verb_unsupported' -and
  $invalidJson.verb -eq 'unknown')
$missingOutput = @(& $pwshPath -NoProfile -File $ScriptPath)
$missingExit = $LASTEXITCODE
$missingJson = $missingOutput | Select-Object -Last 1 | ConvertFrom-Json
Check 'cli.missing_verb_one_json' (
  $missingExit -eq 1 -and
  @($missingOutput).Count -eq 1 -and
  $missingJson.code -eq 'verb_required')

Remove-Item -LiteralPath $root -Recurse -Force -ErrorAction SilentlyContinue
Write-Host ('checks={0} failures={1}' -f $script:checks, $script:failures.Count)
exit ([int]($script:failures.Count -gt 0))
