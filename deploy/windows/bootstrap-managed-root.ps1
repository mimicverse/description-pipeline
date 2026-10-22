param(
  [Parameter(Mandatory=$true)][string]$ManagedRoot,
  [Parameter(Mandatory=$true)][string]$ServiceAccount,
  [string]$PostflightScript = ''
)
$ErrorActionPreference='Stop'

function Emit($payload) {
  $payload | ConvertTo-Json -Depth 12 -Compress
}
function Fail([string]$code, [string]$message) {
  Emit @{passed=$false; code=$code; message=$message}
  exit 1
}
function Normalize([string]$path) {
  return ([System.IO.Path]::GetFullPath($path)).Replace('\', '/').TrimEnd('/')
}
function Within([string]$child, [string]$parent) {
  $c = Normalize $child
  $p = Normalize $parent
  return $c -eq $p -or $c.StartsWith(
    $p + '/',
    [System.StringComparison]::OrdinalIgnoreCase)
}
function Assert-NoReparseAncestor([string]$path) {
  $current = [System.IO.Path]::GetFullPath($path)
  while ($current) {
    if (Test-Path -LiteralPath $current) {
      $item = Get-Item -LiteralPath $current -Force
      if ($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) {
        Fail 'managed_root_reparse_refused' ('path crosses a reparse point: ' + $current)
      }
    }
    $parent = [System.IO.Path]::GetDirectoryName($current)
    if (-not $parent -or $parent -eq $current) { break }
    $current = $parent
  }
}
function Resolve-Sid([string]$identity) {
  try {
    return ([System.Security.Principal.SecurityIdentifier]$identity).Value
  } catch {
    return (New-Object System.Security.Principal.NTAccount($identity)).Translate(
      [System.Security.Principal.SecurityIdentifier]).Value
  }
}
function New-Rule(
  [string]$sid,
  [System.Security.AccessControl.FileSystemRights]$rights,
  [System.Security.AccessControl.InheritanceFlags]$inheritance) {
  $sidObject = New-Object System.Security.Principal.SecurityIdentifier -ArgumentList $sid
  return New-Object System.Security.AccessControl.FileSystemAccessRule -ArgumentList @(
    $sidObject,
    $rights,
    $inheritance,
    [System.Security.AccessControl.PropagationFlags]::None,
    [System.Security.AccessControl.AccessControlType]::Allow)
}
function Get-AclProjection([string]$path) {
  $acl = Get-Acl -LiteralPath $path
  $rules = @($acl.GetAccessRules($true, $true, [System.Security.Principal.SecurityIdentifier]) |
    ForEach-Object {
      [ordered]@{
        sid=[string]$_.IdentityReference
        access_type=[string]$_.AccessControlType
        rights=[long]$_.FileSystemRights
        inherited=[bool]$_.IsInherited
        inheritance=[int]$_.InheritanceFlags
        propagation=[int]$_.PropagationFlags
      }
    } | Sort-Object -Property @(
        { $_.sid },
        { $_.rights },
        { $_.access_type },
        { $_.inheritance },
        { $_.propagation },
        { $_.inherited }
      ))
  return [ordered]@{
    owner_sid=Resolve-Sid ([string]$acl.Owner)
    group_sid=Resolve-Sid ([string]$acl.Group)
    protected=[bool]$acl.AreAccessRulesProtected
    sddl=[string]$acl.GetSecurityDescriptorSddlForm(
      [System.Security.AccessControl.AccessControlSections]::All)
    rules=$rules
  }
}
function Get-ManagedTreeSnapshot(
  [string]$root,
  [string]$serviceSid,
  [System.Collections.Generic.List[object]]$rows) {
  $pending = New-Object 'System.Collections.Generic.Stack[string]'
  $pending.Push($root)
  while ($pending.Count -gt 0) {
    $path = $pending.Pop()
    $item = Get-Item -LiteralPath $path -Force -ErrorAction Stop
    if ($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) {
      throw ('managed tree contains a reparse point: ' + $path)
    }
    $acl = Get-Acl -LiteralPath $path -ErrorAction Stop
    if ($item.PSIsContainer) {
      $security = New-Object System.Security.AccessControl.DirectorySecurity
    } else {
      $security = New-Object System.Security.AccessControl.FileSecurity
    }
    $security.SetSecurityDescriptorSddlForm(
      [string]$acl.GetSecurityDescriptorSddlForm(
        [System.Security.AccessControl.AccessControlSections]::All),
      [System.Security.AccessControl.AccessControlSections]::All)
    $rows.Add([pscustomobject]@{
      path=(Normalize $path)
      container=[bool]$item.PSIsContainer
      security=$security
      projection=Get-AclProjection $path
    })
    if ($item.PSIsContainer) {
      $traversalAcl = Get-Acl -LiteralPath $path -ErrorAction Stop
      $inheritance =
        [System.Security.AccessControl.InheritanceFlags]::ContainerInherit -bor
        [System.Security.AccessControl.InheritanceFlags]::ObjectInherit
      foreach ($sid in @('S-1-5-18', 'S-1-5-32-544', $serviceSid)) {
        $aclRule = New-Rule $sid (
          [System.Security.AccessControl.FileSystemRights]::FullControl) $inheritance
        [void]$traversalAcl.AddAccessRule($aclRule)
      }
      Set-Acl -LiteralPath $path -AclObject $traversalAcl -ErrorAction Stop
      foreach ($child in @(Get-ChildItem -LiteralPath $path -Force -ErrorAction Stop)) {
        $pending.Push($child.FullName)
      }
    }
  }
}
function Set-ManagedAcl([string]$path, [string]$serviceSid) {
  $item = Get-Item -LiteralPath $path -Force -ErrorAction Stop
  if ($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) {
    throw ('managed ACL target is a reparse point: ' + $path)
  }
  $inheritance = if ($item.PSIsContainer) {
    [System.Security.AccessControl.InheritanceFlags]::ContainerInherit -bor
      [System.Security.AccessControl.InheritanceFlags]::ObjectInherit
  } else {
    [System.Security.AccessControl.InheritanceFlags]::None
  }
  if ($item.PSIsContainer) {
    $acl = New-Object System.Security.AccessControl.DirectorySecurity
  } else {
    $acl = New-Object System.Security.AccessControl.FileSecurity
  }
  $acl.SetOwner(
    (New-Object System.Security.Principal.SecurityIdentifier -ArgumentList 'S-1-5-32-544'))
  $acl.SetAccessRuleProtection($true, $false)
  [void]$acl.AddAccessRule((New-Rule 'S-1-5-18' (
    [System.Security.AccessControl.FileSystemRights]::FullControl) $inheritance))
  [void]$acl.AddAccessRule((New-Rule 'S-1-5-32-544' (
    [System.Security.AccessControl.FileSystemRights]::FullControl) $inheritance))
  [void]$acl.AddAccessRule((New-Rule $serviceSid (
    [System.Security.AccessControl.FileSystemRights]::FullControl) $inheritance))
  Set-Acl -LiteralPath $path -AclObject $acl -ErrorAction Stop
  Assert-ManagedAcl $path $serviceSid
}
function Assert-ManagedAcl([string]$path, [string]$serviceSid) {
  $expected = @(
    'S-1-5-18',
    'S-1-5-32-544',
    $serviceSid
  )
  $item = Get-Item -LiteralPath $path -Force -ErrorAction Stop
  $acl = Get-Acl -LiteralPath $path -ErrorAction Stop
  if (-not $acl.AreAccessRulesProtected) {
    throw ('managed ACL is not protected: ' + $path)
  }
  if ((Resolve-Sid ([string]$acl.Owner)) -ne 'S-1-5-32-544') {
    throw ('managed ACL owner is not Administrators: ' + $path)
  }
  $expectedInheritance = if ($item.PSIsContainer) {
    [int](
      [System.Security.AccessControl.InheritanceFlags]::ContainerInherit -bor
      [System.Security.AccessControl.InheritanceFlags]::ObjectInherit)
  } else {
    [int][System.Security.AccessControl.InheritanceFlags]::None
  }
  $rules = @($acl.GetAccessRules(
    $true,
    $true,
    [System.Security.Principal.SecurityIdentifier]))
  if ($rules.Count -ne $expected.Count) {
    throw ('managed ACL does not contain exactly SYSTEM, Administrators and the service account: ' + $path)
  }
  $seen = @{}
  foreach ($rule in $rules) {
    $sid = Resolve-Sid ([string]$rule.IdentityReference)
    if ($sid -notin $expected -or
        $seen.ContainsKey($sid) -or
        $rule.AccessControlType -ne
          [System.Security.AccessControl.AccessControlType]::Allow -or
        [long]$rule.FileSystemRights -ne
          [long][System.Security.AccessControl.FileSystemRights]::FullControl -or
        $rule.IsInherited -or
        [int]$rule.InheritanceFlags -ne $expectedInheritance -or
        [int]$rule.PropagationFlags -ne
          [int][System.Security.AccessControl.PropagationFlags]::None) {
      throw ('managed ACL entry does not match the SYSTEM/Administrators/service matrix: ' + $path)
    }
    $seen[$sid] = $true
  }
  if ($seen.Count -ne $expected.Count) {
    throw ('managed ACL is missing a required principal: ' + $path)
  }
}
function Assert-ServiceInherited([string]$path, [string]$serviceSid) {
  $acl = Get-Acl -LiteralPath $path -ErrorAction Stop
  $matching = @($acl.GetAccessRules(
    $true,
    $true,
    [System.Security.Principal.SecurityIdentifier]) | Where-Object {
      (Resolve-Sid ([string]$_.IdentityReference)) -eq $serviceSid -and
      $_.AccessControlType -eq
        [System.Security.AccessControl.AccessControlType]::Allow -and
      [long]$_.FileSystemRights -eq
        [long][System.Security.AccessControl.FileSystemRights]::FullControl -and
      $_.IsInherited -and
      [int]$_.PropagationFlags -eq
        [int][System.Security.AccessControl.PropagationFlags]::None
    })
  if ($matching.Count -ne 1) {
    throw ('managed root did not propagate service-account access to: ' + $path)
  }
}

$operatorIdentity = [System.Security.Principal.WindowsIdentity]::GetCurrent()
$operator = New-Object System.Security.Principal.WindowsPrincipal -ArgumentList $operatorIdentity
if (-not $operator.IsInRole([System.Security.Principal.WindowsBuiltInRole]::Administrator)) {
  Fail 'managed_root_operator_not_admin' 'run bootstrap from an elevated administrator identity'
}
if (-not $ManagedRoot -or -not [System.IO.Path]::IsPathRooted($ManagedRoot)) {
  Fail 'managed_root_invalid' 'ManagedRoot must be an absolute path'
}
$root = Normalize $ManagedRoot
$driveRoot = Normalize ([System.IO.Path]::GetPathRoot($root))
if ($root -eq $driveRoot) {
  Fail 'managed_root_invalid' 'the filesystem root cannot be a managed root'
}
$restricted = @(
  $env:SystemRoot,
  $env:ProgramFiles,
  ${env:ProgramFiles(x86)},
  $env:ProgramData,
  $env:USERPROFILE,
  (Split-Path -Parent $env:USERPROFILE)
) | Where-Object { $_ }
foreach ($parent in $restricted) {
  if (Within $root $parent) {
    Fail 'managed_root_invalid' ('the managed root is inside a shared system or user root: ' + $parent)
  }
}
Assert-NoReparseAncestor $root
if (-not (Test-Path -LiteralPath $root -PathType Container)) {
  Fail 'managed_root_missing' 'ManagedRoot must be created under change control before bootstrap'
}

$serviceSid = Resolve-Sid $ServiceAccount
if ($serviceSid -ne $operatorIdentity.User.Value) {
  Fail 'managed_root_operator_runtime_mismatch' 'ServiceAccount must be the current administrator runtime'
}
try {
  $serviceUser = Get-LocalUser -SID $serviceSid -ErrorAction Stop
} catch {
  Fail 'managed_root_service_not_local' 'the managed runtime must be an enabled local account'
}
if ($null -eq $serviceUser -or -not [bool]$serviceUser.Enabled) {
  Fail 'managed_root_service_invalid' 'the managed runtime local account is missing or disabled'
}
$adminSid = 'S-1-5-32-544'

$markerPath = Join-Path $root '.description-managed-root.json'
$markerText = '{"schema_version":"solidworks-to-urdf.managed-root/v1"}'
$markerExisted = Test-Path -LiteralPath $markerPath -PathType Leaf
if (Test-Path -LiteralPath $markerPath) {
  Assert-NoReparseAncestor $markerPath
  if (-not (Test-Path -LiteralPath $markerPath -PathType Leaf)) {
    Fail 'managed_root_marker_conflict' 'the managed-root marker is not a regular file'
  }
  $existing = [System.IO.File]::ReadAllText(
    $markerPath,
    (New-Object System.Text.UTF8Encoding($false, $true)))
  if ($existing.Trim() -ne $markerText) {
    Fail 'managed_root_marker_conflict' 'the existing managed-root marker has different contents'
  }
}
Assert-NoReparseAncestor $markerPath
$rootAclBefore = $null
$entriesBefore = New-Object System.Collections.Generic.List[object]
$markerAclBefore = $null
$markerCreated = $false
$probePath = $null
$volumeBefore = (New-Object System.IO.DriveInfo -ArgumentList (
  [System.IO.Path]::GetPathRoot($root))).AvailableFreeSpace
try {
  $rootAclBefore = Get-Acl -LiteralPath $root -ErrorAction Stop
  Get-ManagedTreeSnapshot $root $serviceSid $entriesBefore
  if ($markerExisted) {
    $markerAclBefore = Get-Acl -LiteralPath $markerPath -ErrorAction Stop
  } else {
    [System.IO.File]::WriteAllText(
      $markerPath,
      $markerText,
      (New-Object System.Text.UTF8Encoding($false)))
    $markerCreated = $true
  }

  $entriesBefore |
    Where-Object { (Normalize $_.path) -ne $root } |
    ForEach-Object {
    Set-ManagedAcl ([string]$_.path) $serviceSid
  }
  Set-ManagedAcl $root $serviceSid
  Set-ManagedAcl $markerPath $serviceSid

  $probePath = Join-Path $root (
    '.description-managed-root-probe-' + [guid]::NewGuid().ToString('N'))
  $null = New-Item -ItemType Directory -Path $probePath -ErrorAction Stop
  $probeFile = Join-Path $probePath 'service-access.txt'
  [System.IO.File]::WriteAllText(
    $probeFile,
    'managed-root-service-access',
    (New-Object System.Text.UTF8Encoding($false)))
  Assert-ServiceInherited $probePath $serviceSid
  Assert-ServiceInherited $probeFile $serviceSid

  if ($PostflightScript) {
    $postflight = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath(
      $PostflightScript)
    if (-not (Test-Path -LiteralPath $postflight -PathType Leaf)) {
      throw ('managed-root postflight script is missing: ' + $postflight)
    }
    $powershell = [System.Diagnostics.Process]::GetCurrentProcess().MainModule.FileName
    $result = & $powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass `
      -File $postflight -ManagedRoot $root -ServiceAccount $ServiceAccount 2>&1
    if ($LASTEXITCODE -ne 0) {
      throw ('managed-root postflight failed with exit ' + [string]$LASTEXITCODE)
    }
  }
  if (Test-Path -LiteralPath $probePath) {
    Remove-Item -LiteralPath $probePath -Recurse -Force -ErrorAction Stop
  }
  $probePath = $null
} catch {
  $failure = $_.Exception.Message
  $rollbackErrors = @()
  if ($probePath -and (Test-Path -LiteralPath $probePath)) {
    try {
      Remove-Item -LiteralPath $probePath -Recurse -Force -ErrorAction Stop
    } catch {
      $rollbackErrors += ('probe: ' + $_.Exception.Message)
    }
  }
  if ($null -ne $entriesBefore) {
    foreach ($row in @($entriesBefore | Sort-Object {
        ([string]$_.path).Length
      } -Descending)) {
      try {
        Set-Acl -LiteralPath ([string]$row.path) -AclObject $row.security -ErrorAction Stop
      } catch {
        $rollbackErrors += ([string]$row.path + ': ' + $_.Exception.Message)
      }
    }
  }
  if ($null -ne $rootAclBefore) {
    try {
      Set-Acl -LiteralPath $root -AclObject $rootAclBefore -ErrorAction Stop
    } catch {
      $rollbackErrors += ('root: ' + $_.Exception.Message)
    }
  }
  try {
    if ($markerCreated) {
      if (Test-Path -LiteralPath $markerPath -PathType Leaf) {
        Remove-Item -LiteralPath $markerPath -Force -ErrorAction Stop
      }
    } elseif ($null -ne $markerAclBefore) {
      Set-Acl -LiteralPath $markerPath -AclObject $markerAclBefore -ErrorAction Stop
    }
  } catch {
    $rollbackErrors += ('marker: ' + $_.Exception.Message)
  }
  $volumeAfter = (New-Object System.IO.DriveInfo -ArgumentList (
    [System.IO.Path]::GetPathRoot($root))).AvailableFreeSpace
  $entryCount = $entriesBefore.Count
  $rollbackDocument = [ordered]@{
    passed=($rollbackErrors.Count -eq 0)
    errors=$rollbackErrors
    entry_count=$entryCount
  }
  $failureVolumeDocument = [ordered]@{
    before=$volumeBefore
    after=$volumeAfter
  }
  $failurePayload = [ordered]@{}
  $failurePayload['passed'] = $false
  $failurePayload['code'] = 'managed_root_transaction_failed'
  $failurePayload['message'] = $failure
  $failurePayload['operator_sid'] = $operatorIdentity.User.Value
  $failurePayload['service_sid'] = $serviceSid
  $failurePayload['rollback'] = $rollbackDocument
  $failurePayload['volume'] = $failureVolumeDocument
  Emit $failurePayload
  exit 1
}
$volumeAfter = (New-Object System.IO.DriveInfo -ArgumentList (
  [System.IO.Path]::GetPathRoot($root))).AvailableFreeSpace

$rootAclProjection = Get-AclProjection $root
$markerAclProjection = Get-AclProjection $markerPath
$entriesCount = $entriesBefore.Count
$successVolumeDocument = [ordered]@{
  before=$volumeBefore
  after=$volumeAfter
}
$managedRuntimeDocument = [ordered]@{
  sid=$serviceSid
  account=[string]$serviceUser.Name
  administrator=$true
  privilege_qualification='user-approved-administrator-runtime'
  acl_ace=$true
}
$receipt = [ordered]@{}
$receipt['passed'] = $true
$receipt['managed_root'] = $root
$receipt['marker_path'] = Normalize $markerPath
$receipt['marker_sha256'] = (
  Get-FileHash -LiteralPath $markerPath -Algorithm SHA256).Hash.ToLower()
$receipt['operator_sid'] = $operatorIdentity.User.Value
$receipt['service_sid'] = $serviceSid
$receipt['managed_runtime'] = $managedRuntimeDocument
$receipt['root_acl'] = $rootAclProjection
$receipt['marker_acl'] = $markerAclProjection
$receipt['managed_entry_acl_count'] = $entriesCount
$receipt['volume'] = $successVolumeDocument
Emit $receipt
