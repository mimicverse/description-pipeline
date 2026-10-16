# Settle-semantics tests for deploy/windows/instance-lifecycle.ps1 (offline; pwsh on Linux).
#
#   pwsh -NoProfile -File tests/deploy/test_instance_lifecycle.ps1 [-ScriptPath PATH] [-StaticOnly]
#
# Stub inventory: the OS/network boundary (Get-Process / cmd / the ScheduledTask cmdlets /
# Invoke-RestMethod), the settle timer (Wait-SettleRetryDelay) and the health probe are local
# stubs; the script's own interpreters - Get-ProcessFacts, Test-LockProbeHeld,
# Get-OwnershipRefusalFacts, Start-OrRefuse, Restore-PreviousEndpoint - run for real.  The
# outer-promote cases additionally run the REAL Invoke-Main in a child pwsh via
# tests/deploy/invoke-main-harness.ps1.  Running with -ScriptPath pointing at a pre-change copy
# plus -StaticOnly is the negative-evidence mode: the old wording and the missing
# observer/ownership gates must make that run fail.
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
# Design-lead settle rules: no blind wait/retry, positive owner proof first, three-valued
# observers, and a recorded safe restore on the failure path.
Check 'wording.blind_retry_removed' (-not $source.Contains('waited out for')) 'the pre-change blind-retry wording is still present'
Check 'wording.no_blind_sleep' (-not $source.Contains('Start-Sleep -Seconds 60')) 'an unconditional 60s sleep is still present'
Check 'wording.owner_proof_gate' ($source.Contains('lock_refused_unproven') -and $source.Contains('owner_ended'))
Check 'wording.observer_tristate' ($source.Contains('observer_failed') -and $source.Contains('PROBE_IMPORT_FAILED'))
Check 'wording.restore_path' ($source.Contains('Restore-PreviousEndpoint') -and $source.Contains('restore=$restore'))
Check 'wording.stage_candidate_transform' ($source.Contains('Get-EndpointConfigTransformProgram') -and
  $source.Contains('endpoint_config_unknown_keys') -and $source.Contains('config-transformation.json') -and
  $source.Contains('candidate_config_mismatch'))
Check 'wording.old_copy_removed' (-not $source.Contains('Copy-Item -LiteralPath (Action-Config $task)')) 'the byte-copy stage path is still present'
Check 'wording.no_lock_or_kill' (-not $source.Contains('Remove-Item -LiteralPath $lock')) 'never delete the ownership lock'
if ($StaticOnly) {
  Write-Host ("checks={0} failures={1}" -f $script:checks, $script:failures.Count)
  exit ([int]($script:failures.Count -gt 0))
}

# ---- load the script without running its main flow --------------------------
. $ScriptPath -Verb health -Binding @{ task_name = 'test-endpoint' }
$seams = 'Get-OwnershipRefusalFacts', 'Start-OrRefuse', 'Wait-SettleRetryDelay', 'Read-LockOwner',
  'Get-ProcessFacts', 'Test-LockProbeHeld', 'Restore-PreviousEndpoint', 'Write-LockRefusalReceipt',
  'Get-PreviousGenerationBinding', 'Get-EndpointConfigEarlyProblems', 'Get-EndpointConfigTransformProgram',
  'Get-ServedStateRoot'
Check 'seams.present' (@($seams | Where-Object { -not (Get-Command $_ -ErrorAction SilentlyContinue) }).Count -eq 0)

# In-process refusal cases need the script's top-level Die to throw instead of exiting the
# harness; every existing check exercises throwing interpreters, never Die itself.
function Die { param([string]$code, [string]$message) throw ($code + ': ' + $message) }

# ---- harness: stub only the OS boundary -------------------------------------
$script:TaskMissing = $false
$script:TaskState = 'Ready'
$script:ProcScenario = 'missing'
$script:AccountEnv = @{}
$script:ServedStateRoot = ''
$script:ProbeScenario = 'refused'
$script:SettleOutcomes = @()
$script:Starts = 0
$script:Sleeps = [System.Collections.Generic.List[int]]::new()
$script:CallLog = [System.Collections.Generic.List[string]]::new()
$script:OldHealthOk = $true
$script:HealthProbes = [System.Collections.Generic.List[string]]::new()
function Get-ScheduledTask {
  [CmdletBinding()] param([string]$TaskName)
  if ($script:TaskMissing) { return $null }
  return [pscustomobject]@{ State = $script:TaskState }
}
function Start-ScheduledTask { [CmdletBinding()] param([string]$TaskName) $script:Starts++; $script:CallLog.Add('start') }
function Stop-ScheduledTask { [CmdletBinding()] param([string]$TaskName) $script:CallLog.Add('stop') }
function Register-ScheduledTask {
  [CmdletBinding()] param([string]$TaskName, [string]$Xml, [switch]$Force)
  $script:CallLog.Add('register')
  return $null
}
function Get-Process {
  [CmdletBinding()] param([int]$Id)
  switch ($script:ProcScenario) {
    'missing' {
      Write-Error ('Cannot find a process with the process ID: {0}.' -f $Id) -Category ObjectNotFound
      return
    }
    'denied' {
      Write-Error 'process query denied by injected observer failure' -Category PermissionDenied
      return
    }
    'alive' {
      return [pscustomobject]@{
        Id = $Id; Path = 'C:\gen\runtime\Scripts\description.exe'; StartTime = [datetime]'2026-10-15T00:00:00Z' }
    }
    default { return $null }
  }
}
function cmd {
  switch ($script:ProbeScenario) {
    'refused' { $global:LASTEXITCODE = 3; return 'LOCK_REFUSED' }
    'free' { $global:LASTEXITCODE = 0; return '' }
    default { $global:LASTEXITCODE = 7; return '' }
  }
}
function Wait-Settled {
  param([string]$configPath, [object]$binding)
  if ($script:SettleOutcomes.Count -eq 0) { throw 'no settle outcome queued' }
  $next = $script:SettleOutcomes[0]
  $script:SettleOutcomes = @($script:SettleOutcomes | Select-Object -Skip 1)
  if ($next -eq 'throw') { throw 'the settle window observed a refusal' }
  return @{ health = @{ install = @{ config_digest = 'digest' } }; settled_seconds = 180; listener_pid = 4242 }
}
function Wait-SettleRetryDelay { param([int]$Seconds = 60) $script:Sleeps.Add($Seconds) }
function Health-Probe {
  param([string]$configPath, [object]$binding)
  $script:HealthProbes.Add([string]$binding.version)
  if (-not $script:OldHealthOk) { throw 'the previous endpoint did not answer /health' }
  return @{ ready = $true }
}
function Write-Receipt {
  param([string]$path, [hashtable]$payload)
  $parent = Split-Path -Parent $path
  if (-not (Test-Path -LiteralPath $parent)) { New-Item -ItemType Directory -Path $parent | Out-Null }
  [System.IO.File]::WriteAllText($path, ($payload | ConvertTo-Json -Depth 8 -Compress))
}
function Get-AccountEnvironment { param([string]$Name) return [string]$script:AccountEnv[$Name] }
function Set-AccountEnvironment { param([string]$Name, [string]$Value) $script:AccountEnv[$Name] = $Value }
function Get-ServedStateRoot { param([string]$configPath) return [string]$script:ServedStateRoot }

$root = Join-Path ([System.IO.Path]::GetTempPath()) ("il-settle-" + [guid]::NewGuid().ToString('N'))
$generation = Join-Path $root 'generation'
$promotions = Join-Path $root 'instance-promotions'
$previous = Join-Path $promotions 'previous-test'
$null = New-Item -ItemType Directory -Path $generation -Force
$null = New-Item -ItemType Directory -Path $previous -Force
[System.IO.File]::WriteAllText((Join-Path $previous 'task.xml'), '<Task />')
[System.IO.File]::WriteAllText(
  (Join-Path $previous 'stage-receipt.json'),
  '{"passed":true,"verb":"stage","version":"1.4.3","source_commit":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","source_sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}')
$configPath = Join-Path $generation 'endpoint.json'
[System.IO.File]::WriteAllText($configPath, ('{{"state_root":"{0}"}}' -f ($root -replace '\\', '/')))
$lockPath = Join-Path $root '.endpoint.lock'
$binding = @{ task_name = 'test-endpoint'; port = 8765; state_root = $root; generation = $generation }
function Reset-State {
  $script:TaskMissing = $false
  $script:TaskState = 'Ready'
  $script:ProcScenario = 'missing'
  $script:ProbeScenario = 'refused'
  $script:SettleOutcomes = @()
  $script:Starts = 0
  $script:Sleeps.Clear()
  $script:CallLog.Clear()
  $script:AccountEnv.Clear()
  $script:ServedStateRoot = $root
  $script:OldHealthOk = $true
  $script:HealthProbes.Clear()
  [System.IO.File]::WriteAllText(
    (Join-Path $previous 'stage-receipt.json'),
    '{"passed":true,"verb":"stage","version":"1.4.3","source_commit":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","source_sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}')
  if (Test-Path -LiteralPath $lockPath) { Remove-Item -LiteralPath $lockPath -Force }
  $receipt = Join-Path $promotions 'lock-refusal.json'
  if (Test-Path -LiteralPath $receipt) { Remove-Item -LiteralPath $receipt -Force }
}

# ---- ownership-fact units (real interpreters, stubbed OS boundary) ----------
Reset-State
$cleanConfig = Join-Path $generation 'clean-endpoint.json'
[System.IO.File]::WriteAllText($cleanConfig, '{"schema_version":"solidworks-to-urdf.endpoint/v1","port":8765}')
$legacyConfig = Join-Path $generation 'legacy-endpoint.json'
[System.IO.File]::WriteAllText($legacyConfig, '{"schema_version":"solidworks-to-urdf.endpoint/v1","targets":{}}')
$unknownConfig = Join-Path $generation 'unknown-endpoint.json'
[System.IO.File]::WriteAllText($unknownConfig, '{"schema_version":"solidworks-to-urdf.endpoint/v1","bogus_key":true}')
$duplicateConfig = Join-Path $generation 'duplicate-endpoint.json'
[System.IO.File]::WriteAllText($duplicateConfig, '{"schema_version":"solidworks-to-urdf.endpoint/v1","port":8765,"port":8766}')
$legacyProblems = Get-EndpointConfigEarlyProblems $legacyConfig
Check 'early.targets_known_removable' ((@($legacyProblems.unknown).Count -eq 0) -and (@($legacyProblems.duplicates).Count -eq 0))
Check 'early.clean_config' ((@((Get-EndpointConfigEarlyProblems $cleanConfig).unknown).Count -eq 0))
Check 'early.unknown_reported' ((@((Get-EndpointConfigEarlyProblems $unknownConfig).unknown) -contains 'bogus_key'))
Check 'early.duplicate_reported' ((@((Get-EndpointConfigEarlyProblems $duplicateConfig).duplicates) -contains 'port'))

# ---- account domain pins: restate-only, set on switch, survive restart ------
Reset-State
$canonical = Join-Path $root 'cad-ownership.json'
$pins = Assert-DomainPins $root $generation
Check 'pins.unbound_plan' (($pins.canonical -eq $canonical) -and ($pins.previous_ownership -eq '') -and
  ($pins.previous_root -eq ''))
Set-DomainPins $root $generation
Check 'pins.set_canonical' (($script:AccountEnv['DESCRIPTION_ENDPOINT_ROOT'] -eq $generation) -and
  ($script:AccountEnv['DESCRIPTION_CAD_OWNERSHIP_FILE'] -eq $canonical))
# An equal pin only restates the bound domain.
$pins = Assert-DomainPins $root $generation
Check 'pins.equal_restates' ($pins.canonical -eq $canonical)
# A divergent ownership pin is refused: a second domain is never created silently.
$script:AccountEnv['DESCRIPTION_CAD_OWNERSHIP_FILE'] = (Join-Path $root 'other/cad-ownership.json')
$message = ''
try { $null = Assert-DomainPins $root $generation } catch { $message = $_.Exception.Message }
Check 'pins.divergent_ownership_refused' ($message -match 'cad_domain_override_refused')
# A changed state root cannot slip in as a new domain: the served configuration refuses it first.
$script:AccountEnv['DESCRIPTION_CAD_OWNERSHIP_FILE'] = $canonical
$message = ''
try { $null = Assert-DomainPins (Join-Path $root 'other-state') $generation } catch { $message = $_.Exception.Message }
Check 'pins.state_root_change_refused' ($message -match 'state_root_mismatch')
# A divergent endpoint-root pin is refused: the served generation may only be restated.
$script:AccountEnv['DESCRIPTION_ENDPOINT_ROOT'] = (Join-Path $root 'somewhere-else')
$message = ''
try { $null = Assert-DomainPins $root $generation } catch { $message = $_.Exception.Message }
Check 'pins.divergent_root_refused' ($message -match 'cad_domain_override_refused')
# The binding survives a service restart: the pins are read again, unchanged.
$script:AccountEnv['DESCRIPTION_ENDPOINT_ROOT'] = $generation
Stop-ScheduledTask -TaskName $binding.task_name | Out-Null
Start-ScheduledTask -TaskName $binding.task_name | Out-Null
Check 'pins.survive_restart' (($script:AccountEnv['DESCRIPTION_ENDPOINT_ROOT'] -eq $generation) -and
  ($script:AccountEnv['DESCRIPTION_CAD_OWNERSHIP_FILE'] -eq $canonical))
# An independent default call (no override) resolves the pinned domain.
Check 'pins.independent_default_call' ((Get-AccountEnvironment 'DESCRIPTION_CAD_OWNERSHIP_FILE') -eq $canonical)
# The binding's state_root never self-grants: a served configuration declaring another root refuses.
$script:ServedStateRoot = (Join-Path $root 'other-state')
$message = ''
try { $null = Assert-DomainPins $root $generation } catch { $message = $_.Exception.Message }
Check 'pins.served_state_root_mismatch_refused' ($message -match 'state_root_mismatch')
$script:ServedStateRoot = $root

Reset-State
$facts = Get-OwnershipRefusalFacts $generation $binding
Check 'facts.no_lock_file' ((-not $facts.refused) -and ($facts.state -eq 'no_lock_file'))
[System.IO.File]::WriteAllText($lockPath, '999999')
$script:ProbeScenario = 'free'
$facts = Get-OwnershipRefusalFacts $generation $binding
Check 'facts.lock_free' ((-not $facts.refused) -and ($facts.state -eq 'lock_free'))
$script:ProbeScenario = 'refused'
$facts = Get-OwnershipRefusalFacts $generation $binding
Check 'facts.owner_ended' ($facts.refused -and ($facts.owner_state -eq 'ended') -and ($facts.owner_ended -eq $true) -and
  ($facts.owner_process_alive -eq $false))
$script:ProcScenario = 'alive'
$facts = Get-OwnershipRefusalFacts $generation $binding
Check 'facts.owner_alive' ($facts.refused -and ($facts.owner_state -eq 'alive') -and ($facts.owner_ended -eq $false) -and
  ($facts.owner_process_alive -eq $true))
$script:ProcScenario = 'denied'
$facts = Get-OwnershipRefusalFacts $generation $binding
Check 'facts.owner_query_unknown' ($facts.refused -and ($facts.owner_state -eq 'unknown') -and ($facts.owner_ended -eq $false) -and
  ($null -eq $facts.owner_process_alive) -and ($facts.owner_process_error -match 'denied'))
$script:ProcScenario = 'missing'
[System.IO.File]::WriteAllText($lockPath, 'not-a-pid')
$facts = Get-OwnershipRefusalFacts $generation $binding
Check 'facts.owner_unreadable' ($facts.refused -and ($facts.reason -eq 'owner_unreadable') -and ($facts.owner_state -eq 'unknown') -and
  ($facts.owner_ended -eq $false))
[System.IO.File]::WriteAllText($lockPath, '999999')
$script:ProbeScenario = 'observer'
$facts = Get-OwnershipRefusalFacts $generation $binding
Check 'facts.observer_failed' ((-not $facts.refused) -and ($facts.state -eq 'observer_failed'))
$script:ProbeScenario = 'refused'
$script:TaskState = 'Running'
$facts = Get-OwnershipRefusalFacts $generation $binding
Check 'facts.task_running' ((-not $facts.refused) -and ($facts.state -eq 'task_running'))

# ---- settle flows: retry only on proven-ended; unknown/observer stop ---------
Reset-State
[System.IO.File]::WriteAllText($lockPath, '999999')
$script:SettleOutcomes = @('throw', 'ok')
$result = Start-OrRefuse $configPath $binding $generation $promotions
Check 'flow.retry_once_after_60s' (($script:Starts -eq 2) -and ($result.retried_after_s -eq 60) -and
  ($script:Sleeps.Count -eq 1) -and ($script:Sleeps[0] -ge 60))
Check 'flow.refusal_owner_ended' ($result.lock_refusal.owner_ended -eq $true)
$receipt = Get-Content -LiteralPath (Join-Path $promotions 'lock-refusal.json') -Raw | ConvertFrom-Json
Check 'flow.receipt_records_proof' (($receipt.owner_ended -eq $true) -and ($receipt.retryable -eq $true) -and
  ($receipt.retry_after_s -ge 60) -and ($receipt.owner_state -eq 'ended'))

Reset-State
[System.IO.File]::WriteAllText($lockPath, '999999')
$script:ProcScenario = 'alive'
$script:SettleOutcomes = @('throw')
$message = ''
try { $null = Start-OrRefuse $configPath $binding $generation $promotions } catch { $message = $_.Exception.Message }
Check 'flow.unproven_alive_stops' ($message.StartsWith('lock_refused_unproven:') -and ($message -match 'owner_state=alive') -and
  ($script:Starts -eq 1) -and ($script:Sleeps.Count -eq 0))

Reset-State
[System.IO.File]::WriteAllText($lockPath, '999999')
$script:ProcScenario = 'denied'
$script:SettleOutcomes = @('throw')
$message = ''
try { $null = Start-OrRefuse $configPath $binding $generation $promotions } catch { $message = $_.Exception.Message }
$receipt = Get-Content -LiteralPath (Join-Path $promotions 'lock-refusal.json') -Raw | ConvertFrom-Json
Check 'flow.unproven_unknown_stops' ($message.StartsWith('lock_refused_unproven:') -and ($message -match 'owner_state=unknown') -and
  ($script:Starts -eq 1) -and ($script:Sleeps.Count -eq 0) -and ($receipt.owner_state -eq 'unknown') -and
  ($receipt.retryable -eq $false))

Reset-State
[System.IO.File]::WriteAllText($lockPath, '999999')
$script:ProbeScenario = 'observer'
$script:SettleOutcomes = @('throw')
$message = ''
try { $null = Start-OrRefuse $configPath $binding $generation $promotions } catch { $message = $_.Exception.Message }
$receipt = Get-Content -LiteralPath (Join-Path $promotions 'lock-refusal.json') -Raw | ConvertFrom-Json
Check 'flow.observer_failed_stops' ($message.StartsWith('lock_refused_unproven:') -and ($message -match 'observer_failed') -and
  ($script:Starts -eq 1) -and ($script:Sleeps.Count -eq 0) -and ($receipt.reason -eq 'observer_failed'))

Reset-State
[System.IO.File]::WriteAllText($lockPath, '999999')
$script:SettleOutcomes = @('throw', 'throw')
$message = ''
try { $null = Start-OrRefuse $configPath $binding $generation $promotions } catch { $message = $_.Exception.Message }
Check 'flow.second_refusal_stops' ($message.StartsWith('lock_refused:') -and ($script:Starts -eq 2) -and
  ($script:Sleeps.Count -eq 1))

Reset-State
$script:ProbeScenario = 'free'
$script:SettleOutcomes = @('throw')
$message = ''
try { $null = Start-OrRefuse $configPath $binding $generation $promotions } catch { $message = $_.Exception.Message }
Check 'flow.non_refusal_rethrows' (($message -match 'settle window') -and ($script:Starts -eq 1) -and
  ($script:Sleeps.Count -eq 0))

# ---- failure-path restore: gated start + pinned previous identity -----------
$sourceLauncher = Join-Path $previous 'runtime/Scripts/description.exe'
Reset-State
$restore = Restore-PreviousEndpoint $binding $previous $configPath $generation $sourceLauncher
Check 'restore.success_pinned_health' ($restore.stopped -and $restore.restored -and $restore.startable -and $restore.started -and
  ($restore.healthy -eq $true) -and ($restore.previous_generation -eq $previous) -and
  ($script:HealthProbes.Count -eq 1) -and ($script:HealthProbes[0] -eq '1.4.3') -and
  (($script:CallLog -join ',') -eq 'stop,register,start'))

Reset-State
[System.IO.File]::WriteAllText($lockPath, '999999')
$script:ProcScenario = 'denied'
$restore = Restore-PreviousEndpoint $binding $previous $configPath $generation $sourceLauncher
Check 'restore.gate_unknown_no_start' ($restore.stopped -and $restore.restored -and (-not $restore.startable) -and (-not $restore.started) -and
  ($restore.start_gate -eq 'refused') -and ($restore.error -match 'start gate is refused') -and
  (($script:CallLog -join ',') -eq 'stop,register'))

Reset-State
$script:OldHealthOk = $false
$restore = Restore-PreviousEndpoint $binding $previous $configPath $generation $sourceLauncher
Check 'restore.health_failure_pinned' ($restore.stopped -and $restore.restored -and $restore.started -and (-not $restore.healthy) -and
  ($restore.error -match 'did not answer') -and ($script:HealthProbes.Count -eq 1) -and ($script:HealthProbes[0] -eq '1.4.3'))

Reset-State
Remove-Item -LiteralPath (Join-Path $previous 'stage-receipt.json') -Force
$restore = Restore-PreviousEndpoint $binding $previous $configPath $generation $sourceLauncher
Check 'restore.missing_receipt_unjudged' ($restore.started -and ($null -eq $restore.healthy) -and
  ($restore.error -match 'receipt is unavailable') -and ($script:HealthProbes.Count -eq 0))

# ---- real outer promote flow (child pwsh; real Invoke-Main) -----------------
$harness = Join-Path $PSScriptRoot 'invoke-main-harness.ps1'
$pwshExe = [System.Diagnostics.Process]::GetCurrentProcess().MainModule.FileName
function Invoke-Outer([string]$scenario) {
  $outerRoot = Join-Path ([System.IO.Path]::GetTempPath()) ('il-outer-' + [guid]::NewGuid().ToString('N'))
  $null = New-Item -ItemType Directory -Path $outerRoot -Force
  $json = & $pwshExe -NoProfile -File $harness -ScriptPath $ScriptPath -Scenario $scenario -Root $outerRoot
  $result = ($json | Select-Object -Last 1) | ConvertFrom-Json
  Remove-Item -LiteralPath $outerRoot -Recurse -Force -ErrorAction SilentlyContinue
  return $result
}
$outer = Invoke-Outer 'unknown_owner'
Check 'outer.unknown_owner_no_restart' ((@($outer.calls | Where-Object { $_ -like 'start:*old*' }).Count -eq 0) -and
  ($outer.receipt.restore.startable -eq $false) -and ($outer.receipt.restore.started -eq $false) -and
  ($outer.receipt.restore.start_gate -eq 'refused') -and ($outer.message -match 'lock_refused_unproven'))
Check 'outer.unknown_owner_candidate_started_once' ((@($outer.calls | Where-Object { $_ -like 'start:*new*' }).Count -eq 1))

$outer = Invoke-Outer 'startable_second_refusal'
Check 'outer.startable_starts_old_with_pinned_health' ((@($outer.calls | Where-Object { $_ -like 'start:*old*' }).Count -eq 1) -and
  ($outer.receipt.restore.startable -eq $true) -and ($outer.receipt.restore.started -eq $true) -and
  ($outer.receipt.restore.healthy -eq $true) -and (@($outer.sleeps) -contains 60))

$outer = Invoke-Outer 'startable_missing_receipt'
Check 'outer.missing_receipt_health_unjudged' ((@($outer.calls | Where-Object { $_ -like 'start:*old*' }).Count -eq 1) -and
  ($outer.receipt.restore.started -eq $true) -and ($null -eq $outer.receipt.restore.healthy) -and
  ($outer.receipt.restore.error -match 'receipt is unavailable'))

$outer = Invoke-Outer 'stage_unknown_key_refused'
Check 'outer.stage_unknown_key_refused' (($outer.message -match 'endpoint_config_unknown_keys') -and
  ($outer.message -match 'bogus_key') -and (@($outer.calls).Count -eq 0))

$outer = Invoke-Outer 'stage_duplicate_key_refused'
Check 'outer.stage_duplicate_key_refused' (($outer.message -match 'endpoint_config_duplicate_keys') -and
  (@($outer.calls).Count -eq 0))

$outer = Invoke-Outer 'promote_candidate_mismatch'
Check 'outer.promote_candidate_mismatch' (($outer.message -match 'candidate_config_mismatch') -and
  (@($outer.calls).Count -eq 0))

$outer = Invoke-Outer 'promote_source_mismatch'
Check 'outer.promote_source_mismatch' (($outer.message -match 'candidate_source_mismatch') -and
  (@($outer.calls).Count -eq 0))

Write-Host ("checks={0} failures={1}" -f $script:checks, $script:failures.Count)
exit ([int]($script:failures.Count -gt 0))
