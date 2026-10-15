# Settle-semantics tests for deploy/windows/instance-lifecycle.ps1 (offline; pwsh on Linux).
#
#   pwsh -NoProfile -File tests/deploy/test_instance_lifecycle.ps1 [-ScriptPath PATH] [-StaticOnly]
#
# The script's Windows cmdlets are never invoked: every fact the tests need flows through the
# script's own seams (later definitions win at call time).  Running with -ScriptPath pointing at
# a pre-change copy plus -StaticOnly is the negative-evidence mode: the old wording and the
# missing ownership-proof gate must make this run fail.
[CmdletBinding()]
param(
  [string]$ScriptPath = (Join-Path $PSScriptRoot '..' '..' 'deploy' 'windows' 'instance-lifecycle.ps1'),
  [switch]$StaticOnly
)
$ErrorActionPreference = 'Stop'
$script:checks = 0
$script:failures = [System.Collections.Generic.List[string]]::new()
function Check([string]$name, [bool]$condition, [string]$detail = '') {
  $script:checks++
  if ($condition) {
    Write-Host ("PASS {0}" -f $name)
  } else {
    $script:failures.Add($name)
    Write-Host ("FAIL {0} {1}" -f $name, $detail)
  }
}

# ---- parse + wording gates --------------------------------------------------
$tokens = $null
$parseErrors = $null
$null = [System.Management.Automation.Language.Parser]::ParseFile($ScriptPath, [ref]$tokens, [ref]$parseErrors)
Check 'parse.clean' ($parseErrors.Count -eq 0) (($parseErrors | Out-String).Trim())
$source = Get-Content -LiteralPath $ScriptPath -Raw
# The design-lead wording (394ff8f-class settle rules): no blind wait/retry, ownership proof first.
Check 'wording.blind_retry_removed' (-not $source.Contains('waited out for')) 'the pre-change blind-retry wording is still present'
Check 'wording.owner_proof_gate' ($source.Contains('lock_refused_unproven') -and $source.Contains('owner_ended'))
Check 'wording.no_lock_or_kill' (-not $source.Contains('Remove-Item -LiteralPath $lock')) 'never delete the ownership lock'
if ($StaticOnly) {
  Write-Host ("checks={0} failures={1}" -f $script:checks, $script:failures.Count)
  exit ([int]($script:failures.Count -gt 0))
}

# ---- load the script without running its main flow --------------------------
. $ScriptPath -Verb health -Binding @{ task_name = 'test-endpoint' }
$seams = 'Get-OwnershipRefusalFacts', 'Start-OrRefuse', 'Wait-SettleRetryDelay', 'Read-LockOwner', 'Get-ProcessFacts'
Check 'seams.present' (@($seams | Where-Object { -not (Get-Command $_ -ErrorAction SilentlyContinue) }).Count -eq 0)

# ---- test harness (overrides the script's seams; later definitions win) -----
$script:TaskMissing = $false
$script:TaskState = 'Ready'
$script:LockHeld = $false
$script:OwnerFacts = @{ alive = $false; image = ''; started = '' }
$script:SettleOutcomes = @()
$script:Starts = 0
$script:Sleeps = [System.Collections.Generic.List[int]]::new()
function Get-ScheduledTask {
  [CmdletBinding()] param([string]$TaskName)
  if ($script:TaskMissing) { return $null }
  return [pscustomobject]@{ State = $script:TaskState }
}
function Start-ScheduledTask { [CmdletBinding()] param([string]$TaskName) $script:Starts++ }
function Get-ProcessFacts { param([int]$ownerPid) return $script:OwnerFacts }
function Test-LockProbeHeld { param([string]$generation, [string]$lockPath) return [bool]$script:LockHeld }
function Wait-Settled {
  param([string]$configPath, [object]$binding)
  if ($script:SettleOutcomes.Count -eq 0) { throw 'no settle outcome queued' }
  $next = $script:SettleOutcomes[0]
  $script:SettleOutcomes = @($script:SettleOutcomes | Select-Object -Skip 1)
  if ($next -eq 'throw') { throw 'the settle window observed a refusal' }
  return @{ health = @{ install = @{ config_digest = 'digest' } }; settled_seconds = 180; listener_pid = 4242 }
}
function Wait-SettleRetryDelay { param([int]$Seconds = 60) $script:Sleeps.Add($Seconds) }
function Write-Receipt {
  param([string]$path, [hashtable]$payload)
  $parent = Split-Path -Parent $path
  if (-not (Test-Path -LiteralPath $parent)) { New-Item -ItemType Directory -Path $parent | Out-Null }
  [System.IO.File]::WriteAllText($path, ($payload | ConvertTo-Json -Depth 8 -Compress))
}

$root = Join-Path ([System.IO.Path]::GetTempPath()) ("il-settle-" + [guid]::NewGuid().ToString('N'))
$generation = Join-Path $root 'generation'
$promotions = Join-Path $root 'instance-promotions'
$null = New-Item -ItemType Directory -Path $generation -Force
$configPath = Join-Path $generation 'endpoint.json'
[System.IO.File]::WriteAllText($configPath, ('{{"state_root":"{0}"}}' -f ($root -replace '\\', '/')))
$lockPath = Join-Path $root '.endpoint.lock'
$binding = @{ task_name = 'test-endpoint'; port = 8765; state_root = $root; generation = $generation }
function Reset-State {
  $script:TaskMissing = $false
  $script:TaskState = 'Ready'
  $script:LockHeld = $false
  $script:OwnerFacts = @{ alive = $false; image = ''; started = '' }
  $script:SettleOutcomes = @()
  $script:Starts = 0
  $script:Sleeps.Clear()
  if (Test-Path -LiteralPath $lockPath) { Remove-Item -LiteralPath $lockPath -Force }
}

# ---- ownership-fact units ---------------------------------------------------
Reset-State
$script:LockHeld = $true
Check 'facts.no_lock_file' ((Get-OwnershipRefusalFacts $generation $binding).refused -eq $false)
[System.IO.File]::WriteAllText($lockPath, '999999')
$script:LockHeld = $false
$facts = Get-OwnershipRefusalFacts $generation $binding
Check 'facts.lock_free' (($facts.refused -eq $false) -and ($facts.reason -eq 'lock_free'))
$script:LockHeld = $true
$facts = Get-OwnershipRefusalFacts $generation $binding
Check 'facts.owner_ended' ($facts.refused -and ($facts.owner_pid -eq 999999) -and ($facts.owner_ended -eq $true))
$script:OwnerFacts = @{ alive = $true; image = 'C:\gen\runtime\Scripts\description.exe'; started = '2026-10-15T00:00:00.0000000Z' }
$facts = Get-OwnershipRefusalFacts $generation $binding
Check 'facts.owner_alive' ($facts.refused -and ($facts.owner_ended -eq $false) -and ($facts.owner_process_alive -eq $true))
[System.IO.File]::WriteAllText($lockPath, 'not-a-pid')
$facts = Get-OwnershipRefusalFacts $generation $binding
Check 'facts.owner_unreadable' ($facts.refused -and ($facts.reason -eq 'owner_unreadable') -and ($facts.owner_ended -eq $false))
$script:TaskState = 'Running'
$facts = Get-OwnershipRefusalFacts $generation $binding
Check 'facts.task_running' (($facts.refused -eq $false) -and ($facts.reason -eq 'task_running'))

# ---- settle flow: retry only after the owner is proven ended ----------------
Reset-State
[System.IO.File]::WriteAllText($lockPath, '999999')
$script:LockHeld = $true
$script:SettleOutcomes = @('throw', 'ok')
$result = Start-OrRefuse $configPath $binding $generation $promotions
Check 'flow.retry_once_after_60s' (($script:Starts -eq 2) -and ($result.retried_after_s -eq 60) -and
  ($script:Sleeps.Count -eq 1) -and ($script:Sleeps[0] -ge 60))
Check 'flow.refusal_owner_ended' ($result.lock_refusal.owner_ended -eq $true)
$receipt = Get-Content -LiteralPath (Join-Path $promotions 'lock-refusal.json') -Raw | ConvertFrom-Json
Check 'flow.receipt_records_proof' (($receipt.owner_ended -eq $true) -and ($receipt.retry_after_s -ge 60))

Reset-State
[System.IO.File]::WriteAllText($lockPath, '999999')
$script:LockHeld = $true
$script:OwnerFacts = @{ alive = $true; image = 'C:\unknown\process.exe'; started = '' }
$script:SettleOutcomes = @('throw')
$message = ''
try { $null = Start-OrRefuse $configPath $binding $generation $promotions } catch { $message = $_.Exception.Message }
Check 'flow.unproven_no_retry' ($message.StartsWith('lock_refused_unproven:') -and ($script:Starts -eq 1) -and
  ($script:Sleeps.Count -eq 0))
$receipt = Get-Content -LiteralPath (Join-Path $promotions 'lock-refusal.json') -Raw | ConvertFrom-Json
Check 'flow.unproven_receipt' ($receipt.owner_ended -eq $false)

Reset-State
[System.IO.File]::WriteAllText($lockPath, '999999')
$script:LockHeld = $true
$script:SettleOutcomes = @('throw', 'throw')
$message = ''
try { $null = Start-OrRefuse $configPath $binding $generation $promotions } catch { $message = $_.Exception.Message }
Check 'flow.second_refusal_stops' ($message.StartsWith('lock_refused:') -and ($script:Starts -eq 2) -and
  ($script:Sleeps.Count -eq 1))

Reset-State
$script:SettleOutcomes = @('throw')
$message = ''
try { $null = Start-OrRefuse $configPath $binding $generation $promotions } catch { $message = $_.Exception.Message }
Check 'flow.non_refusal_rethrows' (($message -match 'settle window') -and ($script:Starts -eq 1) -and
  ($script:Sleeps.Count -eq 0))

Write-Host ("checks={0} failures={1}" -f $script:checks, $script:failures.Count)
exit ([int]($script:failures.Count -gt 0))
