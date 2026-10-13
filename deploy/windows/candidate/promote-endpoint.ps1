param(
  [Parameter(Mandatory=$true)][string]$Tag,
  [string]$Root = 'C:\description-v1-review',
  [string]$Stamp = '20261012'
)
$ErrorActionPreference='Stop'
$ProgressPreference='SilentlyContinue'
$root=$Root
$base=$root+'\native-product'+$Tag+'-'+$Stamp
$taskName='description-v1-endpoint'
$promotion=$base+'\promotion'
if(Test-Path -LiteralPath $promotion){throw 'Preserve existing promotion; inspect before retrying'}
$identity=Get-Content -LiteralPath ($base+'\candidate-identity.json') -Raw -Encoding UTF8|ConvertFrom-Json
$installed=Get-Content -LiteralPath ($base+'\installed-windows.json') -Raw -Encoding UTF8|ConvertFrom-Json
if($identity.source_sha256 -ne $installed.tool.source_sha256 -or $identity.source_commit -ne $installed.tool.release.source_commit -or -not $installed.doctor.passed){throw 'Installed candidate identity is not verified'}
$stop=Get-Content -LiteralPath ($base+'\production-stop-receipt.json') -Raw -Encoding UTF8|ConvertFrom-Json
if($stop.state -ne 'stopped' -or $stop.task_after.enabled -or $stop.post.listener_count -ne 0 -or $stop.post.writer_count -ne 0){throw 'Original endpoint stop was not verified'}
$task=Get-ScheduledTask -TaskName $taskName
if($task.State -ne 'Disabled' -or $task.Settings.Enabled -or @($task.Actions).Count -ne 1){throw 'Production task must be stopped and disabled'}
if(Get-Process SLDWORKS -ErrorAction SilentlyContinue){throw 'CAD remains active'}
if(Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue){throw 'Endpoint listener remains'}
$writers=@(Get-CimInstance Win32_Process|Where-Object {$_.CommandLine -match '-m description_pipeline serve|endpoint-launcher\.py'})
if($writers.Count){throw 'Endpoint writer remains; inspect without broad termination'}
$controller=Get-Content -LiteralPath ($base+'\run-journal-$Tag.json') -Raw -Encoding UTF8|ConvertFrom-Json
if($controller.phase -ne 'candidate_finished' -or -not $controller.endpoint_restored){throw 'Candidate controller did not finish cleanly'}
$action=$task.Actions[0]
if($action.Arguments -notmatch 'endpoint-launcher\.py'){throw 'Unexpected original endpoint launcher'}
if($stop.task_before.execute -ne $action.Execute -or $stop.task_before.arguments -ne $action.Arguments){throw 'Production task action changed since the verified stop'}
New-Item -ItemType Directory -Path $promotion|Out-Null
Export-ScheduledTask -TaskName $taskName|Set-Content -LiteralPath ($promotion+'\task-before.xml') -Encoding UTF8
Copy-Item -LiteralPath ($root+'\endpoint.json') -Destination ($promotion+'\endpoint-before.json')
Copy-Item -LiteralPath ($root+'\endpoint-launcher.py') -Destination ($promotion+'\launcher-before.py')
Copy-Item -LiteralPath ($base+'\production-stop-receipt.json') -Destination ($promotion+'\production-stop-receipt.json')
$endpointBeforeSha=(Get-FileHash -LiteralPath ($promotion+'\endpoint-before.json') -Algorithm SHA256).Hash.ToLower()
$stopReceiptSha=(Get-FileHash -LiteralPath ($promotion+'\production-stop-receipt.json') -Algorithm SHA256).Hash.ToLower()
try {
  & ($base+'\runtime\Scripts\python.exe') -I ($base+'\retain-production-state.py')
  if($LASTEXITCODE -ne 0){throw 'Production evidence retention failed'}
  $retained=Get-Content -LiteralPath ($base+'\retained-production-state.json') -Raw -Encoding UTF8|ConvertFrom-Json
  if(-not $retained.complete -or (Get-FileHash -LiteralPath ($base+'\endpoint-promoted.json') -Algorithm SHA256).Hash.ToLower() -ne $retained.config_sha256){throw 'Promoted configuration is not bound to the complete retention receipt'}
  Copy-Item -LiteralPath ($base+'\retained-production-state.json') -Destination ($promotion+'\retained-production-state.json')
  Copy-Item -LiteralPath ($base+'\endpoint-promoted.json') -Destination ($root+'\endpoint.json') -Force
  $actionArgs=@{Execute=($base+'\runtime\Scripts\python.exe');Argument=$action.Arguments}
  if($action.WorkingDirectory){$actionArgs.WorkingDirectory=$action.WorkingDirectory}
  $newAction=New-ScheduledTaskAction @actionArgs
  Set-ScheduledTask -TaskName $taskName -Action $newAction|Out-Null
  Enable-ScheduledTask -TaskName $taskName|Out-Null
  Start-ScheduledTask -TaskName $taskName
  $config=Get-Content -LiteralPath ($root+'\endpoint.json') -Raw -Encoding UTF8|ConvertFrom-Json
  $token=(Get-Content -LiteralPath $config.token_file -Raw).Trim()
  $deadline=(Get-Date).AddSeconds(30)
  $ready=$false
  do {
    try {
      $health=Invoke-RestMethod -Uri 'http://127.0.0.1:8765/health' -Headers @{Authorization=('Bearer '+$token)} -TimeoutSec 3
      if($health.ready -and $health.native_platform){$ready=$true;break}
    } catch { }
    Start-Sleep -Milliseconds 500
  } while((Get-Date) -lt $deadline)
  $token=$null
  if(-not $ready){throw 'Promoted endpoint authenticated health did not become ready'}
  @{state='promoted';source_sha256=$identity.source_sha256;source_commit=$identity.source_commit;runtime=$base+'\runtime';state_root=$config.state_root;retained_files=@($retained.files).Count;config_sha256=$retained.config_sha256;endpoint_before_sha256=$endpointBeforeSha;stop_receipt_sha256=$stopReceiptSha;at=(Get-Date).ToUniversalTime().ToString('o')}|ConvertTo-Json|Set-Content -LiteralPath ($promotion+'\receipt.json') -Encoding UTF8
  Write-Output 'Production endpoint promoted with retained historical and acceptance evidence.'
} catch {
  $failure=$_
  $rollbackHealth='not_attempted'
  $rollbackError=$null
  # Before a successful start, the stopped task can safely restore its original action/config.
  # If a promoted process has started, preserve the failed promotion for exact-process recovery.
  $listeners=@(Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue)
  if($listeners.Count -eq 0 -and (Get-ScheduledTask -TaskName $taskName).State -ne 'Running'){
    try {
      Copy-Item -LiteralPath ($promotion+'\endpoint-before.json') -Destination ($root+'\endpoint.json') -Force
      Set-ScheduledTask -TaskName $taskName -Action $action|Out-Null
      Enable-ScheduledTask -TaskName $taskName|Out-Null
      Start-ScheduledTask -TaskName $taskName
      $before=Get-Content -LiteralPath ($promotion+'\endpoint-before.json') -Raw -Encoding UTF8|ConvertFrom-Json
      $token=(Get-Content -LiteralPath $before.token_file -Raw).Trim()
      $deadline=(Get-Date).AddSeconds(30)
      $rollbackReady=$false
      do {
        try {
          $health=Invoke-RestMethod -Uri 'http://127.0.0.1:8765/health' -Headers @{Authorization=('Bearer '+$token)} -TimeoutSec 3
          if($health.ready -and $health.native_platform){$rollbackReady=$true;break}
        } catch { }
        Start-Sleep -Milliseconds 500
      } while((Get-Date) -lt $deadline)
      $token=$null
      $rollbackHealth=$(if($rollbackReady){'ready'}else{'not_ready'})
    } catch {
      $rollbackError=$_.Exception.Message
      $rollbackHealth='start_failed'
    }
  } else {
    $rollbackHealth='skipped_active_process'
  }
  @{state='failed';error=$failure.Exception.Message;endpoint_before_sha256=$endpointBeforeSha;stop_receipt_sha256=$stopReceiptSha;rollback_health=$rollbackHealth;rollback_error=$rollbackError;at=(Get-Date).ToUniversalTime().ToString('o')}|ConvertTo-Json|Set-Content -LiteralPath ($promotion+'\receipt.json') -Encoding UTF8
  throw $failure
}
