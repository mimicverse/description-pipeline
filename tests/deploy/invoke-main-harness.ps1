# Runs the candidate script's REAL Invoke-Main for one promote scenario (offline).
#
#   invoke-main-harness.ps1 -ScriptPath <lifecycle.ps1> -Scenario <name> -Root <temp dir>
#
# Only the OS/network boundaries and the settle retry timer are stubbed over synthetic temp
# files; the candidate's own Invoke-Main, Start-OrRefuse, refusal interpreters, restore path and
# health probe run for real (Wait-Settled runs real and fails fast on the stub task state).
# Scenarios: unknown_owner | startable_second_refusal | startable_missing_receipt |
# stage_unknown_key_refused | stage_legacy_targets_refused | stage_duplicate_key_refused |
# stage_acl_boundary |
# promote_candidate_mismatch | promote_source_mismatch.
# Prints one JSON object (scenario, message, calls, sleeps, receipt) and exits 0.
param(
  [Parameter(Mandatory=$true)][string]$ScriptPath,
  [Parameter(Mandatory=$true)][string]$Scenario,
  [Parameter(Mandatory=$true)][string]$Root
)
$ErrorActionPreference = 'Stop'
. $ScriptPath -Verb promote -Binding 'unused'

$newGeneration = Join-Path $Root 'new'
$oldGeneration = Join-Path $Root 'old'
$null = New-Item -ItemType Directory -Path $newGeneration -Force
$null = New-Item -ItemType Directory -Path $oldGeneration -Force
$oldConfig = Join-Path $oldGeneration 'endpoint.json'
$newConfig = Join-Path $newGeneration 'endpoint.json'
$token = Join-Path $Root 'local-test-token'
[System.IO.File]::WriteAllText($token, 'synthetic-offline-token')
$configuration = @{state_root=$Root; port=8765; token_file=$token}
if ($Scenario -eq 'stage_unknown_key_refused') {
  # A key the endpoint schema does not define stops the stage.
  $configuration['bogus_key'] = $true
}
if ($Scenario -eq 'stage_legacy_targets_refused') {
  # A retired publication-routing key is no longer a supported configuration shape.
  $configuration['targets'] = @{nd_fixture = @{repository_slug = 'a/b'; base = 'feature/arm'}}
}
if ($Scenario -eq 'stage_duplicate_key_refused') {
  $configurationText = '{"schema_version":"solidworks-to-urdf.endpoint/v1","port":8765,' +
    '"token_file":"' + ($token -replace '\\', '/') + '","port":8766}'
} else {
  $configurationText = $configuration | ConvertTo-Json -Depth 4
}
[System.IO.File]::WriteAllText($oldConfig, $configurationText)
[System.IO.File]::WriteAllText($newConfig, $configurationText)
[System.IO.File]::WriteAllText(
  (Join-Path $newGeneration 'stage-receipt.json'),
  (@{passed=$true; verb='stage'; generation=(Normalize-PathIdentity $newGeneration);
     launch_contract='python_module_v1'; version='1.5.0';
     source_commit=('b' * 40); source_sha256=('b' * 64)} | ConvertTo-Json -Compress))
[System.IO.File]::WriteAllText((Join-Path $Root '.endpoint.lock'), '4242')
if ($Scenario -ne 'startable_missing_receipt') {
  [System.IO.File]::WriteAllText(
    (Join-Path $oldGeneration 'stage-receipt.json'),
    (@{passed=$true; verb='stage'; generation=(Normalize-PathIdentity $oldGeneration); version='1.5.0';
       source_commit='55cdf8da178e37239d3ac199cd27fc50cf56540d';
       source_sha256='66e2223de93b23131e652ea403525de16a21cea0b8facecd5c782b7ad6e2e3ec'} |
      ConvertTo-Json -Compress))
}
$bindingPath = Join-Path $Root 'binding.json'
$archiveName = 'synthetic.zip'
$archiveSha = 'synthetic'
if ($Scenario -eq 'stage_acl_boundary') {
  $archiveMember = Join-Path $Root 'archive-member.txt'
  [System.IO.File]::WriteAllText($archiveMember, 'synthetic archive member')
  Compress-Archive -LiteralPath $archiveMember -DestinationPath (Join-Path $Root $archiveName)
  $archiveSha = (Get-FileHash -LiteralPath (Join-Path $Root $archiveName) -Algorithm SHA256).Hash.ToLower()
}
@{task_name='offline-endpoint'; state_root=$Root; generation=$newGeneration; dir=$Root;
  archive=$archiveName; artifact_sha256=$archiveSha; port=8765; package_root=$Root; output_root=$Root;
  version='1.5.0'; source_commit=('b' * 40); source_sha256=('b' * 64)} |
  ConvertTo-Json | Set-Content -LiteralPath $bindingPath
if ($Scenario -notlike 'stage_*') {
  $candidateSha = (Get-FileHash -LiteralPath $newConfig -Algorithm SHA256).Hash.ToLower()
  $sourceSha = (Get-FileHash -LiteralPath $oldConfig -Algorithm SHA256).Hash.ToLower()
  if ($Scenario -eq 'promote_candidate_mismatch') { $candidateSha = ('0' * 64) }
  if ($Scenario -eq 'promote_source_mismatch') { $sourceSha = ('1' * 64) }
  [System.IO.File]::WriteAllText(
    (Join-Path $newGeneration 'candidate-identity.json'),
    (@{expected=$true; config_digest='digest'; config_sha256=$candidateSha; source_config=$oldConfig;
       source_config_sha256=$sourceSha; version='1.5.0'; source_commit=('b' * 40);
       source_sha256=('b' * 64)} | ConvertTo-Json -Compress))
}

$script:OldAction = [pscustomobject]@{
  Execute = (Join-Path $oldGeneration 'runtime/Scripts/description.exe')
  Arguments = ('serve --config "' + $oldConfig + '"')
  WorkingDirectory = $Root}
$script:CurrentAction = $script:OldAction
$script:RegisteredActions = @($script:OldAction)
$script:Calls = [System.Collections.Generic.List[string]]::new()
$script:Sleeps = [System.Collections.Generic.List[int]]::new()
$script:AccountEnv = @{}

function Get-AccountEnvironment { param([string]$Name) return [string]$script:AccountEnv[$Name] }
function Set-AccountEnvironment { param([string]$Name, [string]$Value) $script:AccountEnv[$Name] = $Value }
function Get-ServedStateRoot { param([string]$configPath) return [string]$Root }
function Get-ScheduledTask {
  [CmdletBinding()] param([string]$TaskName)
  [pscustomobject]@{ State='Ready'; Actions=@($script:RegisteredActions) }
}
function Start-ScheduledTask {
  [CmdletBinding()] param([string]$TaskName)
  $script:Calls.Add('start:' + $script:CurrentAction.Execute)
}
function Stop-ScheduledTask { [CmdletBinding()] param([string]$TaskName) $script:Calls.Add('stop') }
function Get-NetTCPConnection { [CmdletBinding()] param([int]$LocalPort, [string]$State) return $null }
function Get-CimInstance { [CmdletBinding()] param([string]$ClassName) return $null }
function Export-ScheduledTask { param([string]$TaskName) '<Task/>' }
function New-ScheduledTaskAction {
  param([string]$Execute, [string]$Argument, [string]$WorkingDirectory)
  [pscustomobject]@{ Execute=$Execute; Arguments=$Argument }
}
function Set-ScheduledTask {
  param([string]$TaskName, [object]$Action)
  $script:CurrentAction=$Action
  $script:RegisteredActions=@($Action)
}
function Register-ScheduledTask {
  [CmdletBinding()] param([string]$TaskName, [string]$Xml, [switch]$Force)
  $script:CurrentAction=$script:OldAction
  if ($Scenario -eq 'restore_foreign_one_action') {
    $script:ForeignAction=[pscustomobject]@{
      Execute=(Join-Path $Root 'foreign.exe')
      Arguments='foreign-action'
      WorkingDirectory=$Root
    }
    $script:CurrentAction=$script:ForeignAction
    $script:RegisteredActions=@($script:ForeignAction)
  } elseif ($Scenario -eq 'restore_multi_action') {
    $script:RegisteredActions=@(
      $script:OldAction,
      [pscustomobject]@{
        Execute=(Join-Path $Root 'foreign.exe')
        Arguments='foreign-action'
        WorkingDirectory=$Root
      })
  } else {
    $script:RegisteredActions=@($script:OldAction)
  }
  $script:Calls.Add('register:old')
}
function Get-ManagedServiceSid { param($Task) return 'S-1-5-21-offline-harness' }
function Set-ManagedAcl {
  param(
    [string]$Path,
    [bool]$Protected,
    [string]$ServiceSid,
    [switch]$NonInheriting,
    [switch]$Recurse)
  $script:Calls.Add(
    'managed-acl:' + $Path +
    ':protected=' + [string]$Protected +
    ':service-sid=' + $ServiceSid +
    ':non-inheriting=' + [string][bool]$NonInheriting +
    ':recurse=' + [string][bool]$Recurse)
}
function Set-ManagedEndpointAcls {
  param($Config, [string]$EndpointRoot, [string]$ConfigPath, [string]$InstallIdPath, [string]$TaskFile)
  $script:Calls.Add('managed-endpoint-acl:' + $EndpointRoot)
}
function Assert-ManagedEndpointAcls {
  param($Config, [string]$EndpointRoot, [string]$ConfigPath, [string]$InstallIdPath, [string]$TaskFile)
}
function Assert-DomainPins { param([string]$StateRoot, [string]$ServedGeneration) return @() }
if ($Scenario -eq 'stage_acl_boundary') {
  function Expand-Archive {
    param([string]$LiteralPath, [string]$DestinationPath)
    throw 'DIE:injected_stage_acl_boundary:the archive expansion is intentionally stopped'
  }
}
function Get-TaskFilePath { param($Task) return (Join-Path $Root 'task.xml') }
function Get-Process {
  [CmdletBinding()] param([int]$Id)
  if ($Scenario -eq 'unknown_owner') {
    Write-Error 'injected permission denied' -Category PermissionDenied
    return
  }
  Write-Error ('Cannot find a process with the process ID: {0}.' -f $Id) -Category ObjectNotFound
}
function cmd { $global:LASTEXITCODE = 3; 'LOCK_REFUSED' }
function Wait-SettleRetryDelay { param([int]$Seconds = 60) $script:Sleeps.Add($Seconds) }
function Invoke-RestMethod {
  param([string]$Uri, [hashtable]$Headers, [int]$TimeoutSec)
  @{schema_version='solidworks-to-urdf.health/v1'; ready=$true; native_platform=$true;
    release=@{version='1.5.0'; source_commit='55cdf8da178e37239d3ac199cd27fc50cf56540d';
      source_sha256='66e2223de93b23131e652ea403525de16a21cea0b8facecd5c782b7ad6e2e3ec'};
    install=@{install_id='old-install'; config_digest='old-config'}}
}
function Die { param([string]$code, [string]$message) throw ('DIE:' + $code + ':' + $message) }

if ($Scenario -like 'stage_*') {
  $Script:Verb = 'stage'
  $Script:Binding = $bindingPath
  $Script:StageDir = Join-Path $Root 'staged'
} else {
  $Script:Verb = 'promote'
  $Script:Binding = $bindingPath
  $Script:StageDir = $newGeneration
}
$message = ''
$stack = ''
try { Invoke-Main } catch { $message = $_.Exception.Message; $stack = $_.ScriptStackTrace }
$failedFile = Get-ChildItem -LiteralPath (Join-Path $Root 'instance-promotions') -Recurse `
  -Filter 'failed-promotion.json' -ErrorAction SilentlyContinue | Select-Object -First 1
$receipt = if ($failedFile) { Get-Content -LiteralPath $failedFile.FullName -Raw | ConvertFrom-Json } else { $null }
@{scenario=$Scenario; message=$message; stack=$stack; calls=@($script:Calls); sleeps=@($script:Sleeps); receipt=$receipt} |
  ConvertTo-Json -Depth 12 -Compress
exit 0
