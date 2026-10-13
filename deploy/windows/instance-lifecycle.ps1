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
function Config-Path([string]$instanceDir) { Join-Path $instanceDir 'endpoint.json' }
function Runtime-Python([string]$instanceDir) { Join-Path $instanceDir 'runtime\Scripts\python.exe' }
function Runtime-Launcher([string]$instanceDir) { Join-Path $instanceDir 'runtime\Scripts\description.exe' }
function Health-Probe([object]$binding) {
  $config = Read-Json (Config-Path $binding.instance_dir)
  $token = (Get-Content -LiteralPath $config.token_file -Raw).Trim()
  $deadline = (Get-Date).AddSeconds(30)
  do {
    try {
      return Invoke-RestMethod -Uri ("http://127.0.0.1:" + $config.port + "/health") `
        -Headers @{Authorization=('Bearer ' + $token)} -TimeoutSec 3
    } catch { Start-Sleep -Milliseconds 500 }
  } while ((Get-Date) -lt $deadline)
  Die 'endpoint_unhealthy' 'the instance endpoint did not answer /health within 30s'
}

$binding = Read-Json $Binding
foreach ($field in ('task_name','instance_dir','package_root','output_root','state_root',
                    'token_file','port','archive','version','source_commit','source_sha256')) {
  if (-not $binding.$field) { Die 'binding_incomplete' "the binding misses $field" }
}
if ($Verb -eq 'health') {
  Emit @{passed=$true; verb='health'; health=(Health-Probe $binding)}
  exit 0
}

if ($Verb -eq 'stage') {
  if (-not $StageDir) { Die 'stage_dir_required' 'stage needs -StageDir (the new generation directory)' }
  if (Test-Path -LiteralPath $StageDir) { Die 'stage_exists' "preserve the existing generation: $StageDir" }
  if ((Sha256 $binding.archive) -ne $binding.source_sha256) { Die 'archive_mismatch' 'the offline archive digest differs from the release manifest' }
  New-Item -ItemType Directory -Path $StageDir | Out-Null
  Expand-Archive -LiteralPath $binding.archive -DestinationPath $StageDir
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
  $config = @{schema_version='solidworks-to-urdf.endpoint/v1'; package_root=$binding.package_root;
              output_root=$binding.output_root; state_root=$binding.state_root;
              token_file=$binding.token_file; host='127.0.0.1'; port=$binding.port}
  $config | ConvertTo-Json | Set-Content -LiteralPath (Config-Path $StageDir) -Encoding UTF8
  Write-Receipt (Join-Path $StageDir 'stage-receipt.json') @{
    passed=$true; verb='stage'; at=(Get-Date).ToUniversalTime().ToString('o'); generation=$StageDir;
    version=$binding.version; source_commit=$binding.source_commit; source_sha256=$binding.source_sha256}
  Emit @{passed=$true; verb='stage'; generation=$StageDir; receipt=(Join-Path $StageDir 'stage-receipt.json')}
  exit 0
}

$task = Get-ScheduledTask -TaskName $binding.task_name -ErrorAction SilentlyContinue
$promotions = Join-Path $binding.state_root 'instance-promotions'

if ($Verb -eq 'promote') {
  if (-not $StageDir) { Die 'stage_dir_required' 'promote needs -StageDir (the staged generation directory)' }
  $receiptPath = Join-Path $StageDir 'stage-receipt.json'
  if (-not (Test-Path -LiteralPath $receiptPath)) { Die 'stage_receipt_missing' 'the generation has no completed stage receipt' }
  if (-not $task) { Die 'task_missing' 'the instance task is not registered; install the instance before promoting' }
  if (@($task.Actions).Count -ne 1) { Die 'task_ambiguous' 'the instance task must carry exactly one action' }
  New-Item -ItemType Directory -Path $promotions -ErrorAction SilentlyContinue | Out-Null
  $previous = Join-Path $promotions ('previous-' + (Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ'))
  New-Item -ItemType Directory -Path $previous | Out-Null
  Export-ScheduledTask -TaskName $binding.task_name | Set-Content -LiteralPath (Join-Path $previous 'task.xml') -Encoding UTF8
  Copy-Item -LiteralPath (Config-Path $binding.instance_dir) -Destination (Join-Path $previous 'endpoint-before.json')
  Copy-Item -LiteralPath (Config-Path $StageDir) -Destination (Config-Path $binding.instance_dir) -Force
  $action = New-ScheduledTaskAction -Execute (Runtime-Launcher $StageDir) `
    -Argument ('serve --config "' + (Config-Path $binding.instance_dir) + '"') -WorkingDirectory $binding.instance_dir
  Set-ScheduledTask -TaskName $binding.task_name -Action $action | Out-Null
  try {
    Start-ScheduledTask -TaskName $binding.task_name
    $health = Health-Probe $binding
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
  Register-ScheduledTask -TaskName $binding.task_name -Xml (Get-Content -LiteralPath (Join-Path $last.previous 'task.xml') -Raw) -Force | Out-Null
  Copy-Item -LiteralPath (Join-Path $last.previous 'endpoint-before.json') -Destination (Config-Path $binding.instance_dir) -Force
  Start-ScheduledTask -TaskName $binding.task_name
  $health = Health-Probe $binding
  Write-Receipt (Join-Path $promotions 'last-rollback.json') @{
    passed=$true; verb='rollback'; at=(Get-Date).ToUniversalTime().ToString('o');
    restored_from=$last.generation; previous=$last.previous; health=$health}
  Emit @{passed=$true; verb='rollback'; restored_from=$last.generation}
  exit 0
}
