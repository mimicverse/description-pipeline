param(
  [Parameter(Mandatory=$true)][string]$Tag,
  [string]$Root = 'C:\description-v1-review',
  [string]$Stamp = '20261012',
  [Parameter(Mandatory=$true)][string]$TemplateTask
)
$ErrorActionPreference='Stop'
$name="description-product-$Tag-$Stamp"
$base="$Root\native-product$Tag-$Stamp"
if(Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue){throw 'Preserve existing candidate task'}
$template=Get-ScheduledTask -TaskName $TemplateTask
$action=New-ScheduledTaskAction -Execute 'powershell.exe' -Argument ('-NoProfile -ExecutionPolicy Bypass -File "'+$base+'\run-candidate-endpoint.ps1" -Fixture "'+$Tag+'" -Stage "'+$base+'"') -WorkingDirectory $base
$settings=New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Minutes 100) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName $name -Action $action -Principal $template.Principal -Settings $settings -Description 'Bounded installed release candidate acceptance; exact owned endpoint lifecycle and restoration journal.' | Out-Null
Write-Output 'Candidate task registered; not started.'
