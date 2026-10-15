# Runs the candidate script's REAL Invoke-Main for one promote scenario (offline).
#
#   invoke-main-harness.ps1 -ScriptPath <lifecycle.ps1> -Scenario <name> -Root <temp dir>
#
# Only the OS/network boundaries and the settle retry timer are stubbed over synthetic temp
# files; the candidate's own Invoke-Main, Start-OrRefuse, refusal interpreters, restore path and
# health probe run for real (Wait-Settled runs real and fails fast on the stub task state).
# Scenarios: unknown_owner | startable_second_refusal | startable_missing_receipt.
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
$configuration = @{state_root=$Root; port=8765; token_file=$token} | ConvertTo-Json
[System.IO.File]::WriteAllText($oldConfig, $configuration)
[System.IO.File]::WriteAllText($newConfig, $configuration)
[System.IO.File]::WriteAllText((Join-Path $newGeneration 'stage-receipt.json'), '{}')
[System.IO.File]::WriteAllText((Join-Path $Root '.endpoint.lock'), '4242')
if ($Scenario -ne 'startable_missing_receipt') {
  [System.IO.File]::WriteAllText(
    (Join-Path $oldGeneration 'stage-receipt.json'),
    (@{passed=$true; verb='stage'; version='1.4.3'; source_commit=('a' * 40); source_sha256=('a' * 64)} |
      ConvertTo-Json -Compress))
}
$bindingPath = Join-Path $Root 'binding.json'
@{task_name='offline-endpoint'; state_root=$Root; generation=$newGeneration; dir=$Root;
  archive='synthetic.zip'; artifact_sha256='synthetic'; port=8765; package_root=$Root; output_root=$Root;
  version='1.5.0'; source_commit=('b' * 40); source_sha256=('b' * 64)} |
  ConvertTo-Json | Set-Content -LiteralPath $bindingPath

$script:OldAction = [pscustomobject]@{
  Execute = (Join-Path $oldGeneration 'runtime/Scripts/description.exe')
  Arguments = ('serve --config "' + $oldConfig + '"')}
$script:CurrentAction = $script:OldAction
$script:Calls = [System.Collections.Generic.List[string]]::new()
$script:Sleeps = [System.Collections.Generic.List[int]]::new()

function Get-ScheduledTask {
  [CmdletBinding()] param([string]$TaskName)
  [pscustomobject]@{ State='Ready'; Actions=@($script:CurrentAction) }
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
function Set-ScheduledTask { param([string]$TaskName, [object]$Action) $script:CurrentAction=$Action }
function Register-ScheduledTask {
  [CmdletBinding()] param([string]$TaskName, [string]$Xml, [switch]$Force)
  $script:CurrentAction=$script:OldAction
  $script:Calls.Add('register:old')
}
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
    release=@{version='1.4.3'; source_commit=('a' * 40); source_sha256=('a' * 64)};
    install=@{install_id='old-install'; config_digest='old-config'}}
}
function Die { param([string]$code, [string]$message) throw ('DIE:' + $code + ':' + $message) }

$Script:Verb = 'promote'
$Script:Binding = $bindingPath
$Script:StageDir = $newGeneration
$message = ''
try { Invoke-Main } catch { $message = $_.Exception.Message }
$failedFile = Get-ChildItem -LiteralPath (Join-Path $Root 'instance-promotions') -Recurse `
  -Filter 'failed-promotion.json' -ErrorAction SilentlyContinue | Select-Object -First 1
$receipt = if ($failedFile) { Get-Content -LiteralPath $failedFile.FullName -Raw | ConvertFrom-Json } else { $null }
@{scenario=$Scenario; message=$message; calls=@($script:Calls); sleeps=@($script:Sleeps); receipt=$receipt} |
  ConvertTo-Json -Depth 12 -Compress
exit 0
