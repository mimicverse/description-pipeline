# The one isolated-instance lifecycle script.  Every path, task name and port comes from the
# single binding file rendered on the platform host from platform.json + the release manifest;
# nothing here defaults to a personal path, a production task name or a Git credential.  The
# promoted instance runs the documented single native launch:
#     <generation>\runtime\Scripts\description.exe serve --config <generation>\endpoint.json
# Each invocation prints one JSON object; receipts stay under the instance state root.
#
#   instance-lifecycle.ps1 -Verb stage|promote|rollback|health -Binding FILE [-StageDir DIR]
param(
  [Parameter(Mandatory=$true)][ValidateSet('stage','promote','rollback','health')][string]$Verb,
  [Parameter(Mandatory=$true)][string]$Binding,
  [string]$StageDir = ''
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
function Write-Receipt([string]$path, [hashtable]$payload) {
  $parent = Split-Path -Parent $path
  if (-not (Test-Path -LiteralPath $parent)) { New-Item -ItemType Directory -Path $parent | Out-Null }
  Emit $payload | Set-Content -LiteralPath $path -Encoding UTF8
}
function Runtime-Python([string]$instanceDir) { Join-Path $instanceDir 'runtime\Scripts\python.exe' }
function Runtime-Launcher([string]$instanceDir) { Join-Path $instanceDir 'runtime\Scripts\description.exe' }
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
    throw 'the instance endpoint /health release identity differs from the release manifest'
  }
  if (-not $health.install.install_id -or -not $health.install.config_digest) {
    throw 'the instance endpoint /health does not carry its install identity'
  }
  return $health
}

$binding = Read-Json $Binding
foreach ($field in ('task_name','state_root','generation','dir','archive','artifact_sha256','version','source_commit','source_sha256')) {
  if (-not $binding.$field) { Die 'binding_incomplete' "the binding misses $field" }
}
$task = Get-ScheduledTask -TaskName $binding.task_name -ErrorAction SilentlyContinue
if ($Verb -eq 'health') {
  if (-not $task) { Die 'task_missing' 'the instance task is not registered' }
  try { $probe = Health-Probe (Action-Config $task) $binding } catch { Die 'endpoint_unhealthy' $_.Exception.Message }
  Emit @{passed=$true; verb='health'; health=$probe}
  exit 0
}

if ($Verb -eq 'stage') {
  if (-not $StageDir) { Die 'stage_dir_required' 'stage needs -StageDir (the new generation directory)' }
  if (Test-Path -LiteralPath $StageDir) { Die 'stage_exists' "preserve the existing generation: $StageDir" }
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
  Write-Receipt (Join-Path $StageDir 'stage-receipt.json') @{
    passed=$true; verb='stage'; at=(Get-Date).ToUniversalTime().ToString('o'); generation=$StageDir;
    version=$binding.version; source_commit=$binding.source_commit; source_sha256=$binding.source_sha256}
  Emit @{passed=$true; verb='stage'; generation=$StageDir; receipt=(Join-Path $StageDir 'stage-receipt.json')}
  exit 0
}

$promotions = Join-Path $binding.state_root 'instance-promotions'

if ($Verb -eq 'promote') {
  if (-not $StageDir) { Die 'stage_dir_required' 'promote needs -StageDir (the staged generation directory)' }
  $receiptPath = Join-Path $StageDir 'stage-receipt.json'
  if (-not (Test-Path -LiteralPath $receiptPath)) { Die 'stage_receipt_missing' 'the generation has no completed stage receipt' }
  if (-not $task) { Die 'task_missing' 'the instance task is not registered; install the instance before promoting' }
  if (@($task.Actions).Count -ne 1) { Die 'task_ambiguous' 'the instance task must carry exactly one action' }
  $configPath = Action-Config $task
  # The old endpoint stops before anything switches; a listener that survives the stop refuses
  # the promotion instead of being overwritten.
  Stop-ScheduledTask -TaskName $binding.task_name
  $stopDeadline = (Get-Date).AddSeconds(30)
  while ((Get-NetTCPConnection -LocalPort $binding.port -State Listen -ErrorAction SilentlyContinue) -and (Get-Date) -lt $stopDeadline) {
    Start-Sleep -Milliseconds 500
  }
  if (Get-NetTCPConnection -LocalPort $binding.port -State Listen -ErrorAction SilentlyContinue) {
    Die 'stop_failed' 'the old endpoint is still listening; refusing to switch'
  }
  New-Item -ItemType Directory -Path $promotions -ErrorAction SilentlyContinue | Out-Null
  $previous = Join-Path $promotions ('previous-' + (Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ'))
  New-Item -ItemType Directory -Path $previous | Out-Null
  Export-ScheduledTask -TaskName $binding.task_name | Set-Content -LiteralPath (Join-Path $previous 'task.xml') -Encoding UTF8
  Copy-Item -LiteralPath $configPath -Destination (Join-Path $previous 'endpoint-before.json')
  $action = New-ScheduledTaskAction -Execute (Runtime-Launcher $StageDir) `
    -Argument ('serve --config "' + $configPath + '"') -WorkingDirectory $binding.state_root
  Set-ScheduledTask -TaskName $binding.task_name -Action $action | Out-Null
  try {
    Start-ScheduledTask -TaskName $binding.task_name
    $health = Health-Probe $configPath $binding
    Write-Receipt (Join-Path $promotions 'last-promotion.json') @{
      passed=$true; verb='promote'; at=(Get-Date).ToUniversalTime().ToString('o'); generation=$StageDir;
      previous=$previous; health=$health}
    Emit @{passed=$true; verb='promote'; generation=$StageDir; previous=$previous}
    exit 0
  } catch {
    Register-ScheduledTask -TaskName $binding.task_name -Xml (Get-Content -LiteralPath (Join-Path $previous 'task.xml') -Raw) -Force | Out-Null
    Start-ScheduledTask -TaskName $binding.task_name
    Write-Receipt (Join-Path $previous 'failed-promotion.json') @{
      passed=$false; verb='promote'; at=(Get-Date).ToUniversalTime().ToString('o'); generation=$StageDir;
      message=$_.Exception.Message}
    Die 'promote_failed' ('the promotion failed and the previous task was restored: ' + $_.Exception.Message)
  }
}

if ($Verb -eq 'rollback') {
  $record = Join-Path $promotions 'last-promotion.json'
  if (-not (Test-Path -LiteralPath $record)) { Die 'no_promotion_receipt' 'no recorded promotion exists to roll back' }
  $last = Read-Json $record
  if (-not $task) { Die 'task_missing' 'the instance task is not registered' }
  $configPath = Action-Config $task
  Register-ScheduledTask -TaskName $binding.task_name -Xml (Get-Content -LiteralPath (Join-Path $last.previous 'task.xml') -Raw) -Force | Out-Null
  Copy-Item -LiteralPath (Join-Path $last.previous 'endpoint-before.json') -Destination $configPath -Force
  Start-ScheduledTask -TaskName $binding.task_name
  try { $health = Health-Probe $configPath $binding } catch {
    Write-Receipt (Join-Path $promotions 'failed-rollback.json') @{
      passed=$false; verb='rollback'; at=(Get-Date).ToUniversalTime().ToString('o'); message=$_.Exception.Message}
    Die 'rollback_failed' $_.Exception.Message
  }
  Write-Receipt (Join-Path $promotions 'last-rollback.json') @{
    passed=$true; verb='rollback'; at=(Get-Date).ToUniversalTime().ToString('o');
    restored_from=$last.generation; previous=$last.previous; health=$health}
  Emit @{passed=$true; verb='rollback'; restored_from=$last.generation}
  exit 0
}
