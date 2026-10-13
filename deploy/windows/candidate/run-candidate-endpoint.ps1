param(
  [Parameter(Mandatory=$true)][string]$Fixture,
  [Parameter(Mandatory=$true)][string]$Stage,
  [string]$Main = '',
  [string]$IndexPath = '',
  [string]$UploadSha = '',
  [string]$Root = 'C:\description-v1-review',
  [string]$Stamp = '20261012'
)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$B = "$Root\native-product$Fixture-$Stamp"
$RT = $B + '\runtime\Scripts\python.exe'
$Prod = 'description-v1-endpoint'
$Journal = Join-Path $B ('run-journal-' + $Fixture + '.json')
$Data = @{started=(Get-Date).ToUniversalTime().ToString('o'); fixture=$Fixture; stage=$Stage; main=$Main; phase='preflight'; endpoint_was_running=$false; endpoint_restored=$false}
function Save { $Data | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $Journal -Encoding UTF8 }
function ActiveJobs {
  $active=@()
  foreach($jobRoot in @("$Root\endpoint-state\jobs", ($B+'\state\jobs'))) {
  Get-ChildItem -LiteralPath $jobRoot -Filter *.json -ErrorAction SilentlyContinue | ForEach-Object {
    $j=Get-Content -LiteralPath $_.FullName -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($j.status -in @('queued','selecting','running','awaiting_definition')) { $active += $j.run_id }
  }
  }
  return $active
}
if(Test-Path -LiteralPath $Journal) { throw 'Preserve existing candidate journal; use a fresh candidate directory' }
Save
try {
  if(Test-Path -LiteralPath ($B+'\stop-candidate')) { throw 'Preserve existing stop marker; inspect before retrying' }
  if (@(ActiveJobs).Count -gt 0) { throw 'Native endpoint has active work' }
  if (Get-Process SLDWORKS -ErrorAction SilentlyContinue) { throw 'CAD is already running' }
  $Task=Get-ScheduledTask -TaskName $Prod
  $Before=@(Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue | ForEach-Object {
    $p=Get-CimInstance Win32_Process -Filter ('ProcessId='+$_.OwningProcess)
    if($p.ExecutablePath -ne 'C:\Users\33985\AppData\Local\Programs\Python\Python312\python.exe' -or $p.CommandLine -notmatch ('-m description_pipeline serve --config ' + [regex]::Escape("$Root\endpoint.json") + '$')) { throw 'Listener is not the identified managed endpoint' }
    $p
  })
  $Data.endpoint_was_running=($Task.State -eq 'Running' -or $Before.Count -gt 0)
  $Data.endpoint_processes=@($Before | Select-Object ProcessId,CreationDate,CommandLine)
  $Data.phase='stopping_endpoint'; Save
  if($Data.endpoint_was_running) {
    Stop-ScheduledTask -TaskName $Prod
    $end=(Get-Date).AddSeconds(15)
    while((Get-ScheduledTask -TaskName $Prod).State -eq 'Running' -and (Get-Date) -lt $end) { Start-Sleep -Milliseconds 250 }
    if((Get-ScheduledTask -TaskName $Prod).State -eq 'Running') { throw 'Endpoint did not stop' }
    foreach($before in $Before) {
      $current=Get-CimInstance Win32_Process -Filter ('ProcessId='+$before.ProcessId) -ErrorAction SilentlyContinue
      if($current) {
        if($current.CreationDate -ne $before.CreationDate -or $current.CommandLine -ne $before.CommandLine) { throw 'Managed endpoint process identity changed' }
        if(@(ActiveJobs).Count -gt 0 -or (Get-Process SLDWORKS -ErrorAction SilentlyContinue)) { throw 'Native work appeared before stopping the endpoint child' }
        $owned=Get-Process -Id $current.ProcessId -ErrorAction Stop
        $null=$owned.Handle
        if([Math]::Abs(($owned.StartTime-$before.CreationDate).TotalSeconds) -gt 0.01) { throw 'Managed endpoint handle identity changed' }
        $owned.Kill()
        if(-not $owned.WaitForExit(10000)) { throw 'Managed endpoint process did not exit' }
      }
    }
    $deadline=(Get-Date).AddSeconds(15)
    do {
      $listeners=@(Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue)
      if($listeners.Count -eq 0) { break }
      Start-Sleep -Milliseconds 250
    } while((Get-Date) -lt $deadline)
    if ($listeners) { throw 'Endpoint listener remains; refusing concurrent native work' }
  }
  if (Get-Process SLDWORKS -ErrorAction SilentlyContinue) { throw 'CAD became active before the probe' }
  if (@(ActiveJobs).Count -gt 0) { throw 'Native work appeared before starting the probe' }
  if (-not (Test-Path -LiteralPath $Stage)) { throw ('Stage root not found: ' + $Stage) }
  $previous=Get-Content -LiteralPath "$Root\endpoint.json" -Raw -Encoding UTF8 | ConvertFrom-Json
  $config=@{schema_version='solidworks-to-urdf.endpoint/v1';package_root=$B+'\packages';output_root=$B+'\deliveries';state_root=$B+'\state';token_file=$previous.token_file;host='127.0.0.1';port=8765;targets=$previous.targets}
  foreach($path in @($config.package_root,$config.output_root,$config.state_root)){New-Item -ItemType Directory -Path $path -Force|Out-Null}
  $config | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath ($B+'\endpoint-candidate.json') -Encoding UTF8
  $Data.phase='running_candidate'; Save
  $info=New-Object System.Diagnostics.ProcessStartInfo
  $info.FileName=$RT
  $info.Arguments='-I -X utf8 -u -m description_pipeline serve --config "'+$B+'\endpoint-candidate.json"'
  $info.WorkingDirectory=$B
  $info.UseShellExecute=$false
  $info.CreateNoWindow=$true
  $info.RedirectStandardOutput=$true
  $info.RedirectStandardError=$true
  $proc=New-Object System.Diagnostics.Process
  $proc.StartInfo=$info
  if(-not $proc.Start()) { throw 'Candidate endpoint failed to start' }
  $null=$proc.Handle
  $stdout=$proc.StandardOutput.ReadToEndAsync();$stderr=$proc.StandardError.ReadToEndAsync()
  $Data.python_pid=$proc.Id; $Data.python_start=$proc.StartTime.ToUniversalTime().ToString('o'); Save
  $deadline=(Get-Date).AddMinutes(90)  # real M3 capture measured ~35 min; 50 min margin was too tight
  while(-not $proc.HasExited -and (Get-Date) -lt $deadline -and -not(Test-Path -LiteralPath ($B+'\stop-candidate'))){Start-Sleep -Seconds 2}
  if(-not $proc.HasExited){
    $active=@(Get-ChildItem -LiteralPath ($B+'\state\jobs') -Filter '*.json' -ErrorAction SilentlyContinue|ForEach-Object {$j=Get-Content -LiteralPath $_.FullName -Raw -Encoding UTF8|ConvertFrom-Json;if($j.status -in @('selecting','running','awaiting_definition','queued')){$j.run_id}})
    $Data.active_at_stop=$active
    if((Get-Date) -ge $deadline){$Data.error='Candidate lifetime deadline exceeded'}
    $proc.Kill(); $null=$proc.WaitForExit(10000)
  }
  [System.IO.File]::WriteAllText(($B+'\candidate-stdout.log'),$stdout.Result)
  [System.IO.File]::WriteAllText(($B+'\candidate-stderr.log'),$stderr.Result)
  if($proc.ExitCode -ne 0 -and -not(Test-Path -LiteralPath ($B+'\stop-candidate'))){$Data.error='Candidate endpoint exited before requested stop: '+$proc.ExitCode}
  $Data.phase='candidate_finished'
} catch {
  $Data.error=$_.Exception.Message; $Data.phase='failed'
} finally {
  if($proc -and -not $proc.HasExited){$proc.Kill();$null=$proc.WaitForExit(10000)}
  $cleanupDeadline=(Get-Date).AddSeconds(15)
  while((Get-Process SLDWORKS -ErrorAction SilentlyContinue) -and (Get-Date) -lt $cleanupDeadline){Start-Sleep -Milliseconds 500}
  $cad=@(Get-Process SLDWORKS -ErrorAction SilentlyContinue | Select-Object Id,SessionId,StartTime)
  $Data.cad_after=$cad
  if($Data.endpoint_was_running -and $cad.Count -eq 0) {
    try {
      Start-ScheduledTask -TaskName $Prod
      $restoreDeadline=(Get-Date).AddSeconds(20)
      $token=(Get-Content -LiteralPath "$Root\endpoint-secrets\endpoint.token" -Raw).Trim()
      do {
        try {
          $health=Invoke-WebRequest -Uri 'http://127.0.0.1:8765/health' -Headers @{Authorization=('Bearer '+$token)} -UseBasicParsing -TimeoutSec 3
          if($health.StatusCode -eq 200) { $Data.endpoint_restored=$true; break }
        } catch { }
        Start-Sleep -Milliseconds 500
      } while((Get-Date) -lt $restoreDeadline)
      $token=$null
      if(-not $Data.endpoint_restored) { $Data.restore_error='Authenticated endpoint health did not return 200' }
    } catch { $Data.restore_error=$_.Exception.Message }
  }
  if($Data.endpoint_was_running -and $cad.Count -ne 0) { $Data.restore_error='Endpoint restoration deferred because CAD remains active' }
  $Data.finished=(Get-Date).ToUniversalTime().ToString('o'); Save
}
if($Data.error -or $Data.restore_error) { exit 1 }
exit 0
