# Isolated Windows endpoint lifecycle.
#
# One binding file selects the endpoint configuration, release manifest, task identity and
# interpreter.  The endpoint configuration is the only endpoint authority and release.json is
# the only release authority.  The public interface is intentionally limited to:
#
#   instance-lifecycle.ps1 -Verb install|start|drain|stop|status|health -Binding FILE
#
# install stages one immutable generation, registers its task as Disabled and never starts CAD.
# start is the only launch entry point; stop drains first and then proves that no listener or
# writer remains.  status is strictly read-only.  stdout is always one compact result object.
param(
  $Verb = '',
  $Binding = $null
)
$ErrorActionPreference = 'Stop'

$script:LifecyclePhase = 'init'
$script:CurrentVerb = 'unknown'
$script:LifecycleScriptPath = $MyInvocation.MyCommand.Path
$script:BindingSourcePath = ''
$script:BindingFields = @(
  'endpoint_config',
  'release_file',
  'task_name',
  'task_path',
  'service_account',
  'python_executable'
)
$script:ResultSchema = 'solidworks-to-urdf.windows-lifecycle-result/v1'
$script:ReceiptSchema = 'solidworks-to-urdf.windows-install-receipt/v1'
$script:PythonLaunchContract = 'python_module_v1'

function Emit($Document) {
  $Document | ConvertTo-Json -Depth 32 -Compress
}

function Protect-DiagnosticText([string]$Text, [int]$Limit = 1024) {
  if ($null -eq $Text) { return '' }
  $safe = $Text -replace '(?i)(bearer\s+)[^\s,;]+', '$1[redacted]'
  $safe = $safe -replace (
    '(?i)(["'']?(?:token|password|pwd|secret|authorization)["'']?\s*[=:]\s*)' +
    '(?:"[^"]*"|''[^'']*''|[^,;\r\n]+)'), '$1[redacted]'
  $safe = $safe -replace '(?i)([a-z][a-z0-9+.-]*://)[^\s/@]+:[^\s/@]+@', '$1[redacted]@'
  $safe = $safe.Replace("`r", ' ').Replace("`n", ' ')
  if ($safe.Length -gt $Limit) {
    return $safe.Substring(0, $Limit)
  }
  return $safe
}

function New-LifecycleException(
    [string]$Code,
    [string]$Message,
    $Detail = $null,
    [int]$ExitCode = 1) {
  $exception = New-Object System.Exception (Protect-DiagnosticText $Message 2048)
  $exception.Data['code'] = $Code
  $exception.Data['detail'] = $Detail
  $exception.Data['exit_code'] = $ExitCode
  return $exception
}

function Die([string]$Code, [string]$Message, $Detail = $null) {
  throw (New-LifecycleException $Code $Message $Detail 1)
}

function DieUnknown([string]$Code, [string]$Message, $Detail = $null) {
  throw (New-LifecycleException $Code $Message $Detail 2)
}

function Get-ExceptionCode($ErrorRecord, [string]$Default = 'windows_lifecycle_exception') {
  try {
    $value = $ErrorRecord.Exception.Data['code']
    if ($value -is [string] -and $value) { return $value }
  } catch { }
  return $Default
}

function Get-ExceptionExitCode($ErrorRecord, [int]$Default = 2) {
  try {
    $value = $ErrorRecord.Exception.Data['exit_code']
    if ($value -is [int]) { return $value }
    return [int]$value
  } catch {
    return $Default
  }
}

function Get-ExceptionDetail($ErrorRecord) {
  try {
    return $ErrorRecord.Exception.Data['detail']
  } catch {
    return $null
  }
}

function Read-Utf8Text([string]$Path) {
  $encoding = New-Object System.Text.UTF8Encoding($false, $true)
  return [System.IO.File]::ReadAllText($Path, $encoding)
}

function Write-Utf8NoBom([string]$Path, [string]$Text) {
  $parent = [System.IO.Path]::GetDirectoryName([System.IO.Path]::GetFullPath($Path))
  if ($parent -and -not (Test-Path -LiteralPath $parent -PathType Container)) {
    $null = New-Item -ItemType Directory -Path $parent -Force
  }
  $encoding = New-Object System.Text.UTF8Encoding($false)
  [System.IO.File]::WriteAllText($Path, $Text, $encoding)
}

function Write-JsonAtomic(
    [string]$Path,
    $Document,
    [string]$ServiceSid = '') {
  $full = [System.IO.Path]::GetFullPath($Path)
  $parent = [System.IO.Path]::GetDirectoryName($full)
  if (-not (Test-Path -LiteralPath $parent -PathType Container)) {
    $null = New-Item -ItemType Directory -Path $parent -Force
  }
  $partial = Join-Path $parent (
    '.' + [System.IO.Path]::GetFileName($full) + '.' +
    [guid]::NewGuid().ToString('N') + '.partial')
  try {
    Write-Utf8NoBom $partial (Emit $Document)
    if ($ServiceSid) {
      Set-ProtectedAcl $partial $ServiceSid
      Assert-ProtectedAcl $partial $ServiceSid
    }
    Move-Item -LiteralPath $partial -Destination $full -Force
    if ($ServiceSid) {
      Assert-ProtectedAcl $full $ServiceSid
    }
  } finally {
    if (Test-Path -LiteralPath $partial) {
      Remove-Item -LiteralPath $partial -Force -ErrorAction SilentlyContinue
    }
  }
}

function New-StrictJsonFailure([string]$Message, [string]$DuplicatePath = '') {
  $exception = New-Object System.InvalidOperationException $Message
  $exception.Data['json_error'] = $Message
  if ($DuplicatePath) {
    $exception.Data['json_duplicate_path'] = $DuplicatePath
  }
  return $exception
}

function Skip-StrictJsonWhitespace([string]$Text, $State) {
  while ($State.Index -lt $Text.Length) {
    $code = [int]$Text[$State.Index]
    if ($code -ne 0x20 -and $code -ne 0x09 -and
        $code -ne 0x0a -and $code -ne 0x0d) {
      return
    }
    $State.Index++
  }
}

function Read-StrictJsonString([string]$Text, $State, [string]$Path) {
  if ($State.Index -ge $Text.Length -or $Text[$State.Index] -cne '"') {
    throw (New-StrictJsonFailure ('JSON string expected at ' + $Path))
  }
  $State.Index++
  $builder = New-Object System.Text.StringBuilder
  while ($State.Index -lt $Text.Length) {
    $character = [char]$Text[$State.Index]
    $code = [int]$character
    if ($code -eq 0x22) {
      $State.Index++
      return $builder.ToString()
    }
    if ($code -lt 0x20) {
      throw (New-StrictJsonFailure ('JSON string contains a control character at ' + $Path))
    }
    if ($code -ne 0x5c) {
      [void]$builder.Append($character)
      $State.Index++
      continue
    }
    if ($State.Index + 1 -ge $Text.Length) {
      throw (New-StrictJsonFailure ('JSON string escape is incomplete at ' + $Path))
    }
    $escape = [char]$Text[$State.Index + 1]
    if ($escape -ceq '"' -or $escape -ceq '\' -or $escape -ceq '/') {
      [void]$builder.Append($escape)
      $State.Index += 2
    } elseif ($escape -ceq 'b') {
      [void]$builder.Append([char]0x08)
      $State.Index += 2
    } elseif ($escape -ceq 'f') {
      [void]$builder.Append([char]0x0c)
      $State.Index += 2
    } elseif ($escape -ceq 'n') {
      [void]$builder.Append([char]0x0a)
      $State.Index += 2
    } elseif ($escape -ceq 'r') {
      [void]$builder.Append([char]0x0d)
      $State.Index += 2
    } elseif ($escape -ceq 't') {
      [void]$builder.Append([char]0x09)
      $State.Index += 2
    } elseif ($escape -ceq 'u') {
      if ($State.Index + 6 -gt $Text.Length) {
        throw (New-StrictJsonFailure ('JSON unicode escape is incomplete at ' + $Path))
      }
      $digits = $Text.Substring($State.Index + 2, 4)
      if ($digits -cnotmatch '^[0-9A-Fa-f]{4}$') {
        throw (New-StrictJsonFailure ('JSON unicode escape is invalid at ' + $Path))
      }
      [void]$builder.Append([char][Convert]::ToInt32($digits, 16))
      $State.Index += 6
    } else {
      throw (New-StrictJsonFailure ('JSON string escape is invalid at ' + $Path))
    }
  }
  throw (New-StrictJsonFailure ('JSON string is unterminated at ' + $Path))
}

function Read-StrictJsonNumber([string]$Text, $State, [string]$Path) {
  $number = [regex]::Match(
    $Text.Substring($State.Index),
    '^-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?',
    [System.Text.RegularExpressions.RegexOptions]::CultureInvariant)
  if (-not $number.Success) {
    throw (New-StrictJsonFailure ('JSON number is invalid at ' + $Path))
  }
  $State.Index += $number.Length
}

function Read-StrictJsonValue([string]$Text, $State, [string]$Path, [int]$Depth = 0) {
  Skip-StrictJsonWhitespace $Text $State
  if ($State.Index -ge $Text.Length) {
    throw (New-StrictJsonFailure ('JSON value is missing at ' + $Path))
  }
  if ($Depth -gt 64) {
    throw (New-StrictJsonFailure ('JSON nesting exceeds 64 levels at ' + $Path))
  }
  $character = [char]$Text[$State.Index]
  if ($character -ceq '{') {
    $State.Index++
    Skip-StrictJsonWhitespace $Text $State
    if ($State.Index -lt $Text.Length -and $Text[$State.Index] -ceq '}') {
      $State.Index++
      return
    }
    $keys = New-Object System.Collections.ArrayList
    while ($true) {
      Skip-StrictJsonWhitespace $Text $State
      $key = Read-StrictJsonString $Text $State $Path
      foreach ($existing in $keys) {
        if ([string]::Equals(
            [string]$existing,
            [string]$key,
            [System.StringComparison]::Ordinal)) {
          throw (New-StrictJsonFailure (
              'JSON object repeats key ' + $key + ' at ' + $Path) ($Path + '.' + $key))
        }
      }
      [void]$keys.Add($key)
      Skip-StrictJsonWhitespace $Text $State
      if ($State.Index -ge $Text.Length -or $Text[$State.Index] -cne ':') {
        throw (New-StrictJsonFailure ('JSON object misses colon at ' + $Path + '.' + $key))
      }
      $State.Index++
      Read-StrictJsonValue $Text $State ($Path + '.' + $key) ($Depth + 1)
      Skip-StrictJsonWhitespace $Text $State
      if ($State.Index -ge $Text.Length) {
        throw (New-StrictJsonFailure ('JSON object is unterminated at ' + $Path))
      }
      if ($Text[$State.Index] -ceq '}') {
        $State.Index++
        return
      }
      if ($Text[$State.Index] -cne ',') {
        throw (New-StrictJsonFailure ('JSON object misses comma at ' + $Path))
      }
      $State.Index++
    }
  }
  if ($character -ceq '[') {
    $State.Index++
    Skip-StrictJsonWhitespace $Text $State
    if ($State.Index -lt $Text.Length -and $Text[$State.Index] -ceq ']') {
      $State.Index++
      return
    }
    $index = 0
    while ($true) {
      Read-StrictJsonValue $Text $State ($Path + '[' + $index + ']') ($Depth + 1)
      $index++
      Skip-StrictJsonWhitespace $Text $State
      if ($State.Index -ge $Text.Length) {
        throw (New-StrictJsonFailure ('JSON array is unterminated at ' + $Path))
      }
      if ($Text[$State.Index] -ceq ']') {
        $State.Index++
        return
      }
      if ($Text[$State.Index] -cne ',') {
        throw (New-StrictJsonFailure ('JSON array misses comma at ' + $Path))
      }
      $State.Index++
    }
  }
  if ($character -ceq '"') {
    $null = Read-StrictJsonString $Text $State $Path
    return
  }
  if ($character -ceq 't' -and
      $Text.Substring($State.Index).StartsWith('true', [System.StringComparison]::Ordinal)) {
    $State.Index += 4
    return
  }
  if ($character -ceq 'f' -and
      $Text.Substring($State.Index).StartsWith('false', [System.StringComparison]::Ordinal)) {
    $State.Index += 5
    return
  }
  if ($character -ceq 'n' -and
      $Text.Substring($State.Index).StartsWith('null', [System.StringComparison]::Ordinal)) {
    $State.Index += 4
    return
  }
  if ($character -ceq '-' -or ($character -ge '0' -and $character -le '9')) {
    Read-StrictJsonNumber $Text $State $Path
    return
  }
  throw (New-StrictJsonFailure ('JSON value is invalid at ' + $Path))
}

function Assert-StrictJsonText([string]$Text) {
  $state = @{Index=0}
  Skip-StrictJsonWhitespace $Text $state
  Read-StrictJsonValue $Text $state '$'
  Skip-StrictJsonWhitespace $Text $state
  if ($state.Index -ne $Text.Length) {
    throw (New-StrictJsonFailure 'JSON has trailing content')
  }
}

function Read-Json([string]$Path) {
  try {
    $text = (Read-Utf8Text $Path).TrimStart([char]0xFEFF)
    Assert-StrictJsonText $text
    return ($text | ConvertFrom-Json)
  } catch {
    $code = 'json_unreadable'
    if ($_.Exception.Data['json_duplicate_path']) {
      $code = 'json_duplicate_keys'
    }
    Die $code ('cannot read strict JSON file: ' + $Path) @{
      error = Protect-DiagnosticText $_.Exception.Message
      duplicate_path = Protect-DiagnosticText (
        [string]$_.Exception.Data['json_duplicate_path'])
    }
  }
}

function Sha256([string]$Path) {
  return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Get-TextSha256([string]$Text) {
  $sha = [System.Security.Cryptography.SHA256]::Create()
  try {
    $encoding = New-Object System.Text.UTF8Encoding($false)
    return ([System.BitConverter]::ToString(
      $sha.ComputeHash($encoding.GetBytes($Text)))).Replace('-', '').ToLowerInvariant()
  } finally {
    $sha.Dispose()
  }
}

function Get-JsonSha256($Document) {
  return Get-TextSha256 (Emit $Document)
}

function Normalize-PathIdentity([string]$Value) {
  if ($null -eq $Value) { return '' }
  return ([string]$Value).Replace('\', '/').TrimEnd('/')
}

function Test-PathWithin([string]$Path, [string]$Root) {
  $candidate = (Normalize-PathIdentity $Path).ToLowerInvariant()
  $parent = (Normalize-PathIdentity $Root).ToLowerInvariant().TrimEnd('/')
  return $candidate -eq $parent -or $candidate.StartsWith($parent + '/')
}

function Test-AbsolutePath([string]$Path) {
  return (
    [System.IO.Path]::IsPathRooted($Path) -or
    $Path -match '^(?i)[a-z]:[\\/]' -or
    $Path -match '^\\\\'
  )
}

function Find-ReparseAncestor([string]$Path) {
  $current = [System.IO.Path]::GetFullPath($Path)
  while ($current) {
    if (Test-Path -LiteralPath $current) {
      $item = Get-Item -LiteralPath $current -Force -ErrorAction Stop
      if ($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) {
        return $current
      }
    }
    $parent = [System.IO.Path]::GetDirectoryName($current)
    if (-not $parent -or $parent -eq $current) { break }
    $current = $parent
  }
  return ''
}

function Assert-NoReparseAncestor([string]$Path) {
  $unsafe = Find-ReparseAncestor $Path
  if ($unsafe) {
    Die 'windows_path_reparse_refused' ('path crosses a reparse point: ' + $unsafe)
  }
}

function Assert-RegularFile([string]$Path, [string]$Code, [string]$Label) {
  if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
    Die $Code ($Label + ' is missing: ' + $Path)
  }
  $item = Get-Item -LiteralPath $Path -Force -ErrorAction Stop
  if ($item.PSIsContainer -or
      ($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint)) {
    Die $Code ($Label + ' is not a regular file: ' + $Path)
  }
  Assert-NoReparseAncestor $Path
}

function Assert-StringField($Document, [string]$Name, [string]$Where) {
  $value = $Document.$Name
  if ($value -isnot [string] -or [string]::IsNullOrWhiteSpace($value)) {
    Die 'binding_invalid' ($Where + '.' + $Name + ' must be non-empty text')
  }
  return [string]$value
}

function Read-Binding($Source) {
  $script:BindingSourcePath = ''
  $document = $null
  if ($Source -is [string]) {
    if ([string]::IsNullOrWhiteSpace($Source)) {
      Die 'binding_required' 'the binding file is required'
    }
    Assert-RegularFile $Source 'binding_missing' 'the binding file'
    $script:BindingSourcePath = Normalize-PathIdentity (
      [System.IO.Path]::GetFullPath($Source))
    $document = Read-Json $Source
  } elseif ($Source -is [System.Collections.IDictionary]) {
    $object = [ordered]@{}
    foreach ($key in $Source.Keys) {
      $object[[string]$key] = $Source[$key]
    }
    $document = [pscustomobject]$object
  } elseif ($null -ne $Source) {
    $document = $Source
  } else {
    Die 'binding_required' 'the binding file is required'
  }
  $names = @($document.PSObject.Properties.Name)
  if ($names.Count -ne $script:BindingFields.Count -or
      @($script:BindingFields | Where-Object { $_ -cnotin $names }).Count -ne 0) {
    Die 'binding_fields_invalid' (
      'the binding must carry exactly: ' + ($script:BindingFields -join ', ')) @{
      actual = $names
    }
  }
  $binding = [ordered]@{}
  foreach ($name in $script:BindingFields) {
    $binding[$name] = Assert-StringField $document $name 'binding'
  }
  foreach ($name in @('endpoint_config', 'release_file', 'python_executable')) {
    if (-not (Test-AbsolutePath ([string]$binding[$name]))) {
      Die 'binding_fields_invalid' ($name + ' must be an absolute path')
    }
  }
  if ($binding.task_name -notmatch '^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$') {
    Die 'binding_fields_invalid' 'task_name is malformed'
  }
  $taskPath = ([string]$binding.task_path).Replace('/', '\')
  if (-not $taskPath.StartsWith('\')) {
    $taskPath = '\' + $taskPath
  }
  if (-not $taskPath.EndsWith('\')) {
    $taskPath += '\'
  }
  if ($taskPath -match '(^|\\)\.\.?(\\|$)' -or $taskPath -match '[^A-Za-z0-9 _.\-\\]') {
    Die 'binding_fields_invalid' 'task_path is malformed'
  }
  $binding.task_path = $taskPath
  Assert-RegularFile $binding.endpoint_config 'endpoint_config_missing' 'the endpoint configuration'
  Assert-RegularFile $binding.release_file 'release_manifest_missing' 'the release manifest'
  Assert-RegularFile $binding.python_executable 'base_python_missing' 'the base Python executable'
  return $binding
}

function Get-ControlPaths([string]$StateRoot) {
  return [ordered]@{
    state_root = Normalize-PathIdentity $StateRoot
    binding = Normalize-PathIdentity (Join-Path $StateRoot 'windows-binding.json')
    lifecycle = Normalize-PathIdentity (Join-Path $StateRoot 'instance-lifecycle.ps1')
    bootstrap = Normalize-PathIdentity (Join-Path $StateRoot 'bootstrap-managed-root.ps1')
  }
}

function Assert-ControlBindingPath($Binding, [string]$StateRoot) {
  if ([string]::IsNullOrWhiteSpace($script:BindingSourcePath)) {
    Die 'binding_path_required' 'the lifecycle requires the binding file on disk'
  }
  $control = Get-ControlPaths $StateRoot
  if ((Normalize-PathIdentity $script:BindingSourcePath) -cne $control.binding) {
    Die 'binding_path_mismatch' (
      'the binding path must be ' + $control.binding)
  }
  return $control
}

function Install-BytesOnce([string]$Source, [string]$Target, [string]$ConflictCode) {
  Assert-RegularFile $Source $ConflictCode ('control source: ' + $Source)
  $sourcePath = Normalize-PathIdentity ([System.IO.Path]::GetFullPath($Source))
  $targetPath = Normalize-PathIdentity ([System.IO.Path]::GetFullPath($Target))
  $sourceHash = Sha256 $Source
  if ($sourcePath -ceq $targetPath) {
    return [ordered]@{
      path = $targetPath
      sha256 = $sourceHash
      created = $false
    }
  }
  if (Test-Path -LiteralPath $Target) {
    Assert-RegularFile $Target $ConflictCode ('control target: ' + $Target)
    if ((Sha256 $Target) -cne $sourceHash) {
      Die $ConflictCode ('a different control file already exists: ' + $Target)
    }
    return [ordered]@{
      path = $targetPath
      sha256 = $sourceHash
      created = $false
    }
  }
  $parent = Split-Path -Parent $Target
  if (-not (Test-Path -LiteralPath $parent -PathType Container)) {
    $null = New-Item -ItemType Directory -Path $parent -Force
  }
  $partial = Join-Path $parent (
    '.' + (Split-Path -Leaf $Target) + '.' +
    [guid]::NewGuid().ToString('N') + '.partial')
  try {
    [System.IO.File]::WriteAllBytes($partial, [System.IO.File]::ReadAllBytes($Source))
    Move-Item -LiteralPath $partial -Destination $Target
  } finally {
    if (Test-Path -LiteralPath $partial) {
      Remove-Item -LiteralPath $partial -Force -ErrorAction SilentlyContinue
    }
  }
  if ((Sha256 $Target) -cne $sourceHash) {
    Die 'control_copy_mismatch' ('control bytes differ after copy: ' + $Target)
  }
  return [ordered]@{
    path = $targetPath
    sha256 = $sourceHash
    created = $true
  }
}

function Install-ControlFiles(
    $Binding,
    [string]$StateRoot,
    $State) {
  $control = Get-ControlPaths $StateRoot
  $State.files.lifecycle = Install-BytesOnce (
    [string]$script:LifecycleScriptPath) $control.lifecycle 'lifecycle_control_conflict'
  $State.files.bootstrap = Install-BytesOnce (
    (Get-BootstrapScriptPath)) $control.bootstrap 'bootstrap_control_conflict'
  $State.files.binding = Install-BytesOnce (
    [string]$script:BindingSourcePath) $control.binding 'binding_control_conflict'
  return $State.files
}

function Assert-ControlFiles($Binding, $Receipt, [string]$StateRoot) {
  $control = Assert-ControlBindingPath $Binding $StateRoot
  if ((Normalize-PathIdentity ([string]$script:LifecycleScriptPath)) -cne
      $control.lifecycle) {
    Die 'control_entry_mismatch' (
      'run the installed lifecycle entry point: ' + $control.lifecycle)
  }
  foreach ($name in @('binding', 'lifecycle', 'bootstrap')) {
    $path = [string]$control[$name]
    Assert-RegularFile $path 'control_file_missing' ($name + ' control file')
    $expected = [string]$Receipt.($name + '_sha256')
    if (-not $expected -or (Sha256 $path) -cne $expected) {
      Die 'control_file_mismatch' ($name + ' control file differs from install-receipt.json')
    }
  }
  return $control
}

function Get-ObjectProperty($Document, [string]$Name, [string]$Where) {
  if ($null -eq $Document -or
      $Document.PSObject.Properties.Name -cnotcontains $Name) {
    Die 'release_manifest_invalid' ($Where + ' misses ' + $Name)
  }
  return $Document.$Name
}

function Read-ReleaseManifest($Binding) {
  $path = [string]$Binding.release_file
  $document = Read-Json $path
  if ($document.schema_version -cne 'solidworks-to-urdf.release/v1') {
    Die 'release_manifest_invalid' 'release.json carries an unsupported schema_version'
  }
  foreach ($name in @('version', 'source_commit', 'source_tree', 'source_sha256')) {
    $value = Get-ObjectProperty $document $name 'release.json'
    if ($value -isnot [string] -or [string]::IsNullOrWhiteSpace([string]$value)) {
      Die 'release_manifest_invalid' ('release.json ' + $name + ' must be non-empty text')
    }
  }
  if ($document.source_commit -cnotmatch '^[0-9a-f]{40}$' -or
      $document.source_tree -cnotmatch '^[0-9a-f]{40}$') {
    Die 'release_manifest_invalid' 'release.json commit or tree is malformed'
  }
  if ($document.source_sha256 -cnotmatch '^[0-9a-f]{64}$') {
    Die 'release_manifest_invalid' 'release.json source_sha256 is malformed'
  }
  $artifacts = Get-ObjectProperty $document 'artifacts' 'release.json'
  if ($null -eq $artifacts -or $artifacts -is [string] -or
      @($artifacts.PSObject.Properties).Count -eq 0) {
    Die 'release_manifest_invalid' 'release.json artifacts must be a non-empty object'
  }
  $windows = @()
  foreach ($property in @($artifacts.PSObject.Properties)) {
    $name = [string]$property.Name
    $digest = [string]$property.Value
    if ($digest -cnotmatch '^[0-9a-f]{64}$') {
      Die 'release_manifest_invalid' ('artifact digest is malformed: ' + $name)
    }
    if ($name -match '^(?i).+-windows-.+\.zip$') {
      $windows += [pscustomobject]@{name=$name; sha256=$digest}
    }
  }
  if ($windows.Count -ne 1) {
    Die 'windows_archive_ambiguous' (
      'release.json must name exactly one *-windows-*.zip artifact')
  }
  $archiveName = [string]$windows[0].name
  if ([System.IO.Path]::GetFileName($archiveName) -cne $archiveName -or
      $archiveName.Contains('\') -or $archiveName.Contains('/')) {
    Die 'release_manifest_invalid' 'the Windows archive must be a sibling file name'
  }
  $releaseRoot = Split-Path -Parent $path
  $archive = Join-Path $releaseRoot $archiveName
  Assert-RegularFile $archive 'windows_archive_missing' 'the Windows archive'
  if ((Sha256 $archive) -cne [string]$windows[0].sha256) {
    Die 'windows_archive_mismatch' 'the Windows archive digest differs from release.json'
  }
  return [ordered]@{
    path = Normalize-PathIdentity ([System.IO.Path]::GetFullPath($path))
    sha256 = Sha256 $path
    archive_name = $archiveName
    archive_path = Normalize-PathIdentity ([System.IO.Path]::GetFullPath($archive))
    archive_sha256 = [string]$windows[0].sha256
    version = [string]$document.version
    source_commit = [string]$document.source_commit
    source_tree = [string]$document.source_tree
    source_sha256 = [string]$document.source_sha256
  }
}

function Invoke-CapturedProcess([string]$Executable, [string[]]$Arguments) {
  $start = New-Object System.Diagnostics.ProcessStartInfo
  $start.FileName = $Executable
  $start.UseShellExecute = $false
  $start.CreateNoWindow = $true
  $start.RedirectStandardOutput = $true
  $start.RedirectStandardError = $true
  $start.Arguments = (@($Arguments | ForEach-Object {
    # Preserve quotes and backslashes according to the Windows argv rules.
    $escaped = [regex]::Replace($_, '(\\*)"', '$1$1\"')
    $escaped = [regex]::Replace($escaped, '(\\+)$', '$1$1')
    '"' + $escaped + '"'
  }) -join ' ')
  $process = New-Object System.Diagnostics.Process
  $process.StartInfo = $start
  try {
    if (-not $process.Start()) { throw 'the process did not start' }
    $stdout = $process.StandardOutput.ReadToEndAsync()
    $stderr = $process.StandardError.ReadToEndAsync()
    $process.WaitForExit()
    $text = $stdout.GetAwaiter().GetResult() + "`n" + $stderr.GetAwaiter().GetResult()
    return @{
      exit_code = [int]$process.ExitCode
      text = $text
      lines = @($text -split '\r?\n')
    }
  } finally {
    $process.Dispose()
  }
}

function Get-LastJsonLine($Capture) {
  $lines = @($Capture.lines)
  for ($index = $lines.Count - 1; $index -ge 0; $index--) {
    $trimmed = ([string]$lines[$index]).Trim()
    if ($trimmed.StartsWith('{') -and $trimmed.EndsWith('}')) {
      try { return ($trimmed | ConvertFrom-Json) } catch { }
    }
  }
  return $null
}

function Get-EndpointConfigPythonProgram {
  return @'
import json, ntpath, os, sys

def duplicate_checked(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate input key: " + key)
        result[key] = value
    return result

def reject_constant(value):
    raise ValueError("non-finite constant: " + value)

try:
    path = sys.argv[1]
    with open(path, "r", encoding="utf-8") as handle:
        document = json.load(
            handle,
            object_pairs_hook=duplicate_checked,
            parse_constant=reject_constant,
        )
    expected = {
        "schema_version",
        "package_root",
        "output_root",
        "state_root",
        "token_file",
        "host",
        "port",
    }
    if not isinstance(document, dict):
        raise ValueError("the endpoint configuration is not an object")
    if set(document) != expected:
        raise ValueError(
            "the endpoint configuration keys differ: "
            + ",".join(sorted(set(document) ^ expected))
        )
    if document["schema_version"] != "solidworks-to-urdf.endpoint/v1":
        raise ValueError("the endpoint configuration schema is unsupported")
    for key in ("package_root", "output_root", "state_root", "token_file"):
        if not isinstance(document[key], str) or not document[key]:
            raise ValueError(key + " must be non-empty text")
        if not (ntpath.isabs(document[key]) or os.path.isabs(document[key])):
            raise ValueError(key + " must be an absolute path")
    if document["host"] != "127.0.0.1":
        raise ValueError("the endpoint host must be loopback")
    port = document["port"]
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("the endpoint port is invalid")
    token = document["token_file"]
    if os.path.islink(token) or not os.path.isfile(token):
        raise ValueError("the endpoint token is not a regular file")
    with open(token, "rb") as handle:
        value = handle.read()
    if len(value) < 32 or b"\x00" in value:
        raise ValueError("the endpoint token must contain at least 32 non-NUL bytes")
    try:
        token_text = value.decode("ascii").strip()
    except UnicodeDecodeError:
        raise ValueError("the endpoint token must be ASCII")
    if len(token_text) < 32 or any(character.isspace() for character in token_text):
        raise ValueError("the endpoint token must contain 32 non-whitespace ASCII characters")
    print(json.dumps(document, sort_keys=True, separators=(",", ":")))
except Exception as error:
    print(json.dumps({
        "code": "endpoint_config_invalid",
        "message": str(error),
    }, sort_keys=True, separators=(",", ":")))
    raise SystemExit(1)
'@
}

function Read-EndpointConfigStrict($Binding) {
  $capture = Invoke-CapturedProcess ([string]$Binding.python_executable) @(
    '-I',
    '-B',
    '-c',
    (Get-EndpointConfigPythonProgram),
    [string]$Binding.endpoint_config
  )
  $document = Get-LastJsonLine $capture
  if ($capture.exit_code -ne 0 -or $null -eq $document -or
      $document.PSObject.Properties.Name -ccontains 'code') {
    $code = 'endpoint_config_invalid'
    $message = 'the endpoint configuration is invalid'
    if ($null -ne $document) {
      if ($document.code) { $code = [string]$document.code }
      if ($document.message) { $message = [string]$document.message }
    } else {
      $message = Protect-DiagnosticText $capture.text
    }
    Die $code $message
  }
  return [ordered]@{
    schema_version = [string]$document.schema_version
    package_root = Normalize-PathIdentity ([string]$document.package_root)
    output_root = Normalize-PathIdentity ([string]$document.output_root)
    state_root = Normalize-PathIdentity ([string]$document.state_root)
    token_file = Normalize-PathIdentity ([string]$document.token_file)
    host = [string]$document.host
    port = [int]$document.port
    source_path = Normalize-PathIdentity (
      [System.IO.Path]::GetFullPath([string]$Binding.endpoint_config))
    source_sha256 = Sha256 ([string]$Binding.endpoint_config)
  }
}

function Get-ManagedCommonParent($Config, $Binding) {
  $paths = @(
    [string]$Config.package_root,
    [string]$Config.output_root,
    [string]$Config.state_root,
    [string]$Config.token_file,
    [string]$Binding.endpoint_config
  ) | ForEach-Object { [System.IO.Path]::GetFullPath($_) }
  $common = [System.IO.Path]::GetFullPath($paths[0])
  foreach ($path in @($paths | Select-Object -Skip 1)) {
    while (-not (Test-PathWithin ([System.IO.Path]::GetFullPath($path)) $common)) {
      $parent = [System.IO.Path]::GetDirectoryName($common)
      if (-not $parent -or $parent -eq $common) {
        Die 'managed_root_invalid' 'the endpoint inputs have no safe common parent'
      }
      $common = $parent
    }
  }
  $common = ([System.IO.Path]::GetFullPath($common)).TrimEnd('\', '/')
  $driveRoot = ([System.IO.Path]::GetFullPath(
    [System.IO.Path]::GetPathRoot($common))).TrimEnd('\', '/')
  if (-not $common -or $common -ieq $driveRoot) {
    Die 'managed_root_invalid' 'the managed common parent is a filesystem root'
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
    $fullParent = [System.IO.Path]::GetFullPath([string]$parent)
    if (Test-PathWithin $common $fullParent) {
      Die 'managed_root_invalid' (
        'the managed common parent is inside a shared system or user root: ' + $fullParent)
    }
  }
  Assert-NoReparseAncestor $common
  return $common
}

function Get-StableManagedPaths(
    $Config,
    $Binding,
    [string]$Generation,
    [string]$ReceiptPath) {
  $common = Get-ManagedCommonParent $Config $Binding
  $control = Get-ControlPaths ([string]$Config.state_root)
  $candidates = @(
    $common,
    (Join-Path $common '.description-managed-root.json'),
    [string]$Config.package_root,
    [string]$Config.output_root,
    [string]$Config.state_root,
    (Join-Path ([string]$Config.state_root) 'lifecycle'),
    $Generation,
    (Config-Path $Generation),
    [string]$Config.source_path,
    [string]$Config.token_file,
    (Join-Path ([string]$Config.state_root) 'install-id'),
    $ReceiptPath,
    [string]$control.binding,
    [string]$control.lifecycle,
    [string]$control.bootstrap
  )
  $seen = @{}
  $result = @()
  foreach ($candidate in $candidates) {
    if ([string]::IsNullOrWhiteSpace([string]$candidate)) {
      Die 'windows_managed_acl_path_missing' 'a stable managed path is empty'
    }
    $identity = Normalize-PathIdentity (
      [System.IO.Path]::GetFullPath([string]$candidate))
    if (-not $seen.ContainsKey($identity)) {
      $seen[$identity] = $true
      $result += $identity
    }
  }
  return $result
}

function Get-BootstrapScriptPath {
  $path = Join-Path $PSScriptRoot 'bootstrap-managed-root.ps1'
  if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
    $scriptPath = [string]$script:LifecycleScriptPath
    if ($scriptPath) {
      $path = Join-Path (Split-Path -Parent $scriptPath) 'bootstrap-managed-root.ps1'
    }
  }
  if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
    Die 'managed_root_bootstrap_missing' 'bootstrap-managed-root.ps1 is missing'
  }
  return $path
}

function Invoke-ManagedRootBootstrap([string]$ManagedRoot, [string]$ServiceAccount) {
  $script:LifecyclePhase = 'install-bootstrap'
  $powershell = [System.Diagnostics.Process]::GetCurrentProcess().MainModule.FileName
  $arguments = @(
    '-NoProfile',
    '-NonInteractive',
    '-ExecutionPolicy',
    'Bypass',
    '-File',
    (Get-BootstrapScriptPath),
    '-ManagedRoot',
    $ManagedRoot,
    '-ServiceAccount',
    $ServiceAccount
  )
  $capture = Invoke-CapturedProcess $powershell $arguments
  $document = Get-LastJsonLine $capture
  if ($capture.exit_code -ne 0 -or $null -eq $document -or
      $document.passed -isnot [bool] -or $document.passed -ne $true) {
    $code = 'managed_root_bootstrap_failed'
    $message = 'the managed-root bootstrap failed'
    if ($null -ne $document) {
      if ($document.code) { $code = [string]$document.code }
      if ($document.message) { $message = [string]$document.message }
    } else {
      $message = Protect-DiagnosticText $capture.text
    }
    Die $code $message
  }
  return $document
}

function Resolve-Sid([string]$Identity) {
  if ([string]::IsNullOrWhiteSpace($Identity)) {
    Die 'service_account_invalid' 'service_account is empty'
  }
  try {
    return ([System.Security.Principal.SecurityIdentifier]$Identity).Value
  } catch {
    try {
      return (New-Object System.Security.Principal.NTAccount($Identity)).Translate(
        [System.Security.Principal.SecurityIdentifier]).Value
    } catch {
      Die 'service_account_invalid' ('cannot resolve service_account: ' + $Identity)
    }
  }
}

function Get-OperatorSid {
  try {
    return [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
  } catch {
    DieUnknown 'windows_observer_failed' 'the Windows operator identity is unavailable'
  }
}

function Assert-OperatorIsServiceAccount($Binding) {
  if ((Get-OperatorSid) -cne (Resolve-Sid ([string]$Binding.service_account))) {
    Die 'windows_operator_runtime_mismatch' (
      'the lifecycle operator must be the configured service account')
  }
}

function Get-AclProjection([string]$Path) {
  $acl = Get-Acl -LiteralPath $Path -ErrorAction Stop
  $rules = @($acl.GetAccessRules(
      $true,
      $true,
      [System.Security.Principal.SecurityIdentifier]) | ForEach-Object {
      [ordered]@{
        sid = ([System.Security.Principal.SecurityIdentifier]$_.IdentityReference).Value
        access_type = [string]$_.AccessControlType
        rights = [long]$_.FileSystemRights
        inherited = [bool]$_.IsInherited
        inheritance = [int]$_.InheritanceFlags
        propagation = [int]$_.PropagationFlags
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
    owner_sid = Resolve-Sid ([string]$acl.Owner)
    protected = [bool]$acl.AreAccessRulesProtected
    sddl = [string]$acl.GetSecurityDescriptorSddlForm(
      [System.Security.AccessControl.AccessControlSections]::All)
    rules = $rules
  }
}

function New-ManagedAccessRule(
    [string]$Sid,
    [System.Security.AccessControl.FileSystemRights]$Rights,
    [System.Security.AccessControl.InheritanceFlags]$Inheritance) {
  $sidObject = New-Object System.Security.Principal.SecurityIdentifier -ArgumentList $Sid
  return New-Object System.Security.AccessControl.FileSystemAccessRule -ArgumentList @(
    $sidObject,
    $Rights,
    $Inheritance,
    [System.Security.AccessControl.PropagationFlags]::None,
    [System.Security.AccessControl.AccessControlType]::Allow)
}

function Set-ProtectedAcl([string]$Path, [string]$ServiceSid) {
  $item = Get-Item -LiteralPath $Path -Force -ErrorAction Stop
  if ($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) {
    Die 'windows_managed_acl_unsafe' ('managed ACL target is a reparse point: ' + $Path)
  }
  if ($item.PSIsContainer) {
    $acl = New-Object System.Security.AccessControl.DirectorySecurity
    $inheritance =
      [System.Security.AccessControl.InheritanceFlags]::ContainerInherit -bor
      [System.Security.AccessControl.InheritanceFlags]::ObjectInherit
  } else {
    $acl = New-Object System.Security.AccessControl.FileSecurity
    $inheritance = [System.Security.AccessControl.InheritanceFlags]::None
  }
  $acl.SetOwner((New-Object System.Security.Principal.SecurityIdentifier -ArgumentList 'S-1-5-32-544'))
  $acl.SetAccessRuleProtection($true, $false)
  foreach ($sid in @('S-1-5-18', 'S-1-5-32-544', $ServiceSid)) {
    [void]$acl.AddAccessRule((New-ManagedAccessRule $sid (
      [System.Security.AccessControl.FileSystemRights]::FullControl) $inheritance))
  }
  Set-Acl -LiteralPath $Path -AclObject $acl -ErrorAction Stop
}

function Assert-ProtectedAcl([string]$Path, [string]$ServiceSid) {
  $projection = Get-AclProjection $Path
  $item = Get-Item -LiteralPath $Path -Force -ErrorAction Stop
  if ($projection.owner_sid -cne 'S-1-5-32-544' -or
      $projection.protected -ne $true) {
    Die 'windows_managed_acl_invalid' ('managed ACL is not protected by Administrators: ' + $Path)
  }
  $expected = @('S-1-5-18', 'S-1-5-32-544', $ServiceSid)
  if (@($projection.rules).Count -ne 3) {
    Die 'windows_managed_acl_invalid' ('managed ACL rule count differs: ' + $Path)
  }
  $expectedInheritance = if ($item.PSIsContainer) {
    [int](
      [System.Security.AccessControl.InheritanceFlags]::ContainerInherit -bor
      [System.Security.AccessControl.InheritanceFlags]::ObjectInherit)
  } else {
    [int][System.Security.AccessControl.InheritanceFlags]::None
  }
  $seen = @{}
  foreach ($row in @($projection.rules)) {
    $rights = [long]$row.rights
    $sid = [string]$row.sid
    if ($sid -cnotin $expected -or
        $seen.ContainsKey($sid) -or
        [string]$row.access_type -cne 'Allow' -or
        [bool]$row.inherited -ne $false -or
        [int]$row.inheritance -ne $expectedInheritance -or
        [int]$row.propagation -ne 0 -or
        $rights -ne [long][System.Security.AccessControl.FileSystemRights]::FullControl) {
      Die 'windows_managed_acl_invalid' ('managed ACL rule differs: ' + $Path)
    }
    $seen[$sid] = $true
  }
  if (@($expected | Where-Object { -not $seen.ContainsKey($_) }).Count -ne 0) {
    Die 'windows_managed_acl_invalid' ('managed ACL is missing a required principal: ' + $Path)
  }
}

function Set-TaskFileAcl([string]$TaskFile, [string]$ServiceSid) {
  Set-ProtectedAcl $TaskFile $ServiceSid
  Assert-ProtectedAcl $TaskFile $ServiceSid
}

function Set-AccountEnvironment([string]$Name, [string]$Value) {
  [Environment]::SetEnvironmentVariable($Name, $Value, 'User')
}

function Get-AccountEnvironment([string]$Name) {
  return [string][Environment]::GetEnvironmentVariable($Name, 'User')
}

function Get-DomainPins([string]$StateRoot, [string]$EndpointRoot) {
  return [ordered]@{
    DESCRIPTION_ENDPOINT_ROOT = [string]$EndpointRoot
    DESCRIPTION_CAD_OWNERSHIP_FILE = (Join-Path $StateRoot 'cad-ownership.json')
  }
}

function Assert-DomainPins($Pins) {
  foreach ($name in @('DESCRIPTION_ENDPOINT_ROOT', 'DESCRIPTION_CAD_OWNERSHIP_FILE')) {
    $actual = Get-AccountEnvironment $name
    if ($actual -and
        (Normalize-PathIdentity $actual) -ne
          (Normalize-PathIdentity ([string]$Pins[$name]))) {
      Die 'account_environment_conflict' (
        $name + ' already names another managed root: ' + $actual)
    }
  }
}

function Set-DomainPins($Pins) {
  $previous = [ordered]@{}
  try {
    foreach ($name in @('DESCRIPTION_ENDPOINT_ROOT', 'DESCRIPTION_CAD_OWNERSHIP_FILE')) {
      $previous[$name] = Get-AccountEnvironment $name
    }
    foreach ($name in @('DESCRIPTION_ENDPOINT_ROOT', 'DESCRIPTION_CAD_OWNERSHIP_FILE')) {
      Set-AccountEnvironment $name ([string]$Pins[$name])
    }
  } catch {
    $primary = $_
    Restore-DomainPins $previous
    throw $primary
  }
  return $previous
}

function Restore-DomainPins($Previous) {
  if ($null -eq $Previous) { return }
  foreach ($name in @('DESCRIPTION_ENDPOINT_ROOT', 'DESCRIPTION_CAD_OWNERSHIP_FILE')) {
    Set-AccountEnvironment $name ([string]$Previous[$name])
  }
}

function Runtime-Python([string]$Generation) {
  return (Join-Path (Join-Path (Join-Path $Generation 'runtime') 'Scripts') 'python.exe')
}

function Config-Path([string]$Generation) {
  return (Join-Path $Generation 'endpoint.json')
}

function Generation-Path([string]$StateRoot) {
  return (Join-Path (Join-Path $StateRoot 'lifecycle') 'generation')
}

function Runtime-LauncherArguments([string]$Generation) {
  return '-I -B -m description_pipeline.orchestration.windows serve --config "' +
    (Config-Path $Generation) + '"'
}

function Test-Runtime-LauncherAction([string]$Execute, [string]$Arguments, [string]$Generation) {
  if ([string]::IsNullOrWhiteSpace($Execute) -or
      [string]::IsNullOrWhiteSpace($Arguments) -or
      [string]::IsNullOrWhiteSpace($Generation)) {
    return $false
  }
  $expectedExecute = Normalize-PathIdentity (Runtime-Python $Generation)
  $expectedArguments = Runtime-LauncherArguments $Generation
  $executeMatches = (
    (Normalize-PathIdentity $Execute) -ceq $expectedExecute)
  $argumentsMatch = (
    [string]$Arguments -ceq $expectedArguments)
  if (-not $executeMatches -or -not $argumentsMatch) {
    return $false
  }
  return $true
}

function Get-BasePythonIdentity($Binding) {
  $program = @'
import json, platform, struct, sys
print(json.dumps({
    "implementation": platform.python_implementation(),
    "version": platform.python_version(),
    "machine": platform.machine(),
    "platform": platform.system(),
    "pointer_bits": struct.calcsize("P") * 8,
    "executable": sys.executable,
}, sort_keys=True, separators=(",", ":")))
'@
  $capture = Invoke-CapturedProcess ([string]$Binding.python_executable) @(
    '-I', '-B', '-c', $program)
  $document = Get-LastJsonLine $capture
  if ($capture.exit_code -ne 0 -or $null -eq $document) {
    Die 'base_python_invalid' 'the bound Python executable cannot identify itself'
  }
  if ($document.implementation -cne 'CPython' -or
      [string]$document.version -notmatch '^3\.12(?:\.|$)' -or
      [string]$document.platform -cne 'Windows' -or
      [int]$document.pointer_bits -ne 64 -or
      [string]$document.machine -notmatch '^(?i)(amd64|x86_64)$') {
    Die 'base_python_invalid' (
      'the bound Python must be 64-bit CPython 3.12 for Windows')
  }
  if ((Normalize-PathIdentity ([string]$document.executable)) -ine
      (Normalize-PathIdentity ([string]$Binding.python_executable))) {
    Die 'base_python_invalid' 'the interpreter reported another executable path'
  }
  return [ordered]@{
    executable = Normalize-PathIdentity ([string]$document.executable)
    version = [string]$document.version
    machine = [string]$document.machine
    sha256 = Sha256 ([string]$Binding.python_executable)
  }
}

function Assert-EndpointRootsSeparate($Config) {
  $roots = @(
    [string]$Config.package_root,
    [string]$Config.output_root,
    [string]$Config.state_root
  )
  for ($first = 0; $first -lt $roots.Count; $first++) {
    for ($second = $first + 1; $second -lt $roots.Count; $second++) {
      if ((Test-PathWithin $roots[$first] $roots[$second]) -or
          (Test-PathWithin $roots[$second] $roots[$first])) {
        Die 'endpoint_config_invalid' 'package_root, output_root and state_root must be separate'
      }
    }
  }
}

function Expand-ArchiveSafely([string]$Archive, [string]$Destination) {
  Assert-NoReparseAncestor $Destination
  if (Test-Path -LiteralPath $Destination) {
    Die 'install_staging_exists' ('the install staging directory already exists: ' + $Destination)
  }
  $null = New-Item -ItemType Directory -Path $Destination
  Add-Type -AssemblyName System.IO.Compression.FileSystem
  $seen = @{}
  $zip = $null
  try {
    $zip = [System.IO.Compression.ZipFile]::OpenRead($Archive)
    foreach ($entry in @($zip.Entries)) {
      $name = [string]$entry.FullName
      if (-not $name -or $name.Contains('\') -or $name.StartsWith('/') -or
          $name -match '^[A-Za-z]:') {
        Die 'archive_member_unsafe' ('unsafe archive member: ' + $name)
      }
      $segments = @($name.TrimEnd('/').Split('/'))
      if ($segments.Count -eq 0 -or
          @($segments | Where-Object { -not $_ -or $_ -eq '.' -or $_ -eq '..' }).Count -ne 0) {
        Die 'archive_member_unsafe' ('unsafe archive member: ' + $name)
      }
      $key = $name.TrimEnd('/').ToLowerInvariant()
      if ($seen.ContainsKey($key)) {
        Die 'archive_member_unsafe' ('duplicate archive member: ' + $name)
      }
      $seen[$key] = $true
      $target = Join-Path $Destination ($name.Replace('/', [System.IO.Path]::DirectorySeparatorChar))
      $fullTarget = [System.IO.Path]::GetFullPath($target)
      if (-not (Test-PathWithin $fullTarget $Destination)) {
        Die 'archive_member_unsafe' ('archive member escapes staging: ' + $name)
      }
      if ($name.EndsWith('/')) {
        if (-not (Test-Path -LiteralPath $fullTarget)) {
          $null = New-Item -ItemType Directory -Path $fullTarget -Force
        }
        continue
      }
      $parent = [System.IO.Path]::GetDirectoryName($fullTarget)
      if (-not (Test-Path -LiteralPath $parent -PathType Container)) {
        $null = New-Item -ItemType Directory -Path $parent -Force
      }
      $source = $entry.Open()
      try {
        $targetStream = [System.IO.File]::Open(
          $fullTarget,
          [System.IO.FileMode]::CreateNew,
          [System.IO.FileAccess]::Write,
          [System.IO.FileShare]::None)
        try {
          $source.CopyTo($targetStream)
        } finally {
          $targetStream.Dispose()
        }
      } finally {
        $source.Dispose()
      }
    }
  } catch {
    if (Test-Path -LiteralPath $Destination) {
      Remove-Item -LiteralPath $Destination -Recurse -Force -ErrorAction SilentlyContinue
    }
    throw
  } finally {
    if ($null -ne $zip) { $zip.Dispose() }
  }
}

function Assert-ArchiveInventory([string]$Root) {
  $manifestPath = Join-Path $Root 'files.json'
  Assert-RegularFile $manifestPath 'archive_inventory_missing' 'files.json'
  $manifest = Read-Json $manifestPath
  $expected = @{}
  foreach ($property in @($manifest.PSObject.Properties)) {
    $name = [string]$property.Name
    $digest = [string]$property.Value
    if (-not $name -or $name.Contains('\') -or $name.StartsWith('/') -or
        @($name.Split('/') | Where-Object { -not $_ -or $_ -eq '.' -or $_ -eq '..' }).Count -ne 0 -or
        $digest -cnotmatch '^[0-9a-f]{64}$') {
      Die 'archive_inventory_invalid' ('invalid files.json row: ' + $name)
    }
    $expected[$name.ToLowerInvariant()] = @{
      name = $name
      sha256 = $digest
    }
  }
  # files.json is written after inventorying the rest of the release, so it cannot contain its
  # own digest.  It is still an archive member and is verified against its extracted bytes.
  $expected['files.json'] = @{
    name = 'files.json'
    sha256 = Sha256 $manifestPath
  }
  $actual = @{}
  foreach ($file in @(Get-ChildItem -LiteralPath $Root -Recurse -File -Force)) {
    if ($file.Attributes -band [System.IO.FileAttributes]::ReparsePoint) {
      Die 'archive_member_unsafe' ('archive extraction contains a reparse point: ' + $file.FullName)
    }
    $relative = $file.FullName.Substring($Root.Length).TrimStart('\', '/').Replace('\', '/')
    $actual[$relative.ToLowerInvariant()] = $relative
  }
  if ($actual.Count -ne $expected.Count) {
    Die 'archive_inventory_mismatch' 'archive members differ from files.json'
  }
  foreach ($key in $expected.Keys) {
    if (-not $actual.ContainsKey($key)) {
      Die 'archive_inventory_mismatch' ('archive member is missing: ' + $expected[$key].name)
    }
    $path = Join-Path $Root ($expected[$key].name.Replace('/', [System.IO.Path]::DirectorySeparatorChar))
    if ((Sha256 $path) -cne [string]$expected[$key].sha256) {
      Die 'archive_inventory_mismatch' ('archive member digest differs: ' + $expected[$key].name)
    }
  }
}

function Get-RuntimeDoctor([string]$Generation) {
  $runtime = Runtime-Python $Generation
  Assert-RegularFile $runtime 'runtime_python_missing' 'the installed runtime Python'
  $program = @'
import json
from description_pipeline.runtime import tool_record
from description_pipeline.solidworks import doctor
print(json.dumps({
    "tool": tool_record(role="native"),
    "doctor": doctor(),
}, sort_keys=True, separators=(",", ":")))
'@
  $capture = Invoke-CapturedProcess $runtime @('-I', '-B', '-c', $program)
  $document = Get-LastJsonLine $capture
  if ($capture.exit_code -ne 0 -or $null -eq $document) {
    Die 'runtime_doctor_failed' 'the installed runtime refused its doctor check' @{
      output = Protect-DiagnosticText $capture.text
    }
  }
  return $document
}

function New-EndpointVirtualEnvironment([string]$BasePython, [string]$Generation) {
  $capture = Invoke-CapturedProcess $BasePython @('-m', 'venv', (Join-Path $Generation 'runtime'))
  if ($capture.exit_code -ne 0) {
    Die 'venv_failed' 'the bound Python could not create the runtime virtual environment' @{
      output = Protect-DiagnosticText $capture.text
    }
  }
}

function Install-EndpointRequirements([string]$Generation) {
  $runtime = Runtime-Python $Generation
  Assert-RegularFile $runtime 'runtime_python_missing' 'the staged runtime Python'
  $capture = Invoke-CapturedProcess $runtime @(
    '-I', '-B', '-m', 'pip', 'install', '--no-index', '--require-hashes',
    '--find-links', (Join-Path $Generation 'wheels'),
    '-r', (Join-Path $Generation 'requirements.lock'))
  if ($capture.exit_code -ne 0) {
    Die 'pip_failed' 'the hash-locked Windows runtime install failed' @{
      output = Protect-DiagnosticText $capture.text
    }
  }
  $capture = Invoke-CapturedProcess $runtime @('-I', '-B', '-m', 'pip', 'check')
  if ($capture.exit_code -ne 0) {
    Die 'runtime_dependency_conflict' 'the installed Windows runtime is not dependency-consistent' @{
      output = Protect-DiagnosticText $capture.text
    }
  }
}

function Read-EndpointConfigOfficial([string]$Generation) {
  $runtime = Runtime-Python $Generation
  $program = @'
import json, sys
from description_pipeline.orchestration import windows
config = windows.read_config(sys.argv[1])
print(json.dumps({
    "config_digest": config["config_digest"],
    "package_root": str(config["package_root"]),
    "output_root": str(config["output_root"]),
    "state_root": str(config["state_root"]),
    "host": config["host"],
    "port": config["port"],
}, sort_keys=True, separators=(",", ":")))
'@
  $capture = Invoke-CapturedProcess $runtime @(
    '-I', '-B', '-c', $program, (Config-Path $Generation))
  $document = Get-LastJsonLine $capture
  if ($capture.exit_code -ne 0 -or $null -eq $document) {
    Die 'endpoint_config_invalid' 'the installed release rejected endpoint.json' @{
      output = Protect-DiagnosticText $capture.text
    }
  }
  return $document
}

function Assert-ReleaseRuntime($Release, $Doctor) {
  if ($null -eq $Doctor.tool -or $null -eq $Doctor.doctor) {
    Die 'runtime_identity_mismatch' 'the runtime doctor returned no identity'
  }
  if ($Doctor.doctor.passed -isnot [bool] -or $Doctor.doctor.passed -ne $true) {
    Die 'runtime_doctor_failed' 'one or more native runtime checks failed'
  }
  $embedded = $Doctor.tool.release
  if ($null -eq $embedded) {
    Die 'runtime_identity_mismatch' 'the installed package has no release identity'
  }
  if ([string]$Doctor.tool.version -cne [string]$Release.version -or
      [string]$Doctor.tool.source_sha256 -cne [string]$Release.source_sha256 -or
      [string]$embedded.source_commit -cne [string]$Release.source_commit -or
      [string]$embedded.source_sha256 -cne [string]$Release.source_sha256 -or
      [string]$Doctor.tool.runtime.role -cne 'native' -or
      [string]$Doctor.tool.runtime.system -cne 'Windows' -or
      [string]$Doctor.tool.runtime.python -notmatch '^3\.12(?:\.|$)') {
    Die 'runtime_identity_mismatch' 'the installed runtime differs from release.json'
  }
  return [ordered]@{
    version = [string]$Doctor.tool.version
    source_commit = [string]$embedded.source_commit
    source_sha256 = [string]$Doctor.tool.source_sha256
    role = 'native'
    python = [string]$Doctor.tool.runtime.python
    packages = $Doctor.tool.runtime.packages
  }
}

function New-InstallIdIfNeeded([string]$Path) {
  if (Test-Path -LiteralPath $Path) {
    Assert-RegularFile $Path 'install_id_invalid' 'install-id'
    $value = (Read-Utf8Text $Path).Trim()
    if ($value -cnotmatch '^win-[0-9a-f]{32}$') {
      Die 'install_id_invalid' 'install-id is malformed'
    }
    return @{value=$value; created=$false}
  }
  $value = 'win-' + [guid]::NewGuid().ToString('N')
  $parent = Split-Path -Parent $Path
  if (-not (Test-Path -LiteralPath $parent -PathType Container)) {
    $null = New-Item -ItemType Directory -Path $parent -Force
  }
  $partial = Join-Path $parent ('.install-id.' + [guid]::NewGuid().ToString('N') + '.partial')
  try {
    Write-Utf8NoBom $partial ($value + "`n")
    Move-Item -LiteralPath $partial -Destination $Path
  } finally {
    if (Test-Path -LiteralPath $partial) {
      Remove-Item -LiteralPath $partial -Force -ErrorAction SilentlyContinue
    }
  }
  return @{value=$value; created=$true}
}

function Read-InstallId([string]$Path) {
  Assert-RegularFile $Path 'install_id_missing' 'install-id'
  $value = (Read-Utf8Text $Path).Trim()
  if ($value -cnotmatch '^win-[0-9a-f]{32}$') {
    Die 'install_id_invalid' 'install-id is malformed'
  }
  return $value
}

function Assert-InstallStatePair([string]$InstallIdPath, [string]$ReceiptPath) {
  $hasInstallId = Test-Path -LiteralPath $InstallIdPath
  $hasReceipt = Test-Path -LiteralPath $ReceiptPath
  if ($hasInstallId -ne $hasReceipt) {
    Die 'install_state_incomplete' (
      'install-id and install-receipt.json must both exist or both be absent') @{
      install_id_present = [bool]$hasInstallId
      receipt_present = [bool]$hasReceipt
    }
  }
  return [ordered]@{
    install_id_present = [bool]$hasInstallId
    receipt_present = [bool]$hasReceipt
  }
}

function Get-BoundTask($Binding) {
  $tasks = @(Get-ScheduledTask -ErrorAction Stop)
  return @($tasks | Where-Object {
    [string]::Equals(
      [string]$_.TaskName,
      [string]$Binding.task_name,
      [System.StringComparison]::OrdinalIgnoreCase) -and
    [string]::Equals(
      [string]$_.TaskPath,
      [string]$Binding.task_path,
      [System.StringComparison]::OrdinalIgnoreCase)
  })
}

function Get-RequiredTask($Binding) {
  $tasks = @(Get-BoundTask $Binding)
  if ($tasks.Count -eq 0) {
    Die 'task_missing' 'the managed scheduled task is not registered'
  }
  if ($tasks.Count -ne 1) {
    Die 'task_ambiguous' ('expected exactly one managed task, found ' + $tasks.Count)
  }
  return $tasks[0]
}

function Get-TaskFilePath($Task) {
  $root = Join-Path $env:SystemRoot 'System32\Tasks'
  $relative = ([string]$Task.TaskPath).TrimStart('\') + [string]$Task.TaskName
  $path = [System.IO.Path]::GetFullPath((Join-Path $root $relative))
  $prefix = [System.IO.Path]::GetFullPath($root) + [System.IO.Path]::DirectorySeparatorChar
  if (-not $path.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)) {
    Die 'task_path_invalid' 'the scheduled task file escapes the Task Scheduler root'
  }
  return $path
}

function Get-TaskProjection($Task) {
  if (@($Task.Actions).Count -ne 1) {
    Die 'task_action_invalid' 'the task must carry exactly one action'
  }
  $principalSid = Resolve-Sid ([string]$Task.Principal.UserId)
  $xml = [xml](Export-ScheduledTask -TaskName $Task.TaskName -TaskPath $Task.TaskPath)
  $xmlText = [string]$xml.OuterXml
  if (@($xml.SelectNodes(
      '//*[local-name()="Password" and string-length(normalize-space(.)) > 0]')).Count -ne 0) {
    Die 'task_secret_exposure_refused' 'the task XML contains a password'
  }
  $action = [ordered]@{
    execute = [string]$Task.Actions[0].Execute
    arguments = [string]$Task.Actions[0].Arguments
    working_directory = [string]$Task.Actions[0].WorkingDirectory
  }
  $principal = [ordered]@{
    user = [string]$Task.Principal.UserId
    sid = $principalSid
    logon_type = [string]$Task.Principal.LogonType
    run_level = [string]$Task.Principal.RunLevel
  }
  $settings = [ordered]@{
    multiple_instances = [string]$Task.Settings.MultipleInstances
  }
  $taskFileAcl = Get-AclProjection (Get-TaskFilePath $Task)
  $semantic = [ordered]@{
    task_path = [string]$Task.TaskPath
    task_name = [string]$Task.TaskName
    principal = $principal
    action = $action
    settings = $settings
    triggers_sha256 = Get-JsonSha256 @($Task.Triggers)
    security_descriptor_sha256 = Get-JsonSha256 $taskFileAcl
  }
  return [ordered]@{
    task_path = [string]$Task.TaskPath
    task_name = [string]$Task.TaskName
    state = [string]$Task.State
    xml = $xmlText
    xml_sha256 = Get-TextSha256 $xmlText
    semantic_sha256 = Get-JsonSha256 $semantic
    principal = $principal
    action = $action
    settings = $settings
    triggers_sha256 = $semantic.triggers_sha256
    security_descriptor_sha256 = $semantic.security_descriptor_sha256
  }
}

function Assert-TaskProjection(
    $Projection,
    $Binding) {
  $execute = [string]$Projection.action.execute
  $scripts = Split-Path -Parent $execute
  $runtime = Split-Path -Parent $scripts
  $generation = Split-Path -Parent $runtime
  $stateRoot = Split-Path -Parent (Split-Path -Parent $generation)
  if (-not $generation -or -not $stateRoot) {
    Die 'task_identity_mismatch' 'task action does not identify a managed generation'
  }
  if ($Projection.task_name -cne [string]$Binding.task_name -or
      $Projection.task_path -cne [string]$Binding.task_path) {
    Die 'task_identity_mismatch' 'task name or path differs from the binding'
  }
  if ((Resolve-Sid ([string]$Projection.principal.user)) -cne
      (Resolve-Sid ([string]$Binding.service_account))) {
    Die 'task_identity_mismatch' 'task principal differs from service_account'
  }
  if ([string]$Projection.principal.run_level -notin @('Highest', 'HighestAvailable') -or
      [string]$Projection.principal.logon_type -notin @('Interactive', 'InteractiveToken')) {
    Die 'task_identity_mismatch' 'task must run interactively at highest privilege'
  }
  if ([string]$Projection.settings.multiple_instances -cne 'IgnoreNew') {
    Die 'task_identity_mismatch' 'task must enforce a single instance'
  }
  if (-not (Test-Runtime-LauncherAction `
      -Execute ([string]$Projection.action.execute) `
      -Arguments ([string]$Projection.action.arguments) `
      -Generation $generation)) {
    $expectedExecute = Runtime-Python $generation
    $expectedArguments = Runtime-LauncherArguments $generation
    Die 'task_identity_mismatch' 'task action differs from the managed launcher' @{
      execute = [string]$Projection.action.execute
      expected_execute = $expectedExecute
      execute_equal = (
        (Normalize-PathIdentity ([string]$Projection.action.execute)) -ceq
        (Normalize-PathIdentity $expectedExecute))
      arguments = [string]$Projection.action.arguments
      expected_arguments = $expectedArguments
      arguments_equal = (
        [string]$Projection.action.arguments -ceq $expectedArguments)
    }
  }
  if ((Normalize-PathIdentity ([string]$Projection.action.working_directory)) -cne
      (Normalize-PathIdentity $stateRoot)) {
    Die 'task_identity_mismatch' 'task working directory differs from state_root'
  }
}

function Register-DisabledTask($Binding, [string]$Generation) {
  $action = New-ScheduledTaskAction `
    -Execute (Runtime-Python $Generation) `
    -Argument (Runtime-LauncherArguments $Generation) `
    -WorkingDirectory (Split-Path -Parent (Split-Path -Parent $Generation))
  $principal = New-ScheduledTaskPrincipal `
    -UserId ([string]$Binding.service_account) `
    -LogonType Interactive `
    -RunLevel Highest
  $settings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries
  Register-ScheduledTask `
    -TaskName ([string]$Binding.task_name) `
    -TaskPath ([string]$Binding.task_path) `
    -Action $action `
    -Principal $principal `
    -Settings $settings `
    -Description 'URDF endpoint lifecycle task' | Out-Null
  Disable-ScheduledTask `
    -TaskName ([string]$Binding.task_name) `
    -TaskPath ([string]$Binding.task_path) | Out-Null
}

function Remove-ManagedTask($Binding) {
  Unregister-ScheduledTask `
    -TaskName ([string]$Binding.task_name) `
    -TaskPath ([string]$Binding.task_path) `
    -Confirm:$false | Out-Null
}

function Read-InstallReceipt([string]$Path) {
  Assert-RegularFile $Path 'install_receipt_missing' 'install-receipt.json'
  $document = Read-Json $Path
  if ($document.schema_version -cne $script:ReceiptSchema -or
      $document.passed -isnot [bool] -or $document.passed -ne $true) {
    Die 'install_receipt_invalid' 'install-receipt.json is incomplete or unsupported'
  }
  foreach ($field in @(
      'install_id',
      'binding_path',
      'binding_sha256',
      'lifecycle_path',
      'lifecycle_sha256',
      'bootstrap_path',
      'bootstrap_sha256',
      'endpoint_config_sha256',
      'token_path',
      'token_sha256',
      'endpoint_root',
      'config_path',
      'config_sha256',
      'config_digest',
      'release_manifest_path',
      'release_manifest_sha256',
      'archive_name',
      'archive_sha256',
      'version',
      'source_commit',
      'source_tree',
      'source_sha256',
      'task_name',
      'task_path',
      'launch_contract',
      'runtime_python',
      'runtime_python_sha256',
      'base_python',
      'base_python_sha256')) {
    $value = $document.$field
    if ($value -isnot [string] -or [string]::IsNullOrWhiteSpace($value)) {
      Die 'install_receipt_invalid' ('install-receipt.json misses ' + $field)
    }
  }
  if ([string]$document.install_id -cnotmatch '^win-[0-9a-f]{32}$' -or
      [string]$document.config_digest -cnotmatch '^[0-9a-f]{64}$') {
    Die 'install_receipt_invalid' 'install-receipt.json identity is malformed'
  }
  return $document
}

function Assert-InstallReceipt($Receipt, $Binding, $Release, $Config, $InstallId) {
  if ($Receipt.install_id -cne $InstallId -or
      (Normalize-PathIdentity ([string]$Receipt.binding_path)) -cne
        (Get-ControlPaths $Config.state_root).binding -or
      $Receipt.endpoint_config_sha256 -cne $Config.source_sha256 -or
      $Receipt.token_path -cne $Config.token_file -or
      $Receipt.release_manifest_sha256 -cne $Release.sha256 -or
      $Receipt.archive_name -cne $Release.archive_name -or
      $Receipt.archive_sha256 -cne $Release.archive_sha256 -or
      $Receipt.version -cne $Release.version -or
      $Receipt.source_commit -cne $Release.source_commit -or
      $Receipt.source_tree -cne $Release.source_tree -or
      $Receipt.source_sha256 -cne $Release.source_sha256 -or
      $Receipt.task_name -cne $Binding.task_name -or
      $Receipt.task_path -cne $Binding.task_path -or
      $Receipt.launch_contract -cne $script:PythonLaunchContract) {
    Die 'install_receipt_mismatch' 'install-receipt.json differs from the bound inputs'
  }
}

function Get-ListenerConnections([int]$Port) {
  # MSFT_NetTCPConnection State 2 is Listen; no matches return an empty array.
  return @(Get-CimInstance `
    -Namespace 'root/StandardCimv2' `
    -ClassName 'MSFT_NetTCPConnection' `
    -Filter "LocalPort = $Port AND State = 2" `
    -ErrorAction Stop)
}

function Get-ProcessOwnerSid($Process) {
  try {
    $owner = Invoke-CimMethod -InputObject $Process -MethodName GetOwnerSid -ErrorAction Stop
  } catch {
    throw 'the process owner SID is unavailable'
  }
  if ([int]$owner.ReturnValue -ne 0 -or
      [string]::IsNullOrWhiteSpace([string]$owner.Sid)) {
    throw 'the process owner SID is unavailable'
  }
  return [string]$owner.Sid
}

function Get-ProcessLineageEvidence($Process) {
  if ($null -eq $Process -or [int]$Process.ProcessId -le 0) {
    throw 'the process lineage node is missing'
  }
  $image = [string]$Process.ExecutablePath
  if ([string]::IsNullOrWhiteSpace($image)) {
    throw 'the process lineage node has no executable'
  }
  try {
    $started = ([datetime]$Process.CreationDate).ToUniversalTime().ToString('o')
  } catch {
    throw 'the process lineage node has no creation time'
  }
  return [ordered]@{
    pid = [int]$Process.ProcessId
    parent_pid = [int]$Process.ParentProcessId
    image = Normalize-PathIdentity $image
    start_utc = $started
    user_sid = Get-ProcessOwnerSid $Process
    session_id = [int]$Process.SessionId
  }
}

function Get-CommandArgumentsForExecutable([string]$CommandLine, [string]$ExecutablePath) {
  $quoted = '"' + $ExecutablePath + '"'
  $offset = -1
  if ($CommandLine.StartsWith($quoted, [System.StringComparison]::OrdinalIgnoreCase)) {
    $offset = $quoted.Length
  } elseif ($CommandLine.StartsWith(
      $ExecutablePath,
      [System.StringComparison]::OrdinalIgnoreCase)) {
    $offset = $ExecutablePath.Length
  }
  if ($offset -lt 0 -or
      ($offset -lt $CommandLine.Length -and -not [char]::IsWhiteSpace($CommandLine[$offset]))) {
    throw 'the process command line does not begin with its executable'
  }
  return $CommandLine.Substring($offset).Trim()
}

function Read-PyvenvRuntime([string]$RuntimeRoot) {
  $path = Join-Path $RuntimeRoot 'pyvenv.cfg'
  Assert-RegularFile $path 'venv_identity_missing' 'pyvenv.cfg'
  $fields = @{}
  foreach ($line in @((Read-Utf8Text $path).TrimStart([char]0xFEFF) -split '\r?\n')) {
    $trimmed = $line.Trim()
    if (-not $trimmed -or $trimmed.StartsWith('#')) { continue }
    $separator = $trimmed.IndexOf('=')
    if ($separator -lt 1) {
      throw 'the generation pyvenv.cfg has an invalid line'
    }
    $key = $trimmed.Substring(0, $separator).Trim().ToLowerInvariant()
    if ($fields.ContainsKey($key)) {
      throw ('the generation pyvenv.cfg repeats ' + $key)
    }
    $fields[$key] = $trimmed.Substring($separator + 1).Trim()
  }
  foreach ($key in @('home', 'include-system-site-packages', 'version', 'executable', 'command')) {
    if ([string]::IsNullOrWhiteSpace([string]$fields[$key])) {
      throw ('the generation pyvenv.cfg misses ' + $key)
    }
  }
  if ([string]$fields['include-system-site-packages'] -cne 'false') {
    throw 'the generation virtual environment includes system site packages'
  }
  $venvHome = Normalize-PathIdentity ([string]$fields['home'])
  $base = Normalize-PathIdentity ([string]$fields['executable'])
  if (-not $base.StartsWith(
        $venvHome + '/',
        [System.StringComparison]::OrdinalIgnoreCase) -or
      $base -notmatch '(?i)/python\.exe$') {
    throw 'the generation base interpreter differs from pyvenv home'
  }
  Assert-RegularFile ([string]$fields['executable']) 'venv_identity_missing' 'the base interpreter'
  return [ordered]@{
    path = Normalize-PathIdentity $path
    sha256 = Sha256 $path
    home = $venvHome
    version = [string]$fields['version']
    base_interpreter_path = $base
    base_interpreter_sha256 = Sha256 ([string]$fields['executable'])
  }
}

function Test-EndpointSessionIdentity(
    [int]$SchedulerSession,
    [int]$LauncherSession,
    [int]$ListenerSession) {
  return (
    $SchedulerSession -eq 0 -and
    $LauncherSession -gt 0 -and
    $ListenerSession -gt 0 -and
    $LauncherSession -eq $ListenerSession
  )
}

function Test-ProcessCreationOrder(
    [string]$SchedulerStartUtc,
    [string]$LauncherStartUtc,
    [string]$ListenerStartUtc) {
  $times = @()
  foreach ($value in @(
      $SchedulerStartUtc,
      $LauncherStartUtc,
      $ListenerStartUtc)) {
    if ([string]::IsNullOrWhiteSpace([string]$value)) {
      return $false
    }
    try {
      $parsed = [datetime]::Parse(
        [string]$value,
        [System.Globalization.CultureInfo]::InvariantCulture,
        [System.Globalization.DateTimeStyles]::RoundtripKind)
    } catch {
      return $false
    }
    if ($parsed.Kind -ne [System.DateTimeKind]::Utc) {
      return $false
    }
    $times += $parsed
  }
  return (
    $times[0] -le $times[1] -and
    $times[1] -le $times[2]
  )
}

function Get-ListenerEvidence($Binding, $Projection, $Config) {
  try {
    $connections = @(Get-ListenerConnections ([int]$Config.port))
    $processes = @(Get-CimInstance Win32_Process -ErrorAction Stop)
    $services = @(Get-CimInstance Win32_Service -Filter "Name='Schedule'" -ErrorAction Stop)
  } catch {
    DieUnknown 'windows_observer_failed' 'the listener process table could not be observed'
  }
  $pids = @($connections | ForEach-Object { [int]$_.OwningProcess } | Sort-Object -Unique)
  if ($pids.Count -ne 1 -or [int]$pids[0] -le 0) {
    Die 'windows_listener_ambiguous' (
      'expected exactly one endpoint listener, found ' + $pids.Count)
  }
  $listenerPid = [int]$pids[0]
  $listenerMatches = @($processes | Where-Object { [int]$_.ProcessId -eq $listenerPid })
  if ($listenerMatches.Count -ne 1) {
    Die 'windows_listener_identity_mismatch' 'the endpoint listener is missing'
  }
  $listenerProcess = $listenerMatches[0]
  $action = $Projection.action
  $launchers = @()
  foreach ($candidate in $processes) {
    if ((Normalize-PathIdentity ([string]$candidate.ExecutablePath)) -ine
        (Normalize-PathIdentity ([string]$action.execute))) {
      continue
    }
    try {
      if ((Get-CommandArgumentsForExecutable `
          ([string]$candidate.CommandLine) `
          ([string]$action.execute)) -ceq [string]$action.arguments) {
        $launchers += $candidate
      }
    } catch { }
  }
  if ($launchers.Count -ne 1) {
    Die 'windows_listener_ambiguous' (
      'expected exactly one task-action launcher, found ' + $launchers.Count)
  }
  try {
    $listener = Get-ProcessLineageEvidence $listenerProcess
    $launcher = Get-ProcessLineageEvidence $launchers[0]
    $listenerArguments = Get-CommandArgumentsForExecutable `
      ([string]$listenerProcess.CommandLine) ([string]$listenerProcess.ExecutablePath)
  } catch {
    Die 'windows_listener_identity_mismatch' $_.Exception.Message
  }
  if ($listener.pid -eq $launcher.pid -or
      $listener.parent_pid -ne $launcher.pid -or
      $listenerArguments -cne [string]$action.arguments) {
    Die 'windows_listener_identity_mismatch' 'listener is not the direct task launcher child'
  }
  $schedulerMatches = @($processes | Where-Object {
    [int]$_.ProcessId -eq [int]$launcher.parent_pid
  })
  if ($schedulerMatches.Count -ne 1) {
    Die 'windows_listener_identity_mismatch' 'launcher has no exact Task Scheduler parent'
  }
  try {
    $scheduler = Get-ProcessLineageEvidence $schedulerMatches[0]
  } catch {
    Die 'windows_listener_identity_mismatch' $_.Exception.Message
  }
  $schedulerImage = Normalize-PathIdentity (
    Join-Path ([string]$env:SystemRoot) 'System32\svchost.exe')
  if ($scheduler.image -ine $schedulerImage -or
      $scheduler.user_sid -cne 'S-1-5-18' -or
      $scheduler.session_id -ne 0 -or
      $services.Count -ne 1 -or
      [string]$services[0].Name -cne 'Schedule' -or
      [string]$services[0].State -cne 'Running' -or
      [int]$services[0].ProcessId -ne [int]$scheduler.pid) {
    Die 'windows_listener_identity_mismatch' 'launcher parent is not the running Schedule service'
  }
  $venv = $null
  try {
    $venv = Read-PyvenvRuntime (Split-Path -Parent (Split-Path -Parent ([string]$action.execute)))
  } catch {
    Die 'windows_listener_identity_mismatch' $_.Exception.Message
  }
  if ($listener.image -ine [string]$venv.base_interpreter_path -or
      $launcher.user_sid -cne [string]$Projection.principal.sid -or
      $listener.user_sid -cne [string]$Projection.principal.sid -or
      -not (Test-ProcessCreationOrder `
        ([string]$scheduler.start_utc) `
        ([string]$launcher.start_utc) `
        ([string]$listener.start_utc)) -or
      -not (Test-EndpointSessionIdentity `
        -SchedulerSession ([int]$scheduler.session_id) `
        -LauncherSession ([int]$launcher.session_id) `
        -ListenerSession ([int]$listener.session_id))) {
    Die 'windows_listener_identity_mismatch' (
      'process account, interpreter, session, or creation order differs')
  }
  return [ordered]@{
    schema_version = 'solidworks-to-urdf.windows-listener/v2'
    relation = 'direct_venv_child'
    process_arguments_sha256 = Get-TextSha256 ([string]$action.arguments)
    scheduler_service_name = [string]$services[0].Name
    scheduler_service_state = [string]$services[0].State
    scheduler_service_process_id = [int]$services[0].ProcessId
    scheduler_pid = [int]$scheduler.pid
    scheduler_image = [string]$scheduler.image
    scheduler_start_utc = [string]$scheduler.start_utc
    scheduler_user_sid = [string]$scheduler.user_sid
    scheduler_session_id = [int]$scheduler.session_id
    launcher_pid = [int]$launcher.pid
    launcher_parent_pid = [int]$launcher.parent_pid
    launcher_image = [string]$launcher.image
    launcher_image_sha256 = Sha256 ([string]$action.execute)
    launcher_start_utc = [string]$launcher.start_utc
    launcher_user_sid = [string]$launcher.user_sid
    launcher_session_id = [int]$launcher.session_id
    listener_pid = [int]$listener.pid
    listener_parent_pid = [int]$listener.parent_pid
    listener_start_utc = [string]$listener.start_utc
    listener_user_sid = [string]$listener.user_sid
    listener_session_id = [int]$listener.session_id
    venv_config_path = [string]$venv.path
    venv_config_sha256 = [string]$venv.sha256
    venv_home = [string]$venv.home
    venv_version = [string]$venv.version
    base_interpreter_path = [string]$venv.base_interpreter_path
    base_interpreter_sha256 = [string]$venv.base_interpreter_sha256
  }
}

function Read-EndpointHealth($Config, [int]$TimeoutSeconds = 30) {
  $token = (Read-Utf8Text ([string]$Config.token_file)).Trim()
  $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
  $lastError = ''
  do {
    try {
      $health = Invoke-RestMethod `
        -Uri ('http://127.0.0.1:' + [int]$Config.port + '/health') `
        -Headers @{Authorization=('Bearer ' + $token)} `
        -TimeoutSec 3
      return $health
    } catch {
      $lastError = $_.Exception.Message
      if ((Get-Date) -lt $deadline) { Start-Sleep -Milliseconds 500 }
    }
  } while ((Get-Date) -lt $deadline)
  Die 'endpoint_unhealthy' 'the endpoint did not answer /health within the startup window' @{
    error = Protect-DiagnosticText $lastError
  }
}

function Test-NonNegativeInt($Value) {
  return (($Value -is [int] -or $Value -is [long]) -and [long]$Value -ge 0)
}

function Assert-HealthIdentity($Health, $Identity) {
  if ($null -eq $Health -or
      [string]$Health.schema_version -cne 'solidworks-to-urdf.health/v1' -or
      [string]$Health.pipeline_id -cne 'solidworks-to-urdf' -or
      $Health.native_platform -isnot [bool] -or $Health.native_platform -ne $true -or
      $Health.ready -isnot [bool] -or
      [string]$Health.install_id -cne [string]$Identity.install_id -or
      [string]$Health.config_digest -cne [string]$Identity.config_digest -or
      [int]$Health.endpoint.port -ne [int]$Identity.port -or
      [string]$Health.release.role -cne 'native' -or
      [string]$Health.release.version -cne [string]$Identity.release.version -or
      [string]$Health.release.source_commit -cne [string]$Identity.release.source_commit -or
      [string]$Health.release.source_sha256 -cne [string]$Identity.release.source_sha256) {
    Die 'health_identity_mismatch' 'endpoint /health differs from the installed identity'
  }
  if ([string]$Health.target_identity.install_id -cne [string]$Identity.install_id -or
      [string]$Health.target_identity.pipeline_id -cne 'solidworks-to-urdf') {
    Die 'health_identity_mismatch' 'health target_identity differs from install identity'
  }
  foreach ($field in @('active', 'queued', 'unresolved')) {
    if (-not (Test-NonNegativeInt $Health.jobs.$field)) {
      Die 'health_identity_mismatch' ('health jobs.' + $field + ' is invalid')
    }
  }
  if ($Health.cad.external_session_detected -isnot [bool] -or
      -not (Test-NonNegativeInt $Health.cad.owned_sessions) -or
      $Health.cad.lease_held -isnot [bool]) {
    Die 'health_identity_mismatch' 'health CAD facts are invalid'
  }
}

function Get-InstalledState($Binding, [switch]$SkipDoctor, [switch]$SkipPins) {
  $release = Read-ReleaseManifest $Binding
  $sourceConfig = Read-EndpointConfigStrict $Binding
  Assert-EndpointRootsSeparate $sourceConfig
  $installIdPath = Join-Path $sourceConfig.state_root 'install-id'
  $installId = Read-InstallId $installIdPath
  $generation = Generation-Path $sourceConfig.state_root
  $configPath = Config-Path $generation
  Assert-RegularFile $configPath 'endpoint_config_missing' 'the installed endpoint configuration'
  if ((Sha256 $configPath) -cne [string]$sourceConfig.source_sha256) {
    Die 'endpoint_config_mismatch' 'the installed endpoint.json differs from endpoint_config'
  }
  $task = Get-RequiredTask $Binding
  $projection = Get-TaskProjection $task
  Assert-TaskProjection `
    -Projection $projection `
    -Binding $Binding
  $official = Read-EndpointConfigOfficial $generation
  if ((Normalize-PathIdentity ([string]$official.state_root)) -cne $sourceConfig.state_root -or
      (Normalize-PathIdentity ([string]$official.package_root)) -cne $sourceConfig.package_root -or
      (Normalize-PathIdentity ([string]$official.output_root)) -cne $sourceConfig.output_root -or
      [int]$official.port -ne [int]$sourceConfig.port -or
      [string]$official.config_digest -cnotmatch '^[0-9a-f]{64}$') {
    Die 'endpoint_config_identity_mismatch' 'the installed config reader disagrees with endpoint.json'
  }
  $receiptPath = Join-Path $sourceConfig.state_root 'install-receipt.json'
  $receipt = Read-InstallReceipt $receiptPath
  Assert-InstallReceipt $receipt $Binding $release $sourceConfig $installId
  $control = Assert-ControlFiles $Binding $receipt $sourceConfig.state_root
  if ((Normalize-PathIdentity ([string]$receipt.endpoint_root)) -cne
        (Normalize-PathIdentity $generation) -or
      (Normalize-PathIdentity ([string]$receipt.config_path)) -cne
        (Normalize-PathIdentity $configPath) -or
      [string]$receipt.config_sha256 -cne (Sha256 $configPath) -or
      [string]$receipt.config_digest -cne [string]$official.config_digest -or
      (Normalize-PathIdentity ([string]$receipt.release_manifest_path)) -cne
        (Normalize-PathIdentity ([string]$release.path)) -or
      (Normalize-PathIdentity ([string]$receipt.endpoint_config)) -cne
        (Normalize-PathIdentity ([string]$sourceConfig.source_path)) -or
      (Normalize-PathIdentity ([string]$receipt.token_path)) -cne
        (Normalize-PathIdentity ([string]$sourceConfig.token_file)) -or
      [string]$receipt.token_sha256 -cne (Sha256 ([string]$sourceConfig.token_file)) -or
      (Normalize-PathIdentity ([string]$receipt.runtime_python)) -cne
        (Normalize-PathIdentity (Runtime-Python $generation)) -or
      [string]$receipt.runtime_python_sha256 -cne
        (Sha256 (Runtime-Python $generation)) -or
      (Normalize-PathIdentity ([string]$receipt.base_python)) -cne
        (Normalize-PathIdentity ([string]$Binding.python_executable)) -or
      [string]$receipt.base_python_sha256 -cne
        (Sha256 ([string]$Binding.python_executable)) -or
      [string]$receipt.task_xml_sha256 -cne [string]$projection.xml_sha256 -or
      [string]$receipt.task_semantic_sha256 -cne [string]$projection.semantic_sha256 -or
      [string]$receipt.task_acl_sha256 -cne [string]$projection.security_descriptor_sha256) {
    Die 'install_receipt_mismatch' 'install-receipt.json does not describe the current install'
  }
  $serviceSid = Resolve-Sid ([string]$Binding.service_account)
  $stableManagedPaths = @(Get-StableManagedPaths `
    $sourceConfig $Binding $generation $receiptPath)
  foreach ($path in $stableManagedPaths) {
    Assert-ProtectedAcl $path $serviceSid
  }
  Assert-ProtectedAcl (Get-TaskFilePath $task) $serviceSid
  $runtimeRelease = [ordered]@{
    version = [string]$release.version
    source_commit = [string]$release.source_commit
    source_sha256 = [string]$release.source_sha256
  }
  if (-not $SkipDoctor) {
    $doctor = Get-RuntimeDoctor $generation
    $runtimeRelease = Assert-ReleaseRuntime $release $doctor
  }
  $pins = Get-DomainPins $sourceConfig.state_root $generation
  if (-not $SkipPins) {
    Assert-DomainPins $pins
  }
  return [ordered]@{
    task = $task
    projection = $projection
    release = $runtimeRelease
    release_manifest = $release
    source_config = $sourceConfig
    config = $official
    generation = Normalize-PathIdentity $generation
    config_path = Normalize-PathIdentity $configPath
    config_sha256 = Sha256 $configPath
    install_id = $installId
    install_id_path = Normalize-PathIdentity $installIdPath
    receipt = $receipt
    receipt_path = Normalize-PathIdentity $receiptPath
    control = $control
    token_sha256 = Sha256 ([string]$sourceConfig.token_file)
    port = [int]$sourceConfig.port
    pins = $pins
  }
}

function Get-IdentityDocument($State) {
  return [ordered]@{
    install_id = [string]$State.install_id
    endpoint_root = [string]$State.generation
    config_path = [string]$State.config_path
    config_sha256 = [string]$State.config_sha256
    config_digest = [string]$State.config.config_digest
    package_root = [string]$State.source_config.package_root
    output_root = [string]$State.source_config.output_root
    state_root = [string]$State.source_config.state_root
    token_path = [string]$State.source_config.token_file
    token_sha256 = [string]$State.token_sha256
    port = [int]$State.port
    release = [ordered]@{
      version = [string]$State.release.version
      source_commit = [string]$State.release.source_commit
      source_sha256 = [string]$State.release.source_sha256
      role = 'native'
    }
  }
}

function Get-TaskDocument($Projection) {
  return [ordered]@{
    task_name = [string]$Projection.task_name
    task_path = [string]$Projection.task_path
    state = [string]$Projection.state
    action = $Projection.action
    principal = $Projection.principal
    xml_sha256 = [string]$Projection.xml_sha256
    semantic_sha256 = [string]$Projection.semantic_sha256
    security_descriptor_sha256 = [string]$Projection.security_descriptor_sha256
  }
}

function Get-SolidWorksProcesses {
  try {
    return @(Get-CimInstance Win32_Process -Filter "Name='SLDWORKS.exe'" -ErrorAction Stop)
  } catch {
    DieUnknown 'windows_observer_failed' 'the SolidWorks process table could not be observed'
  }
}

function Get-WriterResidue($State) {
  try {
    $processes = @(Get-CimInstance Win32_Process -ErrorAction Stop)
  } catch {
    DieUnknown 'windows_observer_failed' 'the writer process table could not be observed'
  }
  return @($processes | Where-Object {
    $image = Normalize-PathIdentity ([string]$_.ExecutablePath)
    $image -and (
      (Test-PathWithin $image $State.generation) -or
      (Test-PathWithin $image $State.source_config.state_root)
    )
  })
}

function Assert-ListenerPortFree([int]$Port) {
  $connections = @(Get-ListenerConnections $Port)
  if ($connections.Count -ne 0) {
    Die 'listener_port_occupied' (
      'the endpoint port is already occupied by ' + $connections.Count + ' listener(s)')
  }
}

function Get-RunningEvidence($State, [switch]$AllowUnready) {
  if ([string]$State.projection.state -cne 'Running') {
    Die 'task_not_running' (
      'the endpoint task state is not Running: ' + [string]$State.projection.state)
  }
  $listener = Get-ListenerEvidence $State.task $State.projection $State.config
  $health = Read-EndpointHealth $State.config 10
  $identity = Get-IdentityDocument $State
  Assert-HealthIdentity $health $identity
  $identity.target_identity = $health.target_identity
  if (-not $AllowUnready -and
      ($health.ready -isnot [bool] -or $health.ready -ne $true)) {
    Die 'endpoint_unhealthy' 'endpoint /health is not ready'
  }
  return [ordered]@{
    identity = $identity
    listener = $listener
    health = $health
  }
}

function Assert-DrainSafety($State, $Running) {
  $health = $Running.health
  $jobs = [ordered]@{
    active = [long]$health.jobs.active
    queued = [long]$health.jobs.queued
    unresolved = [long]$health.jobs.unresolved
  }
  $cad = [ordered]@{
    external_session_detected = [bool]$health.cad.external_session_detected
    owned_sessions = [long]$health.cad.owned_sessions
    lease_held = [bool]$health.cad.lease_held
    solidworks_processes = @(Get-SolidWorksProcesses).Count
  }
  if ($jobs.active -ne 0 -or $jobs.queued -ne 0 -or $jobs.unresolved -ne 0) {
    Die 'drain_jobs_present' 'endpoint jobs are not all zero' @{jobs=$jobs}
  }
  if ($cad.external_session_detected -or $cad.owned_sessions -ne 0 -or
      $cad.lease_held -or $cad.solidworks_processes -ne 0) {
    Die 'drain_cad_not_idle' 'CAD session, lease, or process evidence is not idle' @{cad=$cad}
  }
  return [ordered]@{
    jobs = $jobs
    cad = $cad
    listener_pid = [int]$Running.listener.listener_pid
    task_state = [string]$State.projection.state
  }
}

function New-InstallCleanupReport {
  return [ordered]@{
    attempted = $true
    passed = $false
    errors = New-Object System.Collections.ArrayList
    steps = [ordered]@{}
  }
}

function Set-InstallCleanupStep(
    $Cleanup,
    [string]$Name,
    $Attempted,
    $Observed,
    $Passed,
    $Remaining,
    [string]$ErrorText = '',
    [string]$Reason = '') {
  if ($ErrorText) {
    [void]$Cleanup.errors.Add($Name + ': ' + (Protect-DiagnosticText $ErrorText))
  }
  $Cleanup.steps[$Name] = [ordered]@{
    attempted = $Attempted
    observed = $Observed
    passed = $Passed
    remaining = $Remaining
    error = Protect-DiagnosticText $ErrorText
    reason = Protect-DiagnosticText $Reason
  }
}

function Remove-OwnedInstallTask($Binding, [string]$Generation) {
  try {
    $tasks = @(Get-BoundTask $Binding)
    if ($tasks.Count -eq 0) {
      return @{observed=$true; passed=$true; remaining=$false; error=''}
    }
    if ($tasks.Count -ne 1) {
      return @{
        observed=$true
        passed=$false
        remaining=$true
        error='cleanup found an ambiguous managed task'
      }
    }
    $projection = Get-TaskProjection $tasks[0]
    if (-not (Test-Runtime-LauncherAction `
        -Execute ([string]$projection.action.execute) `
        -Arguments ([string]$projection.action.arguments) `
        -Generation $Generation)) {
      return @{
        observed=$true
        passed=$false
        remaining=$true
        error='cleanup refused a task that does not carry this install action'
      }
    }
    Remove-ManagedTask $Binding
    $remaining = @(Get-BoundTask $Binding).Count
    return @{
      observed=$true
      passed=($remaining -eq 0)
      remaining=($remaining -ne 0)
      error=$(if ($remaining -eq 0) { '' } else { 'the managed task remains after removal' })
    }
  } catch {
    $removeError = $_.Exception.Message
    try {
      $remaining = @(Get-BoundTask $Binding).Count
      return @{
        observed=$true
        passed=($remaining -eq 0)
        remaining=($remaining -ne 0)
        error=$(if ($remaining -eq 0) { '' } else { $removeError })
      }
    } catch {
      return @{
        observed=$false
        passed=$false
        remaining=$null
        error=($removeError + '; observer: ' + $_.Exception.Message)
      }
    }
  }
}

function Invoke-Install($Binding) {
  $script:LifecyclePhase = 'install-preflight'
  Assert-OperatorIsServiceAccount $Binding
  $release = Read-ReleaseManifest $Binding
  $config = Read-EndpointConfigStrict $Binding
  Assert-EndpointRootsSeparate $config
  $null = Assert-ControlBindingPath $Binding $config.state_root
  $null = Get-BasePythonIdentity $Binding
  $common = Get-ManagedCommonParent $config $Binding
  if (-not (Test-Path -LiteralPath $common -PathType Container)) {
    $null = New-Item -ItemType Directory -Path $common -Force
  }
  Assert-NoReparseAncestor $common
  $tasks = @(Get-BoundTask $Binding)
  if ($tasks.Count -ne 0) {
    Die 'task_exists' 'install requires an unused task name and path'
  }
  $lifecycleRoot = Join-Path $config.state_root 'lifecycle'
  $generation = Join-Path $lifecycleRoot 'generation'
  $pins = Get-DomainPins $config.state_root $generation
  Assert-DomainPins $pins
  if (Test-Path -LiteralPath $generation) {
    Die 'generation_exists' (
      'install requires a clean generation path; preserve the existing runtime: ' + $generation)
  }
  $receiptPath = Join-Path $config.state_root 'install-receipt.json'
  $installIdPath = Join-Path $config.state_root 'install-id'
  $installState = Assert-InstallStatePair $installIdPath $receiptPath
  $existingInstallId = $null
  if ($installState.install_id_present) {
    $existingInstallId = Read-InstallId $installIdPath
    $previousReceipt = Read-InstallReceipt $receiptPath
    Assert-InstallReceipt $previousReceipt $Binding $release $config $existingInstallId
    if ([string]$previousReceipt.binding_sha256 -cne
        (Sha256 ([string]$script:BindingSourcePath))) {
      Die 'install_receipt_mismatch' 'the restored binding differs from install-receipt.json'
    }
    if ([string]$previousReceipt.lifecycle_sha256 -cne
          (Sha256 ([string]$script:LifecycleScriptPath)) -or
        [string]$previousReceipt.bootstrap_sha256 -cne
          (Sha256 (Get-BootstrapScriptPath))) {
      Die 'control_version_mismatch' (
        'the lifecycle or bootstrap helper differs from install-receipt.json')
    }
  }
  $bootstrap = Invoke-ManagedRootBootstrap $common ([string]$Binding.service_account)
  $serviceSid = [string]$bootstrap.service_sid
  if ((Resolve-Sid ([string]$Binding.service_account)) -cne $serviceSid) {
    Die 'managed_root_bootstrap_mismatch' 'bootstrap resolved another service account'
  }
  $marker = Join-Path $common '.description-managed-root.json'
  if (-not (Test-Path -LiteralPath $marker -PathType Leaf)) {
    Die 'managed_root_bootstrap_mismatch' 'the managed-root marker is missing after bootstrap'
  }
  Assert-ProtectedAcl $common $serviceSid
  Assert-ProtectedAcl $marker $serviceSid
  $staging = Join-Path $lifecycleRoot (
    '.install-' + [guid]::NewGuid().ToString('N'))
  $createdInstallId = $false
  $taskRegistrationAttempted = $false
  $generationMoved = $false
  $previousPins = $null
  $controlFiles = $null
  $controlState = [ordered]@{files=[ordered]@{}}
  try {
    foreach ($path in @(
        $config.package_root,
        $config.output_root,
        $config.state_root,
        $lifecycleRoot)) {
      Assert-NoReparseAncestor $path
      if (-not (Test-Path -LiteralPath $path -PathType Container)) {
        $null = New-Item -ItemType Directory -Path $path -Force
      }
    }
    $installId = New-InstallIdIfNeeded $installIdPath
    $createdInstallId = [bool]$installId.created
    $script:LifecyclePhase = 'install-extract'
    Expand-ArchiveSafely $release.archive_path $staging
    Assert-ArchiveInventory $staging
    Copy-Item -LiteralPath $Binding.endpoint_config `
      -Destination (Config-Path $staging) -Force
    if ((Sha256 (Config-Path $staging)) -cne [string]$config.source_sha256) {
      Die 'endpoint_config_mismatch' 'the staged endpoint.json differs from endpoint_config'
    }
    $script:LifecyclePhase = 'install-runtime'
    New-EndpointVirtualEnvironment ([string]$Binding.python_executable) $staging
    Install-EndpointRequirements $staging
    $script:LifecyclePhase = 'install-doctor'
    $doctor = Get-RuntimeDoctor $staging
    $runtimeRelease = Assert-ReleaseRuntime $release $doctor
    $official = Read-EndpointConfigOfficial $staging
    if ([string]$official.config_digest -cnotmatch '^[0-9a-f]{64}$' -or
        (Normalize-PathIdentity ([string]$official.state_root)) -cne $config.state_root) {
      Die 'endpoint_config_identity_mismatch' 'the staged release rejected endpoint.json'
    }
    $script:LifecyclePhase = 'install-finalize'
    Move-Item -LiteralPath $staging -Destination $generation
    $generationMoved = $true
    Set-ProtectedAcl $generation $serviceSid
    $script:LifecyclePhase = 'install-task'
    $taskRegistrationAttempted = $true
    Register-DisabledTask $Binding $generation
    $task = Get-RequiredTask $Binding
    Set-TaskFileAcl (Get-TaskFilePath $task) $serviceSid
    $projection = Get-TaskProjection $task
    Assert-TaskProjection `
      -Projection $projection `
      -Binding $Binding
    if ([string]$projection.state -cne 'Disabled') {
      Die 'task_state_invalid' 'install must leave the endpoint task Disabled'
    }
    $script:LifecyclePhase = 'install-control'
    $controlFiles = Install-ControlFiles `
      $Binding $config.state_root $controlState
    $stableManagedPaths = @(Get-StableManagedPaths `
      $config $Binding $generation $receiptPath)
    $receiptIdentity = Normalize-PathIdentity $receiptPath
    foreach ($path in $stableManagedPaths) {
      if ($path -ceq $receiptIdentity) { continue }
      Set-ProtectedAcl $path $serviceSid
      Assert-ProtectedAcl $path $serviceSid
    }
    $previousPins = Set-DomainPins $pins
    $runtimePython = Runtime-Python $generation
    $receipt = [ordered]@{
      schema_version = $script:ReceiptSchema
      passed = $true
      installed_at_utc = (Get-Date).ToUniversalTime().ToString('o')
      install_id = [string]$installId.value
      binding_path = [string]$controlFiles.binding.path
      binding_sha256 = [string]$controlFiles.binding.sha256
      lifecycle_path = [string]$controlFiles.lifecycle.path
      lifecycle_sha256 = [string]$controlFiles.lifecycle.sha256
      bootstrap_path = [string]$controlFiles.bootstrap.path
      bootstrap_sha256 = [string]$controlFiles.bootstrap.sha256
      endpoint_config = [string]$config.source_path
      endpoint_config_sha256 = [string]$config.source_sha256
      token_path = [string]$config.token_file
      token_sha256 = Sha256 ([string]$config.token_file)
      endpoint_root = Normalize-PathIdentity $generation
      config_path = Normalize-PathIdentity (Config-Path $generation)
      config_sha256 = Sha256 (Config-Path $generation)
      config_digest = [string]$official.config_digest
      release_manifest_path = [string]$release.path
      release_manifest_sha256 = [string]$release.sha256
      archive_name = [string]$release.archive_name
      archive_sha256 = [string]$release.archive_sha256
      version = [string]$release.version
      source_commit = [string]$release.source_commit
      source_tree = [string]$release.source_tree
      source_sha256 = [string]$release.source_sha256
      task_name = [string]$Binding.task_name
      task_path = [string]$Binding.task_path
      launch_contract = $script:PythonLaunchContract
      task_xml_sha256 = [string]$projection.xml_sha256
      task_semantic_sha256 = [string]$projection.semantic_sha256
      task_acl_sha256 = [string]$projection.security_descriptor_sha256
      runtime_python = Normalize-PathIdentity $runtimePython
      runtime_python_sha256 = Sha256 $runtimePython
      base_python = Normalize-PathIdentity ([string]$Binding.python_executable)
      base_python_sha256 = Sha256 ([string]$Binding.python_executable)
      runtime = [ordered]@{
        python = [string]$runtimeRelease.python
        role = 'native'
        packages = $runtimeRelease.packages
      }
      bootstrapped_service_sid = $serviceSid
    }
    $result = [ordered]@{
      schema_version = $script:ResultSchema
      passed = $true
      verb = 'install'
      task = Get-TaskDocument $projection
      identity = [ordered]@{
        install_id = [string]$installId.value
        endpoint_root = Normalize-PathIdentity $generation
        config_path = Normalize-PathIdentity (Config-Path $generation)
        config_sha256 = Sha256 (Config-Path $generation)
        config_digest = [string]$official.config_digest
        release = [ordered]@{
          version = [string]$release.version
          source_commit = [string]$release.source_commit
          source_sha256 = [string]$release.source_sha256
          role = 'native'
        }
      }
      runtime = [ordered]@{
        base_python = Normalize-PathIdentity ([string]$Binding.python_executable)
        endpoint_python = Normalize-PathIdentity $runtimePython
        release = $runtimeRelease
      }
      listener = $null
      health = $null
      receipt = Normalize-PathIdentity $receiptPath
      control = [ordered]@{
        binding = [string]$controlFiles.binding.path
        lifecycle = [string]$controlFiles.lifecycle.path
        bootstrap = [string]$controlFiles.bootstrap.path
      }
    }
    Write-JsonAtomic $receiptPath $receipt $serviceSid
    foreach ($path in $stableManagedPaths) {
      Assert-ProtectedAcl $path $serviceSid
    }
    return $result
  } catch {
    $primary = $_
    $cleanup = New-InstallCleanupReport
    $taskRemaining = $false
    try {
      if ($taskRegistrationAttempted) {
        $taskResult = Remove-OwnedInstallTask $Binding $generation
        $taskSafelyAbsent = (
          $taskResult.observed -is [bool] -and
          $taskResult.observed -eq $true -and
          $taskResult.remaining -is [bool] -and
          $taskResult.remaining -eq $false)
        $taskRemaining = -not $taskSafelyAbsent
        Set-InstallCleanupStep $cleanup 'task' $true $taskResult.observed `
          $taskResult.passed `
          $taskResult.remaining ([string]$taskResult.error)
      } else {
        $taskRemaining = (@(Get-BoundTask $Binding).Count -ne 0)
        Set-InstallCleanupStep $cleanup 'task' $false $true $null $taskRemaining ''
      }

      if ($controlState.files.Count -gt 0) {
        foreach ($name in @('binding', 'lifecycle', 'bootstrap')) {
          if (-not $controlState.files.Contains($name)) { continue }
          $row = $controlState.files[$name]
          if ($taskRemaining -and $row.created) {
            $remaining = Test-Path -LiteralPath $row.path
            Set-InstallCleanupStep `
              $cleanup ('control_' + $name) $false $true $null $remaining '' `
              'preserved because the managed task is not proven absent'
            continue
          }
          $attempted = [bool]$row.created
          if (-not $attempted) {
            $remaining = Test-Path -LiteralPath $row.path
            Set-InstallCleanupStep `
              $cleanup ('control_' + $name) $false $true $null $remaining ''
            continue
          }
          $errorText = ''
          try {
            if (Test-Path -LiteralPath $row.path) {
              Remove-Item -LiteralPath $row.path -Force -ErrorAction Stop
            }
          } catch {
            $errorText = $_.Exception.Message
          }
          $remaining = Test-Path -LiteralPath $row.path
          Set-InstallCleanupStep $cleanup ('control_' + $name) $true $true `
            (-not $remaining) $remaining $errorText
        }
      }

      if ($generationMoved -and -not $taskRemaining) {
        $errorText = ''
        try {
          if (Test-Path -LiteralPath $generation) {
            Remove-Item -LiteralPath $generation -Recurse -Force -ErrorAction Stop
          }
        } catch {
          $errorText = $_.Exception.Message
        }
        $remaining = Test-Path -LiteralPath $generation
        Set-InstallCleanupStep $cleanup 'generation' $true $true `
          (-not $remaining) $remaining $errorText
      } else {
        $remaining = Test-Path -LiteralPath $generation
        $reason = if ($taskRemaining) {
          'preserved because the managed task is not proven absent'
        } else {
          ''
        }
        Set-InstallCleanupStep $cleanup 'generation' $false $true $null $remaining '' $reason
      }

      if (Test-Path -LiteralPath $staging) {
        $errorText = ''
        try {
          Remove-Item -LiteralPath $staging -Recurse -Force -ErrorAction Stop
        } catch {
          $errorText = $_.Exception.Message
        }
        $remaining = Test-Path -LiteralPath $staging
        Set-InstallCleanupStep $cleanup 'staging' $true $true `
          (-not $remaining) $remaining $errorText
      } else {
        Set-InstallCleanupStep $cleanup 'staging' $false $true $null $false ''
      }

      if ($createdInstallId -and -not $taskRemaining) {
        $errorText = ''
        try {
          if (Test-Path -LiteralPath $installIdPath) {
            Remove-Item -LiteralPath $installIdPath -Force -ErrorAction Stop
          }
        } catch {
          $errorText = $_.Exception.Message
        }
        $remaining = Test-Path -LiteralPath $installIdPath
        Set-InstallCleanupStep $cleanup 'install_id' $true $true `
          (-not $remaining) $remaining $errorText
      } else {
        $remaining = Test-Path -LiteralPath $installIdPath
        $reason = if ($taskRemaining -and $createdInstallId) {
          'preserved because the managed task is not proven absent'
        } else {
          ''
        }
        Set-InstallCleanupStep $cleanup 'install_id' $false $true $null $remaining '' $reason
      }

      if ($taskRemaining) {
        Set-InstallCleanupStep `
          $cleanup 'account_pins' $false $true $null $null '' `
          'preserved because the managed task is not proven absent'
      } elseif ($null -ne $previousPins) {
        $pinErrors = New-Object System.Collections.ArrayList
        foreach ($name in @('DESCRIPTION_ENDPOINT_ROOT', 'DESCRIPTION_CAD_OWNERSHIP_FILE')) {
          try {
            Set-AccountEnvironment $name ([string]$previousPins[$name])
          } catch {
            [void]$pinErrors.Add($name + ': ' + $_.Exception.Message)
          }
        }
        $pinsRestored = $true
        foreach ($name in @('DESCRIPTION_ENDPOINT_ROOT', 'DESCRIPTION_CAD_OWNERSHIP_FILE')) {
          if ((Get-AccountEnvironment $name) -cne [string]$previousPins[$name]) {
            $pinsRestored = $false
          }
        }
        Set-InstallCleanupStep $cleanup 'account_pins' $true $true $pinsRestored `
          (-not $pinsRestored) ($pinErrors -join '; ')
      } else {
        Set-InstallCleanupStep $cleanup 'account_pins' $false $true $null $false ''
      }
    } catch {
      [void]$cleanup.errors.Add(
        'cleanup_runner: ' + (Protect-DiagnosticText $_.Exception.Message))
      $taskRemaining = $true
    }

    $cleanup.passed = (
      $cleanup.errors.Count -eq 0 -and
      @($cleanup.steps.Values | Where-Object {
        $_.attempted -eq $true -and $_.passed -ne $true
      }).Count -eq 0 -and
      -not $taskRemaining
    )
    $code = Get-ExceptionCode $primary 'install_failed'
    $message = $primary.Exception.Message
    Die $code $message @{
      phase = $script:LifecyclePhase
      source = Protect-DiagnosticText (
        $primary.InvocationInfo.PositionMessage) 1024
      stack = Protect-DiagnosticText $primary.ScriptStackTrace 2048
      primary_detail = Get-ExceptionDetail $primary
      cleanup = $cleanup
    }
  }
}

function Invoke-Start($Binding) {
  $script:LifecyclePhase = 'start-preflight'
  Assert-OperatorIsServiceAccount $Binding
  $state = Get-InstalledState $Binding
  if ([string]$state.projection.state -eq 'Running') {
    $running = Get-RunningEvidence $state
    return [ordered]@{
      schema_version = $script:ResultSchema
      passed = $true
      verb = 'start'
      task = Get-TaskDocument $state.projection
      identity = $running.identity
      runtime = [ordered]@{
        base_python = Normalize-PathIdentity ([string]$Binding.python_executable)
        endpoint_python = Normalize-PathIdentity (Runtime-Python $state.generation)
        release = $state.release
      }
      listener = $running.listener
      health = $running.health
    }
  }
  if ([string]$state.projection.state -notin @('Disabled', 'Ready')) {
    Die 'task_state_invalid' (
      'start requires a Disabled or Ready task, found ' + [string]$state.projection.state)
  }
  Assert-ListenerPortFree ([int]$state.port)
  $pins = Get-DomainPins $state.source_config.state_root $state.generation
  Assert-DomainPins $pins
  $previousPins = Set-DomainPins $pins
  $started = $false
  try {
    $script:LifecyclePhase = 'start-enable'
    Enable-ScheduledTask `
      -TaskName ([string]$Binding.task_name) `
      -TaskPath ([string]$Binding.task_path) | Out-Null
    $script:LifecyclePhase = 'start-launch'
    $started = $true
    Start-ScheduledTask `
      -TaskName ([string]$Binding.task_name) `
      -TaskPath ([string]$Binding.task_path) | Out-Null
    $script:LifecyclePhase = 'start-verify'
    $task = Get-RequiredTask $Binding
    $projection = Get-TaskProjection $task
    $state.task = $task
    $state.projection = $projection
    $running = $null
    $deadline = (Get-Date).AddSeconds(30)
    do {
      try {
        $running = Get-RunningEvidence $state
      } catch {
        if ((Get-Date) -lt $deadline) { Start-Sleep -Milliseconds 500 }
      }
    } while ($null -eq $running -and (Get-Date) -lt $deadline)
    if ($null -eq $running) {
      $running = Get-RunningEvidence $state
    }
    return [ordered]@{
      schema_version = $script:ResultSchema
      passed = $true
      verb = 'start'
      task = Get-TaskDocument $projection
      identity = $running.identity
      runtime = [ordered]@{
        base_python = Normalize-PathIdentity ([string]$Binding.python_executable)
        endpoint_python = Normalize-PathIdentity (Runtime-Python $state.generation)
        release = $state.release
      }
      listener = $running.listener
      health = $running.health
    }
  } catch {
    $primary = $_
    $cleanupError = ''
    $stoppedAfterFailure = $null
    try {
      Disable-ScheduledTask `
        -TaskName ([string]$Binding.task_name) `
        -TaskPath ([string]$Binding.task_path) -ErrorAction Stop | Out-Null
      Stop-ScheduledTask `
        -TaskName ([string]$Binding.task_name) `
        -TaskPath ([string]$Binding.task_path) -ErrorAction SilentlyContinue | Out-Null
      $stoppedAfterFailure = Wait-EndpointStopped $state
    } catch {
      $cleanupError = Protect-DiagnosticText $_.Exception.Message
    }
    if (-not $started) { Restore-DomainPins $previousPins }
    $code = Get-ExceptionCode $primary 'start_failed'
    $exitCode = Get-ExceptionExitCode $primary 1
    if ($exitCode -eq 2) {
      DieUnknown $code $primary.Exception.Message @{
        phase = $script:LifecyclePhase
        task_disabled_after_failure = ($cleanupError -eq '')
        stopped = $stoppedAfterFailure
        cleanup_error = $cleanupError
      }
    }
    Die $code $primary.Exception.Message @{
      phase = $script:LifecyclePhase
      task_disabled_after_failure = ($cleanupError -eq '')
      stopped = $stoppedAfterFailure
      cleanup_error = $cleanupError
    }
  }
}

function Invoke-Drain($Binding) {
  $script:LifecyclePhase = 'drain'
  Assert-OperatorIsServiceAccount $Binding
  $state = Get-InstalledState $Binding -SkipDoctor -SkipPins
  $running = Get-RunningEvidence $state -AllowUnready
  $safety = Assert-DrainSafety $state $running
  return [ordered]@{
    schema_version = $script:ResultSchema
    passed = $true
    verb = 'drain'
    task = Get-TaskDocument $state.projection
    identity = $running.identity
    runtime = [ordered]@{
      base_python = Normalize-PathIdentity ([string]$Binding.python_executable)
      endpoint_python = Normalize-PathIdentity (Runtime-Python $state.generation)
      release = $state.release
    }
    listener = $running.listener
    health = $running.health
    safety = $safety
  }
}

function Wait-EndpointStopped($State) {
  $deadline = (Get-Date).AddSeconds(30)
  $last = $null
  do {
    $connections = @(Get-ListenerConnections ([int]$State.port))
    $writers = @(Get-WriterResidue $State)
    $cad = @(Get-SolidWorksProcesses)
    $last = [ordered]@{
      listener_count = $connections.Count
      writer_count = $writers.Count
      solidworks_process_count = $cad.Count
    }
    if ($connections.Count -eq 0 -and $writers.Count -eq 0 -and $cad.Count -eq 0) {
      return $last
    }
    if ((Get-Date) -lt $deadline) { Start-Sleep -Milliseconds 500 }
  } while ((Get-Date) -lt $deadline)
  Die 'stop_residue' 'listener or writer processes remain after stop' $last
}

function Invoke-Stop($Binding) {
  $script:LifecyclePhase = 'stop-preflight'
  Assert-OperatorIsServiceAccount $Binding
  $state = Get-InstalledState $Binding -SkipDoctor -SkipPins
  $taskState = [string]$state.projection.state
  $safety = $null
  $stoppedIdentity = $null
  if ($taskState -eq 'Running') {
    $running = Get-RunningEvidence $state -AllowUnready
    $stoppedIdentity = $running.identity
    $safety = Assert-DrainSafety $state $running
  } elseif ($taskState -in @('Disabled', 'Ready')) {
    Assert-ListenerPortFree ([int]$state.port)
    $writers = @(Get-WriterResidue $state)
    $cad = @(Get-SolidWorksProcesses)
    if ($writers.Count -ne 0 -or $cad.Count -ne 0) {
      Die 'stop_residue' 'writer or SolidWorks processes exist before stop' @{
        writer_count = $writers.Count
        solidworks_process_count = $cad.Count
      }
    }
    $safety = [ordered]@{
      jobs = $null
      cad = [ordered]@{
        external_session_detected = $false
        owned_sessions = 0
        lease_held = $false
        solidworks_processes = 0
      }
      listener_pid = $null
      task_state = $taskState
      source = 'already_stopped'
    }
    $stoppedIdentity = Get-IdentityDocument $state
  } else {
    Die 'task_state_invalid' (
      'stop refuses task state ' + $taskState + ' because drain cannot be proven')
  }
  $stopped = $null
  try {
    $script:LifecyclePhase = 'stop-disable'
    Disable-ScheduledTask `
      -TaskName ([string]$Binding.task_name) `
      -TaskPath ([string]$Binding.task_path) | Out-Null
    $script:LifecyclePhase = 'stop-task'
    if ($taskState -eq 'Running') {
      Stop-ScheduledTask `
        -TaskName ([string]$Binding.task_name) `
        -TaskPath ([string]$Binding.task_path) | Out-Null
    }
    $script:LifecyclePhase = 'stop-verify'
    $stopped = Wait-EndpointStopped $state
  } catch {
    $primary = $_
    $observation = $null
    try {
      $taskNow = @(Get-BoundTask $Binding)
      $observation = [ordered]@{
        task_count = $taskNow.Count
        task_state = if ($taskNow.Count -eq 1) { [string]$taskNow[0].State } else { 'unknown' }
        listener_count = @(Get-ListenerConnections ([int]$state.port)).Count
        writer_count = @(Get-WriterResidue $state).Count
      }
    } catch {
      $observation = @{ error = Protect-DiagnosticText $_.Exception.Message }
    }
    DieUnknown (Get-ExceptionCode $primary 'stop_failed') (
      'the endpoint stop did not reach a proven stopped state: ' +
      $primary.Exception.Message) @{
      phase = $script:LifecyclePhase
      observation = $observation
    }
  }
  $task = Get-RequiredTask $Binding
  $projection = Get-TaskProjection $task
  if ([string]$projection.state -eq 'Running') {
    Die 'stop_failed' 'the scheduled task remains Running after stop'
  }
  return [ordered]@{
    schema_version = $script:ResultSchema
    passed = $true
    verb = 'stop'
    task = Get-TaskDocument $projection
    identity = $stoppedIdentity
    runtime = [ordered]@{
      base_python = Normalize-PathIdentity ([string]$Binding.python_executable)
      endpoint_python = Normalize-PathIdentity (Runtime-Python $state.generation)
      release = $state.release
    }
    listener = $null
    health = $null
    safety = $safety
    stopped = $stopped
  }
}

function Invoke-Health($Binding) {
  $script:LifecyclePhase = 'health'
  Assert-OperatorIsServiceAccount $Binding
  $state = Get-InstalledState $Binding
  $running = Get-RunningEvidence $state
  return [ordered]@{
    schema_version = $script:ResultSchema
    passed = $true
    verb = 'health'
    task = Get-TaskDocument $state.projection
    identity = $running.identity
    runtime = [ordered]@{
      base_python = Normalize-PathIdentity ([string]$Binding.python_executable)
      endpoint_python = Normalize-PathIdentity (Runtime-Python $state.generation)
      release = $state.release
    }
    listener = $running.listener
    health = $running.health
  }
}

function Invoke-Status($Binding) {
  $document = [ordered]@{
    schema_version = $script:ResultSchema
    passed = $true
    verb = 'status'
    task = $null
    identity = $null
    runtime = $null
    listener = $null
    health = $null
    status = [ordered]@{
      consistent = $false
      task_exists = $false
      task_state = 'missing'
      error = $null
      solidworks_processes = $null
    }
  }
  try {
    $sourceConfig = Read-EndpointConfigStrict $Binding
    $null = Assert-ControlBindingPath $Binding $sourceConfig.state_root
    Assert-OperatorIsServiceAccount $Binding
    $tasks = @(Get-BoundTask $Binding)
    $document.status.task_exists = ($tasks.Count -ne 0)
    if ($tasks.Count -gt 1) {
      throw (New-LifecycleException 'task_ambiguous' 'more than one managed task exists')
    }
    if ($tasks.Count -eq 0) {
      return $document
    }
    $task = $tasks[0]
    $projection = Get-TaskProjection $task
    $document.task = Get-TaskDocument $projection
    $document.status.task_state = [string]$projection.state
    $state = Get-InstalledState $Binding
    $document.identity = Get-IdentityDocument $state
    $document.runtime = [ordered]@{
      base_python = Normalize-PathIdentity ([string]$Binding.python_executable)
      endpoint_python = Normalize-PathIdentity (Runtime-Python $state.generation)
      release = $state.release
    }
    if ([string]$projection.state -eq 'Running') {
      $running = Get-RunningEvidence $state -AllowUnready
      $document.identity = $running.identity
      $document.listener = $running.listener
      $document.health = $running.health
    } else {
      Assert-ListenerPortFree ([int]$state.port)
    }
    $document.status.solidworks_processes = @(Get-SolidWorksProcesses).Count
    $document.status.consistent = (
      $null -eq $document.health -or
      ($document.health.ready -is [bool] -and $document.health.ready -eq $true))
  } catch {
    $document.status.error = @{
      code = Get-ExceptionCode $_
      message = Protect-DiagnosticText $_.Exception.Message
    }
  }
  return $document
}

function Invoke-LifecycleMain($CommandVerb, $CommandBinding) {
  $script:CurrentVerb = 'unknown'
  try {
    $allowed = @('install', 'start', 'drain', 'stop', 'status', 'health')
    if ($CommandVerb -isnot [string] -or [string]::IsNullOrWhiteSpace($CommandVerb)) {
      Die 'verb_required' ('-Verb must be one of: ' + ($allowed -join ', '))
    }
    if ($CommandVerb -cnotin $allowed) {
      Die 'verb_unsupported' ('unsupported verb: ' + $CommandVerb)
    }
    $script:CurrentVerb = [string]$CommandVerb
    $binding = Read-Binding $CommandBinding
    switch ($script:CurrentVerb) {
      'install' { $result = Invoke-Install $binding }
      'start' { $result = Invoke-Start $binding }
      'drain' { $result = Invoke-Drain $binding }
      'stop' { $result = Invoke-Stop $binding }
      'status' { $result = Invoke-Status $binding }
      'health' { $result = Invoke-Health $binding }
    }
    Emit $result
    exit 0
  } catch {
    $code = Get-ExceptionCode $_
    $exitCode = Get-ExceptionExitCode $_ 2
    if ($exitCode -notin @(1, 2)) { $exitCode = 2 }
    $errorResult = [ordered]@{
      schema_version = $script:ResultSchema
      passed = $false
      verb = $script:CurrentVerb
      code = $code
      message = Protect-DiagnosticText $_.Exception.Message 2048
      detail = Get-ExceptionDetail $_
      phase = $script:LifecyclePhase
    }
    Emit $errorResult
    exit $exitCode
  }
}

if ($MyInvocation.InvocationName -ne '.') {
  Invoke-LifecycleMain $Verb $Binding
}
