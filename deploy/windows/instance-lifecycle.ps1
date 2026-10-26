# The one isolated-instance lifecycle script.  Every path, task name and port comes from the
# single binding file rendered on the platform host from platform.json + the release manifest;
# nothing here defaults to a personal path, a production task name or a Git credential.
#   stage   verifies the transferred archive, installs the generation runtime, renders the
#           generation's own minimal endpoint.json and records its expected config digest
#   promote stops the old endpoint (no listener AND no writer residue under the generation or
#           state root), switches the task to the generation runtime + config, starts it and
#           settles it for >=184s (task Running + listener PID constant + token-authenticated
#           /health ready); a start refused by the ownership lock is recorded together with the
#           lock owner's process facts, and the >=60s-later retry happens exactly once and only
#           after the owner is PROVEN ended.  Owner state and lock probe are three-valued
#           (alive/ended/unknown; refused/free/observer-failed): an unknown owner, a failed
#           observer or a second refusal stops with lock_refused_unproven / lock_refused for
#           diagnosis, never by killing an unknown process or deleting the lock; every failure
#           path restores the previous task definition but starts it only on startable evidence
#           (lock free, or refused with a proven-ended owner) and verifies health against the
#           previous generation's own pinned release identity
#   rollback restores exactly the previous generation directory the caller bounds
#   health  relays the running endpoint's own /health payload
# Each invocation prints one JSON object; receipts stay under the instance state root.
#
#   instance-lifecycle.ps1 -Verb stage|promote|rollback|health -Binding FILE [-StageDir DIR]
#       [-Previous DIR]
param(
  [Parameter(Mandatory=$true)][ValidateSet(
    'stage','promote','rollback','health','status',
    'recovery-snapshot','recovery-restore','recovery-verify','resume'
  )][string]$Verb,
  # Untyped on purpose: PowerShell names are case-insensitive and L175 assigns the parsed
  # binding object back into this variable; a [string] constraint would stringify it.
  [Parameter(Mandatory=$true)]$Binding,
  [string]$StageDir = '',
  [string]$Previous = '',
  [string]$RecoveryDir = '',
  [string]$RecoveryBundle = ''
)
$ErrorActionPreference='Stop'
$ProgressPreference='SilentlyContinue'
$script:LifecyclePhase = 'dispatch'

function Protect-LifecycleDiagnostic([string]$Text, [int]$Limit = 512, [bool]$HidePaths = $true) {
  # Redact before bounding so truncation cannot expose a credential prefix.
  $safe = $Text -replace '(?i)(bearer\s+)[^\s,;]+', '$1[redacted]'
  $safe = $safe -replace '(?i)(["'']?(?:token|password|pwd|secret|authorization)["'']?\s*[=:]\s*)(?:"[^"]*"|''[^'']*''|[^,;\r\n]+)', '$1[redacted]'
  $safe = $safe -replace '(?i)([a-z][a-z0-9+.-]*://)[^\s/@]+:[^\s/@]+@', '$1[redacted]@'
  if ($HidePaths) {
    $safe = $safe -replace '(?i)(?:FileSystem::|file://|\\\\)[^\r\n"<>]*', '[path]'
    $safe = $safe -replace '(?i)(?:[A-Z]:[\\/]|/)[^\r\n"<>]*', '[path]'
  }
  $safe = $safe -replace '[\x00-\x1f\x7f]', ' '
  if ($safe.Length -gt $Limit) { $safe = $safe.Substring(0, $Limit) }
  while ([System.Text.Encoding]::UTF8.GetByteCount($safe) -gt $Limit) {
    $safe = $safe.Substring(0, $safe.Length - 1)
  }
  return $safe
}
function Get-LifecycleTextSha256([string]$Text) {
  $sha = [System.Security.Cryptography.SHA256]::Create()
  try {
    return ([BitConverter]::ToString($sha.ComputeHash(
      [System.Text.Encoding]::UTF8.GetBytes($Text))) -replace '-', '').ToLowerInvariant()
  } finally { $sha.Dispose() }
}
function Add-LifecycleErrorContext($Record, [string]$Role, [string]$Operation, [string]$Path) {
  # Annotate, then bare-rethrow at the caller: retain the original ErrorRecord/HResult.
  try {
    foreach ($pair in @(@('role', $Role), @('operation', $Operation), @('path', $Path))) {
      if (-not $Record.Exception.Data.Contains('lifecycle_' + $pair[0])) {
        $Record.Exception.Data['lifecycle_' + $pair[0]] = $pair[1]
      }
    }
  } catch { } # Diagnostic annotation must never replace the primary exception.
}
function Write-LifecycleException($Record) {
  try {
    $base = $Record.Exception.GetBaseException()
    $rawMessage = [string]$Record.Exception.Message
    $redactedMessage = Protect-LifecycleDiagnostic $rawMessage ([int]::MaxValue)
    $message = Protect-LifecycleDiagnostic $rawMessage
    $path = [string]$Record.Exception.Data['lifecycle_path']
    $payload = [ordered]@{
      passed=$false; code='instance_lifecycle_exception'; verb=[string]$Script:Verb
      phase=(Protect-LifecycleDiagnostic $script:LifecyclePhase 64)
      exception_type=(Protect-LifecycleDiagnostic $Record.Exception.GetType().FullName 160 $false)
      message=$message; message_redacted=($redactedMessage -cne $rawMessage)
      message_truncated=($message -cne $redactedMessage)
      message_bytes=[System.Text.Encoding]::UTF8.GetByteCount($message)
      message_sha256=(Get-LifecycleTextSha256 $redactedMessage)
      hresult=[int]$Record.Exception.HResult
      base_exception_type=(Protect-LifecycleDiagnostic $base.GetType().FullName 160 $false)
      base_hresult=[int]$base.HResult
      error_id=(Protect-LifecycleDiagnostic ([string]$Record.FullyQualifiedErrorId) 160)
      error_id_sha256=(Get-LifecycleTextSha256 (
        Protect-LifecycleDiagnostic ([string]$Record.FullyQualifiedErrorId) ([int]::MaxValue)))
      category=[string]$Record.CategoryInfo.Category
      line=[int]$Record.InvocationInfo.ScriptLineNumber
      script_stack_sha256=(Get-LifecycleTextSha256 ([string]$Record.ScriptStackTrace))
      exception_stack_sha256=(Get-LifecycleTextSha256 ([string]$Record.Exception.StackTrace))
      path=$(if ($path) { '[path]' } else { '' })
      path_sha256=$(if ($path) { Get-LifecycleTextSha256 $path } else { '' })
    }
    foreach ($name in @('role', 'operation')) {
      $payload[$name] = Protect-LifecycleDiagnostic ([string]$Record.Exception.Data['lifecycle_' + $name])
    }
    [Console]::Out.WriteLine(($payload | ConvertTo-Json -Depth 4 -Compress))
  } catch {
    # No dependency on the serializer or a failed evidence sink in the final fallback.
    $verb = if ($Script:Verb -in @('stage','promote','rollback','health','status',
        'recovery-snapshot','recovery-restore','recovery-verify','resume')) { $Script:Verb } else { 'unknown' }
    [Console]::Out.WriteLine('{"passed":false,"code":"instance_lifecycle_exception","verb":"' +
      $verb + '","message":"Exception diagnostics unavailable"}')
  }
}
function Invoke-LifecycleMain {
  try { Invoke-Main } catch {
    Write-LifecycleException $_
    # Stage cannot switch the active task. Other verbs may already have side effects.
    if ($Script:Verb -eq 'stage') { exit 1 }
    exit 2
  }
}

function Emit([hashtable]$payload) {
  $payload | ConvertTo-Json -Depth 8 -Compress
}
function Close-RecoveryTemp {
  # One cleanup attempt only: after a failure the receipt carries the residual path and the
  # caller must not repeat a completed remote action merely to retry deletion.
  $path = [string]$Script:RecoveryTemp
  if (-not $path) {
    return @{attempted=$false; passed=$null; path=''}
  }
  $result = @{attempted=$true; passed=$true; path=$path}
  try {
    if (Test-Path -LiteralPath $path) {
      Remove-Item -LiteralPath $path -Recurse -Force -ErrorAction Stop
    }
  } catch {
    $result.passed = $false
    $result.error = [string]$_.Exception.Message
  }
  $Script:RecoveryTemp = $null
  return $result
}
function Die([string]$code, [string]$message) {
  $cleanup = Close-RecoveryTemp
  $payload = @{passed=$false; code=$code; message=$message; verb=$Script:Verb}
  if ($cleanup.attempted) { $payload.cleanup = $cleanup }
  Emit $payload
  exit 1
}
function DieUnknown([string]$code, [string]$message, $detail = $null) {
  $cleanup = Close-RecoveryTemp
  $payload = @{passed=$false; code=$code; message=$message; detail=$detail; verb=$Script:Verb}
  if ($cleanup.attempted) { $payload.cleanup = $cleanup }
  Emit $payload
  exit 2
}
function Read-Json([string]$path) {
  Get-Content -LiteralPath $path -Raw -Encoding UTF8 | ConvertFrom-Json
}
function Sha256([string]$path) {
  (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLower()
}
function Write-Utf8NoBom([string]$path, [string]$text) {
  # The one canonical byte writer: powershell.exe 5.1 Set-Content -Encoding UTF8 prepends a
  # BOM, which every strict UTF-8 JSON consumer rejects.
  [System.IO.File]::WriteAllText($path, $text, (New-Object System.Text.UTF8Encoding($false)))
}
function Read-Utf8Text([string]$Path) {
  # PowerShell 5.1's default Get-Content encoding follows the active ANSI code page.  Task XML
  # is written as strict UTF-8 without a BOM, so registration must decode the bytes explicitly.
  return [System.IO.File]::ReadAllText(
    $Path,
    (New-Object System.Text.UTF8Encoding($false,$true)))
}
function Write-Receipt([string]$path, $payload) {
  $parent = Split-Path -Parent $path
  if (-not (Test-Path -LiteralPath $parent)) { New-Item -ItemType Directory -Path $parent | Out-Null }
  Write-Utf8NoBom $path (Emit $payload)
}
function Runtime-Python([string]$generation) { Join-Path $generation 'runtime\Scripts\python.exe' }
function Runtime-Launcher([string]$generation) { Join-Path $generation 'runtime\Scripts\python.exe' }
function Runtime-LauncherArguments([string]$generation) {
  '-I -B -m description_pipeline.orchestration.windows serve --config "' + (Config-Path $generation) + '"'
}
function Config-Path([string]$generation) { Join-Path $generation 'endpoint.json' }
function Python-Module-LaunchContract { 'python_module_v1' }
function New-LaunchContractFailure([string]$code, [string]$message) {
  return @{passed=$false; code=$code; message=$message}
}
function Find-GenerationLaunchContract([string]$generation) {
  try {
    $receipt = Join-Path $generation 'stage-receipt.json'
    if (-not (Test-Path -LiteralPath $receipt -PathType Leaf)) {
      return (New-LaunchContractFailure 'stage_receipt_missing' (
        'the generation has no launch-contract receipt: ' + $generation))
    }
    $unsafe = Find-ReparseAncestor $receipt
    if ($unsafe) {
      return (New-LaunchContractFailure 'stage_launch_contract_unsupported' (
        'the stage receipt crosses a reparse point: ' + $unsafe))
    }
    $record = Read-Json $receipt
    if ($record.passed -isnot [bool] -or $record.passed -ne $true) {
      return (New-LaunchContractFailure 'stage_launch_contract_unsupported' (
        'the stage receipt passed field is not the JSON boolean true'))
    }
    if ($record.verb -isnot [string] -or $record.verb -cne 'stage') {
      return (New-LaunchContractFailure 'stage_launch_contract_unsupported' (
        'the stage receipt verb is not the exact JSON string stage'))
    }
    if ($record.generation -isnot [string] -or
        (Normalize-PathIdentity $record.generation) -ne
        (Normalize-PathIdentity $generation)) {
      return (New-LaunchContractFailure 'stage_launch_contract_unsupported' (
        'the stage receipt generation is not an exact string for this generation'))
    }
    if ($record.version -isnot [string] -or
        $record.source_commit -isnot [string] -or
        $record.source_sha256 -isnot [string]) {
      return (New-LaunchContractFailure 'stage_launch_contract_unsupported' (
        'the generation launch contract carries no strict release identity strings'))
    }
    if ($record.launch_contract -isnot [string]) {
      return (New-LaunchContractFailure 'stage_launch_contract_unsupported' (
        'the stage receipt launch_contract is not a JSON string'))
    }
    if ($record.launch_contract -cne (Python-Module-LaunchContract)) {
      return (New-LaunchContractFailure 'stage_launch_contract_unsupported' (
        'the staged generation does not declare the canonical Python module launcher'))
    }
    return @{passed=$true; contract=[string]$record.launch_contract}
  } catch {
    return (New-LaunchContractFailure 'stage_launch_contract_unsupported' (
      'the generation launch contract is unreadable: ' + $_.Exception.Message))
  }
}
function Get-GenerationLaunchContract([string]$generation) {
  $lookup = Find-GenerationLaunchContract $generation
  if (-not $lookup.passed) {
    Die $lookup.code $lookup.message
  }
  return $lookup.contract
}
function Test-Python-Module-LauncherArguments([string]$arguments, [string]$generation) {
  $match = [regex]::Match(
    $arguments,
    '^(-I -B -m description_pipeline\.orchestration\.windows serve --config )"([^"]+)"$')
  return (
    $match.Success -and
    $match.Groups[1].Value -ceq '-I -B -m description_pipeline.orchestration.windows serve --config ' -and
    (Normalize-PathIdentity $match.Groups[2].Value) -eq
      (Normalize-PathIdentity (Config-Path $generation))
  )
}
function Test-Runtime-LauncherAction([string]$execute, [string]$arguments, [string]$generation) {
  return (
    (Normalize-PathIdentity $execute) -eq (Normalize-PathIdentity (Runtime-Launcher $generation)) -and
    (Test-Python-Module-LauncherArguments $arguments $generation)
  )
}
function Test-Runtime-Launcher([string]$execute, [string]$arguments, [string]$generation) {
  $contract = Get-GenerationLaunchContract $generation
  return (
    $contract -ceq (Python-Module-LaunchContract) -and
    (Test-Runtime-LauncherAction $execute $arguments $generation)
  )
}
function Assert-Single-TaskAction([object]$task) {
  if (@($task.Actions).Count -ne 1) {
    Die 'task_ambiguous' 'the instance task must carry exactly one action'
  }
}
function Get-TaskSelectionParameters([object]$binding) {
  $parameters = @{TaskName=[string]$binding.task_name}
  if (-not [string]::IsNullOrWhiteSpace([string]$binding.task_path)) {
    $parameters.TaskPath = [string]$binding.task_path
  }
  return $parameters
}
function Set-BindingTaskPath([object]$binding, [string]$TaskPath) {
  if ($binding -is [hashtable]) {
    $binding.task_path = $TaskPath
  } else {
    $binding | Add-Member -NotePropertyName task_path -NotePropertyValue $TaskPath -Force
  }
}
function Get-BoundTask([object]$binding, [string]$ErrorAction = 'SilentlyContinue') {
  $parameters = Get-TaskSelectionParameters $binding
  $parameters.ErrorAction = $ErrorAction
  return Get-ScheduledTask @parameters
}
function Get-InitialBoundTask([object]$binding) {
  $parameters = Get-TaskSelectionParameters $binding
  $parameters.ErrorAction = 'SilentlyContinue'
  $tasks = @(Get-ScheduledTask @parameters)
  if ($tasks.Count -eq 0) { return $null }
  if ($tasks.Count -ne 1) {
    Die 'windows_task_ambiguous' ('expected exactly one managed task, found ' + $tasks.Count)
  }
  if ([string]::IsNullOrWhiteSpace([string]$binding.task_path)) {
    Set-BindingTaskPath $binding ([string]$tasks[0].TaskPath)
  }
  return $tasks[0]
}
function Start-BoundTask([object]$binding) {
  $parameters = Get-TaskSelectionParameters $binding
  Start-ScheduledTask @parameters
}
function Test-ActionConfigArgument([string]$Arguments, [string]$ConfigPath) {
  $tokens = @()
  $current = [System.Text.StringBuilder]::new()
  $hasToken = $false
  $quoted = $false
  $inQuotes = $false
  foreach ($character in $Arguments.ToCharArray()) {
    if ($character -eq '"') {
      $hasToken = $true
      $quoted = $true
      $inQuotes = -not $inQuotes
      continue
    }
    if ([char]::IsWhiteSpace($character) -and -not $inQuotes) {
      if ($hasToken) {
        $tokens += [pscustomobject]@{
          text=$current.ToString()
          quoted=$quoted
        }
        $current = [System.Text.StringBuilder]::new()
        $hasToken = $false
        $quoted = $false
      }
      continue
    }
    [void]$current.Append($character)
    $hasToken = $true
  }
  if ($inQuotes) { return $false }
  if ($hasToken) {
    $tokens += [pscustomobject]@{
      text=$current.ToString()
      quoted=$quoted
    }
  }
  $optionIndexes = @(
    for ($index = 0; $index -lt $tokens.Count; $index++) {
      $text = [string]$tokens[$index].text
      if ($text -ceq '--config' -or
          $text.StartsWith('--config=', [System.StringComparison]::Ordinal)) {
        $index
      }
    }
  )
  if ($optionIndexes.Count -ne 1) { return $false }
  $optionIndex = [int]$optionIndexes[0]
  if ([string]$tokens[$optionIndex].text -cne '--config' -or
      $optionIndex + 2 -ne $tokens.Count) {
    return $false
  }
  $value = $tokens[$optionIndex + 1]
  return (
    [bool]$value.quoted -and
    (Normalize-PathIdentity ([string]$value.text)) -eq
      (Normalize-PathIdentity $ConfigPath)
  )
}
function Get-TaskGeneration([object]$task) {
  Assert-Single-TaskAction $task
  $execute = [string]$task.Actions[0].Execute
  if (-not $execute) {
    Die 'windows_task_projection_mismatch' 'the instance task action has no executable'
  }
  try {
    $scripts = [System.IO.Path]::GetDirectoryName($execute)
    $runtime = [System.IO.Path]::GetDirectoryName($scripts)
    $generation = [System.IO.Path]::GetDirectoryName($runtime)
  } catch {
    Die 'windows_task_projection_mismatch' 'the instance task executable is not an absolute managed path'
  }
  if (-not $generation -or
      (Normalize-PathIdentity $execute) -ne
      (Normalize-PathIdentity (Runtime-Python $generation))) {
    Die 'windows_task_projection_mismatch' 'the task executable is not the managed generation runtime'
  }
  return $generation
}
function Assert-RunningTask([object]$task) {
  if ([string]$task.State -cne 'Running') {
    Die 'task_not_running' ('the instance task state is not Running: ' + $task.State)
  }
}
function Read-EndpointHealth([string]$configPath) {
  $config = Read-Json $configPath
  $token = (Get-Content -LiteralPath $config.token_file -Raw).Trim()
  $deadline = (Get-Date).AddSeconds(30)
  $health = $null
  do {
    try {
      $health = Invoke-RestMethod -Uri ("http://127.0.0.1:" + $config.port + "/health") `
        -Headers @{Authorization=('Bearer ' + $token)} -TimeoutSec 3
    } catch { Start-Sleep -Milliseconds 500 }
  } while (-not $health -and (Get-Date) -lt $deadline)
  if (-not $health) { throw 'the instance endpoint did not answer /health within 30s' }
  if ($health.schema_version -ne 'solidworks-to-urdf.health/v1' -or -not $health.ready -or -not $health.native_platform) {
    throw 'the instance endpoint /health is not a ready IF-09 identity'
  }
  if (-not $health.install_id -or -not $health.config_digest -or -not $health.target_identity.target_id) {
    throw 'the instance endpoint /health does not carry its install identity'
  }
  return $health
}
function Health-Probe([string]$configPath, [object]$binding) {
  $health = Read-EndpointHealth $configPath
  if ($health.release.version -ne $binding.version -or
      $health.release.source_commit -ne $binding.source_commit -or
      $health.release.source_sha256 -ne $binding.source_sha256) {
    throw 'the instance endpoint /health release identity differs from the bound release'
  }
  return $health
}
function Get-ListenerProcess([int]$port) {
  $connection = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue |
    Select-Object -First 1
  if ($connection) { return [int]$connection.OwningProcess }
  return $null
}
function Get-ListenerEvidence([object]$binding, $TaskProjection) {
  try {
    $listeners = @(Get-NetTCPConnection -LocalPort ([int]$binding.port) -State Listen -ErrorAction Stop)
  } catch {
    if (Test-NetTcpNoMatchError $_) { $listeners = @() } else { throw }
  }
  if ($listeners.Count -ne 1) {
    Die 'windows_listener_ambiguous' ('expected exactly one endpoint listener, found ' + $listeners.Count)
  }
  $listenerPid = [int]$listeners[0].OwningProcess
  $process = Get-CimInstance Win32_Process -Filter ('ProcessId = ' + $listenerPid) -ErrorAction Stop
  if ($null -eq $process) {
    Die 'windows_listener_identity_mismatch' 'the endpoint listener process is missing'
  }
  $processImage = [string]$process.ExecutablePath
  $processCommand = [string]$process.CommandLine
  $action = $TaskProjection.action
  $actionArguments = [string]$action.arguments
  if ((Normalize-PathIdentity $processImage) -ne (Normalize-PathIdentity ([string]$action.execute)) -or
      [string]::IsNullOrWhiteSpace($processCommand) -or
      [string]::IsNullOrWhiteSpace($actionArguments) -or
      $processCommand.IndexOf(
        $actionArguments,
        [System.StringComparison]::Ordinal) -lt 0) {
    Die 'windows_listener_identity_mismatch' 'the endpoint listener does not execute the projected task action'
  }
  $information = Get-Process -Id $listenerPid -IncludeUserName -ErrorAction Stop
  $processSid = Resolve-Sid ([string]$information.UserName)
  if ($processSid -ne [string]$TaskProjection.principal.sid) {
    Die 'windows_listener_identity_mismatch' 'the endpoint listener account differs from the task principal'
  }
  return [ordered]@{
    listener_pid = $listenerPid
    process_start_utc = $information.StartTime.ToUniversalTime().ToString('o')
    process_command_sha256 = Get-TextSha256 $processCommand
    process_user_sid = $processSid
  }
}
function Assert-ListenerMatch($Expected, $Current) {
  foreach ($field in @(
      'listener_pid',
      'process_start_utc',
      'process_command_sha256',
      'process_user_sid')) {
    if ([string]$Expected.$field -cne [string]$Current.$field) {
      Die 'windows_listener_changed' ('the endpoint listener changed during maintenance at ' + $field)
    }
  }
}
function Wait-StartupHealthy([string]$configPath, [object]$binding) {
  $config = Read-Json $configPath
  $token = (Get-Content -LiteralPath $config.token_file -Raw).Trim()
  # Startup grace: the endpoint may need a moment to bind; the sustained window starts at the
  # first all-three-healthy sample and then may not break once.
  $startupDeadline = (Get-Date).AddSeconds(30)
  $listenerPid = $null
  while ($true) {
    $taskNow = Get-BoundTask $binding 'SilentlyContinue'
    if (-not $taskNow -or [string]$taskNow.State -notin @('Queued','Ready','Running')) {
      throw ('the settle startup window observed task state ' +
        $(if ($taskNow) { [string]$taskNow.State } else { 'missing' }))
    }
    $pidNow = $null
    $ready = $false
    if ([string]$taskNow.State -eq 'Running') {
      $pidNow = Get-ListenerProcess $config.port
    }
    if ($null -ne $pidNow) {
      try {
        $probe = Invoke-RestMethod -Uri ("http://127.0.0.1:" + $config.port + "/health") `
          -Headers @{Authorization=('Bearer ' + $token)} -TimeoutSec 3
        $ready = ($probe.ready -eq $true) -and ($probe.native_platform -eq $true) -and `
          $probe.schema_version -eq 'solidworks-to-urdf.health/v1'
      } catch { $ready = $false }
    }
    if ($ready) { $listenerPid = $pidNow; break }
    if ((Get-Date) -ge $startupDeadline) { throw 'the endpoint did not answer /health within the startup window' }
    Start-Sleep -Milliseconds 500
  }
  return @{listener_pid=$listenerPid}
}
function Wait-Settled([string]$configPath, [object]$binding) {
  # The settle window the deployment contract promises: for at least 184 seconds the task is
  # Running, the listener PID on the bound port never changes, and the token-authenticated
  # /health stays ready.  A flapping endpoint can no longer be declared switched inside a
  # 30-second peek: any sample that breaks one of the three conditions fails the promotion.
  $config = Read-Json $configPath
  $token = (Get-Content -LiteralPath $config.token_file -Raw).Trim()
  $startup = Wait-StartupHealthy $configPath $binding
  $listenerPid = [int]$startup.listener_pid
  $timer = [System.Diagnostics.Stopwatch]::StartNew()
  $settleDeadline = (Get-Date).AddSeconds(184)
  while ((Get-Date) -lt $settleDeadline) {
    $taskNow = Get-BoundTask $binding 'SilentlyContinue'
    if (-not $taskNow -or [string]$taskNow.State -ne 'Running') {
      throw ('the settle window observed task state ' + $(if ($taskNow) { [string]$taskNow.State } else { 'missing' }))
    }
    $pidNow = Get-ListenerProcess $config.port
    if ($null -eq $pidNow -or $pidNow -ne $listenerPid) {
      throw 'the settle window observed the listener PID change or disappear'
    }
    try {
      $probe = Invoke-RestMethod -Uri ("http://127.0.0.1:" + $config.port + "/health") `
        -Headers @{Authorization=('Bearer ' + $token)} -TimeoutSec 3
    } catch { throw ('the settle window observed /health not ready: ' + $_.Exception.Message) }
    if (-not $probe.ready -or -not $probe.native_platform -or $probe.schema_version -ne 'solidworks-to-urdf.health/v1') {
      throw 'the settle window observed /health without the ready IF-09 identity'
    }
    Start-Sleep -Seconds 10
  }
  $health = Health-Probe $configPath $binding
  return @{health=$health; settled_seconds=[int][math]::Round($timer.Elapsed.TotalSeconds); listener_pid=$listenerPid}
}
function Get-ProcessFacts([int]$ownerPid) {
  # Three states with positive evidence only: 'alive' (the OS returned the process), 'ended' (the
  # OS answered that no such process exists) and 'unknown' (every other outcome - a permission
  # error or any query failure is never proof that the owner exited).
  try {
    $process = Get-Process -Id $ownerPid -ErrorAction Stop
  } catch {
    $message = [string]$_.Exception.Message
    if ($message -match '(?i)cannot find (a|the) process') {
      return @{state='ended'; alive=$false; image=''; started=''; error=''}
    }
    return @{state='unknown'; alive=$null; image=''; started=''; error=$message}
  }
  if ($null -eq $process) {
    return @{state='unknown'; alive=$null; image=''; started=''; error='the process query returned nothing'}
  }
  $image = ''
  $started = ''
  try { $image = [string]$process.Path } catch { }
  try { $started = $process.StartTime.ToUniversalTime().ToString('o') } catch { }
  return @{state='alive'; alive=$true; image=$image; started=$started; error=''}
}
function Read-LockOwner([string]$lockPath) {
  # The lock file carries the owner PID as a diagnostic record (io.acquire_process_lock); the
  # kernel lock itself lives in the descriptor and dies with the process.
  if (-not (Test-Path -LiteralPath $lockPath)) { return @{present=$false; pid=$null; valid=$false} }
  $raw = ''
  try { $raw = ([string](Get-Content -LiteralPath $lockPath -Raw -ErrorAction Stop)).Trim() } catch {
    return @{present=$true; pid=$null; valid=$false}
  }
  $ownerPid = 0
  $valid = [int]::TryParse($raw, [ref]$ownerPid) -and $ownerPid -gt 0
  return @{present=$true; pid=$(if ($valid) { $ownerPid } else { $null }); valid=$valid}
}
function Test-LockProbeHeld([string]$generation, [string]$lockPath) {
  # Three outcomes from the staged runtime itself: $true = the lock refused the acquire (marker
  # plus the dedicated exit code), $false = the lock is free, $null = the observer failed
  # (missing runtime, import or other error, unknown exit) - never folded into either answer.
  $probeSource = @'
import sys
from pathlib import Path
try:
    from description_pipeline.io import acquire_process_lock
except Exception as error:  # an observer failure, never a lock state
    print("PROBE_IMPORT_FAILED: %s" % error, file=sys.stderr)
    sys.exit(7)
try:
    acquire_process_lock(Path(sys.argv[1]))
except Exception as error:
    if "ownership lock is unavailable" in str(error):
        print("LOCK_REFUSED")
        sys.exit(3)
    print("PROBE_FAILED: %s" % error, file=sys.stderr)
    sys.exit(7)
sys.exit(0)
'@
  $probePath = Join-Path ([System.IO.Path]::GetTempPath()) ('endpoint-lock-probe-' + [guid]::NewGuid().ToString('N') + '.py')
  [System.IO.File]::WriteAllText($probePath, $probeSource, (New-Object System.Text.UTF8Encoding($false)))
  $output = ''
  $exitCode = -1
  try {
    $output = [string]((cmd /c "`"$(Runtime-Python $generation)`" -I -B `"$probePath`" `"$lockPath`" 2>nul") | Out-String)
    $exitCode = [int]$LASTEXITCODE
  } catch {
    $output = ''
    $exitCode = -1
  } finally {
    Remove-Item -LiteralPath $probePath -Force -ErrorAction SilentlyContinue
  }
  if ($exitCode -eq 0) { return $false }
  if ($exitCode -eq 3 -and $output -match 'LOCK_REFUSED') { return $true }
  return $null
}
function Get-OwnershipRefusalFacts([string]$generation, [object]$binding) {
  # deployment.md settle: only positive evidence may license a retry.  Refusal, owner process
  # state and probe observer state are three-valued; a failed observation is unknown /
  # observer_failed and stops the switch instead of being read as a pass.  Nothing here kills a
  # process or deletes the lock.
  $taskNow = Get-BoundTask $binding 'SilentlyContinue'
  if ($taskNow -and [string]$taskNow.State -eq 'Running') {
    return @{refused=$false; state='task_running'; reason='task_running'}
  }
  $config = Read-Json (Config-Path $generation)
  $lockPath = Join-Path ([string]$config.state_root) '.endpoint.lock'
  $owner = Read-LockOwner $lockPath
  if (-not $owner.present) {
    return @{refused=$false; state='no_lock_file'; reason='no_lock_file'; lock_path=$lockPath}
  }
  $probe = Test-LockProbeHeld $generation $lockPath
  if ($null -eq $probe) {
    return @{refused=$false; state='observer_failed'; reason='observer_failed'; lock_path=$lockPath;
             owner_pid=$owner.pid}
  }
  if (-not $probe) { return @{refused=$false; state='lock_free'; reason='lock_free'; lock_path=$lockPath} }
  if (-not $owner.valid) {
    return @{refused=$true; state='refused'; reason='owner_unreadable'; lock_path=$lockPath; owner_pid=$null;
             owner_state='unknown'; owner_process_alive=$null; owner_ended=$false}
  }
  $facts = Get-ProcessFacts ([int]$owner.pid)
  return @{refused=$true; state='refused'; reason='lock_held'; lock_path=$lockPath; owner_pid=[int]$owner.pid;
           owner_state=[string]$facts.state; owner_process_alive=$facts.alive;
           owner_process_image=$facts.image; owner_process_started=$facts.started;
           owner_process_error=$facts.error; owner_ended=([string]$facts.state -eq 'ended')}
}
function Wait-SettleRetryDelay([int]$Seconds = 60) {
  # Reached only after the recorded owner is proven ended (deployment.md settle).
  Start-Sleep -Seconds $Seconds
}
function Write-LockRefusalReceipt([string]$promotions, [string]$stageDir, [hashtable]$refusal, [bool]$retryable) {
  Write-Receipt (Join-Path $promotions 'lock-refusal.json') @{
    verb='promote'; at=$refusal.at; generation=$stageDir; message=$refusal.message; reason=$refusal.reason;
    owner_pid=$refusal.owner_pid; owner_state=$refusal.owner_state; owner_process_alive=$refusal.owner_process_alive;
    owner_ended=$refusal.owner_ended; retry_after_s=60; retryable=$retryable}
}
function Start-OrRefuse([string]$configPath, [object]$binding, [string]$stageDir, [string]$promotions) {
  # Start the switched task and settle it.  A refusal by the ownership lock may be retried exactly
  # once, >=60s after the recorded owner is PROVEN ended; an unknown owner state, a failed lock
  # observer and a second refusal stop the switch for diagnosis.  The refusal facts are recorded
  # on every path; nothing kills processes or deletes the lock.
  $script:SettleRefusal = @{lock_refusal=$null; retried_after_s=$null}
  $result = @{settled=$null; lock_refusal=$null; retried_after_s=$null}
  Start-BoundTask $binding
  try { $result.settled = Wait-Settled $configPath $binding; return $result }
  catch {
    $facts = Get-OwnershipRefusalFacts $stageDir $binding
    if ([string]$facts.state -eq 'observer_failed') {
      $refusal = @{
        message='Another endpoint owns the state root or its ownership lock is unavailable';
        at=(Get-Date).ToUniversalTime().ToString('o'); reason='observer_failed'; lock_path=$facts.lock_path;
        owner_pid=$facts.owner_pid; owner_state='unknown'; owner_process_alive=$null; owner_ended=$false}
      $result.lock_refusal = $refusal
      $script:SettleRefusal = @{lock_refusal=$refusal; retried_after_s=$null}
      Write-LockRefusalReceipt $promotions $stageDir $refusal $false
      throw 'lock_refused_unproven: the lock observer failed; the refusal cannot be interpreted [observer_failed]'
    }
    if (-not $facts.refused) { throw }
    $refusal = @{
      message='Another endpoint owns the state root or its ownership lock is unavailable';
      at=(Get-Date).ToUniversalTime().ToString('o'); reason=[string]$facts.reason; lock_path=$facts.lock_path;
      owner_pid=$facts.owner_pid; owner_state=[string]$facts.owner_state; owner_process_alive=$facts.owner_process_alive;
      owner_process_image=$facts.owner_process_image; owner_process_started=$facts.owner_process_started;
      owner_process_error=$facts.owner_process_error;
      owner_ended=[bool]$facts.owner_ended}
    $result.lock_refusal = $refusal
    $script:SettleRefusal = @{lock_refusal=$refusal; retried_after_s=$null}
    Write-LockRefusalReceipt $promotions $stageDir $refusal $refusal.owner_ended
    if (-not $facts.owner_ended) {
      throw ('lock_refused_unproven: the endpoint lock owner is not proven ended [owner_state=' +
             [string]$facts.owner_state + ' / ' + [string]$facts.reason + ']')
    }
    Wait-SettleRetryDelay 60
    $result.retried_after_s = 60
    $script:SettleRefusal.retried_after_s = 60
    Start-BoundTask $binding
    try { $result.settled = Wait-Settled $configPath $binding; return $result }
    catch {
      $second = Get-OwnershipRefusalFacts $stageDir $binding
      if (-not $second.refused) { throw }
      throw ('lock_refused: the ownership lock was refused twice; stopping for diagnosis [owner_state=' +
             [string]$second.owner_state + ']')
    }
  }
}
function Get-EndpointConfigStrictPrelude {
  # The ONE strict JSON policy for endpoint configurations, shared by the early scan (bound
  # CPython, below) and the candidate generator (staged runtime, next): duplicate keys are
  # rejected through object_pairs_hook - never ConvertFrom-Json, which collapses them - and the
  # allowed key set lives here only.  The scan additionally mirrors the foundation
  # reader's rejection of the non-finite constants; value checks stay with the single
  # candidate/read_config chain and are never duplicated here.
  return @'
import json
import sys
from pathlib import Path


def fail(code, detail):
    print(json.dumps({"error": code, "detail": detail}, sort_keys=True))
    raise SystemExit(1)


def strict_object(pairs, duplicates=None):
    document = {}
    for key, value in pairs:
        if key in document:
            if duplicates is None:
                raise ValueError("duplicate key: %s" % key)
            duplicates.append(key)
        document[key] = value
    return document


ALLOWED_KEYS = ("schema_version", "package_root", "output_root", "state_root", "token_file", "host", "port")

'@
}
function Get-EndpointConfigScanPython {
  # The strict scan runs on the bound CPython 3.12 generation the stage installs with: the
  # py -3.12 launcher on the Windows endpoint hosts (the same interpreter `py -3.12 -m venv`
  # binds later) and python3.12/python3/python elsewhere.  The version is asserted, so a host
  # without CPython 3.12 refuses the scan instead of silently re-parsing with another rule set.
  foreach ($candidate in @(@('py', '-3.12'), @('python3.12'), @('python3'), @('python'))) {
    $executable = [string]$candidate[0]
    if (-not (Get-Command $executable -ErrorAction SilentlyContinue)) { continue }
    $probe = ''
    try {
      $probe = & $executable @($candidate | Select-Object -Skip 1) -c "import sys;print(('%d.%d') % sys.version_info[:2])" 2>$null |
        Select-Object -Last 1
    } catch {
      continue
    }
    if ($LASTEXITCODE -eq 0 -and ([string]$probe).Trim() -eq '3.12') {
      return @{ executable = $executable; prefix = @($candidate | Select-Object -Skip 1) }
    }
  }
  return $null
}
function Get-EndpointConfigEarlyScanProgram {
  # The early fail-closed verdict as data: parse, duplicate keys, unknown keys.  The scan is
  # host-portable PowerShell 5.1-safe: Desktop PowerShell carries no Core-only JSON parser, so
  # the strict read runs on the bound CPython with the shared prelude above.
  return (Get-EndpointConfigStrictPrelude) + @'
def reject_constant(name):
    raise ValueError("non-JSON numeric constant: %s" % name)


duplicates = []
verdict = {"unreadable": "", "duplicates": [], "unknown": []}
try:
    document = json.loads(
        Path(sys.argv[1]).read_text(encoding="utf-8"),
        object_pairs_hook=lambda pairs: strict_object(pairs, duplicates),
        parse_constant=reject_constant,
    )
    if not isinstance(document, dict):
        raise ValueError("the endpoint configuration is not a JSON object")
except Exception as error:
    verdict["unreadable"] = str(error)
else:
    verdict["duplicates"] = sorted(set(duplicates))
    verdict["unknown"] = sorted(set(document) - set(ALLOWED_KEYS))
print(json.dumps(verdict, sort_keys=True))
'@
}
function Get-EndpointConfigEarlyProblems([string]$configPath) {
  # Early fail-closed scan: every key the endpoint schema does not define - `targets` among
  # them - or a repeated key stops the stage explicitly and is never silently dropped.  The
  # running file itself is never modified.  Runs on the bound CPython 3.12, which keeps the
  # scan identical on PowerShell 5.1 Desktop (no Core-only JSON API is required).
  if (-not (Test-Path -LiteralPath $configPath)) { return @{unreadable=''; duplicates=@(); unknown=@()} }
  $python = Get-EndpointConfigScanPython
  if (-not $python) {
    return @{unreadable='no CPython 3.12 interpreter is available for the strict configuration scan';
             duplicates=@(); unknown=@()}
  }
  # PowerShell 5.1 mangles embedded double quotes when a -c program travels as one argv element;
  # the program travels as a real file instead (both strict-read programs do).
  $programPath = Write-PythonProgram 'endpoint-scan' (Get-EndpointConfigEarlyScanProgram)
  $scanRc = 1
  $scanOutput = @()
  try {
    $scanOutput = & $python.executable @($python.prefix) -I -B $programPath $configPath 2>&1
    $scanRc = $LASTEXITCODE
  } finally {
    Remove-Item -LiteralPath $programPath -Force -ErrorAction SilentlyContinue
  }
  if ($scanRc -ne 0) {
    return @{unreadable=('the strict configuration scan failed: ' + [string]($scanOutput | Select-Object -Last 1));
             duplicates=@(); unknown=@()}
  }
  $verdict = $null
  try { $verdict = ([string]($scanOutput | Select-Object -Last 1)) | ConvertFrom-Json } catch {
    return @{unreadable='the strict configuration scan returned no verdict'; duplicates=@(); unknown=@()}
  }
  return @{unreadable=[string]$verdict.unreadable; duplicates=@($verdict.duplicates); unknown=@($verdict.unknown)}
}
function Write-PythonProgram([string]$name, [string]$program) {
  # PowerShell 5.1 mangles embedded double quotes when a multi-line program is passed to a
  # native interpreter as one -c argument; every strict-read program therefore travels as a
  # real file (UTF-8, no BOM) and is removed by its caller after the run.
  $path = Join-Path ([System.IO.Path]::GetTempPath()) ($name + '-' + [guid]::NewGuid().ToString('N') + '.py')
  [System.IO.File]::WriteAllText($path, $program, (New-Object System.Text.UTF8Encoding($false)))
  return $path
}
function Get-EndpointConfigCandidateProgram {
  # The single generator of the candidate configuration.  It runs with the staged runtime so
  # the candidate is serialized and digested by the release's own canonical writer; the policy
  # is deliberately minimal - keep the schema's fields and values, refuse anything else
  # (unknown keys - `targets` among them - duplicate keys, malformed values) instead of
  # guessing or silently dropping.
  # Its strict-read policy is the shared prelude above (one copy with the early scan).
  return (Get-EndpointConfigStrictPrelude) + @'
import hashlib

from description_pipeline import io


source = Path(sys.argv[1])
target = Path(sys.argv[2])
raw = source.read_bytes()
try:
    document = json.loads(raw.decode("utf-8"), object_pairs_hook=strict_object)
except ValueError as error:
    duplicated = "duplicate key" in str(error)
    fail("endpoint_config_duplicate_keys" if duplicated else "endpoint_config_unreadable", str(error))
except Exception as error:
    fail("endpoint_config_unreadable", str(error))
if not isinstance(document, dict):
    fail("endpoint_config_invalid", "the endpoint configuration is not an object")

unknown = sorted(set(document) - set(ALLOWED_KEYS))
if unknown:
    fail("endpoint_config_unknown_keys", ", ".join(unknown))
if document.get("schema_version") != "solidworks-to-urdf.endpoint/v1":
    fail("endpoint_config_invalid_values", "schema_version")
for key in ("package_root", "output_root", "state_root", "token_file", "host"):
    if key in document and not isinstance(document[key], str):
        fail("endpoint_config_invalid_values", key)
port = document.get("port")
if port is not None and (not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535):
    fail("endpoint_config_invalid_values", "port")

candidate = {key: document[key] for key in ALLOWED_KEYS if key in document}
io.write_json(target, candidate)

try:
    from description_pipeline.orchestration.windows import read_config
except Exception as error:
    fail("endpoint_config_validator_unavailable", str(error))
try:
    loaded = read_config(target)
except Exception as error:
    fail("candidate_rejected_by_read_config", str(error))
loaded_digest = loaded.get("config_digest") if isinstance(loaded, dict) else None
if not isinstance(loaded_digest, str) or len(loaded_digest) != 64:
    fail("candidate_digest_unrecorded", repr(loaded_digest))

result = {
    "source_sha256": hashlib.sha256(raw).hexdigest(),
    "candidate_sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
    "canonical_digest": io.digest(candidate),
    "config_digest": loaded_digest,
    "read_config": "passed",
}
print(json.dumps(result, sort_keys=True))
'@
}
function Get-SourceStateAuthorityHash([string]$SourceStatePath) {
  if (-not (Test-Path -LiteralPath $SourceStatePath -PathType Leaf)) {
    throw 'the source-state authority document is missing'
  }
  return Sha256 $SourceStatePath
}
function Read-ValidatedSnapshot(
    [string]$Directory,
    [string]$ExpectedTaskName,
    [string]$ExpectedTaskPath,
    [string]$ExpectedStateRoot,
    [string]$ExpectedSourceStateSha256) {
  $sourcePath = Join-Path $Directory 'source-state.json'
  $taskPath = Join-Path $Directory 'task.xml'
  $configPath = Join-Path $Directory 'endpoint-before.json'
  foreach ($path in @($sourcePath, $taskPath, $configPath)) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
      throw ('the source snapshot is missing ' + [System.IO.Path]::GetFileName($path))
    }
    Assert-NoReparseAncestor $path
  }
  if ($ExpectedSourceStateSha256 -cnotmatch '^[0-9a-f]{64}$' -or
      (Get-SourceStateAuthorityHash $sourcePath) -cne $ExpectedSourceStateSha256) {
    throw 'the sealed source-state authority differs from the bound promotion'
  }
  $source = Read-Json $sourcePath
  if ([string]$source.schema_version -cne 'solidworks-to-urdf.windows-source-state/v1') {
    throw 'the source snapshot schema is unsupported'
  }
  if ([string]::IsNullOrWhiteSpace([string]$source.task_name) -or
      [string]$source.task_name -cne $ExpectedTaskName -or
      [string]$source.task_path -cne $ExpectedTaskPath -or
      (Normalize-PathIdentity ([string]$source.state_root)) -ne
        (Normalize-PathIdentity $ExpectedStateRoot)) {
    throw 'the source snapshot task or state-root identity differs from the binding'
  }
  foreach ($field in @(
      'task_path',
      'task_name',
      'task_xml_sha256',
      'task_semantic_sha256',
      'task_acl_sha256',
      'principal_sid',
      'execute',
      'arguments',
      'working_directory',
      'endpoint_root',
      'config_path',
      'config_sha256',
      'config_digest',
      'package_root',
      'output_root',
      'state_root',
      'install_id_path',
      'install_id',
      'release_identity_sha256')) {
    if ([string]::IsNullOrWhiteSpace([string]$source.$field)) {
      throw ('the source snapshot misses ' + $field)
    }
  }
  if ($null -eq $source.settings -or $null -eq $source.release -or
      [string]$source.release.version -eq '' -or
      [string]$source.release.source_commit -eq '' -or
      [string]$source.release.source_sha256 -eq '' -or
      [string]$source.release.role -cne 'native') {
    throw 'the source snapshot misses task settings or release identity'
  }
  if ((Sha256 $taskPath) -ne [string]$source.task_xml_sha256 -or
      (Sha256 $configPath) -ne [string]$source.config_sha256) {
    throw 'the sealed source task or configuration digest differs'
  }
  if ((Normalize-PathIdentity ([string]$source.config_path)) -ne
      (Normalize-PathIdentity (Config-Path ([string]$source.endpoint_root)))) {
    throw 'the sealed source configuration is outside its generation'
  }
  $installIdPath = Join-Path ([string]$source.state_root) 'install-id'
  if ((Normalize-PathIdentity ([string]$source.execute)) -ne
        (Normalize-PathIdentity (Runtime-Python ([string]$source.endpoint_root))) -or
      (Normalize-PathIdentity ([string]$source.working_directory)) -ne
        (Normalize-PathIdentity ([string]$source.state_root)) -or
      -not (Test-ActionConfigArgument ([string]$source.arguments) (
        [string]$source.config_path)) -or
      (Normalize-PathIdentity ([string]$source.install_id_path)) -ne
        (Normalize-PathIdentity $installIdPath)) {
    throw 'the sealed source action or install identity is outside its generation'
  }
  $lifecycleRoot = Join-Path ([string]$source.state_root) 'lifecycle'
  if (-not (Normalize-PathIdentity ([string]$source.endpoint_root)).StartsWith(
        (Normalize-PathIdentity $lifecycleRoot) + '/',
        [System.StringComparison]::OrdinalIgnoreCase)) {
    throw 'the sealed source generation is outside the managed lifecycle root'
  }
  $taskXml = Read-Utf8Text $taskPath
  try { $xml = [xml]$taskXml } catch { throw 'the sealed source task XML is unreadable' }
  if (@($xml.SelectNodes('//*[local-name()="Password" and string-length(normalize-space(.)) > 0]')).Count -ne 0) {
    throw 'the sealed source task XML carries a password'
  }
  $xmlActions = @($xml.SelectNodes('//*[local-name()="Exec"]'))
  $xmlExecute = @($xml.SelectNodes('//*[local-name()="Exec"]/*[local-name()="Command"]'))
  $xmlArguments = @($xml.SelectNodes('//*[local-name()="Exec"]/*[local-name()="Arguments"]'))
  $xmlWorking = @($xml.SelectNodes('//*[local-name()="Exec"]/*[local-name()="WorkingDirectory"]'))
  if ($xmlActions.Count -ne 1 -or $xmlExecute.Count -ne 1 -or
      $xmlArguments.Count -ne 1 -or $xmlWorking.Count -ne 1 -or
      [string]$xmlExecute[0].InnerText -cne [string]$source.execute -or
      [string]$xmlArguments[0].InnerText -cne [string]$source.arguments -or
      [string]$xmlWorking[0].InnerText -cne [string]$source.working_directory) {
    throw 'the sealed source task XML and source identity differ'
  }
  try { $probe = Read-EndpointRuntimeProbe ([string]$source.endpoint_root) $configPath }
  catch { throw ('the sealed source runtime identity is unavailable: ' + $_.Exception.Message) }
  if ([string]$source.config_digest -cne [string]$probe.config_digest -or
      (Normalize-PathIdentity ([string]$source.package_root)) -ne
        (Normalize-PathIdentity ([string]$probe.package_root)) -or
      (Normalize-PathIdentity ([string]$source.output_root)) -ne
        (Normalize-PathIdentity ([string]$probe.output_root)) -or
      (Normalize-PathIdentity ([string]$source.state_root)) -ne
        (Normalize-PathIdentity ([string]$probe.state_root)) -or
      [int]$source.port -ne [int]$probe.port) {
    throw 'the sealed source configuration identity differs from its runtime'
  }
  $embedded = $probe.release.release
  if ($null -eq $embedded -or
      [string]$source.release.version -cne [string]$probe.release.version -or
      [string]$source.release.source_commit -cne [string]$embedded.source_commit -or
      [string]$source.release.source_sha256 -cne [string]$probe.release.source_sha256 -or
      [string]$source.release.role -cne 'native' -or
      (Get-ReleaseIdentitySha256 $source.release) -cne [string]$source.release_identity_sha256) {
    throw 'the sealed source release identity differs from its runtime'
  }
  if (-not (Test-Path -LiteralPath $installIdPath -PathType Leaf) -or
      (Get-Content -LiteralPath $installIdPath -Raw).Trim() -cne
        [string]$source.install_id) {
    throw 'the sealed source install identity differs from the state root'
  }
  return @{
    source = $source
    source_path = $sourcePath
    task_path = $taskPath
    config_path = $configPath
    task_xml = $taskXml
  }
}
function Read-PromotionSourceAuthority(
    [string]$Promotions,
    [string]$Previous,
    [string]$CurrentGeneration) {
  $records = @()
  foreach ($entry in @(
      @{name='pending-promotion.json'; path=(Join-Path $Promotions 'pending-promotion.json')},
      @{name='last-promotion.json'; path=(Join-Path $Promotions 'last-promotion.json')})) {
    if (-not (Test-Path -LiteralPath $entry.path -PathType Leaf)) { continue }
    Assert-NoReparseAncestor $entry.path
    $record = Read-Json $entry.path
    if ((Normalize-PathIdentity ([string]$record.previous)) -eq
          (Normalize-PathIdentity $Previous) -and
        (Normalize-PathIdentity ([string]$record.generation)) -eq
          (Normalize-PathIdentity $CurrentGeneration)) {
      $records += @{name=$entry.name; record=$record}
    }
  }
  if (@($records).Count -eq 0) {
    throw 'no promotion receipt binds the requested previous and current generations'
  }
  $authorities = @()
  foreach ($entry in $records) {
    $record = $entry.record
    $expectedIntent = if ($entry.name -eq 'pending-promotion.json') { $true } else { $null }
    $expectedPassed = if ($entry.name -eq 'last-promotion.json') { $true } else { $null }
    $authority = [string]$record.source_state_sha256
    if ([string]$record.verb -cne 'promote' -or
        ($null -ne $expectedIntent -and $record.intent -ne $expectedIntent) -or
        ($null -ne $expectedPassed -and $record.passed -ne $expectedPassed) -or
        (Normalize-PathIdentity ([string]$record.target.endpoint_root)) -ne
          (Normalize-PathIdentity $CurrentGeneration) -or
        $authority -cnotmatch '^[0-9a-f]{64}$') {
      throw ('the promotion receipt does not bind a valid source-state authority: ' + $entry.name)
    }
    $authorities += $authority
  }
  $distinct = @($authorities | Select-Object -Unique)
  if ($distinct.Count -ne 1) {
    throw 'the pending and successful promotion receipts bind different source-state authorities'
  }
  return [string]$distinct[0]
}
function Restore-PreviousEndpoint(
    [object]$binding,
    [string]$previous,
    [string]$stageDir,
    [string]$expectedTaskPath,
    [string]$ExpectedSourceStateSha256) {
  # The safe stop/restore every promote failure path runs: stop whatever now serves the task,
  # restore the exact raw task XML and configuration sealed before the switch, then prove that
  # state from the managed generation runtime and the actual endpoint health. The task argument
  # text is opaque here: recovery never reapplies the new target launcher contract to old state.
  $restore = @{stopped=$false; restored=$false; start_gate=''; startable=$false; started=$false;
               healthy=$null; previous_generation=''; source=$null; error=''}
  try {
    $snapshotArguments = @{
      Directory = $previous
      ExpectedTaskName = [string]$binding.task_name
      ExpectedTaskPath = $expectedTaskPath
      ExpectedStateRoot = [string]$binding.state_root
      ExpectedSourceStateSha256 = $ExpectedSourceStateSha256
    }
    $snapshot = Read-ValidatedSnapshot @snapshotArguments
    $source = $snapshot.source
  } catch {
    $restore.error = 'sealed source: ' + $_.Exception.Message
    return $restore
  }
  Set-BindingTaskPath $binding ([string]$source.task_path)
  $restore.previous_generation = [string]$source.endpoint_root
  try {
    Stop-Endpoint $binding
    $restore.stopped = $true
  } catch {
    $restore.error = 'stop: ' + $_.Exception.Message
    return $restore
  }
  try {
    Register-ManagedTask -TaskName $binding.task_name -TaskPath ([string]$source.task_path) `
      -TaskXml $snapshot.task_xml -Force
    $restore.restored = $true
  } catch { if (-not $restore.error) { $restore.error = 'restore: ' + $_.Exception.Message } }
  if ($restore.restored) {
    try {
      Copy-Item -LiteralPath ([string]$snapshot.config_path) -Destination ([string]$source.config_path) -Force
      if ((Sha256 ([string]$source.config_path)) -ne [string]$source.config_sha256) {
        throw 'the restored configuration digest differs'
      }
      $restoredTask = Get-BoundTask $binding 'Stop'
      $projection = Get-TaskProjection $restoredTask
      $identity = Get-EndpointIdentity $restoredTask $projection ([string]$binding.ssh_host)
      $current = New-EndpointSourceState $restoredTask $projection $identity
      Assert-EndpointSourceMatch $source $current
      $restore.source = $current
    } catch {
      $restore.started = $false
      $restore.healthy = $null
      if (-not $restore.error) { $restore.error = 'restored identity: ' + $_.Exception.Message }
      return $restore
    }
  }
  $gate = Get-OwnershipRefusalFacts $stageDir $binding
  $restore.start_gate = [string]$gate.state
  $restore.startable = ($gate.state -in @('lock_free', 'no_lock_file')) -or
    (($gate.state -eq 'refused') -and [bool]$gate.owner_ended)
  if ($restore.restored) {
    if ($restore.startable) {
      try {
        Start-BoundTask $binding
        $restore.started = $true
        $health = Read-EndpointHealth ([string]$source.config_path)
        Assert-HealthSourceMatch $health $source
        $runningTask = Get-BoundTask $binding 'Stop'
        $runningProjection = Get-TaskProjection $runningTask
        $restore.listener = Get-ListenerEvidence $binding $runningProjection
        $restore.healthy = $true
      } catch { if (-not $restore.error) { $restore.error = 'start/health: ' + $_.Exception.Message } }
    } elseif (-not $restore.error) {
      $restore.error = 'the previous endpoint was not started: start gate is ' + [string]$gate.state
    }
  }
  return $restore
}
function Get-WriterResidue([object]$binding) {
  # The endpoint's own writers keep their image under the generation or the instance state root;
  # the sweep loop ends only with no listener AND no such process left (deployment.md settle).
  # Path identity, not path spelling: the declared roots travel slash-normalized while
  # Win32_Process reports backslash images, so both sides compare normalized; the separator
  # stays a boundary and a neighbouring directory never counts.
  $prefixState = (Normalize-PathIdentity $binding.state_root).ToLower()
  $prefixGeneration = (Normalize-PathIdentity $binding.generation).ToLower()
  Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
    $image = if ($_.ExecutablePath) { (Normalize-PathIdentity $_.ExecutablePath).ToLower() } else { '' }
    ($image.StartsWith($prefixState + '/')) -or ($image.StartsWith($prefixGeneration + '/'))
  }
}
function Stop-Endpoint([object]$binding) {
  $stopTask = Get-TaskSelectionParameters $binding
  $stopTask.ErrorAction = 'Stop'
  Stop-ScheduledTask @stopTask
  $deadline = (Get-Date).AddSeconds(30)
  while ($true) {
    $listening = Get-NetTCPConnection -LocalPort $binding.port -State Listen -ErrorAction SilentlyContinue
    $residual = @(Get-WriterResidue $binding)
    if (-not $listening -and $residual.Count -eq 0) { return }
    if ((Get-Date) -ge $deadline) { break }
    Start-Sleep -Milliseconds 500
  }
  throw 'stop_failed: the old endpoint still listens or leaves a writing process under the generation/state root; refusing to switch'
}

# The execution account's canonical domain pins: the endpoint service and same-host independent
# calls resolve the one CAD domain through these; an existing pin may only restate the bound
# canonical path - never create a second domain or silently re-point the served generation.
function Get-AccountEnvironment([string]$Name) {
  return [string][Environment]::GetEnvironmentVariable($Name, 'User')
}
function Set-AccountEnvironment([string]$Name, [string]$Value) {
  [Environment]::SetEnvironmentVariable($Name, $Value, 'User')
}
function Domain-OwnershipPath([string]$StateRoot) {
  return (Join-Path $StateRoot 'cad-ownership.json')
}
function Get-ServedStateRoot([string]$configPath) {
  # The served generation's own runtime reads its endpoint.json through the official parser;
  # the binding's state_root never self-grants - it must equal what the configuration declares.
  $generation = Split-Path -Parent $configPath
  $runtime = Runtime-Python $generation
  $program = "import json,sys;from description_pipeline.orchestration import windows;" +
    "config=windows.read_config(sys.argv[1]);print(json.dumps({'state_root':str(config['state_root'])}))"
  $output = & $runtime -I -B -c $program $configPath
  if ($LASTEXITCODE -ne 0) {
    Die 'served_config_unreadable' ('the served endpoint configuration cannot be read officially: ' + $configPath)
  }
  return [string]((($output | Select-Object -Last 1) | ConvertFrom-Json).state_root)
}
function Normalize-PathIdentity([string]$value) {
  # Windows path declarations travel in more than one legal spelling: the platform.json wire
  # format and the rendered bindings use forward slashes, while the official reader
  # (windows.read_config -> Path.resolve()) reports backslash-normalized paths.  Identity
  # comparisons are representation-insensitive over the two separators and a trailing slash;
  # genuinely different paths still differ.
  if ($null -eq $value) { return '' }
  return ([string]$value).Replace('\', '/').TrimEnd('/')
}
function Assert-DomainPins([string]$StateRoot, [string]$ServedGeneration) {
  $officialRoot = ''
  if ($ServedGeneration) {
    $officialRoot = Get-ServedStateRoot (Config-Path $ServedGeneration)
    if ((Normalize-PathIdentity $officialRoot) -ne (Normalize-PathIdentity $StateRoot)) {
      Die 'state_root_mismatch' ('the served configuration binds another state root: ' + $officialRoot)
    }
  }
  $canonical = Domain-OwnershipPath $StateRoot
  $pinned = [string](Get-AccountEnvironment 'DESCRIPTION_CAD_OWNERSHIP_FILE')
  if ($pinned -and ((Normalize-PathIdentity $pinned) -ne (Normalize-PathIdentity $canonical))) {
    Die 'cad_domain_override_refused' ('the bound CAD domain does not match this installation: ' + $pinned)
  }
  $pinnedRoot = [string](Get-AccountEnvironment 'DESCRIPTION_ENDPOINT_ROOT')
  if ($pinnedRoot -and $ServedGeneration -and
      ((Normalize-PathIdentity $pinnedRoot) -ne (Normalize-PathIdentity $ServedGeneration))) {
    Die 'cad_domain_override_refused' ('the endpoint-root pin does not restate the served generation: ' + $pinnedRoot)
  }
  return @{ canonical=$canonical; previous_ownership=$pinned; previous_root=$pinnedRoot }
}
function Set-DomainPins([string]$StateRoot, [string]$Generation) {
  Set-AccountEnvironment 'DESCRIPTION_ENDPOINT_ROOT' $Generation
  Set-AccountEnvironment 'DESCRIPTION_CAD_OWNERSHIP_FILE' (Domain-OwnershipPath $StateRoot)
}

function Get-BytesSha256([byte[]]$Bytes) {
  $sha = [System.Security.Cryptography.SHA256]::Create()
  try {
    return ([System.BitConverter]::ToString($sha.ComputeHash($Bytes))).Replace('-', '').ToLowerInvariant()
  } finally {
    $sha.Dispose()
  }
}
function Get-TextSha256([string]$Text) {
  $encoding = New-Object System.Text.UTF8Encoding($false)
  return Get-BytesSha256 ($encoding.GetBytes($Text))
}
function Get-JsonSha256($Document) {
  return Get-TextSha256 ($Document | ConvertTo-Json -Depth 20 -Compress)
}
function Get-ReleaseIdentitySha256($Release) {
  return Get-TextSha256 (
    [string]$Release.version + "`n" +
    [string]$Release.source_commit + "`n" +
    [string]$Release.source_sha256 + "`n" +
    [string]$Release.role)
}
function Resolve-Sid([string]$Identity) {
  if (-not $Identity) { throw 'identity is empty' }
  try {
    return ([System.Security.Principal.SecurityIdentifier]$Identity).Value
  } catch {
    return (New-Object System.Security.Principal.NTAccount($Identity)).Translate(
      [System.Security.Principal.SecurityIdentifier]).Value
  }
}
function Get-AclProjection([string]$Path) {
  $acl = Get-Acl -LiteralPath $Path -ErrorAction Stop
  $sections = [System.Security.AccessControl.AccessControlSections]::All
  $rules = @($acl.GetAccessRules($true, $true, [System.Security.Principal.SecurityIdentifier]) |
    ForEach-Object {
      [ordered]@{
        sid = Resolve-Sid ([string]$_.IdentityReference)
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
  $projection = [ordered]@{
    owner_sid = Resolve-Sid ([string]$acl.Owner)
    group_sid = Resolve-Sid ([string]$acl.Group)
    protected = [bool]$acl.AreAccessRulesProtected
    sddl = [string]$acl.GetSecurityDescriptorSddlForm($sections)
    rules = $rules
  }
  $projection['projection_sha256'] = Get-AclProjectionSha256 $projection
  return $projection
}
function Get-AclProjectionSha256($Projection) {
  # ACL projections cross PowerShell and Python canonical JSON transports.  Serialize a
  # dedicated fixed-order document rather than the surrounding object's incidental key order.
  $rules = @($Projection.rules | ForEach-Object {
    [ordered]@{
      sid = [string]$_.sid
      access_type = [string]$_.access_type
      rights = [long]$_.rights
      inherited = [bool]$_.inherited
      inheritance = [int]$_.inheritance
      propagation = [int]$_.propagation
    }
  })
  $canonical = [ordered]@{
    owner_sid = [string]$Projection.owner_sid
    group_sid = [string]$Projection.group_sid
    protected = [bool]$Projection.protected
    sddl = [string]$Projection.sddl
    rules = $rules
  }
  return Get-TextSha256 ($canonical | ConvertTo-Json -Depth 8 -Compress)
}
function Set-AclProjection([string]$Path, $Projection) {
  $sections = [System.Security.AccessControl.AccessControlSections]::All
  $item = Get-Item -LiteralPath $Path -Force -ErrorAction Stop
  if ($item.PSIsContainer) {
    $security = New-Object System.Security.AccessControl.DirectorySecurity
  } else {
    $security = New-Object System.Security.AccessControl.FileSecurity
  }
  $security.SetSecurityDescriptorSddlForm([string]$Projection.sddl, $sections)
  Set-Acl -LiteralPath $Path -AclObject $security -ErrorAction Stop
}
function Get-ManagedAdministratorsSid { return 'S-1-5-32-544' }
function Get-ManagedSystemSid { return 'S-1-5-18' }
function Assert-ManagedOperatorIsAdministrator {
  $identity = [System.Security.Principal.WindowsIdentity]::GetCurrent()
  $principal = New-Object System.Security.Principal.WindowsPrincipal -ArgumentList $identity
  if (-not $principal.IsInRole(
      [System.Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Die 'windows_operator_not_administrator' 'the deployment operator is not an administrator'
  }
  return $identity.User.Value
}
function Get-ManagedServiceAuthorization([string]$ServiceSid) {
  $expectedSid = Resolve-Sid $ServiceSid
  $operatorSid = Assert-ManagedOperatorIsAdministrator
  if ($operatorSid -ne $expectedSid) {
    Die 'windows_operator_runtime_mismatch' 'the deployment operator and endpoint runtime must be the same administrator account'
  }
  try {
    $localUser = Get-LocalUser -SID $expectedSid -ErrorAction Stop
  } catch {
    Die 'windows_service_identity_not_local' ('the endpoint service SID is not a local user: ' + $expectedSid)
  }
  if ($null -eq $localUser -or -not [bool]$localUser.Enabled) {
    Die 'windows_service_identity_invalid' 'the endpoint service local user is missing or disabled'
  }
  Add-Type -AssemblyName System.DirectoryServices.AccountManagement
  $context = New-Object System.DirectoryServices.AccountManagement.PrincipalContext -ArgumentList (
    [System.DirectoryServices.AccountManagement.ContextType]::Machine)
  $principal = $null
  try {
    $principal = [System.DirectoryServices.AccountManagement.UserPrincipal]::FindByIdentity(
      $context,
      [System.DirectoryServices.AccountManagement.IdentityType]::Sid,
      $expectedSid)
    if ($null -eq $principal) {
      Die 'windows_service_identity_invalid' 'the endpoint service account cannot be resolved on this host'
    }
    $groupSids = @($principal.GetAuthorizationGroups() | ForEach-Object {
      [string]$_.SID.Value
    })
  } catch {
    Die 'windows_service_identity_unknown' 'the endpoint service authorization groups are unreadable'
  } finally {
    if ($principal) { $principal.Dispose() }
    $context.Dispose()
  }
  return [pscustomobject]@{
    sid=$expectedSid
    administrator=($groupSids -contains (Get-ManagedAdministratorsSid))
  }
}
function Get-ManagedServiceSid($Task) {
  $authorization = Get-ManagedServiceAuthorization (
    Resolve-Sid ([string]$Task.Principal.UserId))
  if (-not [bool]$authorization.administrator) {
    Die 'windows_runtime_not_administrator' 'the endpoint runtime is not an administrator'
  }
  return [string]$authorization.sid
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
function Assert-ManagedAcl(
  [string]$Path,
  [bool]$Protected,
  [string]$ServiceSid = '',
  [switch]$NonInheriting,
  [switch]$Recurse) {
  $pending = New-Object 'System.Collections.Generic.Stack[string]'
  $pending.Push([System.IO.Path]::GetFullPath($Path))
  $administratorsSid = Get-ManagedAdministratorsSid
  $systemSid = Get-ManagedSystemSid
  if (-not $ServiceSid) {
    $ServiceSid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    $ServiceSid = Resolve-Sid $ServiceSid
    $authorization = Get-ManagedServiceAuthorization $ServiceSid
    if (-not [bool]$authorization.administrator) {
      Die 'windows_runtime_not_administrator' 'the endpoint runtime is not an administrator'
    }
  } else {
    $ServiceSid = Resolve-Sid $ServiceSid
  }
  if ($ServiceSid -in @($administratorsSid, $systemSid)) {
    Die 'windows_managed_acl_invalid' 'the service principal aliases a boundary principal'
  }
  while ($pending.Count -gt 0) {
    $current = $pending.Pop()
    $item = Get-Item -LiteralPath $current -Force -ErrorAction Stop
    if ($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) {
      Die 'windows_managed_acl_unsafe' ('a managed ACL path is a reparse point: ' + $current)
    }
    $projection = Get-AclProjection $current
    $expectedServiceRights =
      if (-not $item.PSIsContainer -and
          (($item.Attributes -band [System.IO.FileAttributes]::ReadOnly) -ne 0)) {
        [System.Security.AccessControl.FileSystemRights]::ReadAndExecute
      } else {
        [System.Security.AccessControl.FileSystemRights]::FullControl
      }
    $expectedRights = @{
      $systemSid = [long][System.Security.AccessControl.FileSystemRights]::FullControl
      $administratorsSid = [long][System.Security.AccessControl.FileSystemRights]::FullControl
      $ServiceSid = [long]$expectedServiceRights
    }
    $expectedInheritance = if ($item.PSIsContainer -and -not $NonInheriting) {
      [int](
        [System.Security.AccessControl.InheritanceFlags]::ContainerInherit -bor
        [System.Security.AccessControl.InheritanceFlags]::ObjectInherit
      )
    } else {
      [int][System.Security.AccessControl.InheritanceFlags]::None
    }
    $rules = @($projection.rules)
    $invalid = $projection.owner_sid -ne $administratorsSid -or
      [bool]$projection.protected -ne $Protected -or
      $rules.Count -ne $expectedRights.Count
    $seenRuleSids = @{}
    foreach ($rule in $rules) {
      $ruleSid = [string]$rule.sid
      $rightsMatch = $false
      if ($expectedRights.ContainsKey($ruleSid)) {
        $expectedRuleRights = [long]$expectedRights[$ruleSid]
        $actualRuleRights = [long]$rule.rights
        $synchronize = [long][System.Security.AccessControl.FileSystemRights]::Synchronize
        $rightsMatch = $actualRuleRights -eq $expectedRuleRights -or
          ($expectedRuleRights -eq [long][System.Security.AccessControl.FileSystemRights]::ReadAndExecute -and
           $actualRuleRights -eq ($expectedRuleRights -bor $synchronize))
      }
      if ($rule.access_type -ne 'Allow' -or
          $seenRuleSids.ContainsKey($ruleSid) -or
          -not $expectedRights.ContainsKey($ruleSid) -or
          -not $rightsMatch -or
          [int]$rule.inheritance -ne $expectedInheritance -or
          [int]$rule.propagation -ne 0 -or
          ($Protected -and [bool]$rule.inherited) -or
          ((-not $Protected) -and -not [bool]$rule.inherited)) {
        $invalid = $true
      }
      $seenRuleSids[$ruleSid] = $true
    }
    if ($seenRuleSids.Count -ne $expectedRights.Count) {
      $invalid = $true
    }
    if ($invalid) {
      $observed = @($rules | ForEach-Object {
        [string]$_.sid + '/' + [string]$_.rights + '/' +
          [string]$_.inheritance + '/' + [string]$_.inherited
      }) -join ';'
      $expected = @($expectedRights.GetEnumerator() | ForEach-Object {
        [string]$_.Key + '/' + [string]$_.Value
      }) -join ';'
      Die 'windows_managed_acl_invalid' (
        'the managed path does not match its minimum permission matrix: ' + $current +
        ' owner=' + [string]$projection.owner_sid +
        ' protected=' + [string]$projection.protected +
        ' expected=' + $expected +
        ' rules=' + $observed)
    }
    if ($Recurse -and $item.PSIsContainer) {
      foreach ($child in @(Get-ChildItem -LiteralPath $current -Force -ErrorAction Stop)) {
        $pending.Push($child.FullName)
      }
    }
  }
}
function Set-ManagedAcl(
  [string]$Path,
  [bool]$Protected,
  [string]$ServiceSid = '',
  [switch]$NonInheriting,
  [switch]$Recurse) {
  $pending = New-Object 'System.Collections.Generic.Stack[string]'
  $pending.Push([System.IO.Path]::GetFullPath($Path))
  $administratorsSid = Get-ManagedAdministratorsSid
  $systemSid = Get-ManagedSystemSid
  if (-not $ServiceSid) {
    $ServiceSid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
  }
  $ServiceSid = Resolve-Sid $ServiceSid
  $authorization = Get-ManagedServiceAuthorization $ServiceSid
  if (-not [bool]$authorization.administrator) {
    Die 'windows_runtime_not_administrator' 'the endpoint runtime is not an administrator'
  }
  while ($pending.Count -gt 0) {
    $current = $pending.Pop()
    $item = Get-Item -LiteralPath $current -Force -ErrorAction Stop
    if ($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) {
      Die 'windows_managed_acl_unsafe' ('a managed ACL path is a reparse point: ' + $current)
    }
    $acl = Get-Acl -LiteralPath $current -ErrorAction Stop
    $administratorsObject = New-Object System.Security.Principal.SecurityIdentifier -ArgumentList $administratorsSid
    $acl.SetOwner($administratorsObject)
    $acl.SetAccessRuleProtection($Protected, (-not $Protected))
    foreach ($rule in @($acl.GetAccessRules(
        $true,
        $false,
        [System.Security.Principal.SecurityIdentifier]))) {
      [void]$acl.RemoveAccessRuleSpecific($rule)
    }
    if ($item.PSIsContainer -and -not $NonInheriting) {
      $inheritance =
        [System.Security.AccessControl.InheritanceFlags]::ContainerInherit -bor
        [System.Security.AccessControl.InheritanceFlags]::ObjectInherit
    } else {
      $inheritance = [System.Security.AccessControl.InheritanceFlags]::None
    }
    if ($Protected) {
      $serviceRights =
        if (-not $item.PSIsContainer -and
            (($item.Attributes -band [System.IO.FileAttributes]::ReadOnly) -ne 0)) {
          [System.Security.AccessControl.FileSystemRights]::ReadAndExecute
        } else {
          [System.Security.AccessControl.FileSystemRights]::FullControl
        }
      [void]$acl.AddAccessRule((New-ManagedAccessRule $systemSid (
        [System.Security.AccessControl.FileSystemRights]::FullControl) $inheritance))
      [void]$acl.AddAccessRule((New-ManagedAccessRule $administratorsSid (
        [System.Security.AccessControl.FileSystemRights]::FullControl) $inheritance))
      [void]$acl.AddAccessRule((New-ManagedAccessRule $ServiceSid $serviceRights $inheritance))
    }
    Set-Acl -LiteralPath $current -AclObject $acl -ErrorAction Stop
    Assert-ManagedAcl $current $Protected $ServiceSid -NonInheriting:$NonInheriting
    if ($Recurse -and $item.PSIsContainer) {
      foreach ($child in @(Get-ChildItem -LiteralPath $current -Force -ErrorAction Stop)) {
        $pending.Push($child.FullName)
      }
    }
  }
}
function Protect-ManagedArchive(
  [string]$Path,
  [string]$ExpectedSha256,
  [string]$ServiceSid) {
  if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
    Die 'archive_missing' ('the managed archive is missing: ' + $Path)
  }
  Set-ItemProperty -LiteralPath $Path -Name IsReadOnly -Value $true -ErrorAction Stop
  Set-ManagedAcl $Path $true $ServiceSid -NonInheriting
  $item = Get-Item -LiteralPath $Path -Force -ErrorAction Stop
  if (-not $item.IsReadOnly) {
    Die 'archive_read_only_failed' ('the managed archive is not read-only: ' + $Path)
  }
  if ((Sha256 $Path) -ne $ExpectedSha256) {
    Die 'archive_mismatch' ('the managed archive digest differs after read-only protection: ' + $Path)
  }
}
function Assert-ManagedArchive(
  [string]$Path,
  [string]$ExpectedSha256,
  [string]$ServiceSid) {
  if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
    Die 'archive_missing' ('the managed archive is missing: ' + $Path)
  }
  $item = Get-Item -LiteralPath $Path -Force -ErrorAction Stop
  if (-not $item.IsReadOnly) {
    Die 'archive_read_only_failed' ('the managed archive is not read-only: ' + $Path)
  }
  Assert-ManagedAcl $Path $true $ServiceSid -NonInheriting
  if ((Sha256 $Path) -ne $ExpectedSha256) {
    Die 'archive_mismatch' ('the managed archive digest differs from the release binding: ' + $Path)
  }
}
function Test-ManagedPathWithin([string]$Path, [string]$Root) {
  $normalizedPath = (Normalize-PathIdentity $Path).TrimEnd('/')
  $normalizedRoot = (Normalize-PathIdentity $Root).TrimEnd('/')
  return $normalizedPath -eq $normalizedRoot -or
    $normalizedPath.StartsWith(
      $normalizedRoot + '/',
      [System.StringComparison]::OrdinalIgnoreCase)
}
function Get-ManagedEndpointPaths($Config, [string]$EndpointRoot, [string]$ConfigPath, [string]$InstallIdPath) {
  $paths = @()
  $seen = @{}
  foreach ($path in @(
      [string]$Config.package_root,
      [string]$Config.output_root,
      [string]$Config.state_root,
      $EndpointRoot,
      $ConfigPath,
      [string]$Config.token_file,
      $InstallIdPath)) {
    if (-not [System.IO.Path]::IsPathRooted($path)) {
      Die 'windows_managed_acl_boundary_invalid' ('a managed endpoint path is not absolute: ' + $path)
    }
    $normalized = (Normalize-PathIdentity $path).TrimEnd('/')
    $key = $normalized.ToLowerInvariant()
    if (-not $seen.ContainsKey($key)) {
      $seen[$key] = $true
      $paths += $normalized
    }
  }
  return $paths
}
function Get-ManagedCommonParent([string[]]$Paths) {
  if ($Paths.Count -eq 0) {
    Die 'windows_managed_acl_boundary_invalid' 'the managed endpoint path set is empty'
  }
  $common = [System.IO.Path]::GetFullPath($Paths[0])
  foreach ($path in @($Paths | Select-Object -Skip 1)) {
    $candidate = [System.IO.Path]::GetFullPath($path)
    while (-not (Test-ManagedPathWithin $candidate $common)) {
      $parent = [System.IO.Path]::GetDirectoryName($common)
      if (-not $parent -or $parent -eq $common) {
        Die 'windows_managed_acl_boundary_invalid' 'the managed endpoint paths have no safe common parent'
      }
      $common = $parent
    }
  }
  $common = (Normalize-PathIdentity $common).TrimEnd('/')
  $driveRoot = (Normalize-PathIdentity ([System.IO.Path]::GetPathRoot($common))).TrimEnd('/')
  if (-not $driveRoot -or $common -eq $driveRoot) {
    Die 'windows_managed_acl_boundary_invalid' 'the managed common parent is a filesystem root'
  }
  $specialRoots = @(
    $env:SystemRoot,
    $env:ProgramFiles,
    ${env:ProgramFiles(x86)},
    $env:ProgramData,
    $env:USERPROFILE,
    (Split-Path -Parent $env:USERPROFILE)
  ) | Where-Object { $_ } | ForEach-Object {
    (Normalize-PathIdentity ([System.IO.Path]::GetFullPath([string]$_))).TrimEnd('/')
  }
  $systemRoot = (Normalize-PathIdentity (
    [System.IO.Path]::GetFullPath([string]$env:SystemRoot))).TrimEnd('/')
  if (@($specialRoots | Where-Object { Test-ManagedPathWithin $common $_ }).Count -gt 0) {
    Die 'windows_managed_acl_boundary_invalid' ('the managed common parent is too broad: ' + $common)
  }
  Assert-NoReparseAncestor $common
  if (-not (Test-Path -LiteralPath $common -PathType Container)) {
    Die 'windows_managed_acl_boundary_invalid' ('the managed common parent is not a directory: ' + $common)
  }
  foreach ($path in $Paths) {
    if (-not (Test-ManagedPathWithin $path $common)) {
      Die 'windows_managed_acl_boundary_invalid' ('a managed endpoint path is outside the common parent: ' + $path)
    }
  }
  return $common
}
function Get-ManagedEndpointAclRows(
  $Config,
  [string]$EndpointRoot,
  [string]$ConfigPath,
  [string]$InstallIdPath,
  [string]$TaskFile = '') {
  $paths = @(Get-ManagedEndpointPaths $Config $EndpointRoot $ConfigPath $InstallIdPath)
  $common = Get-ManagedCommonParent $paths
  $managedRootMarker = Join-Path $common '.description-managed-root.json'
  $expectedMarker = '{"schema_version":"solidworks-to-urdf.managed-root/v1"}'
  if (-not (Test-Path -LiteralPath $managedRootMarker -PathType Leaf) -or
      (Read-Utf8Text $managedRootMarker).Trim() -ne $expectedMarker) {
    Die 'windows_managed_acl_marker_missing' (
      'the common parent is not marked for exclusive description-pipeline management: ' + $common)
  }
  Assert-ManagedAcl $managedRootMarker $true
  $stateRoot = Normalize-PathIdentity ([string]$Config.state_root)
  $lifecycleRoot = Normalize-PathIdentity (
    [System.IO.Path]::Combine([string]$Config.state_root, 'lifecycle'))
  $secretRoot = Normalize-PathIdentity (
    [System.IO.Path]::GetDirectoryName([string]$Config.token_file))
  foreach ($writable in @(
      [string]$Config.package_root,
      [string]$Config.output_root,
      $stateRoot,
      $lifecycleRoot,
      $EndpointRoot,
      $common)) {
    if ((Normalize-PathIdentity $writable) -eq $secretRoot) {
      Die 'windows_managed_acl_secret_root_unsafe' 'the token directory is also a writable managed root'
    }
  }
  $rows = @()
  $seen = @{}
  $rules = @(
    @{role='common_parent'; path=$common; recurse=$false; non_inheriting=$false; required=$true},
    @{role='managed_root_marker'; path=$managedRootMarker; recurse=$false; non_inheriting=$false; required=$true},
    @{role='package_root'; path=$Config.package_root; recurse=$false; non_inheriting=$false; required=$true},
    @{role='output_root'; path=$Config.output_root; recurse=$false; non_inheriting=$false; required=$true},
    @{role='state_root'; path=$stateRoot; recurse=$false; non_inheriting=$false; required=$true},
    @{role='lifecycle_root'; path=$lifecycleRoot; recurse=$false; non_inheriting=$false; required=$true},
    @{role='endpoint_secrets_root'; path=$secretRoot; recurse=$true; non_inheriting=$false; required=$true},
    @{role='endpoint_root'; path=$EndpointRoot; recurse=$true; non_inheriting=$false; required=$true},
    @{role='endpoint_config'; path=$ConfigPath; recurse=$false; non_inheriting=$false; required=$true},
    @{role='endpoint_token'; path=$Config.token_file; recurse=$false; non_inheriting=$false; required=$true},
    @{role='install_id'; path=$InstallIdPath; recurse=$false; non_inheriting=$false; required=$true},
    @{role='cad_ownership'; path=([System.IO.Path]::Combine($stateRoot, 'cad-ownership.json')); recurse=$false; non_inheriting=$false; required=$false},
    @{role='cad_lease'; path=([System.IO.Path]::Combine($stateRoot, 'cad-ownership.json.lease')); recurse=$false; non_inheriting=$false; required=$false}
  )
  if ($TaskFile) {
    $rules += @{
      role='task_file'
      path=$TaskFile
      recurse=$false
      non_inheriting=$false
      required=$true
    }
  }
  foreach ($rule in $rules) {
    $normalized = (Normalize-PathIdentity ([string]$rule.path)).TrimEnd('/')
    Assert-NoReparseAncestor $normalized
    $key = $normalized.ToLowerInvariant()
    if (-not $seen.ContainsKey($key)) {
      $seen[$key] = $rule
      $rows += [pscustomobject]@{
        role=[string]$rule.role
        path=$normalized
        recurse=[bool]$rule.recurse
        non_inheriting=[bool]$rule.non_inheriting
        allow_service_owner=$true
        required=[bool]$rule.required
      }
    }
  }
  return $rows
}
function Assert-ManagedRuntimeLeafAcl(
  [string]$Path,
  [string]$ParentPath,
  [string]$ServiceSid) {
  # Atomic runtime writes inherit the verified state directory's exact DACL.
  Assert-NoReparseAncestor $Path
  Assert-ManagedAcl $ParentPath $true $ServiceSid
  $parent = Get-Item -LiteralPath $ParentPath -Force -ErrorAction Stop
  $leaf = Get-Item -LiteralPath $Path -Force -ErrorAction Stop
  if (-not $parent.PSIsContainer -or $leaf.PSIsContainer -or
      ([System.IO.Path]::GetDirectoryName([System.IO.Path]::GetFullPath($Path)) -ne
       [System.IO.Path]::GetFullPath($ParentPath)) -or
      (($leaf.Attributes -band [System.IO.FileAttributes]::ReadOnly) -ne 0)) {
    Die 'windows_managed_acl_invalid' ('the runtime leaf is outside its writable state boundary: ' + $Path)
  }
  $projection = Get-AclProjection $Path
  Assert-ManagedAcl $Path ([bool]$projection.protected) $ServiceSid
}
function Set-ManagedEndpointAcls(
  $Config,
  [string]$EndpointRoot,
  [string]$ConfigPath,
  [string]$InstallIdPath,
  [string]$TaskFile = '') {
  $serviceSid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
  foreach ($row in @(Get-ManagedEndpointAclRows $Config $EndpointRoot $ConfigPath $InstallIdPath $TaskFile)) {
    try {
    if (-not (Test-Path -LiteralPath $row.path)) {
      if ($row.required) {
        Die 'windows_managed_acl_missing' ('a managed endpoint path is missing: ' + $row.path)
      }
      continue
    }
    Set-ManagedAcl $row.path $true $serviceSid `
      -NonInheriting:$row.non_inheriting -Recurse:$row.recurse
    } catch {
      Add-LifecycleErrorContext $_ ([string]$row.role) 'set' ([string]$row.path)
      throw
    }
  }
}
function Assert-ManagedEndpointAcls(
  $Config,
  [string]$EndpointRoot,
  [string]$ConfigPath,
  [string]$InstallIdPath,
  [string]$TaskFile = '') {
  $serviceSid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
  foreach ($row in @(Get-ManagedEndpointAclRows $Config $EndpointRoot $ConfigPath $InstallIdPath $TaskFile)) {
    try {
    if (-not (Test-Path -LiteralPath $row.path)) {
      if ($row.required) {
        Die 'windows_managed_acl_missing' ('a managed endpoint path is missing: ' + $row.path)
      }
      continue
    }
    if ($row.role -in @('cad_ownership', 'cad_lease')) {
      Assert-ManagedRuntimeLeafAcl $row.path ([string]$Config.state_root) $serviceSid
    } else {
      Assert-ManagedAcl $row.path $true $serviceSid `
        -NonInheriting:$row.non_inheriting -Recurse:$row.recurse
    }
    } catch {
      Add-LifecycleErrorContext $_ ([string]$row.role) 'assert' ([string]$row.path)
      throw
    }
  }
}
function Get-ManagedAclProjectionForRole($Acl, [string]$Role) {
  switch ($Role) {
    'common_parent' { return $Acl.common_parent }
    'managed_root_marker' { return $Acl.managed_root_marker }
    'lifecycle_root' { return $Acl.lifecycle_root }
    'endpoint_secrets_root' { return $Acl.endpoint_secrets_root }
    'task_file' { return $Acl.task_file }
    'endpoint_config' { return $Acl.endpoint_config }
    'endpoint_token' { return $Acl.endpoint_token }
    'install_id' { return $Acl.install_id }
    'cad_ownership' { return $Acl.cad_ownership }
    'cad_lease' { return $Acl.cad_lease }
    'package_root' { return $Acl.roots.package_root }
    'output_root' { return $Acl.roots.output_root }
    'state_root' { return $Acl.roots.state_root }
    'endpoint_root' { return $Acl.roots.endpoint_root }
    default { Die 'windows_recovery_state_invalid' ('unknown Windows ACL role: ' + $Role) }
  }
}
function Get-TaskFilePath($Task) {
  $root = Join-Path $env:SystemRoot 'System32\Tasks'
  $relative = ([string]$Task.TaskPath).TrimStart('\') + [string]$Task.TaskName
  $path = [System.IO.Path]::GetFullPath((Join-Path $root $relative))
  $prefix = [System.IO.Path]::GetFullPath($root) + [System.IO.Path]::DirectorySeparatorChar
  if (-not $path.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw 'the scheduled task file escapes the Task Scheduler root'
  }
  return $path
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
    Die 'windows_recovery_target_unsafe' ('recovery path crosses a reparse point: ' + $unsafe)
  }
}
function Register-ManagedTask(
  [string]$TaskName,
  [string]$TaskPath,
  [string]$TaskXml,
  [switch]$Force
) {
  $parameters = @{
    TaskName = $TaskName
    Xml = $TaskXml
    ErrorAction = 'Stop'
  }
  if ($TaskPath) { $parameters.TaskPath = $TaskPath }
  if ($Force) { $parameters.Force = $true }
  Register-ScheduledTask @parameters | Out-Null
}
function Stop-ManagedTask([string]$TaskName, [string]$TaskPath) {
  $parameters = @{TaskName = $TaskName; ErrorAction = 'Stop'}
  if ($TaskPath) { $parameters.TaskPath = $TaskPath }
  Stop-ScheduledTask @parameters
}
function Disable-ManagedTask([string]$TaskName, [string]$TaskPath) {
  $parameters = @{TaskName = $TaskName; ErrorAction = 'Stop'}
  if ($TaskPath) { $parameters.TaskPath = $TaskPath }
  Disable-ScheduledTask @parameters | Out-Null
}
function Enable-ManagedTask([string]$TaskName, [string]$TaskPath) {
  $parameters = @{TaskName = $TaskName; ErrorAction = 'Stop'}
  if ($TaskPath) { $parameters.TaskPath = $TaskPath }
  Enable-ScheduledTask @parameters | Out-Null
}
function Get-RecoveryTask([string]$TaskName, [string]$TaskPath = '') {
  $tasks = @(Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue)
  if ($TaskPath) {
    $tasks = @($tasks | Where-Object { [string]$_.TaskPath -eq $TaskPath })
  }
  if ($tasks.Count -ne 1) {
    Die 'windows_task_ambiguous' ('expected exactly one managed task, found ' + $tasks.Count)
  }
  return $tasks[0]
}
function New-TaskSemanticDocument(
    [string]$TaskPath,
    [string]$TaskName,
    $Principal,
    $Action,
    $Settings,
    [string]$TriggersSha256,
    [string]$SecurityDescriptorSha256) {
  return [ordered]@{
    task_path = $TaskPath
    task_name = $TaskName
    uri = ($TaskPath + $TaskName)
    principal = $Principal
    action = $Action
    settings = $Settings
    triggers_sha256 = $TriggersSha256
    security_descriptor_sha256 = $SecurityDescriptorSha256
  }
}
function Get-TaskProjection($Task) {
  if (@($Task.Actions).Count -ne 1) {
    Die 'windows_action_invalid' 'the Windows task must carry exactly one action'
  }
  $authorization = Get-ManagedServiceAuthorization (
    Resolve-Sid ([string]$Task.Principal.UserId))
  $principalSid = [string]$authorization.sid
  if (-not [bool]$authorization.administrator) {
    Die 'windows_runtime_not_administrator' 'the endpoint runtime is not an administrator'
  }
  $xml = [xml](Export-ScheduledTask -TaskName $Task.TaskName -TaskPath $Task.TaskPath)
  $xmlText = [string]$xml.OuterXml
  $passwordNodes = @($xml.SelectNodes('//*[local-name()="Password" and string-length(normalize-space(.)) > 0]'))
  if ($passwordNodes.Count -ne 0) {
    Die 'windows_secret_exposure_refused' 'the scheduled task XML carries a password'
  }
  $taskFile = Get-TaskFilePath $Task
  $taskFileAcl = Get-AclProjection $taskFile
  $principal = [ordered]@{
    user = [string]$Task.Principal.UserId
    sid = $principalSid
    logon_type = [string]$Task.Principal.LogonType
    run_level = [string]$Task.Principal.RunLevel
    group_id = [string]$Task.Principal.GroupId
    administrator = [bool]$authorization.administrator
  }
  if ($principal.logon_type -notin @('Interactive', 'InteractiveToken')) {
    Die 'windows_logon_type_unsupported' 'the task principal is not interactive'
  }
  if ($principal.run_level -notin @('Highest', 'HighestAvailable')) {
    Die 'windows_run_level_not_elevated' 'the task principal does not request the elevated administrator token'
  }
  $action = [ordered]@{
    execute = [string]$Task.Actions[0].Execute
    arguments = [string]$Task.Actions[0].Arguments
    working_directory = [string]$Task.Actions[0].WorkingDirectory
  }
  foreach ($field in @('execute', 'arguments', 'working_directory')) {
    if ([string]::IsNullOrWhiteSpace([string]$action[$field])) {
      Die 'windows_action_invalid' ('the scheduled task action misses ' + $field)
    }
  }
  $xmlActions = @($xml.SelectNodes('//*[local-name()="Exec"]'))
  $xmlExecute = @($xml.SelectNodes('//*[local-name()="Exec"]/*[local-name()="Command"]'))
  $xmlArguments = @($xml.SelectNodes('//*[local-name()="Exec"]/*[local-name()="Arguments"]'))
  $xmlWorking = @($xml.SelectNodes('//*[local-name()="Exec"]/*[local-name()="WorkingDirectory"]'))
  if ($xmlActions.Count -ne 1 -or
      $xmlExecute.Count -ne 1 -or [string]$xmlExecute[0].InnerText -ne $action.execute -or
      $xmlArguments.Count -ne 1 -or [string]$xmlArguments[0].InnerText -ne $action.arguments -or
      $xmlWorking.Count -ne 1 -or [string]$xmlWorking[0].InnerText -ne $action.working_directory) {
    Die 'windows_action_invalid' 'the scheduled task XML and live action differ'
  }
  $uriNode = $xml.SelectSingleNode('//*[local-name()="URI"]')
  if ($null -eq $uriNode -or [string]$uriNode.InnerText -ne ([string]$Task.TaskPath + [string]$Task.TaskName)) {
    Die 'windows_task_projection_mismatch' 'the scheduled task URI differs from its task path and name'
  }
  $settings = [ordered]@{
    multiple_instances = [string]$Task.Settings.MultipleInstances
    execution_time_limit = [string]$Task.Settings.ExecutionTimeLimit
    restart_count = [int]$Task.Settings.RestartCount
    restart_interval = [string]$Task.Settings.RestartInterval
    start_when_available = [bool]$Task.Settings.StartWhenAvailable
    disallow_start_if_on_batteries = [bool]$Task.Settings.DisallowStartIfOnBatteries
    stop_if_going_on_batteries = [bool]$Task.Settings.StopIfGoingOnBatteries
    run_only_if_network_available = [bool]$Task.Settings.RunOnlyIfNetworkAvailable
  }
  if ($settings.multiple_instances -cne 'IgnoreNew') {
    Die 'windows_action_invalid' 'the scheduled task does not enforce a single instance'
  }
  $semantic = New-TaskSemanticDocument ([string]$Task.TaskPath) ([string]$Task.TaskName) `
    $principal $action $settings (Get-JsonSha256 @($Task.Triggers)) `
    ([string]$taskFileAcl.projection_sha256)
  return [ordered]@{
    task_path = [string]$Task.TaskPath
    task_name = [string]$Task.TaskName
    state = [string]$Task.State
    xml_sha256 = Get-TextSha256 $xmlText
    semantic_sha256 = Get-JsonSha256 $semantic
    principal = $principal
    action = $action
    settings = $settings
    triggers_sha256 = $semantic.triggers_sha256
    security_descriptor_sha256 = $semantic.security_descriptor_sha256
    xml = $xmlText
  }
}
function Get-RecoveryFileRow([string]$Role, [string]$Path, [string]$Member) {
  $item = Get-Item -LiteralPath $Path -ErrorAction Stop
  if ($item.PSIsContainer -or ($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint)) {
    Die 'windows_recovery_state_invalid' ('Windows recovery input is not a regular file: ' + $Path)
  }
  return [ordered]@{
    role = $Role
    path = Normalize-PathIdentity $Path
    member = $Member
    bytes = [long]$item.Length
    sha256 = Sha256 $Path
  }
}
function Read-EndpointRuntimeProbe([string]$Generation, [string]$ConfigPath) {
  $python = Runtime-Python $Generation
  if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw 'the endpoint generation runtime is missing'
  }
  $program = "import json,sys;" +
    "from description_pipeline.orchestration import windows;" +
    "from description_pipeline.runtime import tool_record;" +
    "c=windows.read_config(sys.argv[1]);" +
    "print(json.dumps({'config_digest':c['config_digest'],'package_root':str(c['package_root'])," +
    "'output_root':str(c['output_root']),'state_root':str(c['state_root'])," +
    "'port':c['port'],'release':tool_record(role='native')},sort_keys=True))"
  $output = & $python -I -B -c $program $ConfigPath
  if ($LASTEXITCODE -ne 0) {
    throw 'the installed endpoint runtime could not identify itself'
  }
  return (($output | Select-Object -Last 1) | ConvertFrom-Json)
}
function Get-EndpointIdentity($Task, $TaskProjection, [string]$SshHost) {
  $generation = Get-TaskGeneration $Task
  $configPath = Config-Path $generation
  Assert-NoReparseAncestor $configPath
  $configDocument = Read-Json $configPath
  $early = Get-EndpointConfigEarlyProblems $configPath
  if ($early.unreadable -or @($early.duplicates).Count -gt 0 -or
      @($early.unknown).Count -gt 0) {
    Die 'endpoint_config_invalid' 'the managed endpoint configuration is not the strict current schema'
  }
  $stateRoot = [string]$configDocument.state_root
  $lifecycleRoot = Join-Path $stateRoot 'lifecycle'
  foreach ($path in @(
      $configPath,
      ([string]$Task.Actions[0].Execute),
      $generation,
      $stateRoot,
      $lifecycleRoot)) {
    Assert-NoReparseAncestor $path
  }
  if (-not (Normalize-PathIdentity $generation).StartsWith(
        (Normalize-PathIdentity $lifecycleRoot) + '/',
        [System.StringComparison]::OrdinalIgnoreCase)) {
    Die 'windows_task_projection_mismatch' 'the task generation is outside the managed lifecycle root'
  }
  if (-not (Test-Path -LiteralPath (Runtime-Python $generation))) {
    Die 'windows_endpoint_not_provisioned' 'the task generation has no installed runtime'
  }
  if ((Normalize-PathIdentity $configPath) -ne (Normalize-PathIdentity (Config-Path $generation))) {
    Die 'windows_task_projection_mismatch' 'the task action does not bind its generation configuration'
  }
  if (-not (Test-ActionConfigArgument (
        [string]$Task.Actions[0].Arguments) $configPath)) {
    Die 'windows_task_projection_mismatch' 'the raw source action does not carry its managed configuration path'
  }
  if ((Normalize-PathIdentity ([string]$Task.Actions[0].WorkingDirectory)) -ne
      (Normalize-PathIdentity ([string]$configDocument.state_root))) {
    Die 'windows_task_projection_mismatch' 'the task working directory differs from the endpoint state root'
  }
  try { $probe = Read-EndpointRuntimeProbe $generation $configPath }
  catch { Die 'windows_recovery_identity_unreadable' $_.Exception.Message }
  if ((Normalize-PathIdentity ([string]$configDocument.state_root)) -ne
        (Normalize-PathIdentity ([string]$probe.state_root)) -or
      (Normalize-PathIdentity ([string]$configDocument.package_root)) -ne
        (Normalize-PathIdentity ([string]$probe.package_root)) -or
      (Normalize-PathIdentity ([string]$configDocument.output_root)) -ne
        (Normalize-PathIdentity ([string]$probe.output_root)) -or
      [int]$configDocument.port -ne [int]$probe.port) {
    Die 'endpoint_config_identity_mismatch' 'the raw and official endpoint configuration identities differ'
  }
  $stateRoot = [string]$probe.state_root
  $installIdPath = Join-Path $stateRoot 'install-id'
  Assert-NoReparseAncestor $installIdPath
  if (-not (Test-Path -LiteralPath $installIdPath)) {
    Die 'windows_install_id_mismatch' 'the endpoint state root has no install identity'
  }
  $installId = (Get-Content -LiteralPath $installIdPath -Raw).Trim()
  if ($installId -notmatch '^win-[0-9a-f]{32}$') {
    Die 'windows_install_id_mismatch' 'the endpoint install identity is malformed'
  }
  $embedded = $probe.release.release
  if ($null -eq $embedded) {
    Die 'windows_release_identity_mismatch' 'the installed endpoint has no embedded release identity'
  }
  $release = [ordered]@{
    version = [string]$probe.release.version
    source_commit = [string]$embedded.source_commit
    source_sha256 = [string]$probe.release.source_sha256
    role = 'native'
  }
  $ownershipPin = Get-AccountEnvironment 'DESCRIPTION_CAD_OWNERSHIP_FILE'
  $endpointPin = Get-AccountEnvironment 'DESCRIPTION_ENDPOINT_ROOT'
  foreach ($path in @(
      ([string]$configDocument.token_file),
      ([string]$probe.package_root),
      ([string]$probe.output_root),
      ([string]$probe.state_root),
      $generation)) {
    Assert-NoReparseAncestor $path
  }
  if ((Normalize-PathIdentity $ownershipPin) -ne
      (Normalize-PathIdentity (Domain-OwnershipPath $stateRoot)) -or
      (Normalize-PathIdentity $endpointPin) -ne (Normalize-PathIdentity $generation)) {
    Die 'windows_recovery_state_invalid' 'the endpoint user environment pins are not canonical'
  }
  return [ordered]@{
    operator_sid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    operator_administrator = $true
    runtime_sid = [string]$taskProjection.principal.sid
    runtime_administrator = [bool]$taskProjection.principal.administrator
    runtime_privilege_qualification = $(
      if ([bool]$taskProjection.principal.administrator) {
        'user-approved-administrator-runtime'
      } else {
        'non-administrative-runtime'
      }
    )
    endpoint_root = Normalize-PathIdentity $generation
    config_path = Normalize-PathIdentity $configPath
    config_sha256 = Sha256 $configPath
    config_digest = [string]$probe.config_digest
    package_root = Normalize-PathIdentity ([string]$probe.package_root)
    output_root = Normalize-PathIdentity ([string]$probe.output_root)
    state_root = Normalize-PathIdentity $stateRoot
    port = [int]$probe.port
    token_path = Normalize-PathIdentity ([string]$configDocument.token_file)
    token_sha256 = Sha256 ([string]$configDocument.token_file)
    install_id_path = Normalize-PathIdentity $installIdPath
    install_id = $installId
    release_identity_sha256 = Get-ReleaseIdentitySha256 $release
    release = $release
    domain_environment = [ordered]@{
      DESCRIPTION_ENDPOINT_ROOT = Get-AccountEnvironment 'DESCRIPTION_ENDPOINT_ROOT'
      DESCRIPTION_CAD_OWNERSHIP_FILE = Get-AccountEnvironment 'DESCRIPTION_CAD_OWNERSHIP_FILE'
      scope = 'user'
    }
  }
}
function New-EndpointSourceState($Task, $TaskProjection, $Identity) {
  return [ordered]@{
    schema_version = 'solidworks-to-urdf.windows-source-state/v1'
    task_path = [string]$TaskProjection.task_path
    task_name = [string]$TaskProjection.task_name
    task_xml_sha256 = [string]$TaskProjection.xml_sha256
    task_semantic_sha256 = [string]$TaskProjection.semantic_sha256
    task_acl_sha256 = [string]$TaskProjection.security_descriptor_sha256
    principal_sid = [string]$TaskProjection.principal.sid
    principal = $TaskProjection.principal
    execute = [string]$TaskProjection.action.execute
    arguments = [string]$TaskProjection.action.arguments
    working_directory = [string]$TaskProjection.action.working_directory
    settings = $TaskProjection.settings
    triggers_sha256 = [string]$TaskProjection.triggers_sha256
    endpoint_root = [string]$Identity.endpoint_root
    config_path = [string]$Identity.config_path
    config_sha256 = [string]$Identity.config_sha256
    config_digest = [string]$Identity.config_digest
    package_root = [string]$Identity.package_root
    output_root = [string]$Identity.output_root
    state_root = [string]$Identity.state_root
    port = [int]$Identity.port
    install_id_path = [string]$Identity.install_id_path
    install_id = [string]$Identity.install_id
    release_identity_sha256 = [string]$Identity.release_identity_sha256
    release = $Identity.release
  }
}
function Assert-EndpointSourceMatch($Expected, $Current) {
  $fields = @(
    'task_path',
    'task_name',
    'task_semantic_sha256',
    'task_acl_sha256',
    'principal_sid',
    'execute',
    'arguments',
    'working_directory',
    'triggers_sha256',
    'endpoint_root',
    'config_path',
    'config_sha256',
    'config_digest',
    'package_root',
    'output_root',
    'state_root',
    'install_id_path',
    'install_id',
    'release_identity_sha256')
  foreach ($field in $fields) {
    $expectedValue = [string]$Expected.$field
    $currentValue = [string]$Current.$field
    if ($field -in @('endpoint_root', 'config_path', 'package_root', 'output_root', 'state_root',
        'install_id_path', 'execute', 'working_directory')) {
      $expectedValue = Normalize-PathIdentity $expectedValue
      $currentValue = Normalize-PathIdentity $currentValue
    }
    if ($expectedValue -cne $currentValue) {
      Die 'windows_source_identity_mismatch' ('the restored source state differs at ' + $field)
    }
  }
  if ([int]$Expected.port -ne [int]$Current.port -or
      (Get-JsonSha256 $Expected.release) -ne (Get-JsonSha256 $Current.release) -or
      (Get-JsonSha256 $Expected.settings) -ne (Get-JsonSha256 $Current.settings)) {
    Die 'windows_source_identity_mismatch' 'the restored source release, port, or settings differs'
  }
}
function Assert-HealthSourceMatch($Health, $Source) {
  if ([string]$Health.install_id -cne [string]$Source.install_id -or
      [string]$Health.config_digest -cne [string]$Source.config_digest -or
      [string]$Health.release.version -cne [string]$Source.release.version -or
      [string]$Health.release.source_commit -cne [string]$Source.release.source_commit -or
      [string]$Health.release.source_sha256 -cne [string]$Source.release.source_sha256) {
    Die 'windows_source_health_mismatch' 'the restored endpoint health differs from the sealed source identity'
  }
}
function Get-RecoveryComparableState($State) {
  $files = @($State.files | Where-Object { [string]$_.role -cne 'task_xml' })
  $members = @($State.members | Where-Object { [string]$_.member -cne 'task.xml' })
  return [ordered]@{
    schema_version = [string]$State.schema_version
    ssh_host = [string]$State.ssh_host
    task = [ordered]@{
      task_path = [string]$State.task.task_path
      task_name = [string]$State.task.task_name
      state = [string]$State.task.state
      semantic_sha256 = [string]$State.task.semantic_sha256
      principal = $State.task.principal
      action = $State.task.action
      settings = $State.task.settings
      triggers_sha256 = [string]$State.task.triggers_sha256
      security_descriptor_sha256 = [string]$State.task.security_descriptor_sha256
    }
    identity = $State.identity
    files = $files
    acl = $State.acl
    cad = $State.cad
    members = $members
  }
}
function Get-RecoveryCadState($Identity) {
  $ownershipPath = Join-Path ([string]$Identity.state_root) 'cad-ownership.json'
  $leasePath = $ownershipPath + '.lease'
  Assert-NoReparseAncestor $ownershipPath
  Assert-NoReparseAncestor $leasePath
  $ledgerPresent = Test-Path -LiteralPath $ownershipPath
  $ledgerSha = $null
  if ($ledgerPresent) {
    try {
      $ledger = Read-Json $ownershipPath
    } catch {
      DieUnknown 'windows_cad_ownership_unknown' 'the CAD ownership ledger is unreadable'
    }
    if ($ledger.schema_version -ne 'solidworks-to-urdf.cad-ownership/v1' -or
        $null -eq $ledger.records) {
      DieUnknown 'windows_cad_ownership_unknown' 'the CAD ownership ledger is malformed'
    }
    $ledgerSha = Sha256 $ownershipPath
  }
  $oldRoot = $env:DESCRIPTION_ENDPOINT_ROOT
  $oldLedger = $env:DESCRIPTION_CAD_OWNERSHIP_FILE
  try {
    $env:DESCRIPTION_ENDPOINT_ROOT = [string]$Identity.endpoint_root
    $env:DESCRIPTION_CAD_OWNERSHIP_FILE = $ownershipPath
    $program = "import json;" +
      "from description_pipeline.sources.solidworks import isolation;" +
      "print(json.dumps({'facts':isolation.cad_session_facts(isolation.registered_executable())," +
      "'lease':isolation.domain_lease_facts()},sort_keys=True))"
    $output = & (Runtime-Python ([string]$Identity.endpoint_root)) -I -B -c $program
    if ($LASTEXITCODE -ne 0) {
      DieUnknown 'windows_cad_ownership_unknown' 'the read-only CAD ownership probe failed'
    }
    $probe = ($output | Select-Object -Last 1) | ConvertFrom-Json
  } finally {
    $env:DESCRIPTION_ENDPOINT_ROOT = $oldRoot
    $env:DESCRIPTION_CAD_OWNERSHIP_FILE = $oldLedger
  }
  $problems = @($probe.facts.problems)
  if ($probe.lease.held -ne $false -or $probe.facts.lease_held -ne $false) {
    Die 'windows_cad_lease_held' 'the CAD domain lease is still held'
  }
  $state = [ordered]@{
    ownership_path = Normalize-PathIdentity $ownershipPath
    ownership_sha256 = $ledgerSha
    ledger_present = [bool]$ledgerPresent
    lease_path = Normalize-PathIdentity $leasePath
    lease_held = $probe.facts.lease_held
    external_detected = $probe.facts.external_session_detected
    problems = $problems
  }
  if ($state.lease_held -ne $false -or $state.external_detected -ne $false -or $problems.Count -ne 0) {
    Die 'windows_cad_ownership_unknown' 'the Windows CAD domain is not proven free'
  }
  return $state
}
function Test-NetTcpNoMatchError($ErrorRecord) {
  $category = [string]$ErrorRecord.CategoryInfo.Category
  $errorId = [string]$ErrorRecord.FullyQualifiedErrorId
  return $category -eq 'ObjectNotFound' -and
    $errorId -match '^CmdletizationQuery_NotFound(?:_[A-Za-z0-9_]+)?,Get-NetTCPConnection$'
}
function Assert-RecoveryIdle($Identity) {
  try {
    $listeners = @(Get-NetTCPConnection -LocalPort ([int]$Identity.port) -State Listen -ErrorAction Stop)
  } catch {
    if (Test-NetTcpNoMatchError $_) {
      $listeners = @()
    } else {
      DieUnknown 'windows_recovery_observer_failed' 'the endpoint listener state could not be observed' @{
        category = [string]$_.CategoryInfo.Category
        error_id = [string]$_.FullyQualifiedErrorId
      }
    }
  }
  if ($listeners.Count -ne 0) {
    Die 'windows_listener_present' 'the endpoint port still has a listener'
  }
  $stateRoot = (Normalize-PathIdentity ([string]$Identity.state_root)).ToLower()
  $generation = (Normalize-PathIdentity ([string]$Identity.endpoint_root)).ToLower()
  try {
    $residue = @(Get-CimInstance Win32_Process -ErrorAction Stop | Where-Object {
      $image = if ($_.ExecutablePath) { (Normalize-PathIdentity ([string]$_.ExecutablePath)).ToLower() } else { '' }
      ($image.StartsWith($stateRoot + '/')) -or ($image.StartsWith($generation + '/'))
    })
  } catch {
    DieUnknown 'windows_recovery_observer_failed' 'the endpoint writer process table could not be observed'
  }
  if ($residue.Count -ne 0) {
    Die 'windows_writer_residue' 'an endpoint writer process remains under a managed root'
  }
  $lockPath = Join-Path ([string]$Identity.state_root) '.endpoint.lock'
  $held = Test-LockProbeHeld ([string]$Identity.endpoint_root) $lockPath
  if ($null -eq $held) {
    DieUnknown 'windows_recovery_observer_failed' 'the endpoint ownership lock could not be observed'
  }
  if ($held) {
    Die 'windows_endpoint_lock_held' 'the endpoint ownership lock is still held'
  }
}
function Assert-RecoveryStopped($TaskProjection, $Identity, $CadState) {
  if ($TaskProjection.state -ne 'Disabled') {
    Die 'windows_task_not_disabled' 'the managed Windows task is not disabled'
  }
  Assert-RecoveryIdle $Identity
  if ($CadState.lease_held -ne $false -or $CadState.external_detected -ne $false -or
      @($CadState.problems).Count -ne 0) {
    Die 'windows_cad_ownership_unknown' 'the Windows CAD domain is not proven free'
  }
}
function Get-RecoveryCurrentState([string]$SshHost, [string]$TaskPath = '', [string]$TaskName = '') {
  $task = Get-RecoveryTask $TaskName $TaskPath
  $taskProjection = Get-TaskProjection $task
  $identity = Get-EndpointIdentity $task $taskProjection $SshHost
  $cad = Get-RecoveryCadState $identity
  Assert-RecoveryStopped $taskProjection $identity $cad
  $taskFile = Get-TaskFilePath $task
  foreach ($path in @(
      $taskFile,
      ([string]$identity.config_path),
      ([string]$identity.token_path),
      ([string]$identity.install_id_path),
      ([string]$identity.package_root),
      ([string]$identity.output_root),
      ([string]$identity.state_root),
      ([string]$identity.endpoint_root),
      ([string]$cad.ownership_path),
      ([string]$cad.lease_path))) {
    Assert-NoReparseAncestor $path
  }
  $configDocument = Read-Json ([string]$identity.config_path)
  $managedPaths = @(Get-ManagedEndpointPaths $configDocument $identity.endpoint_root `
    $identity.config_path $identity.install_id_path)
  $managedCommonParent = Get-ManagedCommonParent $managedPaths
  $managedRootMarker = Join-Path $managedCommonParent '.description-managed-root.json'
  Assert-ManagedEndpointAcls $configDocument ([string]$identity.endpoint_root) `
    ([string]$identity.config_path) ([string]$identity.install_id_path) `
    $taskFile
  $taskXmlEncoding = New-Object System.Text.UTF8Encoding($false)
  $taskXmlText = [string]$taskProjection.xml
  $taskXmlBytes = $taskXmlEncoding.GetBytes($taskXmlText)
  $files = @(
    [ordered]@{
      role = 'task_xml'
      path = Normalize-PathIdentity $taskFile
      member = 'task.xml'
      bytes = [long]$taskXmlBytes.Length
      sha256 = Get-BytesSha256 $taskXmlBytes
    },
    (Get-RecoveryFileRow 'managed_root_marker' $managedRootMarker 'managed-root.json'),
    (Get-RecoveryFileRow 'endpoint_config' ([string]$identity.config_path) 'endpoint.json'),
    (Get-RecoveryFileRow 'endpoint_token' ([string]$identity.token_path) 'token'),
    (Get-RecoveryFileRow 'install_id' ([string]$identity.install_id_path) 'install-id')
  )
  if ([bool]$cad.ledger_present) {
    $files += Get-RecoveryFileRow 'cad_ownership' ([string]$cad.ownership_path) 'cad-ownership.json'
  }
  $aclByRole = @{}
  foreach ($row in @(Get-ManagedEndpointAclRows $configDocument $identity.endpoint_root `
      $identity.config_path $identity.install_id_path $taskFile)) {
    if (Test-Path -LiteralPath $row.path) {
      $aclByRole[[string]$row.role] = Get-AclProjection $row.path
    }
  }
  $acl = [ordered]@{
    common_parent = $aclByRole['common_parent']
    managed_root_marker = $aclByRole['managed_root_marker']
    lifecycle_root = $aclByRole['lifecycle_root']
    endpoint_secrets_root = $aclByRole['endpoint_secrets_root']
    task_file = $aclByRole['task_file']
    endpoint_config = $aclByRole['endpoint_config']
    endpoint_token = $aclByRole['endpoint_token']
    install_id = $aclByRole['install_id']
    cad_ownership = $aclByRole['cad_ownership']
    cad_lease = $aclByRole['cad_lease']
    roots = [ordered]@{
      package_root = $aclByRole['package_root']
      output_root = $aclByRole['output_root']
      state_root = $aclByRole['state_root']
      endpoint_root = $aclByRole['endpoint_root']
    }
  }
  # The canonical release archive is immutable lifecycle input, not mutable recovery state.
  # It stays outside this bundle and is revalidated as ReadOnly+ACL+digest before every
  # promote/rollback reuse; recovery restores runtime/configuration state only.
  $state = [ordered]@{
    schema_version = 'solidworks-to-urdf.windows-recovery/v2'
    ssh_host = $SshHost
    task = [ordered]@{
      task_path = $taskProjection.task_path
      task_name = $taskProjection.task_name
      state = $taskProjection.state
      xml_sha256 = $taskProjection.xml_sha256
      semantic_sha256 = $taskProjection.semantic_sha256
      principal = $taskProjection.principal
      action = $taskProjection.action
      settings = $taskProjection.settings
      triggers_sha256 = $taskProjection.triggers_sha256
      security_descriptor_sha256 = $taskProjection.security_descriptor_sha256
    }
    identity = [ordered]@{
      operator_sid = $identity.operator_sid
      operator_administrator = $identity.operator_administrator
      runtime_sid = $identity.runtime_sid
      runtime_administrator = $identity.runtime_administrator
      runtime_privilege_qualification = $identity.runtime_privilege_qualification
      endpoint_root = $identity.endpoint_root
      config_path = $identity.config_path
      config_sha256 = $identity.config_sha256
      config_digest = $identity.config_digest
      package_root = $identity.package_root
      output_root = $identity.output_root
      state_root = $identity.state_root
      port = $identity.port
      token_path = $identity.token_path
      token_sha256 = $identity.token_sha256
      install_id_path = $identity.install_id_path
      install_id = $identity.install_id
      release_identity_sha256 = $identity.release_identity_sha256
      domain_environment = $identity.domain_environment
    }
    files = $files
    acl = $acl
    cad = $cad
    members = @($files | ForEach-Object {
      [ordered]@{ member = $_.member; bytes = $_.bytes; sha256 = $_.sha256 }
    })
  }
  return @{ state=$state; task=$task; task_projection=$taskProjection; identity=$identity; task_file=$taskFile }
}
function Set-PrivateRecoveryAcl([string]$Path) {
  $sid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User
  $item = Get-Item -LiteralPath $Path -Force -ErrorAction Stop
  if ($item.PSIsContainer) {
    $security = New-Object System.Security.AccessControl.DirectorySecurity
    $inheritance = [System.Security.AccessControl.InheritanceFlags]::ContainerInherit -bor
      [System.Security.AccessControl.InheritanceFlags]::ObjectInherit
  } else {
    $security = New-Object System.Security.AccessControl.FileSecurity
    $inheritance = [System.Security.AccessControl.InheritanceFlags]::None
  }
  $security.SetAccessRuleProtection($true, $false)
  $rule = New-Object System.Security.AccessControl.FileSystemAccessRule(
    $sid, [System.Security.AccessControl.FileSystemRights]::FullControl,
    $inheritance,
    [System.Security.AccessControl.PropagationFlags]::None,
    [System.Security.AccessControl.AccessControlType]::Allow)
  $security.AddAccessRule($rule)
  Set-Acl -LiteralPath $Path -AclObject $security -ErrorAction Stop
}
function Read-RecoveryBundle([string]$Path) {
  Add-Type -AssemblyName System.IO.Compression.FileSystem
  if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
    Die 'windows_recovery_bundle_invalid' 'the Windows recovery bundle is missing'
  }
  try {
    $archive = [System.IO.Compression.ZipFile]::OpenRead($Path)
  } catch {
    Die 'windows_recovery_bundle_invalid' 'the Windows recovery bundle is unreadable'
  }
  try {
    $names = @($archive.Entries | ForEach-Object { $_.FullName })
    if ($names.Count -ne (@($names | Sort-Object -Unique)).Count) {
      Die 'windows_recovery_bundle_invalid' 'the Windows recovery bundle repeats a member'
    }
    foreach ($name in $names) {
      if (-not $name -or $name.Contains('/') -or $name.Contains('\') -or $name -eq '.' -or $name -eq '..') {
        Die 'windows_recovery_bundle_invalid' ('unsafe Windows recovery member: ' + $name)
      }
    }
    $stateEntry = $archive.GetEntry('windows-state.json')
    if ($null -eq $stateEntry) {
      Die 'windows_recovery_bundle_invalid' 'the Windows recovery bundle has no state'
    }
    $reader = New-Object System.IO.StreamReader($stateEntry.Open())
    try { $state = ($reader.ReadToEnd() | ConvertFrom-Json) } finally { $reader.Dispose() }
  } finally {
    $archive.Dispose()
  }
  if ($state.schema_version -ne 'solidworks-to-urdf.windows-recovery/v2') {
    Die 'windows_recovery_bundle_invalid' 'the Windows recovery state schema is unsupported'
  }
  $expectedNames = @(@($state.members) | ForEach-Object { [string]$_.member })
  $expectedNames += 'windows-state.json'
  if ((@($names | Sort-Object) -join "`n") -ne (@($expectedNames | Sort-Object) -join "`n")) {
    Die 'windows_recovery_bundle_invalid' 'the Windows recovery bundle members differ from its state'
  }
  return $state
}
function Expand-RecoveryBundle([string]$Path, [string]$Destination) {
  $state = Read-RecoveryBundle $Path
  $root = Join-Path $Destination 'bundle'
  if (Test-Path -LiteralPath $root) {
    Die 'windows_recovery_bundle_invalid' 'the Windows recovery extraction directory already exists'
  }
  New-Item -ItemType Directory -Path $root | Out-Null
  try {
    [System.IO.Compression.ZipFile]::ExtractToDirectory($Path, $root)
  } catch {
    Die 'windows_recovery_bundle_invalid' 'the Windows recovery bundle could not be extracted'
  }
  foreach ($row in @($state.members)) {
    if (-not $row.member -or -not (Test-Path -LiteralPath (Join-Path $root ([string]$row.member)) -PathType Leaf)) {
      Die 'windows_recovery_bundle_invalid' ('the Windows recovery member is missing: ' + $row.member)
    }
    if ((Sha256 (Join-Path $root ([string]$row.member))) -ne [string]$row.sha256) {
      Die 'windows_recovery_bundle_invalid' ('the Windows recovery member hash differs: ' + $row.member)
    }
  }
  return @{ state=$state; root=$root }
}
function Restore-RecoveryFile([string]$Source, [string]$Target, $Acl, [string]$Sha256) {
  Assert-NoReparseAncestor $Target
  $parent = Split-Path -Parent $Target
  if (-not (Test-Path -LiteralPath $parent -PathType Container)) {
    Die 'windows_endpoint_not_provisioned' ('the target parent directory is missing: ' + $parent)
  }
  $parentItem = Get-Item -LiteralPath $parent -Force
  if ($parentItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) {
    Die 'windows_recovery_target_unsafe' ('the target parent is a reparse point: ' + $parent)
  }
  if (Test-Path -LiteralPath $Target) {
    $targetItem = Get-Item -LiteralPath $Target -Force
    if ($targetItem.PSIsContainer -or
        ($targetItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint)) {
      Die 'windows_recovery_target_unsafe' ('the target is not a regular file: ' + $Target)
    }
    if ((Sha256 $Target) -ne $Sha256) {
      Die 'windows_restore_conflict' ('the target already carries different bytes: ' + $Target)
    }
    Set-AclProjection $Target $Acl
    if ((Get-AclProjection $Target).projection_sha256 -ne $Acl.projection_sha256) {
      Die 'windows_acl_restore_failed' ('the target ACL differs: ' + $Target)
    }
    return
  }
  $partial = Join-Path $parent ('.' + (Split-Path -Leaf $Target) + '.' + [guid]::NewGuid().ToString('N') + '.partial')
  try {
    Copy-Item -LiteralPath $Source -Destination $partial -Force
    Set-AclProjection $partial $Acl
    Move-Item -LiteralPath $partial -Destination $Target
  } finally {
    if (Test-Path -LiteralPath $partial) { Remove-Item -LiteralPath $partial -Force -ErrorAction SilentlyContinue }
  }
  if ((Sha256 $Target) -ne $Sha256 -or (Get-AclProjection $Target).projection_sha256 -ne $Acl.projection_sha256) {
    Die 'windows_recovery_verification_failed' ('the restored file did not read back exactly: ' + $Target)
  }
}
function Invoke-RecoverySnapshot($Binding) {
  if (-not $RecoveryDir) { Die 'recovery_dir_required' 'recovery-snapshot needs -RecoveryDir' }
  if (-not (Test-Path -LiteralPath $RecoveryDir -PathType Container)) {
    Die 'recovery_dir_required' 'the Windows recovery directory is missing'
  }
  Assert-NoReparseAncestor $RecoveryDir
  $recoveryDirItem = Get-Item -LiteralPath $RecoveryDir -Force
  if ($recoveryDirItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) {
    Die 'windows_recovery_target_unsafe' 'the Windows recovery directory is a reparse point'
  }
  Set-PrivateRecoveryAcl $RecoveryDir
  foreach ($field in ('ssh_host','task_name','state_root','package_root','output_root','port')) {
    if (-not $Binding.$field) { Die 'binding_incomplete' ('the snapshot binding misses ' + $field) }
  }
  $current = Get-RecoveryCurrentState ([string]$Binding.ssh_host) '' ([string]$Binding.task_name)
  if ((Normalize-PathIdentity $current.identity.state_root) -ne (Normalize-PathIdentity ([string]$Binding.state_root)) -or
      (Normalize-PathIdentity $current.identity.package_root) -ne (Normalize-PathIdentity ([string]$Binding.package_root)) -or
      (Normalize-PathIdentity $current.identity.output_root) -ne (Normalize-PathIdentity ([string]$Binding.output_root)) -or
      [int]$current.identity.port -ne [int]$Binding.port) {
    Die 'windows_recovery_state_invalid' 'the live Windows endpoint differs from the snapshot binding'
  }
  $work = Join-Path $RecoveryDir 'bundle'
  $zip = Join-Path $RecoveryDir 'recovery.zip'
  if ((Test-Path -LiteralPath $work) -or (Test-Path -LiteralPath $zip)) {
    Die 'windows_recovery_target_unsafe' 'the Windows recovery output already exists'
  }
  New-Item -ItemType Directory -Path $work | Out-Null
  Set-PrivateRecoveryAcl $work
  try {
    [System.IO.File]::WriteAllText((Join-Path $work 'task.xml'), [string]$current.task_projection.xml, (New-Object System.Text.UTF8Encoding($false)))
    foreach ($row in @($current.state.files)) {
      if ($row.role -eq 'task_xml') { continue }
      $source = [string]$row.path
      Copy-Item -LiteralPath $source -Destination (Join-Path $work ([string]$row.member)) -Force
    }
    [System.IO.File]::WriteAllText(
      (Join-Path $work 'windows-state.json'),
      ($current.state | ConvertTo-Json -Depth 20 -Compress),
      (New-Object System.Text.UTF8Encoding($false)))
    Compress-Archive -Path (Join-Path $work '*') -DestinationPath $zip -CompressionLevel Optimal -Force
    Set-PrivateRecoveryAcl $zip
  } finally {
    if (Test-Path -LiteralPath $work) { Remove-Item -LiteralPath $work -Recurse -Force -ErrorAction Stop }
  }
  Emit @{
    passed=$true; verb='recovery-snapshot'; task_state=[string]$current.task_projection.state;
    endpoint='stopped'; cad_ownership='free'; bundle_sha256=(Sha256 $zip); bundle_bytes=(Get-Item -LiteralPath $zip).Length
  }
}
function Invoke-RecoveryRestore($Binding, [bool]$VerifyOnly) {
  if (-not $RecoveryBundle) { Die 'recovery_bundle_required' 'recovery restore needs -RecoveryBundle' }
  Assert-NoReparseAncestor $RecoveryBundle
  if (-not (Test-Path -LiteralPath $RecoveryBundle -PathType Leaf)) {
    Die 'windows_recovery_bundle_invalid' 'the transferred Windows recovery bundle is missing'
  }
  $recoveryBundleItem = Get-Item -LiteralPath $RecoveryBundle -Force -ErrorAction Stop
  if ($recoveryBundleItem.PSIsContainer -or
      ($recoveryBundleItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint)) {
    Die 'windows_recovery_bundle_invalid' 'the transferred Windows recovery bundle is not a regular file'
  }
  if ((Sha256 $RecoveryBundle) -ne [string]$Binding.recovery_bundle_sha256) {
    Die 'windows_recovery_bundle_invalid' 'the transferred Windows recovery bundle hash differs'
  }
  $temp = Join-Path ([System.IO.Path]::GetTempPath()) ('windows-recovery-' + [guid]::NewGuid().ToString('N'))
  Assert-NoReparseAncestor $temp
  New-Item -ItemType Directory -Path $temp | Out-Null
  $Script:RecoveryTemp = $temp
  $tempItem = Get-Item -LiteralPath $temp -Force -ErrorAction Stop
  if (-not $tempItem.PSIsContainer -or
      ($tempItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint)) {
    Die 'windows_recovery_target_unsafe' 'the Windows recovery temporary directory is not a regular directory'
  }
  $ownedTask = $false
  $registeringTask = $false
  $state = $null
  $cleanup = @{attempted=$false; passed=$null; path=$temp}
  try {
    Set-PrivateRecoveryAcl $temp
    $extracted = Expand-RecoveryBundle $RecoveryBundle $temp
    $state = $extracted.state
    $expected = $Binding.expected
    if ($state.ssh_host -ne $Binding.ssh_host -or
        $state.task.task_path -ne $Binding.task_path -or
        $state.task.task_name -ne $Binding.task_name -or
        $state.task.principal.sid -ne $Binding.principal_sid -or
        $state.task.xml_sha256 -ne $expected.task_xml_sha256 -or
        $state.task.semantic_sha256 -ne $expected.task_semantic_sha256 -or
        $state.acl.task_file.projection_sha256 -ne $expected.task_acl_sha256 -or
        $state.identity.config_sha256 -ne $expected.config_sha256 -or
        $state.identity.config_digest -ne $expected.config_digest -or
        $state.identity.token_sha256 -ne $expected.token_sha256 -or
        $state.identity.install_id -ne $expected.install_id -or
        $state.identity.release_identity_sha256 -ne $expected.release_identity_sha256 -or
        $state.cad.ownership_sha256 -ne $expected.cad_ownership_sha256) {
      Die 'windows_recovery_state_invalid' 'the Windows recovery bundle differs from the target binding'
    }
    if ([string]$state.identity.operator_sid -ne
        [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value) {
      Die 'windows_operator_principal_mismatch' 'the SSH account differs from the recovery principal'
    }
    foreach ($root in @($state.identity.package_root,$state.identity.output_root,$state.identity.state_root)) {
      Assert-NoReparseAncestor ([string]$root)
      if (-not (Test-Path -LiteralPath ([string]$root) -PathType Container)) {
        Die 'windows_endpoint_not_provisioned' ('the existing endpoint root is missing: ' + $root)
      }
      $rootItem = Get-Item -LiteralPath ([string]$root) -Force
      if ($rootItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) {
        Die 'windows_endpoint_not_provisioned' ('the existing endpoint root is a reparse point: ' + $root)
      }
    }
    if (-not (Test-Path -LiteralPath (Runtime-Python ([string]$state.identity.endpoint_root)))) {
      Die 'windows_endpoint_not_provisioned' 'the existing endpoint runtime is missing'
    }
    if (-not $VerifyOnly) {
      $existing = @(Get-BoundTask $Binding 'SilentlyContinue')
      if ($existing.Count -gt 1) {
        Die 'windows_task_ambiguous' 'more than one task carries the managed name'
      }
      if ($existing.Count -eq 1) {
        $existingProjection = Get-TaskProjection $existing[0]
        $existingTaskFile = Get-TaskFilePath $existing[0]
        $existingTaskAcl = Get-AclProjection $existingTaskFile
        if ([string]$existing[0].TaskPath -ne [string]$Binding.task_path -or
            $existingProjection.semantic_sha256 -ne [string]$state.task.semantic_sha256 -or
            $existingProjection.xml_sha256 -ne [string]$state.task.xml_sha256 -or
            $existingTaskAcl.projection_sha256 -ne [string]$state.acl.task_file.projection_sha256) {
          Die 'windows_task_projection_mismatch' 'the existing task is not the archived managed task'
        }
        $ownedTask = $true
        if ($existing[0].State -eq 'Running') { Stop-ManagedTask ([string]$existing[0].TaskName) ([string]$existing[0].TaskPath) }
        Disable-ManagedTask ([string]$existing[0].TaskName) ([string]$existing[0].TaskPath)
      }
      $cadBefore = Get-RecoveryCadState $state.identity
      Assert-RecoveryIdle $state.identity
      foreach ($row in @($state.files)) {
        if ($row.role -eq 'task_xml') { continue }
        $acl = switch ($row.role) {
          'managed_root_marker' { $state.acl.managed_root_marker }
          'endpoint_config' { $state.acl.endpoint_config }
          'endpoint_token' { $state.acl.endpoint_token }
          'install_id' { $state.acl.install_id }
          'cad_ownership' { $state.acl.cad_ownership }
          default { Die 'windows_recovery_state_invalid' ('unknown Windows recovery file role: ' + $row.role) }
        }
        Restore-RecoveryFile (Join-Path $extracted.root ([string]$row.member)) ([string]$row.path) $acl ([string]$row.sha256)
      }
      if ($existing.Count -eq 0) {
        $taskFolder = Join-Path $env:SystemRoot ('System32\Tasks\' + ([string]$Binding.task_path).TrimStart('\'))
        if (-not (Test-Path -LiteralPath $taskFolder -PathType Container)) {
          Die 'windows_endpoint_not_provisioned' 'the existing Task Scheduler folder is missing'
        }
        Assert-NoReparseAncestor (Join-Path $taskFolder ([string]$Binding.task_name))
        $registeringTask = $true
        Register-ManagedTask -TaskName ([string]$Binding.task_name) -TaskPath ([string]$Binding.task_path) `
          -TaskXml (Read-Utf8Text (Join-Path $extracted.root 'task.xml'))
        $registeringTask = $false
        $ownedTask = $true
        Disable-ManagedTask ([string]$Binding.task_name) ([string]$Binding.task_path)
      }
      $configDocument = Read-Json ([string]$state.identity.config_path)
      $taskFile = Join-Path $env:SystemRoot ('System32\Tasks\' + ([string]$Binding.task_path).TrimStart('\') + [string]$Binding.task_name)
      foreach ($row in @(Get-ManagedEndpointAclRows $configDocument $state.identity.endpoint_root `
          $state.identity.config_path $state.identity.install_id_path $taskFile)) {
        $projection = Get-ManagedAclProjectionForRole $state.acl $row.role
        if (-not (Test-Path -LiteralPath $row.path)) {
          if ($null -eq $projection) { continue }
          Die 'windows_endpoint_not_provisioned' ('an ACL restore path is missing: ' + $row.path)
        }
        if ($null -eq $projection) {
          Die 'windows_recovery_state_invalid' ('a present managed path has no archived ACL: ' + $row.path)
        }
        Set-AclProjection $row.path $projection
      }
      Assert-ManagedEndpointAcls $configDocument ([string]$state.identity.endpoint_root) `
        ([string]$state.identity.config_path) ([string]$state.identity.install_id_path) `
        $taskFile
      Set-DomainPins ([string]$state.identity.state_root) ([string]$state.identity.endpoint_root)
    }
    $current = Get-RecoveryCurrentState ([string]$Binding.ssh_host) ([string]$Binding.task_path) ([string]$Binding.task_name)
    if ((Get-JsonSha256 (Get-RecoveryComparableState $current.state)) -ne
        (Get-JsonSha256 (Get-RecoveryComparableState $state))) {
      Die 'windows_recovery_verification_failed' 'Windows recovery did not read back the archived task semantics and managed state'
    }
    $cleanup = Close-RecoveryTemp
    @{
      passed=$true; verb=$Script:Verb; task_state=[string]$current.task_projection.state;
      endpoint='stopped'; cad_ownership='free'; state=$current.state;
      cleanup=$cleanup
    } | ConvertTo-Json -Depth 32 -Compress
  } catch {
    $primaryError = $_
    $failedOwner = $ownedTask
    if (-not $VerifyOnly -and $registeringTask -and $state) {
      try {
        $candidate = @(Get-BoundTask $Binding 'Stop')
        if ($candidate.Count -eq 1) {
          $candidateProjection = Get-TaskProjection $candidate[0]
          $failedOwner = $candidateProjection.xml_sha256 -eq [string]$state.task.xml_sha256 -and
            $candidateProjection.semantic_sha256 -eq [string]$state.task.semantic_sha256
        }
      } catch {
        $failedOwner = $false
      }
    }
    if (-not $VerifyOnly -and $failedOwner) {
      try { Disable-ManagedTask ([string]$Binding.task_name) ([string]$Binding.task_path) } catch { }
    }
    $cleanup = Close-RecoveryTemp
    Emit @{
      passed=$false
      code='windows_recovery_exception'
      message=$primaryError.Exception.Message
      detail=@{error=$primaryError.Exception.Message; cleanup=$cleanup}
      verb=$Script:Verb
    }
    exit 2
  } finally {
    if ($Script:RecoveryTemp) { Close-RecoveryTemp | Out-Null }
  }
}
function Invoke-RecoveryResume($Binding) {
  $task = Get-RecoveryTask ([string]$Binding.task_name) ([string]$Binding.task_path)
  $projection = Get-TaskProjection $task
  Set-BindingTaskPath $Binding ([string]$projection.task_path)
  $identity = Get-EndpointIdentity $task $projection ([string]$Binding.ssh_host)
  if ($identity.install_id -ne [string]$Binding.install_id -or
      $identity.config_digest -ne [string]$Binding.config_digest -or
      $identity.release_identity_sha256 -ne [string]$Binding.release_identity_sha256) {
    Die 'windows_resume_identity_mismatch' 'the Windows endpoint identity differs from the active install record'
  }
  $cad = Get-RecoveryCadState $identity
  $configDocument = Read-Json ([string]$identity.config_path)
  $taskFile = Get-TaskFilePath $task
  Set-ManagedEndpointAcls $configDocument ([string]$identity.endpoint_root) `
    ([string]$identity.config_path) ([string]$identity.install_id_path) `
    $taskFile
  Assert-ManagedEndpointAcls $configDocument ([string]$identity.endpoint_root) `
    ([string]$identity.config_path) ([string]$identity.install_id_path) `
    $taskFile
  if ($projection.state -eq 'Running') {
    $settled = Wait-Settled ([string]$identity.config_path) $Binding
  } elseif ($projection.state -eq 'Disabled') {
    Assert-RecoveryStopped $projection $identity $cad
    Set-DomainPins ([string]$identity.state_root) ([string]$identity.endpoint_root)
    Enable-ManagedTask ([string]$task.TaskName) ([string]$task.TaskPath)
    try {
      $started = Start-OrRefuse ([string]$identity.config_path) $Binding ([string]$identity.endpoint_root) `
        (Join-Path ([string]$identity.state_root) 'instance-promotions')
      $settled = $started.settled
    } catch {
      try { Disable-ManagedTask ([string]$task.TaskName) ([string]$task.TaskPath) } catch { }
      throw
    }
  } else {
    Die 'windows_resume_not_disabled' ('the Windows task is neither Disabled nor Running: ' + $projection.state)
  }
  $health = $settled.health
  if ($health.install_id -ne [string]$Binding.install_id -or
      $health.config_digest -ne [string]$Binding.config_digest -or
      $health.release.version -ne [string]$Binding.version -or
      $health.release.source_commit -ne [string]$Binding.source_commit -or
      $health.release.source_sha256 -ne [string]$Binding.source_sha256) {
    try { Disable-ManagedTask ([string]$task.TaskName) ([string]$task.TaskPath) } catch { }
    Die 'windows_health_mismatch' 'the resumed endpoint did not report the bound identity'
  }
  Emit @{passed=$true; verb='resume'; task_state='Running'; settled_seconds=$settled.settled_seconds;
    listener_pid=$settled.listener_pid; health=$health}
}

function Invoke-Main {
# (Body intentionally kept unindented so this refactor's diff stays reviewable.)
$script:LifecyclePhase = [string]$Script:Verb
$binding = Read-Json $Binding
if ($Verb -in @('recovery-snapshot','recovery-restore','recovery-verify','resume')) {
  if ([string]::IsNullOrWhiteSpace([string]$binding.script_sha256) -or
      (Sha256 $PSCommandPath) -ne [string]$binding.script_sha256) {
    Die 'windows_recovery_transport_unknown' 'the remote lifecycle script differs from the transferred binding'
  }
}
if ($Verb -eq 'recovery-snapshot') { Invoke-RecoverySnapshot $binding; exit 0 }
if ($Verb -eq 'recovery-restore') { Invoke-RecoveryRestore $binding $false; exit 0 }
if ($Verb -eq 'recovery-verify') { Invoke-RecoveryRestore $binding $true; exit 0 }
if ($Verb -eq 'resume') { Invoke-RecoveryResume $binding; exit 0 }
foreach ($field in ('task_name','state_root','generation','dir','archive','artifact_sha256','port',
                    'package_root','output_root','version','source_commit','source_sha256')) {
  if (-not $binding.$field) { Die 'binding_incomplete' "the binding misses $field" }
}
$task = Get-InitialBoundTask $binding
$promotions = Join-Path $binding.state_root 'instance-promotions'

if ($Verb -eq 'health') {
  if (-not $task) { Die 'task_missing' 'the instance task is not registered' }
  Assert-RunningTask $task
  $projection = Get-TaskProjection $task
  Set-BindingTaskPath $binding ([string]$projection.task_path)
  $identity = Get-EndpointIdentity $task $projection ([string]$binding.ssh_host)
  if ([int]$identity.port -ne [int]$binding.port) {
    Die 'windows_task_projection_mismatch' 'the running endpoint port differs from the binding'
  }
  $listener = Get-ListenerEvidence $binding $projection
  try { $probe = Read-EndpointHealth ([string]$identity.config_path) }
  catch { Die 'endpoint_unhealthy' $_.Exception.Message }
  $source = New-EndpointSourceState $task $projection $identity
  Assert-HealthSourceMatch $probe $source
  Emit @{passed=$true; verb='health'; health=$probe; listener=$listener}
  exit 0
}

if ($Verb -eq 'status') {
  # Read-only relay of live Windows facts plus the sealed promotion records. Windows alone
  # interprets the raw task and actual process; the Linux operator consumes the explicit
  # source/target/UNKNOWN relation below without parsing launcher arguments.
  $pending = $null
  $pendingPath = Join-Path $promotions 'pending-promotion.json'
  if (Test-Path -LiteralPath $pendingPath) { $pending = Read-Json $pendingPath }
  $last = $null
  $lastPath = Join-Path $promotions 'last-promotion.json'
  if (Test-Path -LiteralPath $lastPath) { $last = Read-Json $lastPath }
  $pendingRollback = $null
  $pendingRollbackPath = Join-Path $promotions 'pending-rollback.json'
  try {
    if (Test-Path -LiteralPath $pendingRollbackPath -ErrorAction Stop) {
      $rollbackIntentItem = Get-Item -LiteralPath $pendingRollbackPath -Force -ErrorAction Stop
      if ($rollbackIntentItem -isnot [System.IO.FileInfo] -or
          ($rollbackIntentItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint)) {
        throw 'the pending rollback intent is not a regular file'
      }
      $unsafe = Find-ReparseAncestor $pendingRollbackPath
      if ($unsafe) { throw ('the rollback intent crosses a reparse point: ' + $unsafe) }
      $rollbackIntent = Read-Json $pendingRollbackPath
      if ([string]$rollbackIntent.verb -cne 'rollback' -or
          $rollbackIntent.intent -ne $true -or
          [string]::IsNullOrWhiteSpace([string]$rollbackIntent.previous) -or
          [string]::IsNullOrWhiteSpace([string]$rollbackIntent.rollback_generation) -or
          [string]::IsNullOrWhiteSpace([string]$rollbackIntent.restore_to) -or
          [string]::IsNullOrWhiteSpace([string]$rollbackIntent.current_snapshot) -or
          [string]$rollbackIntent.source_state_sha256 -cnotmatch '^[0-9a-f]{64}$') {
        throw 'the pending rollback intent is incomplete or malformed'
      }
      $pendingRollback = @{
        state='present'
        previous=[string]$rollbackIntent.previous
        target_generation=[string]$rollbackIntent.rollback_generation
        restore_to=[string]$rollbackIntent.restore_to
        current_snapshot=[string]$rollbackIntent.current_snapshot
        source_state_sha256=[string]$rollbackIntent.source_state_sha256
        source=$rollbackIntent.source
        target=$rollbackIntent.target
      }
    }
  } catch {
    $pendingRollback = @{
      state='unknown'
      code='rollback_intent_unreadable'
      path=$pendingRollbackPath
      error=$_.Exception.Message
    }
  }
  $target = Normalize-PathIdentity (Join-Path ([string]$binding.dir) 'generation')
  $live = $null
  if ($task) {
    $actionCount = @($task.Actions).Count
    $live = @{
      action_count=$actionCount
      state=[string]$task.State
      task_xml=$null
      task_xml_sha256=$null
      task_semantic_sha256=$null
      task_acl_sha256=$null
      endpoint_root=$null
      config_path=$null
      config_sha256=$null
      config_digest=$null
      install_id=$null
      release_identity_sha256=$null
      release=$null
      source=$null
      listener=$null
      health=$null
      maintenance_relation='unknown'
      error=$null
    }
    try {
      $projection = Get-TaskProjection $task
      Set-BindingTaskPath $binding ([string]$projection.task_path)
      $identity = Get-EndpointIdentity $task $projection ([string]$binding.ssh_host)
      $source = New-EndpointSourceState $task $projection $identity
      $listener = $null
      $health = $null
      if ([string]$task.State -eq 'Running') {
        if ([int]$identity.port -ne [int]$binding.port) {
          throw 'the running endpoint port differs from the binding'
        }
        $listener = Get-ListenerEvidence $binding $projection
        $health = Read-EndpointHealth ([string]$identity.config_path)
        Assert-HealthSourceMatch $health $source
      }
      $records = @($pending, $last) | Where-Object {
        $null -ne $_ -and
        (Normalize-PathIdentity ([string]$_.generation)) -eq $target
      }
      $record = if (@($records).Count -gt 0) { @($records)[0] } else { $null }
      $relation = 'unknown'
      if ($record -and [string]$task.State -eq 'Running') {
        $targetIdentity = $record.target
        $targetTaskPath = Join-Path ([string]$record.previous) 'target-task.json'
        if (Test-Path -LiteralPath $targetTaskPath -PathType Leaf) {
          $targetIdentity = Read-Json $targetTaskPath
        }
        if ($targetIdentity -and
            (Normalize-PathIdentity ([string]$identity.endpoint_root)) -eq $target -and
            [string]$identity.config_sha256 -ceq [string]$targetIdentity.config_sha256 -and
            [string]$identity.config_digest -ceq [string]$targetIdentity.config_digest -and
            [string]$projection.xml_sha256 -ceq [string]$targetIdentity.task_xml_sha256 -and
            [string]$projection.semantic_sha256 -ceq [string]$targetIdentity.task_semantic_sha256 -and
            (Get-JsonSha256 $projection.principal) -ceq
              (Get-JsonSha256 $targetIdentity.principal) -and
            (Get-JsonSha256 $projection.settings) -ceq
              (Get-JsonSha256 $targetIdentity.settings) -and
            (Test-Runtime-Launcher ([string]$projection.action.execute) `
              ([string]$projection.action.arguments) ([string]$identity.endpoint_root))) {
          $relation = 'target'
        } elseif ($record.source) {
          try {
            Assert-EndpointSourceMatch $record.source $source
            $relation = 'source'
          } catch {
            $relation = 'unknown'
          }
        }
      }
      $live.task_xml_sha256 = [string]$projection.xml_sha256
      $live.task_xml = [string]$projection.xml
      $live.task_semantic_sha256 = [string]$projection.semantic_sha256
      $live.task_acl_sha256 = [string]$projection.security_descriptor_sha256
      $live.endpoint_root = [string]$identity.endpoint_root
      $live.config_path = [string]$identity.config_path
      $live.config_sha256 = [string]$identity.config_sha256
      $live.config_digest = [string]$identity.config_digest
      $live.install_id = [string]$identity.install_id
      $live.release_identity_sha256 = [string]$identity.release_identity_sha256
      $live.release = $identity.release
      $live.source = $source
      $live.listener = $listener
      $live.health = $health
      $live.maintenance_relation = $relation
    } catch {
      $live.error = $_.Exception.Message
      if ($actionCount -eq 1) {
        $live.execute = [string]$task.Actions[0].Execute
        $live.arguments = [string]$task.Actions[0].Arguments
        $live.working_directory = [string]$task.Actions[0].WorkingDirectory
      }
    }
  }
  Emit @{passed=$true; verb='status'; live_task=$live; pending=$pending;
    pending_rollback=$pendingRollback; last_promotion=$last}
  exit 0
}

if ($Verb -eq 'stage') {
  if (-not $StageDir) { Die 'stage_dir_required' 'stage needs -StageDir (the new generation directory)' }
  if (Test-Path -LiteralPath $StageDir) { Die 'stage_exists' "preserve the existing generation: $StageDir" }
  if (-not $task) { Die 'task_missing' 'the instance task is not registered' }
  Assert-RunningTask $task
  # Early fail-closed scan: anything the endpoint schema does not define - `targets` among
  # them - or a duplicate key stops the stage here, before any heavy work, and is never
  # silently dropped.
  $projection = Get-TaskProjection $task
  Set-BindingTaskPath $binding ([string]$projection.task_path)
  $runningGeneration = Get-TaskGeneration $task
  $runningConfig = Config-Path $runningGeneration
  $early = Get-EndpointConfigEarlyProblems $runningConfig
  if ($early.unreadable) {
    Die 'endpoint_config_unreadable' ('the running endpoint configuration cannot be parsed: ' +
      $runningConfig + ' (' + $early.unreadable + ')')
  }
  if (@($early.duplicates).Count -gt 0) {
    Die 'endpoint_config_duplicate_keys' ('the running endpoint configuration repeats keys: ' +
      ($early.duplicates -join ', ') + ' in ' + $runningConfig)
  }
  if (@($early.unknown).Count -gt 0) {
    Die 'endpoint_config_unknown_keys' ('the running endpoint configuration carries keys the current ' +
      'schema does not define: ' + ($early.unknown -join ', ') + ' in ' + $runningConfig +
      '; remove them under change control and re-run stage')
  }
  $runningIdentity = Get-EndpointIdentity $task $projection ([string]$binding.ssh_host)
  if ([int]$runningIdentity.port -ne [int]$binding.port) {
    Die 'windows_task_projection_mismatch' 'the running endpoint port differs from the binding'
  }
  $null = Get-ListenerEvidence $binding $projection
  $runningHealth = Read-EndpointHealth ([string]$runningIdentity.config_path)
  Assert-HealthSourceMatch $runningHealth $runningIdentity
  $runningDocument = Read-Json $runningConfig
  $serviceSid = Get-ManagedServiceSid $task
  $runningEndpointRoot = [string]$runningIdentity.endpoint_root
  $runningInstallIdPath = Join-Path ([string]$runningDocument.state_root) 'install-id'
  $runningTaskFile = Get-TaskFilePath $task
  Set-ManagedEndpointAcls $runningDocument $runningEndpointRoot $runningConfig `
    $runningInstallIdPath $runningTaskFile
  # The canonical domain pins are validated now (an existing pin may only restate the canonical
  # path and the served generation); stage only plans them - promote performs the switch.
  $pins = Assert-DomainPins $binding.state_root $runningGeneration
  $runningBindingPath = Join-Path (Split-Path -Parent $runningGeneration) 'windows-binding.json'
  if (Test-Path -LiteralPath $runningBindingPath -PathType Leaf) {
    $runningBinding = Read-Json $runningBindingPath
    $runningArchive = Join-Path ([string]$runningBinding.dir) ([string]$runningBinding.archive)
    Protect-ManagedArchive $runningArchive ([string]$runningBinding.artifact_sha256) $serviceSid
  } else {
    Die 'archive_binding_missing' 'the running generation carries no archive binding for read-only protection'
  }
  $archive = Join-Path $binding.dir $binding.archive
  if ((Sha256 $archive) -ne $binding.artifact_sha256) { Die 'archive_mismatch' 'the transferred archive digest differs from the release manifest' }
  Protect-ManagedArchive $archive $binding.artifact_sha256 $serviceSid
  New-Item -ItemType Directory -Path $StageDir | Out-Null
  Set-ManagedAcl $StageDir $true -Recurse
  Expand-Archive -LiteralPath $archive -DestinationPath $StageDir
  $files = Read-Json (Join-Path $StageDir 'files.json')
  foreach ($name in $files.PSObject.Properties.Name) {
    if ((Sha256 (Join-Path $StageDir $name)) -ne $files.$name) { Die 'archive_file_mismatch' "file digest mismatch: $name" }
  }
  & py -3.12 -m venv (Join-Path $StageDir 'runtime')
  if ($LASTEXITCODE -ne 0) { Die 'venv_failed' 'python -m venv failed' }
  $runtime = Runtime-Python $StageDir
  & $runtime -I -B -m pip install --no-index --require-hashes --find-links (Join-Path $StageDir 'wheels') -r (Join-Path $StageDir 'requirements.lock')
  if ($LASTEXITCODE -ne 0) { Die 'pip_failed' 'the hash-locked install failed' }
  $script:LifecyclePhase = 'stage-doctor'
  $doctor = & $runtime -I -B -c "import json;from description_pipeline.runtime import tool_record;from description_pipeline.solidworks import doctor;print(json.dumps({'tool':tool_record(),'doctor':doctor()}))"
  if ($LASTEXITCODE -ne 0) { Die 'doctor_failed' 'the installed runtime refused doctor' }
  $identity = $doctor | ConvertFrom-Json
  if ($identity.tool.version -ne $binding.version -or $identity.tool.source_sha256 -ne $binding.source_sha256 -or $identity.tool.release.source_commit -ne $binding.source_commit -or -not $identity.doctor.passed) {
    Die 'identity_mismatch' 'the staged runtime identity differs from the release manifest'
  }
  # The generation consumes a generated candidate configuration: the staged runtime's own
  # canonical writer keeps the schema's fields and values, refuses unknown or duplicate keys,
  # and the release's own read_config test-loads the candidate before stage accepts it; the
  # running file stays byte-sealed for controlled recovery.  A configuration carrying
  # `targets` - or any key outside the strict schema - is refused at stage.
  $script:LifecyclePhase = 'stage-candidate'
  $candidateProgram = Get-EndpointConfigCandidateProgram
  $candidatePath = Write-PythonProgram 'endpoint-candidate' $candidateProgram
  $candidateRc = 1
  $candidateOutput = @()
  try {
    $candidateOutput = & $runtime -I -B $candidatePath $runningConfig (Config-Path $StageDir)
    $candidateRc = $LASTEXITCODE
  } finally {
    Remove-Item -LiteralPath $candidatePath -Force -ErrorAction SilentlyContinue
  }
  if ($candidateRc -ne 0) {
    $problem = ($candidateOutput | Select-Object -Last 1)
    $code = 'endpoint_config_candidate_failed'
    $detail = [string]$problem
    try {
      $parsed = $problem | ConvertFrom-Json
      if ($parsed.error) { $code = [string]$parsed.error; $detail = [string]$parsed.detail }
    } catch { }
    Die $code $detail
  }
  $candidateInfo = ($candidateOutput | Select-Object -Last 1) | ConvertFrom-Json
  $expected = [string]$candidateInfo.config_digest
  $candidateDocument = Read-Json (Config-Path $StageDir)
  $script:LifecyclePhase = 'stage-acl-set'
  Set-ManagedEndpointAcls $candidateDocument $StageDir (Config-Path $StageDir) `
    $runningInstallIdPath $runningTaskFile
  $script:LifecyclePhase = 'stage-acl-assert'
  Assert-ManagedEndpointAcls $candidateDocument $StageDir (Config-Path $StageDir) `
    $runningInstallIdPath $runningTaskFile
  $script:LifecyclePhase = 'stage-receipt'
  Write-Receipt (Join-Path $StageDir 'candidate-identity.json') @{
    expected=$true; at=(Get-Date).ToUniversalTime().ToString('o'); source_config=$runningConfig;
    source_config_sha256=[string]$candidateInfo.source_sha256;
    config_digest=$expected; config_sha256=[string]$candidateInfo.candidate_sha256;
    canonical_digest=[string]$candidateInfo.canonical_digest;
    read_config=[string]$candidateInfo.read_config; version=$binding.version;
    source_commit=$binding.source_commit; source_sha256=$binding.source_sha256}
  Write-Receipt (Join-Path $StageDir 'stage-receipt.json') @{
    passed=$true; verb='stage'; at=(Get-Date).ToUniversalTime().ToString('o'); generation=$StageDir;
    launch_contract=(Python-Module-LaunchContract);
    version=$binding.version; source_commit=$binding.source_commit; source_sha256=$binding.source_sha256;
    pins=@{ endpoint_root=$StageDir; ownership=$pins.canonical }}
  Emit @{passed=$true; verb='stage'; generation=$StageDir; receipt=(Join-Path $StageDir 'stage-receipt.json')}
  exit 0
}

if ($Verb -eq 'promote') {
  if (-not $StageDir) { Die 'stage_dir_required' 'promote needs -StageDir (the staged generation directory)' }
  $receiptPath = Join-Path $StageDir 'stage-receipt.json'
  if (-not (Test-Path -LiteralPath $receiptPath)) { Die 'stage_receipt_missing' 'the generation has no completed stage receipt' }
  if (-not (Test-Path -LiteralPath (Config-Path $StageDir))) { Die 'generation_config_missing' 'the generation carries no endpoint configuration' }
  if (-not $task) { Die 'task_missing' 'the instance task is not registered; install the instance before promoting' }
  Assert-RunningTask $task
  # The candidate configuration is consumed, never regenerated here: the staged bytes must match
  # the digest the stage recorded, and that record must belong to the configuration that is
  # running right now.
  $candidate = Read-Json (Join-Path $StageDir 'candidate-identity.json')
  if (-not $candidate.config_sha256) {
    Die 'candidate_config_unrecorded' 'the staged generation records no candidate configuration digest; re-run stage with the release toolchain'
  }
  if ((Sha256 (Config-Path $StageDir)) -ne [string]$candidate.config_sha256) {
    Die 'candidate_config_mismatch' ('the staged candidate configuration differs from the staged digest: ' + (Config-Path $StageDir))
  }
  $sourceProjection = Get-TaskProjection $task
  Set-BindingTaskPath $binding ([string]$sourceProjection.task_path)
  $sourceIdentity = Get-EndpointIdentity $task $sourceProjection ([string]$binding.ssh_host)
  $previousConfig = [string]$sourceIdentity.config_path
  $previousGeneration = [string]$sourceIdentity.endpoint_root
  if (-not $candidate.source_config_sha256) {
    Die 'candidate_source_unrecorded' 'the staged generation records no source configuration digest; re-run stage with the release toolchain'
  }
  if ((Sha256 $previousConfig) -ne [string]$candidate.source_config_sha256) {
    Die 'candidate_source_mismatch' ('the staged candidate belongs to another running configuration: ' + $previousConfig)
  }
  if ([int]$sourceIdentity.port -ne [int]$binding.port) {
    Die 'windows_task_projection_mismatch' 'the running source endpoint port differs from the binding'
  }
  $sourceListener = Get-ListenerEvidence $binding $sourceProjection
  $sourceHealth = Read-EndpointHealth $previousConfig
  Assert-HealthSourceMatch $sourceHealth $sourceIdentity
  $sourceState = New-EndpointSourceState $task $sourceProjection $sourceIdentity
  if ((Get-GenerationLaunchContract $StageDir) -cne (Python-Module-LaunchContract)) {
    Die 'stage_launch_contract_unsupported' 'the staged candidate does not declare the Python module launcher contract'
  }
  # The equality check runs before any disruption: a pin that is not the canonical domain, or an
  # endpoint-root pin that does not restate the served generation, refuses the promotion.
  $pins = Assert-DomainPins $binding.state_root $previousGeneration
  $serviceSid = Get-ManagedServiceSid $task
  Assert-ManagedArchive (Join-Path $binding.dir $binding.archive) `
    $binding.artifact_sha256 $serviceSid
  $candidateDocument = Read-Json (Config-Path $StageDir)
  $installIdPath = Join-Path ([string]$candidateDocument.state_root) 'install-id'
  $taskFile = Get-TaskFilePath $task
  Assert-ManagedEndpointAcls $candidateDocument $StageDir (Config-Path $StageDir) `
    $installIdPath $taskFile
  New-Item -ItemType Directory -Path $promotions -ErrorAction SilentlyContinue | Out-Null
  $previous = Join-Path $promotions ('previous-' + (Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssfffZ') +
    '-' + [guid]::NewGuid().ToString('N'))
  New-Item -ItemType Directory -Path $previous | Out-Null
  Write-Utf8NoBom (Join-Path $previous 'task.xml') ([string]$sourceProjection.xml)
  Copy-Item -LiteralPath $previousConfig -Destination (Join-Path $previous 'endpoint-before.json')
  if ((Sha256 (Join-Path $previous 'task.xml')) -ne [string]$sourceProjection.xml_sha256 -or
      (Sha256 (Join-Path $previous 'endpoint-before.json')) -ne [string]$sourceIdentity.config_sha256) {
    Die 'source_snapshot_mismatch' 'the sealed source task or configuration differs from the live projection'
  }
  Write-Receipt (Join-Path $previous 'source-state.json') $sourceState
  $sourceStateSha256 = Get-SourceStateAuthorityHash (Join-Path $previous 'source-state.json')
  $pendingTarget = @{
    endpoint_root=(Normalize-PathIdentity $StageDir);
    config_path=(Normalize-PathIdentity (Config-Path $StageDir));
    config_sha256=[string]$candidate.config_sha256;
    config_digest=[string]$candidate.config_digest;
    launch_contract=(Python-Module-LaunchContract);
    version=[string]$binding.version;
    source_commit=[string]$binding.source_commit;
    source_sha256=[string]$binding.source_sha256
  }
  # The intent record is written BEFORE anything switches: even a mid-switch death leaves the
  # generation identity and the bound previous generation on record.
  Write-Receipt (Join-Path $promotions 'pending-promotion.json') @{
    intent=$true; verb='promote'; at=(Get-Date).ToUniversalTime().ToString('o'); generation=$StageDir;
    previous=$previous; task_before=(Join-Path $previous 'task.xml');
    source=$sourceState; source_state_sha256=$sourceStateSha256; source_listener=$sourceListener;
    target=$pendingTarget}
  $mutationStarted = $false
  try {
    $preStopTask = Get-BoundTask $binding 'Stop'
    $preStopProjection = Get-TaskProjection $preStopTask
    $preStopIdentity = Get-EndpointIdentity $preStopTask $preStopProjection ([string]$binding.ssh_host)
    $preStopSource = New-EndpointSourceState $preStopTask $preStopProjection $preStopIdentity
    Assert-EndpointSourceMatch $sourceState $preStopSource
    $preStopListener = Get-ListenerEvidence $binding $preStopProjection
    Assert-ListenerMatch $sourceListener $preStopListener
    $preStopHealth = Read-EndpointHealth ([string]$preStopIdentity.config_path)
    Assert-HealthSourceMatch $preStopHealth $sourceState
    $mutationStarted = $true
    Stop-Endpoint $binding
    Set-ManagedEndpointAcls $candidateDocument $StageDir (Config-Path $StageDir) `
      $installIdPath $taskFile
    $action = New-ScheduledTaskAction -Execute (Runtime-Launcher $StageDir) `
      -Argument (Runtime-LauncherArguments $StageDir) -WorkingDirectory $binding.state_root
    $setTask = Get-TaskSelectionParameters $binding
    $setTask.Action = $action
    $setTask.ErrorAction = 'Stop'
    Set-ScheduledTask @setTask | Out-Null
    $targetTask = Get-BoundTask $binding 'Stop'
    $targetProjection = Get-TaskProjection $targetTask
    if (-not (Test-Runtime-Launcher ([string]$targetProjection.action.execute) `
        ([string]$targetProjection.action.arguments) $StageDir)) {
      throw 'the switched task does not carry the canonical target action'
    }
    if ((Get-JsonSha256 $targetProjection.principal) -ne
          (Get-JsonSha256 $sourceProjection.principal) -or
        (Get-JsonSha256 $targetProjection.settings) -ne
          (Get-JsonSha256 $sourceProjection.settings) -or
        [string]$targetProjection.triggers_sha256 -cne
          [string]$sourceProjection.triggers_sha256 -or
        [string]$targetProjection.security_descriptor_sha256 -cne
          [string]$sourceProjection.security_descriptor_sha256) {
      throw 'the target task changed principal, triggers, settings, or security descriptor'
    }
    Write-Utf8NoBom (Join-Path $previous 'target-task.xml') ([string]$targetProjection.xml)
    $targetState = [ordered]@{
      schema_version = 'solidworks-to-urdf.windows-target-state/v1'
      task_xml_sha256 = [string]$targetProjection.xml_sha256
      task_semantic_sha256 = [string]$targetProjection.semantic_sha256
      task_acl_sha256 = [string]$targetProjection.security_descriptor_sha256
      principal = $targetProjection.principal
      action = $targetProjection.action
      settings = $targetProjection.settings
      endpoint_root = [string]$pendingTarget.endpoint_root
      config_path = [string]$pendingTarget.config_path
      config_sha256 = [string]$pendingTarget.config_sha256
      config_digest = [string]$pendingTarget.config_digest
      launch_contract = [string]$pendingTarget.launch_contract
      version = [string]$pendingTarget.version
      source_commit = [string]$pendingTarget.source_commit
      source_sha256 = [string]$pendingTarget.source_sha256
    }
    Write-Receipt (Join-Path $previous 'target-task.json') $targetState
    # The service reads the pins at start: set them for the execution account before starting the
    # new generation; a failed promotion restores the previous values below.
    Set-DomainPins $binding.state_root $StageDir
    Assert-ManagedEndpointAcls $candidateDocument $StageDir (Config-Path $StageDir) `
      $installIdPath $taskFile
    $started = Start-OrRefuse (Config-Path $StageDir) $binding $StageDir $promotions
    $settled = $started.settled
    $lockRefusal = $started.lock_refusal
    $retriedAfter = $started.retried_after_s
    $health = $settled.health
    if ($health.config_digest -ne $candidate.config_digest) {
      throw 'the running endpoint config digest differs from the staged generation configuration'
    }
    $targetListener = Get-ListenerEvidence $binding $targetProjection
    $receipt = @{
      passed=$true; verb='promote'; at=(Get-Date).ToUniversalTime().ToString('o'); generation=$StageDir;
      previous=$previous; source=$sourceState;
      source_state_sha256=$sourceStateSha256; source_listener=$sourceListener;
      target=$targetState; health=$health; listener=$targetListener;
      settled_seconds=$settled.settled_seconds; listener_pid=$settled.listener_pid}
    if ($lockRefusal) { $receipt['lock_refusal']=$lockRefusal; $receipt['retried']=@{after_s=$retriedAfter} }
    Write-Receipt (Join-Path $promotions 'last-promotion.json') $receipt
    Emit @{passed=$true; verb='promote'; generation=$StageDir; previous=$previous}
    exit 0
  } catch {
    $restore = if ($mutationStarted) {
      Set-AccountEnvironment 'DESCRIPTION_ENDPOINT_ROOT' $pins.previous_root
      Set-AccountEnvironment 'DESCRIPTION_CAD_OWNERSHIP_FILE' $pins.previous_ownership
      Restore-PreviousEndpoint -binding $binding -previous $previous -stageDir $StageDir `
        -expectedTaskPath ([string]$sourceProjection.task_path) `
        -ExpectedSourceStateSha256 $sourceStateSha256
    } else {
      @{stopped=$false; restored=$false; started=$false; healthy=$null; error='mutation did not begin'}
    }
    $failed = @{
      passed=$false; verb='promote'; at=(Get-Date).ToUniversalTime().ToString('o'); generation=$StageDir;
      message=$_.Exception.Message; source_state_sha256=$sourceStateSha256; restore=$restore}
    $failureCode = 'promote_failed'
    if ($_.Exception.Message.StartsWith('lock_refused_unproven:')) { $failureCode = 'lock_refused_unproven' }
    elseif ($_.Exception.Message.StartsWith('lock_refused:')) { $failureCode = 'lock_refused' }
    if ($script:SettleRefusal -and $script:SettleRefusal.lock_refusal) {
      $failed['lock_refusal']=$script:SettleRefusal.lock_refusal
      if ($script:SettleRefusal.retried_after_s) { $failed['retried']=@{after_s=$script:SettleRefusal.retried_after_s} }
    }
    Write-Receipt (Join-Path $previous 'failed-promotion.json') $failed
    if (-not $mutationStarted) {
      Die 'promote_preflight_failed' (
        'the promotion was refused before any Windows task or domain-pin mutation: ' +
        $_.Exception.Message)
    }
    if ($restore.healthy -ne $true) {
      Die 'promote_restore_failed' (
        'the promotion failed and the sealed source endpoint is not proven healthy: ' +
        [string]$restore.error + '; primary=' + $_.Exception.Message)
    }
    Die $failureCode ('the promotion failed and the sealed source endpoint was restored: ' + $_.Exception.Message)
  }
}

if ($Verb -eq 'rollback') {
  # Exactly the sealed source snapshot the caller bounds - never "the latest promotion".
  if (-not $Previous) { Die 'previous_required' 'rollback needs -Previous (the bound previous generation directory)' }
  if (-not $task) { Die 'task_missing' 'the instance task is not registered' }
  Assert-RunningTask $task
  $currentProjection = Get-TaskProjection $task
  Set-BindingTaskPath $binding ([string]$currentProjection.task_path)
  $currentIdentity = Get-EndpointIdentity $task $currentProjection ([string]$binding.ssh_host)
  try {
    $expectedSourceStateSha256 = Read-PromotionSourceAuthority $promotions $Previous `
      ([string]$currentIdentity.endpoint_root)
    $validatedPrevious = Read-ValidatedSnapshot `
      -Directory $Previous `
      -ExpectedTaskName ([string]$binding.task_name) `
      -ExpectedTaskPath ([string]$currentProjection.task_path) `
      -ExpectedStateRoot ([string]$currentIdentity.state_root) `
      -ExpectedSourceStateSha256 $expectedSourceStateSha256
  } catch {
    $message = 'the bound previous snapshot is not safely readable: ' + $_.Exception.Message
    Write-Receipt (Join-Path $promotions 'failed-rollback.json') @{
      passed=$false; verb='rollback'; at=(Get-Date).ToUniversalTime().ToString('o');
      code='previous_invalid'; message=$message; previous=$Previous}
    Die 'previous_invalid' $message
  }
  $currentHealth = Read-EndpointHealth ([string]$currentIdentity.config_path)
  Assert-HealthSourceMatch $currentHealth $currentIdentity
  $currentListener = Get-ListenerEvidence $binding $currentProjection
  $currentSource = New-EndpointSourceState $task $currentProjection $currentIdentity
  $targetSource = $validatedPrevious.source
  $pins = Assert-DomainPins $binding.state_root ([string]$currentIdentity.endpoint_root)
  New-Item -ItemType Directory -Path $promotions -ErrorAction SilentlyContinue | Out-Null
  $rollbackSnapshot = Join-Path $promotions ('rollback-before-' +
    (Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssfffZ') + '-' +
    [guid]::NewGuid().ToString('N'))
  New-Item -ItemType Directory -Path $rollbackSnapshot | Out-Null
  Write-Utf8NoBom (Join-Path $rollbackSnapshot 'task.xml') ([string]$currentProjection.xml)
  Copy-Item -LiteralPath ([string]$currentIdentity.config_path) `
    -Destination (Join-Path $rollbackSnapshot 'endpoint-before.json')
  Write-Receipt (Join-Path $rollbackSnapshot 'source-state.json') $currentSource
  $currentSnapshotAuthority = Get-SourceStateAuthorityHash (
    Join-Path $rollbackSnapshot 'source-state.json')
  if ((Sha256 (Join-Path $rollbackSnapshot 'task.xml')) -ne [string]$currentProjection.xml_sha256 -or
      (Sha256 (Join-Path $rollbackSnapshot 'endpoint-before.json')) -ne
        [string]$currentIdentity.config_sha256 -or
      $currentSnapshotAuthority -cnotmatch '^[0-9a-f]{64}$') {
    Die 'source_snapshot_mismatch' 'the current rollback source could not be sealed exactly'
  }
  Write-Receipt (Join-Path $promotions 'pending-rollback.json') @{
    intent=$true; verb='rollback'; at=(Get-Date).ToUniversalTime().ToString('o');
    rollback_generation=[string]$currentIdentity.endpoint_root;
    restore_to=[string]$targetSource.endpoint_root; previous=$Previous;
    current_snapshot=$rollbackSnapshot; source=$currentSource;
    source_state_sha256=$currentSnapshotAuthority; target=$targetSource}
  $preStopTask = Get-BoundTask $binding 'Stop'
  $preStopProjection = Get-TaskProjection $preStopTask
  $preStopIdentity = Get-EndpointIdentity $preStopTask $preStopProjection ([string]$binding.ssh_host)
  $preStopSource = New-EndpointSourceState $preStopTask $preStopProjection $preStopIdentity
  Assert-EndpointSourceMatch $currentSource $preStopSource
  Assert-ListenerMatch $currentListener (Get-ListenerEvidence $binding $preStopProjection)
  $preStopHealth = Read-EndpointHealth ([string]$preStopIdentity.config_path)
  Assert-HealthSourceMatch $preStopHealth $currentSource
  Set-AccountEnvironment 'DESCRIPTION_ENDPOINT_ROOT' ([string]$targetSource.endpoint_root)
  Set-AccountEnvironment 'DESCRIPTION_CAD_OWNERSHIP_FILE' (Domain-OwnershipPath $binding.state_root)
  $restore = Restore-PreviousEndpoint -binding $binding -previous $Previous `
    -stageDir ([string]$currentIdentity.endpoint_root) `
    -expectedTaskPath ([string]$currentProjection.task_path) `
    -ExpectedSourceStateSha256 $expectedSourceStateSha256
  if (-not $restore.restored -or -not $restore.started -or $restore.healthy -ne $true) {
    Set-AccountEnvironment 'DESCRIPTION_ENDPOINT_ROOT' ([string]$currentIdentity.endpoint_root)
    Set-AccountEnvironment 'DESCRIPTION_CAD_OWNERSHIP_FILE' (Domain-OwnershipPath $binding.state_root)
    if (-not $restore.stopped) {
      Write-Receipt (Join-Path $promotions 'failed-rollback.json') @{
        passed=$false; verb='rollback'; at=(Get-Date).ToUniversalTime().ToString('o');
        code='rollback_stop_failed'; restore=$restore;
        source_state_sha256=$expectedSourceStateSha256;
        current_source_state_sha256=$currentSnapshotAuthority;
        current_snapshot=$rollbackSnapshot; previous=$Previous}
      Die 'rollback_stop_failed' (
        'the current Windows endpoint was not stopped; no restored task was registered: ' +
        [string]$restore.error)
    }
    $currentRecovery = Restore-PreviousEndpoint -binding $binding -previous $rollbackSnapshot `
      -stageDir ([string]$currentIdentity.endpoint_root) `
      -expectedTaskPath ([string]$currentProjection.task_path) `
      -ExpectedSourceStateSha256 $currentSnapshotAuthority
    Write-Receipt (Join-Path $promotions 'failed-rollback.json') @{
      passed=$false; verb='rollback'; at=(Get-Date).ToUniversalTime().ToString('o');
      code=$(if ($currentRecovery.healthy -eq $true) {
        'rollback_failed_current_restored'
      } else {
        'rollback_failed_current_unrecovered'
      });
      restore=$restore; current_recovery=$currentRecovery;
      source_state_sha256=$expectedSourceStateSha256;
      current_source_state_sha256=$currentSnapshotAuthority;
      current_snapshot=$rollbackSnapshot; previous=$Previous}
    $recoveryMessage = if ($currentRecovery.healthy -eq $true) {
      'the current endpoint was restored'
    } else {
      'the current endpoint could not be restored: ' + [string]$currentRecovery.error
    }
    Die 'rollback_failed' ('the sealed source endpoint could not be restored: ' +
      [string]$restore.error + '; ' + $recoveryMessage)
  }
  Set-DomainPins $binding.state_root ([string]$targetSource.endpoint_root)
  $health = Read-EndpointHealth ([string]$targetSource.config_path)
  Assert-HealthSourceMatch $health $targetSource
  Write-Receipt (Join-Path $promotions ('rollback-' + (Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ') + '.json')) @{
    passed=$true; verb='rollback'; at=(Get-Date).ToUniversalTime().ToString('o');
    restored_from=$Previous; rollback_source=$rollbackSnapshot; source=$targetSource;
    source_state_sha256=$expectedSourceStateSha256; restore=$restore; health=$health}
  Emit @{passed=$true; verb='rollback'; restored_from=$Previous}
  exit 0
}

}
if ($MyInvocation.InvocationName -ne '.') { Invoke-LifecycleMain }
