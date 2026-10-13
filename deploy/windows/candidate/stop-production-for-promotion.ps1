param(
  [string]$Root = 'C:\description-v1-review',
  [string]$Tag = '',
  [string]$Stamp = '20261012',
  [string]$Base = '',
  [string]$TaskName = 'description-v1-endpoint',
  [string]$ServeConfig = '',
  # The venv python.exe is a redirector on this host: the real listener runs under the
  # base interpreter below, observed identically across earlier release journals.
  [string]$BaseInterpreter = 'C:\Users\33985\AppData\Local\Programs\Python\Python312\python.exe'
)
if(-not $Tag){throw 'Set -Tag to the candidate generation tag'}
if(-not $Base){$Base="$Root\native-product$Tag-$Stamp"}
if(-not $ServeConfig){$ServeConfig="$Root\endpoint.json"}
# Stop and disable the ORIGINAL production endpoint before the retention/promotion.
#   - Requires the candidate controller journal to be finished with the endpoint restored,
#     every candidate and production native job terminal and no CAD.
#   - Captures the original task enabled state/action and the listener PID/start/command.
#   - The listener must run under the recorded base interpreter (task Execute is the venv
#     redirector); known launcher/config writers may run under either interpreter, never
#     an arbitrary python.
#   - Never broad kills: only the identified managed processes are terminated, each
#     re-verified by PID + creation time + command line + handle start time.
#   - Records production-stop-receipt.json; the task is deliberately left Disabled and
#     stopped, including on failure, so no live writer exists during promotion.
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$Journal = Join-Path $Base "run-journal-$Tag.json"
$CandidateConfig = Join-Path $Base 'endpoint-candidate.json'
$ReceiptPath = Join-Path $Base 'production-stop-receipt.json'
$LauncherPath = Join-Path $Root 'endpoint-launcher.py'
$ActiveStates = @('queued','selecting','running','awaiting_definition')

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

function Get-KnownWriters {
  # Writer candidates only: this machine's processes that reference the known original
  # endpoint artifacts.  Candidate-harness processes (other roots) never match.
  @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
      $cmd = [string]$_.CommandLine
      if (-not $cmd) { $false }
      elseif ($cmd -match 'endpoint-launcher\.py' -and $cmd -match [regex]::Escape($LauncherPath)) { $true }
      elseif ($cmd -match '-m description_pipeline serve' -and $cmd -match [regex]::Escape($ServeConfig)) { $true }
      else { $false }
    })
}

function Writer-Record($process) {
  [ordered]@{
    pid = [int]$process.ProcessId
    start = $process.CreationDate.ToString('o')
    command = $process.CommandLine
  }
}

if (Test-Path -LiteralPath $ReceiptPath) {
  throw ('Preserve the existing stop receipt; inspect before retrying: ' + $ReceiptPath)
}
$Receipt = [ordered]@{
  schema_version = 'solidworks-to-urdf.production-stop/v1'
  at = (Get-Date).ToUniversalTime().ToString('o')
  host = $env:COMPUTERNAME
  user = $env:USERNAME
  task = $TaskName
  serve_config = $ServeConfig
  base_interpreter = $BaseInterpreter
  state = 'capturing'
}
function Save-Receipt {
  $Receipt | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $ReceiptPath -Encoding UTF8
}

try {
  $journal = Get-Content -LiteralPath $Journal -Raw -Encoding UTF8 | ConvertFrom-Json
  if ($journal.phase -ne 'candidate_finished') { throw ('Candidate controller journal is not finished: ' + $journal.phase) }
  if ($journal.endpoint_restored -ne $true) { throw 'Candidate controller did not restore the production endpoint' }

  $original = Get-Content -LiteralPath $ServeConfig -Raw -Encoding UTF8 | ConvertFrom-Json
  $candidate = Get-Content -LiteralPath $CandidateConfig -Raw -Encoding UTF8 | ConvertFrom-Json
  foreach ($stateRoot in @($original.state_root, $candidate.state_root)) {
    $active = @(Get-ActiveJobs $stateRoot)
    if ($active.Count) { throw ('Native jobs are still active under ' + $stateRoot + ': ' + ($active -join ',')) }
  }
  if (Get-Process SLDWORKS -ErrorAction SilentlyContinue) { throw 'CAD is active; refusing to stop the endpoint' }

  $task = Get-ScheduledTask -TaskName $TaskName
  if (@($task.Actions).Count -ne 1) { throw 'Production task does not have exactly one action' }
  $action = $task.Actions[0]
  if ($action.Arguments -notmatch 'endpoint-launcher\.py') { throw 'Unexpected original endpoint launcher' }
  $expectedExe = $action.Execute
  if (-not (Test-Path -LiteralPath $expectedExe)) { throw ('Task base python is missing: ' + $expectedExe) }
  $Receipt.task_before = [ordered]@{
    state = $task.State.ToString()
    enabled = [bool]$task.Settings.Enabled
    execute = $action.Execute
    arguments = $action.Arguments
    working_directory = $action.WorkingDirectory
  }

  $listeners = @(Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue | ForEach-Object {
      $listener = Get-CimInstance Win32_Process -Filter ('ProcessId=' + $_.OwningProcess)
      if (-not $listener) { throw ('Listener process disappeared during capture: pid=' + $_.OwningProcess) }
      if ($listener.ExecutablePath -ne $BaseInterpreter -or
          $listener.CommandLine -notmatch ('-m description_pipeline serve --config ' + [regex]::Escape($ServeConfig) + '$')) {
        throw ('Listener is not the identified managed endpoint (expected base interpreter ' + $BaseInterpreter +
          '): pid=' + $_.OwningProcess)
      }
      $listener
    })
  $writers = @(Get-KnownWriters)
  $managed = @((@($listeners) + @($writers | Where-Object {
        $_.ExecutablePath -eq $expectedExe -or $_.ExecutablePath -eq $BaseInterpreter
      })) |
    Sort-Object ProcessId -Unique)
  $foreign = @($writers | Where-Object {
      $_.ExecutablePath -ne $expectedExe -and $_.ExecutablePath -ne $BaseInterpreter
    })
  $Receipt.listener_before = @($listeners | ForEach-Object { Writer-Record $_ })
  $Receipt.managed_before = @($managed | ForEach-Object { Writer-Record $_ })
  if ($foreign.Count) {
    $Receipt.foreign_writers = @($foreign | ForEach-Object { Writer-Record $_ })
    throw ('Endpoint writer processes run under an interpreter that is neither the task python nor the recorded ' +
      'base interpreter; refusing broad kill: ' +
      (($foreign | ForEach-Object { $_.ProcessId }) -join ','))
  }
  $Receipt.before_saved = $true
  Save-Receipt

  $Receipt.phase = 'disabling'
  Save-Receipt
  Disable-ScheduledTask -TaskName $TaskName | Out-Null
  if ($task.State -eq 'Running') { Stop-ScheduledTask -TaskName $TaskName | Out-Null }
  $deadline = (Get-Date).AddSeconds(15)
  while ((Get-ScheduledTask -TaskName $TaskName).State -in @('Running','Queued') -and (Get-Date) -lt $deadline) {
    Start-Sleep -Milliseconds 250
  }
  if ((Get-ScheduledTask -TaskName $TaskName).State -in @('Running','Queued')) { throw 'Production endpoint task did not stop' }

  $terminated = @()
  foreach ($before in $managed) {
    $current = Get-CimInstance Win32_Process -Filter ('ProcessId=' + $before.ProcessId) -ErrorAction SilentlyContinue
    if (-not $current) { continue }
    if ($current.CreationDate -ne $before.CreationDate -or $current.CommandLine -ne $before.CommandLine) {
      throw 'Managed endpoint process identity changed'
    }
    foreach ($stateRoot in @($original.state_root, $candidate.state_root)) {
      $active = @(Get-ActiveJobs $stateRoot)
      if ($active.Count) { throw ('Native work appeared under ' + $stateRoot + ' while stopping the endpoint') }
    }
    if (Get-Process SLDWORKS -ErrorAction SilentlyContinue) { throw 'CAD appeared while stopping the endpoint' }
    $owned = Get-Process -Id $current.ProcessId -ErrorAction Stop
    $null = $owned.Handle
    if ([Math]::Abs(($owned.StartTime - $before.CreationDate).TotalSeconds) -gt 0.01) {
      throw 'Managed endpoint handle identity changed'
    }
    $owned.Kill()
    if (-not $owned.WaitForExit(10000)) { throw 'Managed endpoint process did not exit' }
    $terminated += (Writer-Record $before)
  }
  $Receipt.terminated = $terminated

  $deadline = (Get-Date).AddSeconds(15)
  while ($true) {
    $stillListening = @(Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue)
    $stillWriting = @(Get-KnownWriters)
    if ($stillListening.Count -eq 0 -and $stillWriting.Count -eq 0) { break }
    if ((Get-Date) -ge $deadline) {
      $Receipt.still_listening = @($stillListening | ForEach-Object { [int]$_.OwningProcess })
      $Receipt.still_writing = @($stillWriting | ForEach-Object { Writer-Record $_ })
      throw 'Endpoint listener or writer remains; refusing broad kill, inspect manually'
    }
    Start-Sleep -Milliseconds 250
  }

  $after = Get-ScheduledTask -TaskName $TaskName
  $Receipt.task_after = [ordered]@{ state = $after.State.ToString(); enabled = [bool]$after.Settings.Enabled }
  $Receipt.post = [ordered]@{ listener_count = 0; writer_count = 0 }
  $Receipt.state = 'stopped'
  $Receipt.finished = (Get-Date).ToUniversalTime().ToString('o')
  Save-Receipt
  Write-Output ('Production endpoint stopped and disabled for promotion (task ' + $TaskName + ', terminated ' +
    $terminated.Count + ' managed process(es)); receipt: ' + $ReceiptPath)
  exit 0
} catch {
  $Receipt.state = 'failed'
  $Receipt.error = $_.Exception.Message
  $Receipt.finished = (Get-Date).ToUniversalTime().ToString('o')
  try { Save-Receipt } catch { }
  Write-Error ('Production stop failed (task remains disabled/stopped; inspect before any promotion): ' +
    $_.Exception.Message)
  exit 1
}
