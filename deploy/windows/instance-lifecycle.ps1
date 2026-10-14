# The one isolated-instance lifecycle script.  Every path, task name and port comes from the
# single binding file rendered on the platform host from platform.json + the release manifest;
# nothing here defaults to a personal path, a production task name or a Git credential.
#   stage   verifies the transferred archive, installs the generation runtime, renders the
#           generation's own minimal endpoint.json and records its expected config digest
#   promote stops the old endpoint (no listener AND no writer residue under the generation or
#           state root), switches the task to the generation runtime + config, starts it and
#           settles it for >=180s (task Running + listener PID constant + token-authenticated
#           /health ready); a start refused by the ownership lock is recorded, waited out for
#           >=60s and retried exactly once, and a second refusal stops with lock_refused
#   rollback restores exactly the previous generation directory the caller bounds
#   health  relays the running endpoint's own /health payload
# Each invocation prints one JSON object; receipts stay under the instance state root.
#
#   instance-lifecycle.ps1 -Verb stage|promote|rollback|health -Binding FILE [-StageDir DIR]
#       [-Previous DIR]
param(
  [Parameter(Mandatory=$true)][ValidateSet('stage','promote','rollback','health','status')][string]$Verb,
  [Parameter(Mandatory=$true)][string]$Binding,
  [string]$StageDir = '',
  [string]$Previous = ''
)
$ErrorActionPreference='Stop'
$ProgressPreference='SilentlyContinue'

function Emit([hashtable]$payload) {
  $payload | ConvertTo-Json -Depth 8 -Compress
}
function Die([string]$code, [string]$message) {
  Emit @{passed=$false; code=$code; message=$message; verb=$Script:Verb}
  exit 1
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
function Test-OwnershipLockRefusal([string]$generation, [object]$binding) {
  # The scheduler does not capture the launcher's stderr, so the documented refusal is proven by
  # the condition its message describes: the launcher exited without answering /health while the
  # endpoint ownership lock under the instance state root is held elsewhere.  The probe acquires
  # the same lock through the staged runtime's own owner function (released on probe exit).
  $taskNow = Get-ScheduledTask -TaskName $binding.task_name -ErrorAction SilentlyContinue
  if ($taskNow -and [string]$taskNow.State -eq 'Running') { return $false }
  $config = Read-Json (Config-Path $generation)
  $lock = Join-Path ([string]$config.state_root) '.endpoint.lock'
  if (-not (Test-Path -LiteralPath $lock)) { return $false }
  & (Runtime-Python $generation) -I -c "import sys;from description_pipeline.io import acquire_process_lock;acquire_process_lock(sys.argv[1])" $lock 2>$null | Out-Null
  return ($LASTEXITCODE -ne 0)
}
function Get-WriterResidue([object]$binding) {
  # The endpoint's own writers keep their image under the generation or the instance state root;
  # the sweep loop ends only with no listener AND no such process left (deployment.md settle).
  $prefixState = ([string]$binding.state_root).TrimEnd('\').ToLower()
  $prefixGeneration = ([string]$binding.generation).TrimEnd('\').ToLower()
  Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
    $image = if ($_.ExecutablePath) { ([string]$_.ExecutablePath).ToLower() } else { '' }
    ($image.StartsWith($prefixState + '\')) -or ($image.StartsWith($prefixGeneration + '\'))
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

$binding = Read-Json $Binding
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
  $archive = Join-Path $binding.dir $binding.archive
  if ((Sha256 $archive) -ne $binding.artifact_sha256) { Die 'archive_mismatch' 'the transferred archive digest differs from the release manifest' }
  New-Item -ItemType Directory -Path $StageDir | Out-Null
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
  # The generation carries its own minimal endpoint configuration; the token path stays the one
  # the running instance already uses.  The expected config digest is computed by the staged
  # runtime's own canonical owner - the endpoint's own /health stays the only install identity.
  # The generation configuration is a byte copy of the running instance's own configuration
  # (token path and roots preserved; no re-serialization, no BOM).
  Copy-Item -LiteralPath (Action-Config $task) -Destination (Config-Path $StageDir)
  $expected = & $runtime -I -c "import json,sys;from description_pipeline import io;print(io.digest(json.load(open(sys.argv[1],encoding='utf-8'))))" (Config-Path $StageDir)
  if ($LASTEXITCODE -ne 0) { Die 'config_digest_failed' 'the staged runtime could not digest the generation configuration' }
  Write-Receipt (Join-Path $StageDir 'candidate-identity.json') @{
    expected=$true; config_digest=("$expected".Trim()); version=$binding.version;
    source_commit=$binding.source_commit; source_sha256=$binding.source_sha256}
  Write-Receipt (Join-Path $StageDir 'stage-receipt.json') @{
    passed=$true; verb='stage'; at=(Get-Date).ToUniversalTime().ToString('o'); generation=$StageDir;
    version=$binding.version; source_commit=$binding.source_commit; source_sha256=$binding.source_sha256}
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
  $previousConfig = Action-Config $task
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
  $lockRefusal = $null
  $retriedAfter = $null
  try {
    Start-ScheduledTask -TaskName $binding.task_name
    try {
      $settled = Wait-Settled (Config-Path $StageDir) $binding
    } catch {
      if (-not (Test-OwnershipLockRefusal $StageDir $binding)) { throw }
      # Documented transient refusal (deployment.md settle): record it durably, wait at least
      # 60 seconds and retry exactly once.
      $lockRefusal = @{
        message='Another endpoint owns the state root or its ownership lock is unavailable';
        at=(Get-Date).ToUniversalTime().ToString('o')}
      Write-Receipt (Join-Path $promotions 'lock-refusal.json') @{
        verb='promote'; at=$lockRefusal.at; generation=$StageDir; message=$lockRefusal.message; retry_after_s=60}
      Start-Sleep -Seconds 60
      $retriedAfter = 60
      Start-ScheduledTask -TaskName $binding.task_name
      try {
        $settled = Wait-Settled (Config-Path $StageDir) $binding
      } catch {
        if (-not (Test-OwnershipLockRefusal $StageDir $binding)) { throw }
        # A second refusal is stop-and-diagnose, never a third attempt.
        Register-ScheduledTask -TaskName $binding.task_name -Xml (Get-Content -LiteralPath (Join-Path $previous 'task.xml') -Raw) -Force | Out-Null
        Start-ScheduledTask -TaskName $binding.task_name
        Write-Receipt (Join-Path $previous 'failed-promotion.json') @{
          passed=$false; verb='promote'; at=(Get-Date).ToUniversalTime().ToString('o'); generation=$StageDir;
          lock_refusal=$lockRefusal; retried=@{after_s=$retriedAfter};
          second_refusal_at=(Get-Date).ToUniversalTime().ToString('o'); message=$_.Exception.Message}
        Die 'lock_refused' ('the ownership lock was refused twice; stopping for diagnosis: ' + $_.Exception.Message)
      }
    }
    $health = $settled.health
    $candidate = Read-Json (Join-Path $StageDir 'candidate-identity.json')
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
    Register-ScheduledTask -TaskName $binding.task_name -Xml (Get-Content -LiteralPath (Join-Path $previous 'task.xml') -Raw) -Force | Out-Null
    Start-ScheduledTask -TaskName $binding.task_name
    $failed = @{
      passed=$false; verb='promote'; at=(Get-Date).ToUniversalTime().ToString('o'); generation=$StageDir;
      message=$_.Exception.Message}
    if ($lockRefusal) { $failed['lock_refusal']=$lockRefusal; $failed['retried']=@{after_s=$retriedAfter} }
    Write-Receipt (Join-Path $previous 'failed-promotion.json') $failed
    Die 'promote_failed' ('the promotion failed and the previous task was restored: ' + $_.Exception.Message)
  }
}

if ($Verb -eq 'rollback') {
  # Exactly the generation directory the caller bounds - never "the latest promotion".
  if (-not $Previous) { Die 'previous_required' 'rollback needs -Previous (the bound previous generation directory)' }
  if (-not (Test-Path -LiteralPath (Join-Path $Previous 'task.xml'))) { Die 'previous_missing' "the bound previous generation carries no task snapshot: $Previous" }
  if (-not $task) { Die 'task_missing' 'the instance task is not registered' }
  Stop-Endpoint $binding
  Register-ScheduledTask -TaskName $binding.task_name -Xml (Get-Content -LiteralPath (Join-Path $Previous 'task.xml') -Raw) -Force | Out-Null
  # The restored task names the previous generation's own configuration; that file is the one
  # the recorded endpoint-before.json replaces - the candidate generation stays untouched.
  $restored = Get-ScheduledTask -TaskName $binding.task_name
  $previousConfig = Action-Config $restored
  Copy-Item -LiteralPath (Join-Path $Previous 'endpoint-before.json') -Destination $previousConfig -Force
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
