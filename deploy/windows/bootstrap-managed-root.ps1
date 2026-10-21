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
$adminSid = 'S-1-5-32-544'

$markerPath = Join-Path $root '.description-managed-root.json'
$markerText = '{"schema_version":"solidworks-to-urdf.managed-root/v1"}'
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
$markerAcl = Get-Acl -LiteralPath $markerPath
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
Set-Acl -LiteralPath $markerPath -AclObject $markerAcl

$rootAcl = Get-Acl -LiteralPath $root
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
Set-Acl -LiteralPath $root -AclObject $rootAcl

Emit @{
  passed=$true
  managed_root=$root
  marker_path=(Normalize $markerPath)
  marker_sha256=(Get-FileHash -LiteralPath $markerPath -Algorithm SHA256).Hash.ToLower()
  operator_sid=$operatorIdentity.User.Value
  service_sid=$serviceSid
  root_acl=Get-AclProjection $root
  marker_acl=Get-AclProjection $markerPath
}
