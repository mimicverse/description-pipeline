# Exercise the real process-boundary exception writer with offline OS seams.
param([string]$ScriptPath, [string]$Scenario, [string]$Root)
$ErrorActionPreference = 'Stop'
. $ScriptPath -Verb stage -Binding 'unused'
if ($Scenario -in @('promote', 'rollback', 'recovery-snapshot')) { $Script:Verb = $Scenario }
function Get-ManagedEndpointAclRows {
  return @([pscustomobject]@{role='endpoint_config'; path=$Root; required=$true; recurse=$false; non_inheriting=$false})
}
function Set-ManagedAcl {
  param($Path, $Protected, $ServiceSid, [switch]$NonInheriting, [switch]$Recurse)
  throw [System.UnauthorizedAccessException]::new('injected ACL set failure')
}
function Assert-ManagedAcl {
  param($Path, $Protected, $ServiceSid, [switch]$NonInheriting, [switch]$Recurse)
  throw [System.IO.IOException]::new('injected ACL assert failure')
}
function Invoke-Main {
  $script:LifecyclePhase = 'stage-candidate'
  switch ($Scenario) {
    'die' { Die 'original_refusal' 'original refusal'; return }
    'die-unknown' { DieUnknown 'original_unknown' 'original unknown'; return }
    'set' { Set-ManagedEndpointAcls @{} $Root $Root $Root; return }
    'assert' { Assert-ManagedEndpointAcls @{} $Root $Root $Root; return }
    'noisy' { Write-Output 'pip succeeded'; [Console]::Error.WriteLine('#< CLIXML') }
    'fallback' { function script:ConvertTo-Json { throw 'serializer failure' } }
    'sensitive' {
      throw ('{"token":"SECRET_JSON","password":"SECRET SPACE"} Bearer SECRET_BEARER ' +
        'postgresql://user:SECRET_URI@host/db Password=SECRET_DSN; ' + ('long' * 400))
    }
    'paths' {
      throw 'Drive "C:\Program Files\SECRET_DIR\file" Apostrophe "C:\Users\O''Brien\SECRET_APOSTROPHE" UNC "\\SECRET_HOST\SECRET SHARE\name" FileSystem::SECRET_PROVIDER\path file://SECRET_FILE/path'
    }
  }
  # A wrapped native-style exception retains both the wrapper and base HResult.
  throw [System.Reflection.TargetInvocationException]::new(
    'injected wrapper', [System.IO.IOException]::new('injected native failure'))
}
Invoke-LifecycleMain
