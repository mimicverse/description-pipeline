<#
.SYNOPSIS
Rehearse a release candidate on the Windows host that runs SolidWorks.

.DESCRIPTION
The release checklist asks for a native install, an offline first run and a CAD probe.  Doing that by
hand is where the evidence gets lost: the 0.3.17 record had to be reconstructed from a worker that was
still running hours later, because nobody kept the transcript or the job directory.

This script installs the candidate bundle into an isolated root, proves the bundle against the
SHA256SUMS beside it, runs Setup, Start, Status, Doctor (which opens the configured assembly and
walks it read-only) and the offline `quickstart --run` with the freshly installed runtime, writes one
JSON summary and the full transcript next to the bundle, and then stops the worker, unregisters the
task and removes the root unless -KeepInstalled is given.

It refuses the production worker by name: the task name may not be `description-pipeline-worker`, and
an existing install root that already names a different task is not reused.

.EXAMPLE
powershell -ExecutionPolicy Bypass -File tools\native\win-rehearsal.ps1 `
    -Bundle .\description-worker-0.3.18-windows-x86_64.zip `
    -Assembly 'C:\Users\Me\AppData\Local\swbridge\validation\...\run\cad\robot.SLDASM'
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Bundle,
    [string]$InstallRoot = 'C:\dwv-rehearsal',
    [int]$Port = 18769,
    [string]$TaskName = 'description-pipeline-worker-rehearsal',
    [string]$Python = "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe",
    [string]$Assembly,
    [string]$Configuration = 'Default',
    [string]$ExpectedSha256,
    [string]$UpdateFrom,
    [string]$Evidence,
    [switch]$KeepInstalled
)

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.Encoding]::UTF8

function Stop-WithReason([string]$Message) {
    Write-Output ("REFUSING: {0}" -f $Message)
    exit 1
}

function Invoke-CapturedProcess([string]$FilePath, [string[]]$Arguments) {
    # A child process's console output never reaches this session's transcript: the 0.3.19 log was a
    # transcript header and footer around nothing, while the words that prove the upgrade - which
    # version replaced which - scrolled past on the terminal and were lost.  Capture them here so the
    # summary, or the rehearsal's own output, carries the evidence.
    # Merging a native command's stderr with ``2>&1`` turns each line into an error record, and under
    # this script's ``$ErrorActionPreference = 'Stop'`` that ends the run: the 0.3.20 rehearsal died
    # at the adapter's unsaved-changes advisory before it could write a summary.  Relax it for the
    # call and record the exit code instead of the throw.
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    $text = ''
    $code = -1
    try {
        $text = ((& $FilePath @Arguments 2>&1) -join "`n").Trim()
        $code = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previous
    }
    return @{ exit = $code; log = $text }
}

function Invoke-RawRequest([int]$Port, [string]$RequestLine, [string]$HostHeader, [string[]]$Extra = @()) {
    # One request with a header PowerShell cannot forge through its own web cmdlets:
    # ``Invoke-WebRequest`` will not let a caller replace ``Host``, which is exactly the header the
    # rebinding guard reads.  Only the status code comes back - a bare string here would be output,
    # which is how the first draft returned an array and let every comparison pass.
    $client = New-Object System.Net.Sockets.TcpClient
    $client.Connect('127.0.0.1', $Port)
    $stream = $client.GetStream()
    $lines = @($RequestLine, "Host: $HostHeader") + $Extra + @('Connection: close', '', '')
    $payload = [Text.Encoding]::ASCII.GetBytes(($lines -join "`r`n"))
    $stream.Write($payload, 0, $payload.Length)
    $stream.Flush()
    $reader = New-Object IO.StreamReader($stream)
    $status = 0
    $statusLine = $reader.ReadLine()
    if ($statusLine -match 'HTTP/1\.[01]\s+(\d{3})') { $status = [int]$Matches[1] }
    $reader.Close()
    $client.Close()
    return $status
}

if ($TaskName -eq 'description-pipeline-worker') {
    Stop-WithReason 'the production task name is reserved; pass another -TaskName'
}
if (-not (Test-Path -LiteralPath $Bundle)) {
    Stop-WithReason ("no bundle at {0}" -f $Bundle)
}
if (-not (Test-Path -LiteralPath $Python)) {
    Stop-WithReason ("no CPython 3.12 at {0}; pass -Python" -f $Python)
}
if (Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue) {
    Stop-WithReason ("port {0} is already listening; stop that worker or pass another -Port" -f $Port)
}
$existing = Join-Path $InstallRoot 'worker-host.json'
if (Test-Path -LiteralPath $existing) {
    $named = (Get-Content -LiteralPath $existing -Raw | ConvertFrom-Json).task_name
    if ($named -ne $TaskName) {
        Stop-WithReason ("{0} belongs to task {1}; pass another -InstallRoot" -f $InstallRoot, $named)
    }
}

$bundleName = Split-Path -Leaf $Bundle
$directory = Split-Path -Parent (Resolve-Path -LiteralPath $Bundle)
$sumFile = Join-Path $directory 'SHA256SUMS'
if (-not $ExpectedSha256 -and (Test-Path -LiteralPath $sumFile)) {
    $line = Get-Content -LiteralPath $sumFile |
        Where-Object { $_.Trim().EndsWith($bundleName) } | Select-Object -First 1
    if ($line) { $ExpectedSha256 = ($line.Trim() -split '\s+')[0].ToLower() }
}
if (-not $ExpectedSha256) {
    Stop-WithReason 'no SHA256SUMS beside the bundle and no -ExpectedSha256; the bytes cannot be proven'
}
$ActualSha256 = (Get-FileHash -LiteralPath $Bundle -Algorithm SHA256).Hash.ToLower()
if ($ActualSha256 -ne $ExpectedSha256.ToLower()) {
    Stop-WithReason ("{0} hashes to {1}, not the published {2}" -f $bundleName, $ActualSha256, $ExpectedSha256)
}

if (-not $Evidence) { $Evidence = Join-Path $directory 'native-rehearsal.json' }
$Transcript = [IO.Path]::ChangeExtension($Evidence, '.log')
$started = (Get-Date).ToUniversalTime().ToString('o')
Start-Transcript -Path $Transcript -Force | Out-Null

$steps = @{}
$statusText = ''
$doctorText = ''

try {
    Write-Output ("=== {0} ===" -f $bundleName)
    Write-Output ("sha256 {0} (matches the bundle's own SHA256SUMS)" -f $ActualSha256)

    New-Item -ItemType Directory -Force -Path $InstallRoot | Out-Null
    # `-UpdateFrom` rehearses the upgrade: the older release is installed first, and this bundle then
    # replaces it — which is what a user does every time a release comes out, and what silently failed
    # until an update was made to take its expectation from the new archive's own release.
    $installedFrom = $Bundle
    if ($UpdateFrom) {
        if (-not (Test-Path -LiteralPath $UpdateFrom)) { Stop-WithReason ("no archive at {0} to update from" -f $UpdateFrom) }
        $installedFrom = $UpdateFrom
    }
    Expand-Archive -LiteralPath $installedFrom -DestinationPath (Join-Path $InstallRoot 'bundle') -Force
    $worker = Join-Path $InstallRoot 'bundle\worker.ps1'
    $config = Join-Path $InstallRoot 'worker-host.json'

    $arguments = @(
        '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $worker, '-Action', 'Setup',
        '-Bundle', $installedFrom, '-Config', $config,
        '-InstallRoot', $InstallRoot, '-Python', $Python, '-Port', "$Port", '-TaskName', $TaskName, '-Force'
    )
    if ($installedFrom -eq $Bundle) { $arguments += @('-BundleSha256', $ExpectedSha256) }
    if ($Assembly) { $arguments += @('-Assembly', $Assembly, '-AssemblyConfiguration', $Configuration) }
    & powershell @arguments
    $steps['setup'] = $LASTEXITCODE

    $update = @{ exit = -1; from = ''; to = ''; log = ''; rollback_log = '' }
    $updater = $worker
    if ($steps['setup'] -eq 0 -and $UpdateFrom) {
        # The new archive is updated with *its own* launcher, exactly as the guides tell an operator to.
        Expand-Archive -LiteralPath $Bundle -DestinationPath (Join-Path $InstallRoot 'bundle-update') -Force
        $updater = Join-Path $InstallRoot 'bundle-update\worker.ps1'
        $updateRun = Invoke-CapturedProcess 'powershell' @(
            '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $updater, '-Action', 'Update',
            '-Bundle', $Bundle, '-Config', $config
        )
        $update.exit = $updateRun.exit
        $update.log = $updateRun.log
        Write-Output $updateRun.log
        $state = Get-Content (Join-Path $InstallRoot 'current.json') -Raw | ConvertFrom-Json
        $update.from = $state.previous_version
        $update.to = $state.version
    }

    if ($steps['setup'] -eq 0) {
        & powershell -NoProfile -ExecutionPolicy Bypass -File $worker -Action Start -Config $config
        $steps['start'] = $LASTEXITCODE
    }
    # Binding to loopback keeps other machines out, but every page in the user's own browser can reach
    # 127.0.0.1: a rebinding name arrives in `Host` and a cross-site POST carries `Origin`.  The worker
    # refuses both, and this is the machine where that answer can actually be observed.
    $security = @{ foreign_host = 0; foreign_origin = 0; unknown_route = 0 }
    if ($steps['start'] -eq 0) {
        $security.foreign_host = [int](Invoke-RawRequest $Port 'GET /health HTTP/1.1' 'evil.example' @())
        $security.foreign_origin = [int](Invoke-RawRequest $Port 'GET /health HTTP/1.1' "127.0.0.1:$Port" @(
            'Origin: https://evil.example'
        ))
        $security.unknown_route = [int](Invoke-RawRequest $Port 'GET /nope HTTP/1.1' "127.0.0.1:$Port" @())
        Write-Output ("security: foreign Host -> {0}, foreign Origin -> {1}, unknown route -> {2}" -f `
            $security.foreign_host, $security.foreign_origin, $security.unknown_route)
    }
    if ($steps['start'] -eq 0) {
        $statusText = (& powershell -NoProfile -ExecutionPolicy Bypass -File $worker -Action Status -Config $config) -join "`n"
        $steps['status'] = $LASTEXITCODE
    }
    if ($steps['status'] -eq 0) {
        $doctorText = (& powershell -NoProfile -ExecutionPolicy Bypass -File $worker -Action Doctor -Config $config) -join "`n"
        $steps['doctor'] = $LASTEXITCODE
    }

    # The active version, not the first directory on disk: an upgrade rehearsal has both installed, and
    # the first one is the version it updated *from* (the 0.3.17 summary said 0.3.17 while the worker
    # answered 0.3.18).
    $installed = @(Get-ChildItem (Join-Path $InstallRoot 'versions') -Directory -ErrorAction SilentlyContinue |
        Select-Object -ExpandProperty Name)
    $active = ''
    $currentFile = Join-Path $InstallRoot 'current.json'
    if (Test-Path -LiteralPath $currentFile) { $active = (Get-Content $currentFile -Raw | ConvertFrom-Json).version }
    $runtime = if ($active) { Join-Path $InstallRoot "versions\$active\venv\Scripts\description.cmd" } else { '' }
    $description = if ($runtime -and (Test-Path -LiteralPath $runtime)) { $runtime } else { '' }
    $quickstart = @{ exit = 1; passed = $false; qualified_for = @() }
    # The Doctor line above is written for a human.  The release record needs the facts - which
    # SolidWorks revision, how many components were walked - so ask the same worker for its
    # machine-readable report and keep the summary readable by whoever writes that record.
    $solidworksRevision = ''
    $solidworksLicense = ''
    $components = $null
    $massProperties = $null
    $mujoco = ''
    $workerVersion = ''
    if ($description) {
        $target = "http://127.0.0.1:$Port"
        $arguments = @('worker', 'doctor', '--target', $target)
        if ($Assembly) { $arguments += @('--assembly', $Assembly, '--configuration', $Configuration) }
        # ``description worker doctor`` keeps its machine-readable report on stdout and its advisories
        # on stderr on purpose; the capture merges both so the advisory reaches the transcript, and
        # the helper keeps the merge from ending the run.  The report is what gets parsed; the lines
        # before it are the adapter's own words, and they are written out for the operator.
        $rawDoctor = (Invoke-CapturedProcess $description $arguments).log
        $brace = $rawDoctor.IndexOf('{')
        if ($brace -gt 0) {
            Write-Output $rawDoctor.Substring(0, $brace).Trim()
        } elseif ($brace -lt 0 -and $rawDoctor) {
            Write-Output $rawDoctor
        }
        try {
            $doctor = $rawDoctor.Substring($rawDoctor.IndexOf('{')) | ConvertFrom-Json
            $solidworks = $doctor.checks | Where-Object { $_.name -eq 'solidworks' } | Select-Object -First 1
            $walked = $doctor.checks | Where-Object { $_.name -eq 'collection' } | Select-Object -First 1
            if ($solidworks) {
                $solidworksRevision = [string]$solidworks.revision
                $solidworksLicense = [string]$solidworks.license_type
            }
            if ($walked) {
                $components = $walked.components
                $massProperties = $walked.mass_properties
            }
        } catch {
            Write-Output ("worker doctor output was not a report: {0}" -f $_.Exception.Message)
        }
        try {
            $health = Invoke-RestMethod -Uri "$target/health" -TimeoutSec 15
            $workerVersion = [string]$health.worker_version
        } catch {
            Write-Output ("worker health was not readable: {0}" -f $_.Exception.Message)
        }

        $demo = Join-Path $InstallRoot 'demo'
        $report = (& $description quickstart $demo --run) -join "`n"
        $quickstart.exit = $LASTEXITCODE
        try {
            $parsed = $report.Substring($report.IndexOf('{')) | ConvertFrom-Json
            $quickstart.passed = [bool]$parsed.passed
            $quickstart.qualified_for = @($parsed.qualified_for)
        } catch {
            Write-Output ("quickstart output was not a report: {0}" -f $_.Exception.Message)
        }
        $version = (& $description --version)
    } else {
        Write-Output 'no installed runtime found for the offline first run'
        $version = ''
    }

    $localPipeline = ''
    $match = [regex]::Match($statusText, 'local pipeline\s*:\s*(.+)')
    if ($match.Success) { $localPipeline = $match.Groups[1].Value.Trim() }
    $mujocoMatch = [regex]::Match($localPipeline, 'mujoco\s+(\S+)')
    if ($mujocoMatch.Success) { $mujoco = $mujocoMatch.Groups[1].Value }
    $collection = [regex]::Match($doctorText, 'collection\s*:\s*(.+)')

    if ($UpdateFrom) {
        # Last, so everything above ran on the version the release ships; the rollback only has to
        # prove that the previous version is still installable and comes back when asked.
        $rollbackRun = Invoke-CapturedProcess 'powershell' @(
            '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $updater, '-Action', 'Rollback',
            '-Config', $config
        )
        $update.rollback_exit = $rollbackRun.exit
        $update.rollback_log = $rollbackRun.log
        Write-Output $rollbackRun.log
        $update.rolled_back_to = (Get-Content (Join-Path $InstallRoot 'current.json') -Raw | ConvertFrom-Json).version
    }

    $passed = ($steps.Values | Where-Object { $_ -ne 0 }).Count -eq 0 -and
        $steps.Count -eq 4 -and
        $quickstart.passed -and
        $collection.Success -and
        $collection.Groups[1].Value -match 'collectable=True|collectable=true' -and
        $security.foreign_host -eq 403 -and $security.foreign_origin -eq 403 -and $security.unknown_route -eq 404 -and
        (-not $UpdateFrom -or ($update.exit -eq 0 -and $update.rollback_exit -eq 0))

    $summary = [ordered]@{
        schema_version      = 'description.rehearsal/v1'
        bundle              = $Bundle
        bundle_sha256       = $ActualSha256
        update_from         = $UpdateFrom
        update              = $update
        security            = $security
        version             = ("$version").Trim()
        versions_installed  = $installed
        install_root        = $InstallRoot
        port                = $Port
        task_name           = $TaskName
        assembly            = $Assembly
        configuration       = $(if ($Assembly) { $Configuration } else { '' })
        setup               = $steps['setup']
        start               = $steps['start']
        status              = $steps['status']
        doctor              = $steps['doctor']
        local_pipeline      = $localPipeline
        mujoco              = $mujoco
        worker_version      = $workerVersion
        solidworks_revision = $solidworksRevision
        solidworks_license  = $solidworksLicense
        components          = $components
        mass_properties     = $massProperties
        collection          = $collection.Groups[1].Value.Trim()
        quickstart          = $quickstart
        started_at          = $started
        finished_at         = (Get-Date).ToUniversalTime().ToString('o')
        passed              = [bool]$passed
        cleaned_up          = [bool](-not $KeepInstalled)
    }
    # UTF8 without a BOM: the summary is read by other tools, and Windows PowerShell 5.1 writes one.
    [IO.File]::WriteAllText(
        $Evidence,
        ($summary | ConvertTo-Json -Depth 5),
        (New-Object Text.UTF8Encoding($false))
    )
} finally {
    if (-not $KeepInstalled) {
        $worker = Join-Path $InstallRoot 'bundle\worker.ps1'
        $config = Join-Path $InstallRoot 'worker-host.json'
        if (Test-Path -LiteralPath $worker) {
            & powershell -NoProfile -ExecutionPolicy Bypass -File $worker -Action Stop -Config $config
        }
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath $InstallRoot -Recurse -Force -ErrorAction SilentlyContinue
    }
    Stop-Transcript | Out-Null
}

Write-Output ("summary: {0}" -f $Evidence)
Write-Output ("transcript: {0}" -f $Transcript)
Get-Content -LiteralPath $Evidence -Raw
exit $(if ($passed) { 0 } else { 1 })
