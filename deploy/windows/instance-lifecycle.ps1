# The one isolated-instance lifecycle script.  Every path, task name and port comes from the
# single binding file rendered on the platform host from platform.json + the release manifest;
# nothing here defaults to a personal path, a production task name or a Git credential.
#   stage   verifies the transferred archive, installs the generation runtime, renders the
#           generation's own minimal endpoint.json and records its expected config digest
#   promote stops the old endpoint (no listener AND no writer residue under the generation or
#           state root), switches the task to the generation runtime + config, starts it and
#           settles it for >=180s (task Running + listener PID constant + token-authenticated
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
function Write-Receipt([string]$path, [hashtable]$payload) {
  $parent = Split-Path -Parent $path
  if (-not (Test-Path -LiteralPath $parent)) { New-Item -ItemType Directory -Path $parent | Out-Null }
  Write-Utf8NoBom $path (Emit $payload)
}
function Runtime-Python([string]$generation) { Join-Path $generation 'runtime\Scripts\python.exe' }
function Runtime-Launcher([string]$generation) { Join-Path $generation 'runtime\Scripts\description.exe' }
function Config-Path([string]$generation) { Join-Path $generation 'endpoint.json' }
function Action-Config([object]$task) {
  $match = [regex]::Match([string]$task.Actions[0].Arguments, 'serve --config\s+"?([^"]+)"?')
  if (-not $match.Success) { Die 'task_ambiguous' 'the instance task does not run serve --config' }
  $match.Groups[1].Value
}
function Health-Probe([string]$configPath, [object]$binding) {
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
  if ($health.release.version -ne $binding.version -or $health.release.source_commit -ne $binding.source_commit -or $health.release.source_sha256 -ne $binding.source_sha256) {
    throw 'the instance endpoint /health release identity differs from the bound release'
  }
  if (-not $health.install.install_id -or -not $health.install.config_digest) {
    throw 'the instance endpoint /health does not carry its install identity'
  }
  return $health
}
function Get-ListenerProcess([int]$port) {
  $connection = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue |
    Select-Object -First 1
  if ($connection) { return [int]$connection.OwningProcess }
  return $null
}
function Wait-Settled([string]$configPath, [object]$binding) {
  # The settle window the deployment contract promises: for at least 180 seconds the task is
  # Running, the listener PID on the bound port never changes, and the token-authenticated
  # /health stays ready.  A flapping endpoint can no longer be declared switched inside a
  # 30-second peek: any sample that breaks one of the three conditions fails the promotion.
  $config = Read-Json $configPath
  $token = (Get-Content -LiteralPath $config.token_file -Raw).Trim()
  # Startup grace: the endpoint may need a moment to bind; the sustained window starts at the
  # first all-three-healthy sample and then may not break once.
  $startupDeadline = (Get-Date).AddSeconds(30)
  $listenerPid = $null
  while ($true) {
    $taskNow = Get-ScheduledTask -TaskName $binding.task_name -ErrorAction SilentlyContinue
    if (-not $taskNow -or [string]$taskNow.State -ne 'Running') {
      throw ('the settle window observed task state ' + $(if ($taskNow) { [string]$taskNow.State } else { 'missing' }))
    }
    $pidNow = Get-ListenerProcess $config.port
    $ready = $false
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
  $timer = [System.Diagnostics.Stopwatch]::StartNew()
  $settleDeadline = (Get-Date).AddSeconds(180)
  while ((Get-Date) -lt $settleDeadline) {
    $taskNow = Get-ScheduledTask -TaskName $binding.task_name -ErrorAction SilentlyContinue
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
    $output = [string]((cmd /c "`"$(Runtime-Python $generation)`" -I `"$probePath`" `"$lockPath`" 2>nul") | Out-String)
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
  $taskNow = Get-ScheduledTask -TaskName $binding.task_name -ErrorAction SilentlyContinue
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
  Start-ScheduledTask -TaskName $binding.task_name
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
    Start-ScheduledTask -TaskName $binding.task_name
    try { $result.settled = Wait-Settled $configPath $binding; return $result }
    catch {
      $second = Get-OwnershipRefusalFacts $stageDir $binding
      if (-not $second.refused) { throw }
      throw ('lock_refused: the ownership lock was refused twice; stopping for diagnosis [owner_state=' +
             [string]$second.owner_state + ']')
    }
  }
}
function Get-PreviousGenerationBinding([string]$generation, [object]$binding) {
  # The previous generation's own pinned release identity, read from its stage receipt; without a
  # receipt the binding stays unavailable instead of borrowing the candidate's new identity.
  if (-not $generation) { return $null }
  $receipt = Join-Path $generation 'stage-receipt.json'
  if (-not (Test-Path -LiteralPath $receipt)) { return $null }
  try { $record = Read-Json $receipt } catch { return $null }
  if (-not ($record.version -and $record.source_commit -and $record.source_sha256)) { return $null }
  return @{ task_name=$binding.task_name; state_root=$binding.state_root; generation=$generation;
            port=$binding.port; version=$record.version; source_commit=$record.source_commit;
            source_sha256=$record.source_sha256 }
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
    $scanOutput = & $python.executable @($python.prefix) -I $programPath $configPath 2>&1
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
function Restore-PreviousEndpoint([object]$binding, [string]$previous, [string]$previousConfig, [string]$stageDir, [string]$sourceLauncher) {
  # The safe stop/restore every promote failure path runs: stop whatever now serves the task,
  # restore the task snapshot, then start the previous generation only on startable evidence
  # (lock free, no lock file, or refused with a proven-ended owner) - an unknown owner or a
  # failed observer keeps the endpoint stopped for diagnosis.  Health is verified against the
  # previous generation's own pinned release identity; without that receipt the health stays
  # unjudged instead of being misfired at the new binding.  The facts land in the failure
  # receipt; the admission stays closed regardless.
  $restore = @{stopped=$false; restored=$false; start_gate=''; startable=$false; started=$false;
               healthy=$null; previous_generation=''; error=''}
  try {
    Stop-ScheduledTask -TaskName $binding.task_name -ErrorAction SilentlyContinue
    $restore.stopped = $true
  } catch { if (-not $restore.error) { $restore.error = 'stop: ' + $_.Exception.Message } }
  try {
    Register-ManagedTask -TaskName $binding.task_name -TaskPath ([string]$binding.task_path) `
      -TaskXml (Read-Utf8Text (Join-Path $previous 'task.xml')) -Force
    $restore.restored = $true
  } catch { if (-not $restore.error) { $restore.error = 'restore: ' + $_.Exception.Message } }
  $previousGeneration = ''
  if ($sourceLauncher) {
    try {
      $directory = [System.IO.Path]::GetDirectoryName($sourceLauncher)
      $directory = if ($directory) { [System.IO.Path]::GetDirectoryName($directory) } else { '' }
      $previousGeneration = if ($directory) { [System.IO.Path]::GetDirectoryName($directory) } else { '' }
    } catch { $previousGeneration = '' }
  }
  $restore.previous_generation = [string]$previousGeneration
  $gate = Get-OwnershipRefusalFacts $stageDir $binding
  $restore.start_gate = [string]$gate.state
  $restore.startable = ($gate.state -in @('lock_free', 'no_lock_file')) -or
    (($gate.state -eq 'refused') -and [bool]$gate.owner_ended)
  if ($restore.restored) {
    if ($restore.startable) {
      try {
        Start-ScheduledTask -TaskName $binding.task_name
        $restore.started = $true
        $pinned = Get-PreviousGenerationBinding $previousGeneration $binding
        if ($null -eq $pinned) {
          if (-not $restore.error) {
            $restore.error = 'the previous generation receipt is unavailable; the health probe stays unbound'
          }
        } else {
          $null = Health-Probe $previousConfig $pinned
          $restore.healthy = $true
        }
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
  Stop-ScheduledTask -TaskName $binding.task_name
  $deadline = (Get-Date).AddSeconds(30)
  while ($true) {
    $listening = Get-NetTCPConnection -LocalPort $binding.port -State Listen -ErrorAction SilentlyContinue
    $residual = @(Get-WriterResidue $binding)
    if (-not $listening -and $residual.Count -eq 0) { return }
    if ((Get-Date) -ge $deadline) { break }
    Start-Sleep -Milliseconds 500
  }
  Die 'stop_failed' 'the old endpoint still listens or leaves a writing process under the generation/state root; refusing to switch'
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
  $output = & $runtime -I -c $program $configPath
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
    } | Sort-Object sid, rights, access_type, inheritance, propagation, inherited)
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
function Get-ManagedServiceSid($Task) {
  $serviceSid = Resolve-Sid ([string]$Task.Principal.UserId)
  $operatorSid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
  if ($operatorSid -ne $serviceSid) {
    Die 'windows_operator_principal_mismatch' 'the SSH account differs from the task principal'
  }
  return $serviceSid
}
function Assert-ManagedAcl([string]$Path, [string]$ServiceSid, [switch]$Recurse) {
  $pending = New-Object 'System.Collections.Generic.Stack[string]'
  $pending.Push([System.IO.Path]::GetFullPath($Path))
  $expectedSid = Resolve-Sid $ServiceSid
  while ($pending.Count -gt 0) {
    $current = $pending.Pop()
    $item = Get-Item -LiteralPath $current -Force -ErrorAction Stop
    if ($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) {
      Die 'windows_managed_acl_unsafe' ('a managed ACL path is a reparse point: ' + $current)
    }
    $projection = Get-AclProjection $current
    $expectedInheritance = if ($item.PSIsContainer) {
      [int](
        [System.Security.AccessControl.InheritanceFlags]::ContainerInherit -bor
        [System.Security.AccessControl.InheritanceFlags]::ObjectInherit
      )
    } else {
      [int][System.Security.AccessControl.InheritanceFlags]::None
    }
    $rules = @($projection.rules)
    if ($projection.owner_sid -ne $expectedSid -or
        -not [bool]$projection.protected -or
        $rules.Count -ne 1 -or
        [string]$rules[0].sid -ne $expectedSid -or
        [string]$rules[0].access_type -ne 'Allow' -or
        [long]$rules[0].rights -ne [long][System.Security.AccessControl.FileSystemRights]::FullControl -or
        [bool]$rules[0].inherited -or
        [int]$rules[0].inheritance -ne $expectedInheritance -or
        [int]$rules[0].propagation -ne 0) {
      Die 'windows_managed_acl_invalid' ('the managed path does not have the one-owner full-control ACL: ' + $current)
    }
    if ($Recurse -and $item.PSIsContainer) {
      foreach ($child in @(Get-ChildItem -LiteralPath $current -Force -ErrorAction Stop)) {
        $pending.Push($child.FullName)
      }
    }
  }
}
function Set-ManagedAcl([string]$Path, [string]$ServiceSid, [switch]$Recurse) {
  $pending = New-Object 'System.Collections.Generic.Stack[string]'
  $pending.Push([System.IO.Path]::GetFullPath($Path))
  $expectedSid = Resolve-Sid $ServiceSid
  while ($pending.Count -gt 0) {
    $current = $pending.Pop()
    $item = Get-Item -LiteralPath $current -Force -ErrorAction Stop
    if ($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) {
      Die 'windows_managed_acl_unsafe' ('a managed ACL path is a reparse point: ' + $current)
    }
    $acl = Get-Acl -LiteralPath $current -ErrorAction Stop
    $sidObject = New-Object System.Security.Principal.SecurityIdentifier -ArgumentList $expectedSid
    $acl.SetOwner($sidObject)
    $acl.SetAccessRuleProtection($true, $false)
    foreach ($rule in @($acl.GetAccessRules(
        $true,
        $false,
        [System.Security.Principal.SecurityIdentifier]))) {
      [void]$acl.RemoveAccessRuleSpecific($rule)
    }
    if ($item.PSIsContainer) {
      $inheritance =
        [System.Security.AccessControl.InheritanceFlags]::ContainerInherit -bor
        [System.Security.AccessControl.InheritanceFlags]::ObjectInherit
    } else {
      $inheritance = [System.Security.AccessControl.InheritanceFlags]::None
    }
    $rule = New-Object System.Security.AccessControl.FileSystemAccessRule -ArgumentList @(
      $sidObject,
      [System.Security.AccessControl.FileSystemRights]::FullControl,
      $inheritance,
      [System.Security.AccessControl.PropagationFlags]::None,
      [System.Security.AccessControl.AccessControlType]::Allow)
    [void]$acl.AddAccessRule($rule)
    Set-Acl -LiteralPath $current -AclObject $acl -ErrorAction Stop
    Assert-ManagedAcl $current $expectedSid
    if ($Recurse -and $item.PSIsContainer) {
      foreach ($child in @(Get-ChildItem -LiteralPath $current -Force -ErrorAction Stop)) {
        $pending.Push($child.FullName)
      }
    }
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
  if ($common -in $specialRoots -or (Test-ManagedPathWithin $common $systemRoot)) {
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
function Get-ManagedEndpointAclRows($Config, [string]$EndpointRoot, [string]$ConfigPath, [string]$InstallIdPath) {
  $paths = @(Get-ManagedEndpointPaths $Config $EndpointRoot $ConfigPath $InstallIdPath)
  $common = Get-ManagedCommonParent $paths
  $rows = @()
  $seen = @{}
  foreach ($path in @($common + $paths)) {
    $normalized = (Normalize-PathIdentity $path).TrimEnd('/')
    $key = $normalized.ToLowerInvariant()
    if (-not $seen.ContainsKey($key)) {
      $seen[$key] = $true
      $rows += [pscustomobject]@{
        path=$normalized
        recurse=($normalized -eq $common)
      }
    }
  }
  return $rows
}
function Set-ManagedEndpointAcls($Config, [string]$EndpointRoot, [string]$ConfigPath, [string]$InstallIdPath, [string]$ServiceSid) {
  foreach ($row in @(Get-ManagedEndpointAclRows $Config $EndpointRoot $ConfigPath $InstallIdPath)) {
    if (-not (Test-Path -LiteralPath $row.path)) {
      Die 'windows_managed_acl_missing' ('a managed endpoint path is missing: ' + $row.path)
    }
    Set-ManagedAcl $row.path $ServiceSid -Recurse:$row.recurse
  }
}
function Assert-ManagedEndpointAcls($Config, [string]$EndpointRoot, [string]$ConfigPath, [string]$InstallIdPath, [string]$ServiceSid) {
  foreach ($row in @(Get-ManagedEndpointAclRows $Config $EndpointRoot $ConfigPath $InstallIdPath)) {
    if (-not (Test-Path -LiteralPath $row.path)) {
      Die 'windows_managed_acl_missing' ('a managed endpoint path is missing: ' + $row.path)
    }
    Assert-ManagedAcl $row.path $ServiceSid -Recurse:$row.recurse
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
function Assert-NoReparseAncestor([string]$Path) {
  $current = [System.IO.Path]::GetFullPath($Path)
  while ($current) {
    if (Test-Path -LiteralPath $current) {
      $item = Get-Item -LiteralPath $current -Force -ErrorAction Stop
      if ($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) {
        Die 'windows_recovery_target_unsafe' ('recovery path crosses a reparse point: ' + $current)
      }
    }
    $parent = [System.IO.Path]::GetDirectoryName($current)
    if (-not $parent -or $parent -eq $current) { break }
    $current = $parent
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
function Get-TaskProjection($Task) {
  if (@($Task.Actions).Count -ne 1) {
    Die 'windows_action_invalid' 'the Windows task must carry exactly one action'
  }
  $principalSid = Resolve-Sid ([string]$Task.Principal.UserId)
  $operatorSid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
  if ($operatorSid -ne $principalSid) {
    Die 'windows_operator_principal_mismatch' 'the SSH account differs from the task principal'
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
  }
  if ($principal.logon_type -notin @('Interactive', 'InteractiveToken')) {
    Die 'windows_logon_type_unsupported' 'the task principal is not interactive'
  }
  $action = [ordered]@{
    execute = [string]$Task.Actions[0].Execute
    arguments = [string]$Task.Actions[0].Arguments
    working_directory = [string]$Task.Actions[0].WorkingDirectory
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
  $semantic = [ordered]@{
    task_path = [string]$Task.TaskPath
    task_name = [string]$Task.TaskName
    uri = ([string]$Task.TaskPath + [string]$Task.TaskName)
    principal = $principal
    action = $action
    settings = $settings
    triggers_sha256 = Get-JsonSha256 @($Task.Triggers)
    security_descriptor_sha256 = [string]$taskFileAcl.projection_sha256
  }
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
function Get-RecoveryIdentity($Task, $TaskProjection, [string]$SshHost) {
  $configPath = Action-Config $Task
  Assert-NoReparseAncestor $configPath
  $configDocument = Read-Json $configPath
  $execute = [string]$Task.Actions[0].Execute
  $scripts = Split-Path -Parent $execute
  $runtime = Split-Path -Parent $scripts
  $generation = Split-Path -Parent $runtime
  $stateRoot = [string]$configDocument.state_root
  $lifecycleRoot = Join-Path $stateRoot 'lifecycle'
  foreach ($path in @($configPath, $execute, $generation, $stateRoot, $lifecycleRoot)) {
    Assert-NoReparseAncestor $path
  }
  if ((Normalize-PathIdentity $execute) -ne
      (Normalize-PathIdentity (Join-Path $generation 'runtime\Scripts\description.exe')) -or
      -not (Normalize-PathIdentity $generation).StartsWith(
        (Normalize-PathIdentity $lifecycleRoot) + '/',
        [System.StringComparison]::OrdinalIgnoreCase) -or
      (Normalize-PathIdentity $configPath) -ne
      (Normalize-PathIdentity (Join-Path $generation 'endpoint.json')) -or
      [string]$Task.Actions[0].Arguments -ne
      ('serve --config "' + (Config-Path $generation) + '"')) {
    Die 'windows_task_projection_mismatch' 'the task action does not bind the managed endpoint generation'
  }
  if (-not (Test-Path -LiteralPath (Runtime-Python $generation))) {
    Die 'windows_endpoint_not_provisioned' 'the task generation has no installed runtime'
  }
  if ((Normalize-PathIdentity $configPath) -ne (Normalize-PathIdentity (Config-Path $generation))) {
    Die 'windows_task_projection_mismatch' 'the task action does not bind its generation configuration'
  }
  if ((Normalize-PathIdentity ([string]$Task.Actions[0].WorkingDirectory)) -ne
      (Normalize-PathIdentity ([string]$configDocument.state_root))) {
    Die 'windows_task_projection_mismatch' 'the task working directory differs from the endpoint state root'
  }
  $python = Runtime-Python $generation
  $program = "import json,sys;" +
    "from description_pipeline.orchestration import windows;" +
    "from description_pipeline.runtime import tool_record;" +
    "c=windows.read_config(sys.argv[1]);" +
    "print(json.dumps({'config_digest':c['config_digest'],'package_root':str(c['package_root'])," +
    "'output_root':str(c['output_root']),'state_root':str(c['state_root']),'port':c['port']," +
    "'release':tool_record(role='native')},sort_keys=True))"
  $output = & $python -I -c $program $configPath
  if ($LASTEXITCODE -ne 0) {
    Die 'windows_recovery_identity_unreadable' 'the installed endpoint runtime could not identify itself'
  }
  $probe = ($output | Select-Object -Last 1) | ConvertFrom-Json
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
    user_environment = [ordered]@{
      DESCRIPTION_ENDPOINT_ROOT = Get-AccountEnvironment 'DESCRIPTION_ENDPOINT_ROOT'
      DESCRIPTION_CAD_OWNERSHIP_FILE = Get-AccountEnvironment 'DESCRIPTION_CAD_OWNERSHIP_FILE'
    }
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
    $output = & (Runtime-Python ([string]$Identity.endpoint_root)) -I -c $program
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
    $errorId -match '^CmdletizationQuery_NotFound_[A-Za-z0-9_]+,Get-NetTCPConnection$'
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
  $identity = Get-RecoveryIdentity $task $taskProjection $SshHost
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
  Assert-ManagedEndpointAcls $configDocument ([string]$identity.endpoint_root) `
    ([string]$identity.config_path) ([string]$identity.install_id_path) `
    ([string]$taskProjection.principal.sid)
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
    (Get-RecoveryFileRow 'endpoint_config' ([string]$identity.config_path) 'endpoint.json'),
    (Get-RecoveryFileRow 'endpoint_token' ([string]$identity.token_path) 'token'),
    (Get-RecoveryFileRow 'install_id' ([string]$identity.install_id_path) 'install-id')
  )
  if ([bool]$cad.ledger_present) {
    $files += Get-RecoveryFileRow 'cad_ownership' ([string]$cad.ownership_path) 'cad-ownership.json'
  }
  $acl = [ordered]@{
    task_file = Get-AclProjection $taskFile
    endpoint_config = Get-AclProjection ([string]$identity.config_path)
    endpoint_token = Get-AclProjection ([string]$identity.token_path)
    install_id = Get-AclProjection ([string]$identity.install_id_path)
    cad_ownership = $(if ([bool]$cad.ledger_present) { Get-AclProjection ([string]$cad.ownership_path) } else { $null })
    roots = [ordered]@{
      package_root = Get-AclProjection ([string]$identity.package_root)
      output_root = Get-AclProjection ([string]$identity.output_root)
      state_root = Get-AclProjection ([string]$identity.state_root)
      endpoint_root = Get-AclProjection ([string]$identity.endpoint_root)
    }
  }
  $state = [ordered]@{
    schema_version = 'solidworks-to-urdf.windows-recovery/v1'
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
      user_environment = $identity.user_environment
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
  if ($state.schema_version -ne 'solidworks-to-urdf.windows-recovery/v1') {
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
    endpoint='stopped'; cad_ownership='free'; bundle_sha256=(Sha256 $zip)
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
      $existing = @(Get-ScheduledTask -TaskName ([string]$Binding.task_name) -ErrorAction SilentlyContinue)
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
      Set-AclProjection (Join-Path $env:SystemRoot ('System32\Tasks\' + ([string]$Binding.task_path).TrimStart('\') + [string]$Binding.task_name)) $state.acl.task_file
      Set-AclProjection ([string]$state.identity.package_root) $state.acl.roots.package_root
      Set-AclProjection ([string]$state.identity.output_root) $state.acl.roots.output_root
      Set-AclProjection ([string]$state.identity.state_root) $state.acl.roots.state_root
      Set-AclProjection ([string]$state.identity.endpoint_root) $state.acl.roots.endpoint_root
      Assert-ManagedEndpointAcls $configDocument ([string]$state.identity.endpoint_root) `
        ([string]$state.identity.config_path) ([string]$state.identity.install_id_path) `
        ([string]$state.task.principal.sid)
      Set-DomainPins ([string]$state.identity.state_root) ([string]$state.identity.endpoint_root)
    }
    $current = Get-RecoveryCurrentState ([string]$Binding.ssh_host) ([string]$Binding.task_path) ([string]$Binding.task_name)
    if ((Get-JsonSha256 $current.state) -ne (Get-JsonSha256 $state)) {
      Die 'windows_recovery_verification_failed' 'Windows recovery did not read back the archived state'
    }
    $cleanup = Close-RecoveryTemp
    Emit @{
      passed=$true; verb=$Script:Verb; task_state=[string]$current.task_projection.state;
      endpoint='stopped'; cad_ownership='free'; state_sha256=(Get-JsonSha256 $state);
      cleanup=$cleanup
    }
  } catch {
    $primaryError = $_
    $failedOwner = $ownedTask
    if (-not $VerifyOnly -and $registeringTask -and $state) {
      try {
        $candidate = @(Get-ScheduledTask -TaskName ([string]$Binding.task_name) -ErrorAction Stop |
          Where-Object { [string]$_.TaskPath -eq [string]$Binding.task_path })
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
  $task = Get-RecoveryTask ([string]$Binding.task_name) ''
  $projection = Get-TaskProjection $task
  $identity = Get-RecoveryIdentity $task $projection ([string]$Binding.ssh_host)
  if ($identity.install_id -ne [string]$Binding.install_id -or
      $identity.config_digest -ne [string]$Binding.config_digest -or
      $identity.release_identity_sha256 -ne [string]$Binding.release_identity_sha256) {
    Die 'windows_resume_identity_mismatch' 'the Windows endpoint identity differs from the active install record'
  }
  $cad = Get-RecoveryCadState $identity
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
  if ($health.install.install_id -ne [string]$Binding.install_id -or
      $health.install.config_digest -ne [string]$Binding.config_digest -or
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
$task = Get-ScheduledTask -TaskName $binding.task_name -ErrorAction SilentlyContinue
$promotions = Join-Path $binding.state_root 'instance-promotions'

if ($Verb -eq 'health') {
  if (-not $task) { Die 'task_missing' 'the instance task is not registered' }
  try { $probe = Health-Probe (Action-Config $task) $binding } catch { Die 'endpoint_unhealthy' $_.Exception.Message }
  Emit @{passed=$true; verb='health'; health=$probe}
  exit 0
}

if ($Verb -eq 'status') {
  # Read-only relay of the LIVE task facts plus the maintenance records: the platform host
  # reconciles an unacknowledged switch from what the task actually runs, never by resending it.
  $live = $null
  if ($task) {
    $liveConfig = ''
    try { $liveConfig = Action-Config $task } catch { }
    $live = @{execute=$task.Actions[0].Execute; arguments=$task.Actions[0].Arguments;
              config=$liveConfig; state=[string]$task.State}
  }
  $pending = $null
  $pendingPath = Join-Path $promotions 'pending-promotion.json'
  if (Test-Path -LiteralPath $pendingPath) { $pending = Read-Json $pendingPath }
  $last = $null
  $lastPath = Join-Path $promotions 'last-promotion.json'
  if (Test-Path -LiteralPath $lastPath) { $last = Read-Json $lastPath }
  Emit @{passed=$true; verb='status'; live_task=$live; pending=$pending; last_promotion=$last}
  exit 0
}

if ($Verb -eq 'stage') {
  if (-not $StageDir) { Die 'stage_dir_required' 'stage needs -StageDir (the new generation directory)' }
  if (Test-Path -LiteralPath $StageDir) { Die 'stage_exists' "preserve the existing generation: $StageDir" }
  if (-not $task) { Die 'task_missing' 'the instance task is not registered' }
  # Early fail-closed scan: anything the endpoint schema does not define - `targets` among
  # them - or a duplicate key stops the stage here, before any heavy work, and is never
  # silently dropped.
  $runningConfig = Action-Config $task
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
  $runningDocument = Read-Json $runningConfig
  $serviceSid = Get-ManagedServiceSid $task
  $runningEndpointRoot = Split-Path -Parent $runningConfig
  $runningInstallIdPath = Join-Path ([string]$runningDocument.state_root) 'install-id'
  Set-ManagedEndpointAcls $runningDocument $runningEndpointRoot $runningConfig `
    $runningInstallIdPath $serviceSid
  # The canonical domain pins are validated now (an existing pin may only restate the canonical
  # path and the served generation); stage only plans them - promote performs the switch.
  $pins = Assert-DomainPins $binding.state_root (Split-Path -Parent $runningConfig)
  $archive = Join-Path $binding.dir $binding.archive
  if ((Sha256 $archive) -ne $binding.artifact_sha256) { Die 'archive_mismatch' 'the transferred archive digest differs from the release manifest' }
  New-Item -ItemType Directory -Path $StageDir | Out-Null
  Set-ManagedAcl $StageDir $serviceSid -Recurse
  Expand-Archive -LiteralPath $archive -DestinationPath $StageDir
  $files = Read-Json (Join-Path $StageDir 'files.json')
  foreach ($name in $files.PSObject.Properties.Name) {
    if ((Sha256 (Join-Path $StageDir $name)) -ne $files.$name) { Die 'archive_file_mismatch' "file digest mismatch: $name" }
  }
  & py -3.12 -m venv (Join-Path $StageDir 'runtime')
  if ($LASTEXITCODE -ne 0) { Die 'venv_failed' 'python -m venv failed' }
  $runtime = Runtime-Python $StageDir
  & $runtime -I -m pip install --no-index --require-hashes --find-links (Join-Path $StageDir 'wheels') -r (Join-Path $StageDir 'requirements.lock')
  if ($LASTEXITCODE -ne 0) { Die 'pip_failed' 'the hash-locked install failed' }
  $doctor = & $runtime -I -c "import json;from description_pipeline.runtime import tool_record;from description_pipeline.solidworks import doctor;print(json.dumps({'tool':tool_record(),'doctor':doctor()}))"
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
  $candidateProgram = Get-EndpointConfigCandidateProgram
  $candidatePath = Write-PythonProgram 'endpoint-candidate' $candidateProgram
  $candidateRc = 1
  $candidateOutput = @()
  try {
    $candidateOutput = & $runtime -I $candidatePath $runningConfig (Config-Path $StageDir)
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
  Set-ManagedEndpointAcls $candidateDocument $StageDir (Config-Path $StageDir) `
    $runningInstallIdPath $serviceSid
  Assert-ManagedEndpointAcls $candidateDocument $StageDir (Config-Path $StageDir) `
    $runningInstallIdPath $serviceSid
  Write-Receipt (Join-Path $StageDir 'candidate-identity.json') @{
    expected=$true; at=(Get-Date).ToUniversalTime().ToString('o'); source_config=$runningConfig;
    source_config_sha256=[string]$candidateInfo.source_sha256;
    config_digest=$expected; config_sha256=[string]$candidateInfo.candidate_sha256;
    canonical_digest=[string]$candidateInfo.canonical_digest;
    read_config=[string]$candidateInfo.read_config; version=$binding.version;
    source_commit=$binding.source_commit; source_sha256=$binding.source_sha256}
  Write-Receipt (Join-Path $StageDir 'stage-receipt.json') @{
    passed=$true; verb='stage'; at=(Get-Date).ToUniversalTime().ToString('o'); generation=$StageDir;
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
  if (@($task.Actions).Count -ne 1) { Die 'task_ambiguous' 'the instance task must carry exactly one action' }
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
  $previousConfig = Action-Config $task
  if (-not $candidate.source_config_sha256) {
    Die 'candidate_source_unrecorded' 'the staged generation records no source configuration digest; re-run stage with the release toolchain'
  }
  if ((Sha256 $previousConfig) -ne [string]$candidate.source_config_sha256) {
    Die 'candidate_source_mismatch' ('the staged candidate belongs to another running configuration: ' + $previousConfig)
  }
  # The equality check runs before any disruption: a pin that is not the canonical domain, or an
  # endpoint-root pin that does not restate the served generation, refuses the promotion.
  $pins = Assert-DomainPins $binding.state_root (Split-Path -Parent $previousConfig)
  $serviceSid = Get-ManagedServiceSid $task
  $candidateDocument = Read-Json (Config-Path $StageDir)
  $installIdPath = Join-Path ([string]$candidateDocument.state_root) 'install-id'
  Set-ManagedEndpointAcls $candidateDocument $StageDir (Config-Path $StageDir) `
    $installIdPath $serviceSid
  Assert-ManagedEndpointAcls $candidateDocument $StageDir (Config-Path $StageDir) `
    $installIdPath $serviceSid
  $sourceExecute = $task.Actions[0].Execute
  $sourceConfig = $previousConfig
  Stop-Endpoint $binding
  New-Item -ItemType Directory -Path $promotions -ErrorAction SilentlyContinue | Out-Null
  $previous = Join-Path $promotions ('previous-' + (Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ'))
  New-Item -ItemType Directory -Path $previous | Out-Null
  Export-ScheduledTask -TaskName $binding.task_name | Set-Content -LiteralPath (Join-Path $previous 'task.xml') -Encoding UTF8
  Copy-Item -LiteralPath $previousConfig -Destination (Join-Path $previous 'endpoint-before.json')
  # The intent record is written BEFORE anything switches: even a mid-switch death leaves the
  # generation identity and the bound previous generation on record.
  Write-Receipt (Join-Path $promotions 'pending-promotion.json') @{
    intent=$true; verb='promote'; at=(Get-Date).ToUniversalTime().ToString('o'); generation=$StageDir;
    previous=$previous; task_before=(Join-Path $previous 'task.xml');
    source_launcher=$sourceExecute; source_config=$sourceConfig}
  $action = New-ScheduledTaskAction -Execute (Runtime-Launcher $StageDir) `
    -Argument ('serve --config "' + (Config-Path $StageDir) + '"') -WorkingDirectory $binding.state_root
  Set-ScheduledTask -TaskName $binding.task_name -Action $action | Out-Null
  # The service reads the pins at start: set them for the execution account before starting the
  # new generation; a failed promotion restores the previous values below.
  Set-DomainPins $binding.state_root $StageDir
  Assert-ManagedEndpointAcls $candidateDocument $StageDir (Config-Path $StageDir) `
    $installIdPath $serviceSid
  try {
    $started = Start-OrRefuse (Config-Path $StageDir) $binding $StageDir $promotions
    $settled = $started.settled
    $lockRefusal = $started.lock_refusal
    $retriedAfter = $started.retried_after_s
    $health = $settled.health
    if ($health.install.config_digest -ne $candidate.config_digest) {
      throw 'the running endpoint config digest differs from the staged generation configuration'
    }
    $receipt = @{
      passed=$true; verb='promote'; at=(Get-Date).ToUniversalTime().ToString('o'); generation=$StageDir;
      previous=$previous; source_launcher=$sourceExecute; source_config=$sourceConfig; health=$health;
      settled_seconds=$settled.settled_seconds; listener_pid=$settled.listener_pid}
    if ($lockRefusal) { $receipt['lock_refusal']=$lockRefusal; $receipt['retried']=@{after_s=$retriedAfter} }
    Write-Receipt (Join-Path $promotions 'last-promotion.json') $receipt
    Emit @{passed=$true; verb='promote'; generation=$StageDir; previous=$previous}
    exit 0
  } catch {
    Set-AccountEnvironment 'DESCRIPTION_ENDPOINT_ROOT' $pins.previous_root
    Set-AccountEnvironment 'DESCRIPTION_CAD_OWNERSHIP_FILE' $pins.previous_ownership
    $restore = Restore-PreviousEndpoint $binding $previous $previousConfig $StageDir $sourceExecute
    $failed = @{
      passed=$false; verb='promote'; at=(Get-Date).ToUniversalTime().ToString('o'); generation=$StageDir;
      message=$_.Exception.Message; restore=$restore}
    $failureCode = 'promote_failed'
    if ($_.Exception.Message.StartsWith('lock_refused_unproven:')) { $failureCode = 'lock_refused_unproven' }
    elseif ($_.Exception.Message.StartsWith('lock_refused:')) { $failureCode = 'lock_refused' }
    if ($script:SettleRefusal -and $script:SettleRefusal.lock_refusal) {
      $failed['lock_refusal']=$script:SettleRefusal.lock_refusal
      if ($script:SettleRefusal.retried_after_s) { $failed['retried']=@{after_s=$script:SettleRefusal.retried_after_s} }
    }
    Write-Receipt (Join-Path $previous 'failed-promotion.json') $failed
    Die $failureCode ('the promotion failed and the previous task was restored: ' + $_.Exception.Message)
  }
}

if ($Verb -eq 'rollback') {
  # Exactly the generation directory the caller bounds - never "the latest promotion".
  if (-not $Previous) { Die 'previous_required' 'rollback needs -Previous (the bound previous generation directory)' }
  if (-not (Test-Path -LiteralPath (Join-Path $Previous 'task.xml'))) { Die 'previous_missing' "the bound previous generation carries no task snapshot: $Previous" }
  if (-not $task) { Die 'task_missing' 'the instance task is not registered' }
  # The pin must restate the generation being served right now; the rollback then re-points the
  # pins to the restored generation - a legitimate switch, never a divergent override.
  $servingConfig = Action-Config $task
  $pins = Assert-DomainPins $binding.state_root (Split-Path -Parent $servingConfig)
  Stop-Endpoint $binding
  Register-ManagedTask -TaskName $binding.task_name -TaskPath ([string]$binding.task_path) `
    -TaskXml (Read-Utf8Text (Join-Path $Previous 'task.xml')) -Force
  # The restored task names the previous generation's own configuration; that file is the one
  # the recorded endpoint-before.json replaces - the candidate generation stays untouched.
  $restored = Get-ScheduledTask -TaskName $binding.task_name
  $previousConfig = Action-Config $restored
  Copy-Item -LiteralPath (Join-Path $Previous 'endpoint-before.json') -Destination $previousConfig -Force
  $serviceSid = Get-ManagedServiceSid $restored
  $restoredDocument = Read-Json $previousConfig
  $installIdPath = Join-Path ([string]$restoredDocument.state_root) 'install-id'
  Set-ManagedEndpointAcls $restoredDocument (Split-Path -Parent $previousConfig) `
    $previousConfig $installIdPath $serviceSid
  Set-DomainPins $binding.state_root (Split-Path -Parent $previousConfig)
  Assert-ManagedEndpointAcls $restoredDocument (Split-Path -Parent $previousConfig) `
    $previousConfig $installIdPath $serviceSid
  Start-ScheduledTask -TaskName $binding.task_name
  try { $health = Health-Probe $previousConfig $binding } catch {
    Write-Receipt (Join-Path $promotions 'failed-rollback.json') @{
      passed=$false; verb='rollback'; at=(Get-Date).ToUniversalTime().ToString('o'); message=$_.Exception.Message}
    Die 'rollback_failed' $_.Exception.Message
  }
  Write-Receipt (Join-Path $promotions ('rollback-' + (Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ') + '.json')) @{
    passed=$true; verb='rollback'; at=(Get-Date).ToUniversalTime().ToString('o');
    restored_from=$Previous; health=$health}
  Emit @{passed=$true; verb='rollback'; restored_from=$Previous}
  exit 0
}

}
if ($MyInvocation.InvocationName -ne '.') { Invoke-Main }
