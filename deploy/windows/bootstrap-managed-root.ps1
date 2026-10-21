param(
  [Parameter(Mandatory=$true)][string]$ManagedRoot,
  [Parameter(Mandatory=$true)][string]$ServiceAccount
)
$ErrorActionPreference='Stop'

function Emit([hashtable]$payload) {
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
    } | Sort-Object sid, rights, access_type, inheritance, propagation, inherited)
  return [ordered]@{
    owner_sid=Resolve-Sid ([string]$acl.Owner)
    group_sid=Resolve-Sid ([string]$acl.Group)
    protected=[bool]$acl.AreAccessRulesProtected
    rules=$rules
  }
}
function Assert-ManagedAcl([string]$path) {
  $expected = @{}
  $expected['S-1-5-18'] = [long][System.Security.AccessControl.FileSystemRights]::FullControl
  $expected['S-1-5-32-544'] = [long][System.Security.AccessControl.FileSystemRights]::FullControl
  $acl = Get-Acl -LiteralPath $path -ErrorAction Stop
  if (-not $acl.AreAccessRulesProtected) {
    throw ('managed ACL is not protected: ' + $path)
  }
  if ((Resolve-Sid ([string]$acl.Owner)) -ne 'S-1-5-32-544') {
    throw ('managed ACL owner is not Administrators: ' + $path)
  }
  $rules = @($acl.GetAccessRules(
    $true,
    $true,
    [System.Security.Principal.SecurityIdentifier]))
  if ($rules.Count -ne $expected.Count) {
    throw ('managed ACL does not contain exactly two entries: ' + $path)
  }
  foreach ($rule in $rules) {
    $sid = Resolve-Sid ([string]$rule.IdentityReference)
    if (-not $expected.ContainsKey($sid) -or
        $rule.AccessControlType -ne
          [System.Security.AccessControl.AccessControlType]::Allow -or
        [long]$rule.FileSystemRights -ne [long]$expected[$sid] -or
        $rule.IsInherited -or
        [int]$rule.InheritanceFlags -ne
          [int][System.Security.AccessControl.InheritanceFlags]::None -or
        [int]$rule.PropagationFlags -ne
          [int][System.Security.AccessControl.PropagationFlags]::None) {
      throw ('managed ACL entry does not match the administrator/system matrix: ' + $path)
    }
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
} else {
  [System.IO.File]::WriteAllText(
    $markerPath,
    $markerText,
    (New-Object System.Text.UTF8Encoding($false)))
}
Assert-NoReparseAncestor $markerPath
$rootAclBefore = Get-Acl -LiteralPath $root -ErrorAction Stop
$markerAclBefore = if ($markerExisted) {
  Get-Acl -LiteralPath $markerPath -ErrorAction Stop
} else {
  $null
}
$markerCreated = -not $markerExisted
try {
  $markerAcl = Get-Acl -LiteralPath $markerPath -ErrorAction Stop
  $markerAcl.SetOwner((New-Object System.Security.Principal.SecurityIdentifier -ArgumentList $adminSid))
  $markerAcl.SetAccessRuleProtection($true, $false)
  foreach ($rule in @($markerAcl.GetAccessRules(
      $true,
      $false,
      [System.Security.Principal.SecurityIdentifier]))) {
    [void]$markerAcl.RemoveAccessRuleSpecific($rule)
  }
  [void]$markerAcl.AddAccessRule((New-Rule 'S-1-5-18' (
    [System.Security.AccessControl.FileSystemRights]::FullControl) (
    [System.Security.AccessControl.InheritanceFlags]::None)))
  [void]$markerAcl.AddAccessRule((New-Rule $adminSid (
    [System.Security.AccessControl.FileSystemRights]::FullControl) (
    [System.Security.AccessControl.InheritanceFlags]::None)))
  Set-Acl -LiteralPath $markerPath -AclObject $markerAcl -ErrorAction Stop

  $rootAcl = Get-Acl -LiteralPath $root -ErrorAction Stop
  $rootAcl.SetOwner((New-Object System.Security.Principal.SecurityIdentifier -ArgumentList $adminSid))
  $rootAcl.SetAccessRuleProtection($true, $false)
  foreach ($rule in @($rootAcl.GetAccessRules(
      $true,
      $false,
      [System.Security.Principal.SecurityIdentifier]))) {
    [void]$rootAcl.RemoveAccessRuleSpecific($rule)
  }
  [void]$rootAcl.AddAccessRule((New-Rule 'S-1-5-18' (
    [System.Security.AccessControl.FileSystemRights]::FullControl) (
    [System.Security.AccessControl.InheritanceFlags]::None)))
  [void]$rootAcl.AddAccessRule((New-Rule $adminSid (
    [System.Security.AccessControl.FileSystemRights]::FullControl) (
    [System.Security.AccessControl.InheritanceFlags]::None)))
  Set-Acl -LiteralPath $root -AclObject $rootAcl -ErrorAction Stop
  Assert-ManagedAcl $root
  Assert-ManagedAcl $markerPath
} catch {
  $failure = $_.Exception.Message
  $rollbackErrors = @()
  try {
    Set-Acl -LiteralPath $root -AclObject $rootAclBefore -ErrorAction Stop
  } catch {
    $rollbackErrors += ('root: ' + $_.Exception.Message)
  }
  try {
    if ($markerCreated) {
      if (Test-Path -LiteralPath $markerPath -PathType Leaf) {
        Remove-Item -LiteralPath $markerPath -Force -ErrorAction Stop
      }
    } else {
      Set-Acl -LiteralPath $markerPath -AclObject $markerAclBefore -ErrorAction Stop
    }
  } catch {
    $rollbackErrors += ('marker: ' + $_.Exception.Message)
  }
  Emit @{
    passed=$false
    code='managed_root_acl_failed'
    message=$failure
    operator_sid=$operatorIdentity.User.Value
    service_sid=$serviceSid
    rollback=[ordered]@{
      passed=($rollbackErrors.Count -eq 0)
      errors=$rollbackErrors
    }
  }
  exit 1
}

Emit @{
  passed=$true
  managed_root=$root
  marker_path=(Normalize $markerPath)
  marker_sha256=(Get-FileHash -LiteralPath $markerPath -Algorithm SHA256).Hash.ToLower()
  operator_sid=$operatorIdentity.User.Value
  service_sid=$serviceSid
  managed_runtime=[ordered]@{
    sid=$serviceSid
    account=[string]$serviceUser.Name
    administrator=$true
    privilege_qualification='user-approved-administrator-runtime'
    acl_ace=$false
  }
  root_acl=Get-AclProjection $root
  marker_acl=Get-AclProjection $markerPath
}
