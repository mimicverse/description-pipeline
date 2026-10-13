param(
  [string]$Base = '',
  [string]$Fixture = '',
  [string]$Root = 'C:\description-v1-review',
  [string]$Stamp = '20261012',
  [string]$CandidateConfig = '',
  [string]$BaseInterpreter = 'C:\Users\33985\AppData\Local\Programs\Python\Python312\python.exe',
  [string]$ProductionConfig = '',
  [int]$Port = 8765,
  [string]$Reason = 'operator requested safe candidate endpoint shutdown'
)
if(-not $Fixture){throw 'Set -Fixture to the candidate generation tag'}
if(-not $Base){$Base="$Root\native-product$Fixture-$Stamp"}
if(-not $ProductionConfig){$ProductionConfig="$Root\endpoint.json"}
# Safe candidate shutdown for the observed launcher topology:
#   run-candidate-endpoint.ps1 -> venv launcher python (journal python_pid)
#     -> direct child base-python listener (the process owning Port)
# The child is the process that must be stopped; the launcher then exits
# naturally (or is reaped by the controller, which is safe once the child is
# gone).  The stop marker is written BEFORE the child is stopped - that is the
# wrapper intent signal - and the script itself always stops the validated
# child, so the marker-triggered launcher kill can never orphan a live child.
# Read-only until the validated stop; no broad kills; no journal edits.
# Port settlement after the stop accepts exactly two end states: no listener,
# or the verified managed PRODUCTION endpoint (the wrapper may legitimately
# restore it once the candidate child is gone).  Any candidate or unrelated
# listener on the port is a failure; the final owner is recorded explicitly.
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
if (-not $CandidateConfig) { $CandidateConfig = Join-Path $Base 'endpoint-candidate.json' }
$Marker = Join-Path $Base 'stop-candidate'
$ReceiptPath = Join-Path $Base 'candidate-safe-stop.json'
$Journal = Join-Path $Base ('run-journal-' + $Fixture + '.json')
$RuntimeLauncher = Join-Path $Base 'runtime\Scripts\python.exe'
$ActiveStates = @('queued', 'selecting', 'running', 'awaiting_definition')
$CandidateStateRoot = Join-Path $Base 'state'
$ProductionStateRoot = "$Root\endpoint-state"

$Data = [ordered]@{
  schema_version = 'solidworks-to-urdf.candidate-safe-stop/v1'
  at = (Get-Date).ToUniversalTime().ToString('o')
  reason = $Reason
  base = $Base
  fixture = $Fixture
  candidate_config = $CandidateConfig
  marker = $Marker
  phase = 'preflight'
  marker_written = $false
  launcher_killed = $false
}
function Save-Receipt {
  $Data | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $ReceiptPath -Encoding UTF8
}
function Get-ActiveJobs([string]$StateRoot) {
  $active = @()
  $jobs = Join-Path $StateRoot 'jobs'
  if (Test-Path -LiteralPath $jobs) {
    Get-ChildItem -LiteralPath $jobs -Filter '*.json' -ErrorAction Stop | ForEach-Object {
      $job = Get-Content -LiteralPath $_.FullName -Raw -Encoding UTF8 | ConvertFrom-Json
      if ($job.status -in $ActiveStates) { $active += $job.run_id }
    }
  }
  return $active
}
function Writer-Record($process) {
  [ordered]@{
    pid = [int]$process.ProcessId
    start = ([datetime]$process.CreationDate).ToUniversalTime().ToString('o')
    executable = $process.ExecutablePath
    command = $process.CommandLine
    parent_pid = [int]$process.ParentProcessId
  }
}
function Get-ListenerRows {
  @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue | ForEach-Object {
      $row = Get-CimInstance Win32_Process -Filter ('ProcessId=' + $_.OwningProcess) -ErrorAction SilentlyContinue
      if (-not $row) { throw ('Listener process disappeared during capture: pid=' + $_.OwningProcess) }
      $row
    })
}

if (Test-Path -LiteralPath $ReceiptPath) {
  throw ('Preserve the existing safe-stop receipt; inspect before retrying: ' + $ReceiptPath)
}
Save-Receipt
try {
  # 1) Zero native jobs under BOTH owners, no CAD, candidate config present.
  foreach ($stateRoot in @($CandidateStateRoot, $ProductionStateRoot)) {
    $active = @(Get-ActiveJobs $stateRoot)
    if ($active.Count) { throw ('Native jobs are still active under ' + $stateRoot + ': ' + ($active -join ',')) }
  }
  if (Get-Process SLDWORKS -ErrorAction SilentlyContinue) { throw 'CAD is active; refusing to stop the candidate endpoint' }
  if (-not (Test-Path -LiteralPath $CandidateConfig)) { throw ('Candidate config not found: ' + $CandidateConfig) }

  # 2) Journal identity (required): the recorded launcher is the parent link proof.
  if (-not (Test-Path -LiteralPath $Journal)) { throw ('Candidate controller journal not found: ' + $Journal) }
  $journal = Get-Content -LiteralPath $Journal -Raw -Encoding UTF8 | ConvertFrom-Json
  $Data.journal = [ordered]@{
    phase = $journal.phase
    fixture = $journal.fixture
    python_pid = if ($journal.python_pid) { [int]$journal.python_pid } else { $null }
    python_start = $journal.python_start
  }
  if ($journal.fixture -and $journal.fixture -ne $Fixture) { throw ('Journal fixture does not match: ' + $journal.fixture) }
  if (-not $journal.python_pid) { throw 'Journal carries no launcher python_pid; refusing an unbound stop' }
  $LauncherPid = [int]$journal.python_pid

  # 3) Exactly one candidate listener on the port, on the exact candidate config.
  $listeners = @(Get-ListenerRows)
  $Data.phase = 'validating'
  $Data.listener_count_before = $listeners.Count
  if ($listeners.Count -eq 0) {
    $Data.state = 'already_stopped'
    $Data.phase = 'finished'
    $Data.finished = (Get-Date).ToUniversalTime().ToString('o')
    Save-Receipt
    Write-Output 'No candidate listener is running; no marker written (a stale marker would poison the next run).'
    exit 0
  }
  if ($listeners.Count -gt 1) { throw ('Several listeners own port ' + $Port + '; inspect manually') }
  $listener = $listeners[0]
  $configPattern = '-m description_pipeline serve --config "?' + [regex]::Escape($CandidateConfig) + '"?$'
  if ($listener.ExecutablePath -ne $BaseInterpreter -or $listener.CommandLine -notmatch $configPattern) {
    if ($listener.CommandLine -match '-m description_pipeline serve --config') {
      throw 'The port listener is not the candidate endpoint (different config); stop the production owner instead'
    }
    throw ('The port listener is not the identified candidate endpoint: pid=' + $listener.ProcessId)
  }
  if ([int]$listener.ParentProcessId -ne $LauncherPid) {
    throw ('Listener parent link does not match the journal launcher: parent=' + $listener.ParentProcessId +
      ' journal=' + $LauncherPid)
  }
  $Data.listener_before = Writer-Record $listener
  $launcherRow = Get-CimInstance Win32_Process -Filter ('ProcessId=' + $LauncherPid) -ErrorAction SilentlyContinue
  if ($launcherRow) {
    if ($launcherRow.ExecutablePath -ne $RuntimeLauncher) {
      throw ('Journal launcher runs an unexpected interpreter: ' + $launcherRow.ExecutablePath)
    }
    if ($launcherRow.CommandLine -notmatch $configPattern) {
      throw 'Journal launcher command does not serve the candidate config'
    }
    if ($journal.python_start) {
      $recorded = ([datetime]$journal.python_start).ToUniversalTime()
      $observed = ([datetime]$launcherRow.CreationDate).ToUniversalTime()
      if ([Math]::Abs(($observed - $recorded).TotalSeconds) -gt 1.5) {
        throw 'Journal launcher start time does not match the live process'
      }
    }
    $Data.launcher_before = Writer-Record $launcherRow
  } else {
    $Data.launcher_before = $null
    $Data.launcher_note = 'journal launcher already exited; parent link still validated by the recorded pid'
  }

  # 4) Wrapper intent FIRST: the marker records that this stop is requested, so the
  #    controller's exit-code check cannot misread the launcher exit as a crash.
  $markerBody = [ordered]@{
    requested_at = (Get-Date).ToUniversalTime().ToString('o')
    requested_by = $env:USERNAME
    reason = $Reason
    launcher_pid = $LauncherPid
    listener_pid = [int]$listener.ProcessId
    listener_start = ([datetime]$listener.CreationDate).ToUniversalTime().ToString('o')
  }
  $markerBody | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $Marker -Encoding UTF8
  $Data.marker_written = $true
  $Data.marker_written_at = (Get-Date).ToUniversalTime().ToString('o')
  Save-Receipt

  # 5) Stop the CHILD (the listener); never the launcher.
  $Data.phase = 'stopping_listener'
  Save-Receipt
  $current = Get-CimInstance Win32_Process -Filter ('ProcessId=' + [int]$listener.ProcessId) -ErrorAction SilentlyContinue
  if ($current) {
    if ([datetime]$current.CreationDate -ne [datetime]$listener.CreationDate -or $current.CommandLine -ne $listener.CommandLine) {
      throw 'Listener process identity changed before the stop'
    }
    $owned = Get-Process -Id $current.ProcessId -ErrorAction Stop
    $null = $owned.Handle
    if ([Math]::Abs(($owned.StartTime.ToUniversalTime() - ([datetime]$listener.CreationDate).ToUniversalTime()).TotalSeconds) -gt 0.01) {
      throw 'Listener process handle identity changed'
    }
    $owned.Kill()
    if (-not $owned.WaitForExit(10000)) { throw 'Candidate listener did not exit' }
  }
  $Data.listener_stop = [ordered]@{ pid = [int]$listener.ProcessId; exited = $true }

  # 6) Port settlement: the wrapper may restore the managed production
  #    listener once the candidate child is gone, so the port may end up owned
  #    by production - accepted ONLY for the exact managed endpoint.  Any
  #    candidate or unrelated listener is a failure, and the final owner is
  #    recorded instead of an assumed zero count.
  $productionPattern = '-m description_pipeline serve --config "?' + [regex]::Escape($ProductionConfig) + '"?$'
  $deadline = (Get-Date).AddSeconds(30)
  while ($true) {
    $remaining = @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue | ForEach-Object {
        Get-CimInstance Win32_Process -Filter ('ProcessId=' + $_.OwningProcess) -ErrorAction SilentlyContinue
      } | Where-Object { $_ })
    if ($remaining.Count -eq 0) {
      $Data.final_owner = 'none'
      $Data.listener_after = $null
      break
    }
    if ($remaining.Count -eq 1 -and $remaining[0].ExecutablePath -eq $BaseInterpreter -and
        $remaining[0].CommandLine -match $productionPattern) {
      $Data.final_owner = 'managed_production'
      $Data.listener_after = Writer-Record $remaining[0]
      break
    }
    if ((Get-Date) -ge $deadline) {
      $Data.listener_after_evidence = @($remaining | ForEach-Object { Writer-Record $_ })
      throw 'The port is neither free nor the exact managed production endpoint; inspect without broad termination'
    }
    Start-Sleep -Milliseconds 250
  }
  $Data.listener_count_after = if ($Data.final_owner -eq 'managed_production') { 1 } else { 0 }

  # 7) Let the launcher exit naturally (the marker lets the controller reap it).
  $Data.phase = 'releasing_launcher'
  $launcherDeadline = (Get-Date).AddSeconds(30)
  $launcherExited = $true
  while (Get-CimInstance Win32_Process -Filter ('ProcessId=' + $LauncherPid) -ErrorAction SilentlyContinue) {
    if ((Get-Date) -ge $launcherDeadline) { $launcherExited = $false; break }
    Start-Sleep -Milliseconds 500
  }
  $Data.launcher_exit = [ordered]@{ pid = $LauncherPid; exited_within_window = $launcherExited; killed_by_this_script = $false }
  if (-not $launcherExited) { $Data.launcher_note = 'launcher still running; the controller owns its reap (child is gone, no orphan risk)' }

  # 8) Final evidence: no listener, no CAD, zero native jobs.
  if (Get-Process SLDWORKS -ErrorAction SilentlyContinue) { throw 'CAD appeared during the candidate stop' }
  foreach ($stateRoot in @($CandidateStateRoot, $ProductionStateRoot)) {
    $active = @(Get-ActiveJobs $stateRoot)
    if ($active.Count) { throw ('Native work appeared under ' + $stateRoot + ' during the stop') }
  }
  $Data.state = 'stopped'
  $Data.phase = 'finished'
  $Data.finished = (Get-Date).ToUniversalTime().ToString('o')
  Save-Receipt
  Write-Output ('Candidate listener ' + [int]$listener.ProcessId + ' stopped safely; launcher ' + $LauncherPid +
    ' released; final port owner: ' + $Data.final_owner + '; marker: ' + $Marker + '; receipt: ' + $ReceiptPath)
  exit 0
} catch {
  $Data.state = 'failed'
  $Data.error = $_.Exception.Message
  $Data.finished = (Get-Date).ToUniversalTime().ToString('o')
  try { Save-Receipt } catch { }
  Write-Error ('Candidate safe stop failed; inspect the receipt and processes manually: ' + $_.Exception.Message)
  exit 1
}
