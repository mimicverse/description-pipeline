<#
.SYNOPSIS
    Fixed-version deployment entry point for the SolidWorks collection worker.

.DESCRIPTION
    One script for Install / Start / Stop / Doctor / Update / Rollback / Status.
    The worker runs in the logged-in desktop session (interactive scheduled task),
    because SolidWorks COM needs a desktop; SSH or a service session cannot
    collect CAD.  Versions live side by side and switching is a pointer change,
    so an update never overwrites the version that is currently serving.

    Install and Update require the bundle's SHA256 as 64 hex characters, from
    worker-host.json (bundle_sha256) or -BundleSha256.  The bundle build prints it
    in its JSON result; without it the bundle is not installed.

.EXAMPLE
    powershell -File .\worker.ps1 -Action Setup   -Bundle .\description-worker-<version>-windows-x86_64.zip -Assembly D:\models\robot.SLDASM -AssemblyConfiguration Default
    powershell -File .\worker.ps1 -Action Install -Bundle .\description-worker-0.3.1.zip -Config .\worker-host.json
    powershell -File .\worker.ps1 -Action Start   -Config .\worker-host.json
    powershell -File .\worker.ps1 -Action Doctor  -Config .\worker-host.json
    # Update takes the new archive with its release SHA256SUMS (or -BundleSha256); the digest in
    # worker-host.json belongs to the version that is installed and would refuse every update.
    powershell -File .\worker.ps1 -Action Update  -Bundle .\description-worker-<new-version>.zip -BundleSha256 <64 hex> -Config .\worker-host.json
    powershell -File .\worker.ps1 -Action Rollback -Config .\worker-host.json
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('Setup', 'Install', 'Start', 'Stop', 'Doctor', 'Update', 'Rollback', 'Status')]
    [string]$Action,
    [string]$Config,
    [string]$Bundle,
    [string]$BundleSha256,
    # Setup writes the host configuration for you; these options avoid hand-editing JSON.
    [string]$InstallRoot,
    [string]$Python,
    [string]$Assembly,
    [string]$AssemblyConfiguration,
    [int]$Port = 0,
    [string]$TaskName,
    [switch]$Force,
    [switch]$NoInstall
)

$ErrorActionPreference = 'Stop'

# A first-time user may not have 3.12 anywhere on the machine.  Naming a way to get one, and the
# exact re-run, is more useful than repeating the flag they have just failed to set; the Linux
# installer says the same thing, so the two first runs read alike.
$PythonAdvice = 'install CPython 3.12 (winget install Python.Python.3.12), reopen the shell, or pass -Python with its full path'

function Write-Step([string]$Message) { Write-Host "== $Message" }
function Write-Info([string]$Message) { Write-Host "   $Message" }
function Stop-WithError([string]$Message) { throw $Message }

function Get-HostConfig([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path)) { Stop-WithError "host config not found: $Path" }
    $config = Get-Content -LiteralPath $Path -Raw -Encoding UTF8 | ConvertFrom-Json
    foreach ($key in @('install_root', 'user')) {
        if (-not $config.$key) { Stop-WithError "worker-host.json needs '$key'" }
    }
    if (-not $config.port) { $config | Add-Member -NotePropertyName port -NotePropertyValue 8765 -Force }
    if (-not $config.host) { $config | Add-Member -NotePropertyName host -NotePropertyValue '127.0.0.1' -Force }
    if (-not $config.jobs_root) {
        $config | Add-Member -NotePropertyName jobs_root `
            -NotePropertyValue (Join-Path $config.install_root 'jobs') -Force
    }
    if (-not $config.task_name) {
        $config | Add-Member -NotePropertyName task_name `
            -NotePropertyValue 'description-pipeline-worker' -Force
    }
    if (-not $config.keep_versions) { $config | Add-Member -NotePropertyName keep_versions -NotePropertyValue 3 -Force }
    return $config
}

function Get-FileSha256([string]$Path) { (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant() }

function Get-PowerShellPath {
    # Setup hands over to Install and Doctor in child processes, so find this host's executable.

    foreach ($name in @('powershell.exe', 'pwsh.exe', 'pwsh')) {
        $candidate = Join-Path $PSHOME $name
        if (Test-Path -LiteralPath $candidate) { return $candidate }
    }
    foreach ($name in @('powershell.exe', 'pwsh.exe', 'pwsh')) {
        $command = Get-Command $name -ErrorAction SilentlyContinue
        if ($command) { return $command.Source }
    }
    Stop-WithError 'no PowerShell executable was found to run the next step'
}

function Resolve-BundleDigest([string]$BundlePath, [string]$Explicit, [switch]$Strict) {
    # An explicit digest wins; otherwise verify against an adjacent SHA256SUMS, else pin the file.
    # `-Strict` drops that last step: an update decides which bundle runs next, so it accepts an
    # expectation from the release (its SHA256SUMS, or -BundleSha256) and refuses to invent one.

    if ($Explicit) { return (Assert-Bundle $BundlePath $Explicit) }
    $directory = Split-Path -Parent (Resolve-Path -LiteralPath $BundlePath).Path
    $sums = Join-Path $directory 'SHA256SUMS'
    $name = Split-Path -Leaf $BundlePath
    if (Test-Path -LiteralPath $sums) {
        foreach ($line in Get-Content -LiteralPath $sums) {
            $parts = @(($line -split '\s+') | Where-Object { $_ })
            if ($parts.Count -ge 2 -and $parts[1].TrimStart('*') -eq $name) {
                Write-Info "digest verified against SHA256SUMS"
                return (Assert-Bundle $BundlePath $parts[0])
            }
        }
        if ($Strict) {
            Stop-WithError ("SHA256SUMS next to $name does not list it; download the archive with its " +
                'release SHA256SUMS, or pass -BundleSha256 from the release page')
        }
        Write-Info "SHA256SUMS does not list $name; pinning the digest of the file as provided"
        return (Get-FileSha256 $BundlePath)
    }
    if ($Strict) {
        Stop-WithError ("no SHA256SUMS next to $name; download the archive with its release SHA256SUMS, " +
            'or pass -BundleSha256 from the release page')
    }
    Write-Info 'no SHA256SUMS next to the bundle; pinning the digest of the file as provided'
    return (Get-FileSha256 $BundlePath)
}

function New-HostConfig {
    # Write worker-host.json from options, so a first-time operator never edits JSON by hand.

    param(
        [string]$Path,
        [string]$Bundle,
        [string]$BundleSha256,
        [string]$InstallRoot,
        [string]$Python,
        [string]$Assembly,
        [string]$AssemblyConfiguration,
        [int]$Port,
        [string]$TaskName,
        [switch]$Force
    )
    if (-not $Bundle) { Stop-WithError '-Bundle is required for Setup' }
    if (-not $InstallRoot) {
        $base = if ($env:LOCALAPPDATA) { $env:LOCALAPPDATA } else { $env:USERPROFILE }
        $InstallRoot = Join-Path $base 'DescriptionWorker'
    }
    if (-not $Python) { $Python = (Get-Command python -ErrorAction SilentlyContinue).Source }
    if (-not $Python) { Stop-WithError "CPython 3.12 was not found; $PythonAdvice" }
    if (-not (Test-Path -LiteralPath $Python)) { Stop-WithError "no interpreter at $Python; $PythonAdvice" }
    $found = ''
    try { $found = (& $Python --version 2>&1 | Out-String).Trim() } catch { $found = '' }
    & $Python -c 'import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 12) else 1)'
    if ($LASTEXITCODE -ne 0) { Stop-WithError "the worker needs CPython 3.12; $Python is $found. $PythonAdvice" }
    if (-not $TaskName) { $TaskName = 'description-pipeline-worker' }
    if ($Port -le 0) { $Port = 8765 }
    $digest = Resolve-BundleDigest $Bundle $BundleSha256
    $config = [ordered]@{
        install_root  = $InstallRoot
        jobs_root     = (Join-Path $InstallRoot 'jobs')
        user          = [Security.Principal.WindowsIdentity]::GetCurrent().Name
        host          = '127.0.0.1'
        port          = $Port
        task_name     = $TaskName
        python        = $Python
        keep_versions = 3
        bundle_sha256 = $digest
        assembly      = if ($Assembly) { $Assembly } else { '' }
        configuration = if ($AssemblyConfiguration) { $AssemblyConfiguration } else { '' }
    }
    $parent = Split-Path -Parent $Path
    if ($parent -and -not (Test-Path -LiteralPath $parent)) {
        New-Item -ItemType Directory -Force -Path $parent | Out-Null
    }
    $json = $config | ConvertTo-Json
    if (Test-Path -LiteralPath $Path) {
        # Repeating the same command must be harmless; changing the settings must be deliberate.
        $existing = -join ((Get-Content -LiteralPath $Path -Raw -Encoding UTF8) -replace '\s', '')
        if ($existing -eq (-join ($json -replace '\s', ''))) {
            Write-Info "host config already matches these options: $Path"
            return $config
        }
        if (-not $Force) {
            Stop-WithError ("host config already exists with other options: $Path; edit it, or pass -Force " +
                'to write the options given here')
        }
        [IO.File]::WriteAllText($Path, $json, [Text.UTF8Encoding]::new($false))
        Write-Info "host config rewritten: $Path"
        return $config
    }
    [IO.File]::WriteAllText($Path, $json, [Text.UTF8Encoding]::new($false))
    Write-Info "host config written: $Path"
    return $config
}

function Test-SameConfig([string]$Left, [string]$Right) {
    # Both files are written by New-HostConfig, so the settings compare as text.

    if (-not (Test-Path -LiteralPath $Left) -or -not (Test-Path -LiteralPath $Right)) { return $false }
    $left = -join ((Get-Content -LiteralPath $Left -Raw -Encoding UTF8) -replace '\s', '')
    $right = -join ((Get-Content -LiteralPath $Right -Raw -Encoding UTF8) -replace '\s', '')
    return ($left -eq $right)
}

function Write-InstalledEndpoint([string]$InstalledConfigPath, $NewHost) {
    # The worker can only be stopped through the configuration it was installed with, so when the
    # listen address or port moved, say so before Install runs into it.

    if (-not (Test-Path -LiteralPath $InstalledConfigPath)) { return }
    $installed = Get-HostConfig $InstalledConfigPath
    if ($installed.install_root -eq $NewHost.install_root -and $installed.host -eq $NewHost.host -and
        $installed.port -eq $NewHost.port) { return }
    if (-not (Invoke-WorkerEndpoint $installed '/health')) { return }
    Write-Info ("the running worker still listens on {0}:{1}" -f $installed.host, $installed.port)
    Write-Info ("stop it first: powershell -File .\worker.ps1 -Action Stop -Config `"$InstalledConfigPath`"")
    Write-Info 'then run this command again'
}

function Assert-Bundle([string]$Path, [string]$Expected) {
    if (-not (Test-Path -LiteralPath $Path)) { Stop-WithError "bundle not found: $Path" }
    # Install and Update decide what runs as the collector, so the digest is
    # required: an unset digest is not "no expectation", it is an unchecked
    # bundle.  There is deliberately no flag to skip this.
    if (-not $Expected -or $Expected -notmatch '^[0-9a-fA-F]{64}$') {
        Stop-WithError ("worker-host.json needs bundle_sha256 (or -BundleSha256) as 64 hex characters; " +
            "take the expected value from the build output or the release SHA256SUMS file - never from the " +
            "archive that is about to be installed")
    }
    $digest = Get-FileSha256 $Path
    if ($digest -ne $Expected.ToLowerInvariant()) {
        Stop-WithError "bundle digest mismatch: expected $Expected, got $digest"
    }
    Write-Info "bundle sha256 $digest"
    return $digest
}

function Test-WorkerBusy($Health) {
    # A worker can hold jobs this process never queued (recovered after a
    # restart), so the record-level counters from /health count as busy too.
    if (-not $Health) { return $false }
    $hasCadState = if ($Health -is [System.Collections.IDictionary]) {
        $Health.Contains('cad_operation_active')
    } else { $null -ne $Health.PSObject.Properties['cad_operation_active'] }
    $hasRecovery = if ($Health -is [System.Collections.IDictionary]) {
        $Health.Contains('cad_recovery_required')
    } else { $null -ne $Health.PSObject.Properties['cad_recovery_required'] }
    if ($hasCadState -and $Health.cad_operation_active -and
        -not ($hasRecovery -and $Health.cad_recovery_required)) { return $true }
    if ($Health.runner.current -or $Health.runner.queued) { return $true }
    if ($Health.jobs -and ($Health.jobs.queued -or $Health.jobs.running)) { return $true }
    return $false
}

function Read-BundleVersion([string]$Path) {
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    try {
        $archive = [IO.Compression.ZipFile]::OpenRead($Path)
    } catch {
        Stop-WithError ("bundle is not a readable ZIP: $Path; download the release archive again " +
            "(the .NET error was: $($_.Exception.Message))")
    }
    try {
        $entry = $archive.Entries | Where-Object { $_.FullName -eq 'version.json' } | Select-Object -First 1
        if (-not $entry) { Stop-WithError 'bundle has no version.json' }
        $reader = New-Object IO.StreamReader($entry.Open())
        try { $payload = $reader.ReadToEnd() | ConvertFrom-Json } finally { $reader.Close() }
    } finally { $archive.Dispose() }
    if ($payload.version -notmatch '^\d+\.\d+\.\d+$') { Stop-WithError 'bundle needs a semantic version' }
    return $payload
}

function Install-Version(
        [string]$Zip, [string]$InstallRoot, [string]$Version, [string]$HostConfigPath, [string]$BundleSha256 = '') {
    $target = Join-Path $InstallRoot "versions\$Version"
    $incoming = ([string]$BundleSha256).Trim().ToLowerInvariant()
    if (Test-Path -LiteralPath $target) {
        # A failed activation leaves the new version installed but not active.  Retrying the same
        # bundle must be possible, and it must be possible *safely*: the directory is reused only
        # when the digest recorded at install time is the digest being applied now.
        $provenance = Join-Path $target 'bundle.sha256'
        $recorded = if (Test-Path -LiteralPath $provenance) {
            (Get-Content -LiteralPath $provenance -Raw -Encoding UTF8).Trim().ToLowerInvariant()
        } else { '' }
        if ($incoming -and $recorded -eq $incoming) {
            Write-Info "version $Version is already installed from this bundle; reusing $target"
            return $target
        }
        $why = if ($recorded) {
            "it was installed from a bundle with digest $recorded, not $incoming"
        } else {
            'no bundle digest was recorded for it, so this action cannot prove it is the same bytes'
        }
        Stop-WithError ("version already installed: $Version, and $why; verify that nothing runs from " +
            "$target, remove that directory, and run this action again - or install a newer version")
    }
    $partial = Join-Path $InstallRoot ("versions\." + $Version + "." + [guid]::NewGuid().ToString('N') + '.partial')
    New-Item -ItemType Directory -Force -Path $partial | Out-Null
    try {
        Expand-Archive -LiteralPath $Zip -DestinationPath $partial -Force
        $hostDestination = Join-Path $InstallRoot 'worker-host.json'
        if (-not [string]::Equals([IO.Path]::GetFullPath($HostConfigPath),
                [IO.Path]::GetFullPath($hostDestination), [StringComparison]::OrdinalIgnoreCase)) {
            Copy-Item -LiteralPath $HostConfigPath -Destination $hostDestination -Force
        }
        if ($incoming) { Set-Content -LiteralPath (Join-Path $partial 'bundle.sha256') -Value $incoming -Encoding ASCII }
        # Configuration errors must not leave a directory that looks installed.
        Move-Item -LiteralPath $partial -Destination $target
    } finally {
        if (Test-Path -LiteralPath $partial) { Remove-Item -LiteralPath $partial -Recurse -Force }
    }
    Write-Info "installed $Version -> $target"
    return $target
}

function Ensure-Venv([string]$InstallRoot, [string]$Python) {
    $venv = Join-Path $InstallRoot 'venv'
    if (Test-Path -LiteralPath (Join-Path $venv 'Scripts\python.exe')) { return $venv }
    $pythonPath = $Python
    if (-not $pythonPath) { $pythonPath = (Get-Command python -ErrorAction SilentlyContinue).Source }
    if (-not $pythonPath) { Stop-WithError 'no Python interpreter found; set "python" in worker-host.json' }
    & $pythonPath -c "import sys; assert sys.version_info[:2] == (3, 12), 'Worker requires CPython 3.12'"
    if ($LASTEXITCODE -ne 0) { Stop-WithError 'worker wheels require CPython 3.12' }
    Write-Step "creating venv from $pythonPath"
    & $pythonPath -m venv $venv
    if ($LASTEXITCODE -ne 0) { Stop-WithError "venv creation failed" }
    return $venv
}

function Install-OfflineWheels([string]$VersionDir, [string]$Venv) {
    $wheels = Join-Path $VersionDir 'wheels'
    if (-not (Test-Path -LiteralPath $wheels)) { Stop-WithError 'offline wheel set is missing from the bundle' }
    $python = Join-Path $Venv 'Scripts\python.exe'
    Write-Step 'installing offline dependencies'
    & $python -m pip install --no-index --find-links $wheels --disable-pip-version-check -r (Join-Path $VersionDir 'requirements.txt')
    if ($LASTEXITCODE -ne 0) { Stop-WithError 'offline dependency installation failed' }
}

function Test-VersionRuntimeReady([string]$VersionDir) {
    # A version is only reusable when the interpreter *and* the runtime entry Publish-VersionRuntime
    # writes are there: an install interrupted between them would otherwise be reused with no
    # `description.cmd`, and the operator's first command would fail with no explanation.
    $venv = Join-Path $VersionDir 'venv\Scripts'
    return (Test-Path -LiteralPath (Join-Path $venv 'python.exe')) -and
           (Test-Path -LiteralPath (Join-Path $venv 'description.cmd'))
}

function Install-WorkerVersion(
        [string]$Zip, $ConfigHost, [string]$Version, [string]$ConfigPath, [string]$BundleSha256 = '') {
    $target = Join-Path $ConfigHost.install_root "versions\$Version"
    $existed = Test-Path -LiteralPath $target
    $versionDir = Install-Version $Zip $ConfigHost.install_root $Version $ConfigPath $BundleSha256
    try {
        if ($existed -and (Test-VersionRuntimeReady $versionDir)) {
            Write-Info "runtime for $Version is already in place; reusing it"
            return $versionDir
        }
        $venv = Ensure-Venv $versionDir $ConfigHost.python
        Install-OfflineWheels $versionDir $venv
        Publish-VersionRuntime $versionDir $venv
    } catch {
        # Only a directory this attempt created may be removed: a reused installation is verified
        # evidence and must survive a transient dependency failure.
        if (-not $existed) { Remove-Item -LiteralPath $versionDir -Recurse -Force }
        throw
    }
    return $versionDir
}

function New-Runner([string]$InstallRoot, [string]$Version, $ConfigHost) {
    $versionDir = Join-Path $InstallRoot "versions\$Version"
    $python = Join-Path $versionDir 'venv\Scripts\python.exe'
    $cmd = Join-Path $versionDir 'run-worker.cmd'
    $lines = @(
        '@echo off',
        'chcp 65001 >nul',
        "set `"PYTHONPATH=$versionDir\src`"",
        "set `"DESCRIPTION_WORKER_JOBS=$($ConfigHost.jobs_root)`"",
        "`"$python`" -m description_pipeline.sources.solidworks.worker --serve --host $($ConfigHost.host) --port $($ConfigHost.port) --jobs-root `"$($ConfigHost.jobs_root)`"",
        'exit /b %ERRORLEVEL%'
    )
    [IO.File]::WriteAllLines($cmd, $lines, [Text.UTF8Encoding]::new($false))
    return $cmd
}

function Register-WorkerTask([string]$TaskName, [string]$Runner, $ConfigHost) {
    Write-Step "registering interactive task $TaskName"
    $action = New-ScheduledTaskAction -Execute $Runner -WorkingDirectory (Split-Path -Parent $Runner)
    $principal = New-ScheduledTaskPrincipal -UserId $ConfigHost.user -LogonType Interactive -RunLevel Limited
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $ConfigHost.user
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -MultipleInstances IgnoreNew -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) `
        -ExecutionTimeLimit ([TimeSpan]::Zero) -StartWhenAvailable
    try {
        Register-ScheduledTask -TaskName $TaskName -Action $action -Principal $principal `
            -Trigger $trigger -Settings $settings `
            -Description 'MimicVerse description pipeline SolidWorks worker' -Force | Out-Null
        return 'scheduled_task'
    } catch {
        # Registering a task needs elevation.  Without it, a logon entry in the
        # user's own Startup folder still starts the worker in the interactive
        # session, which is the property that actually matters here.
        Write-Info "task registration failed ($($_.Exception.Message)); using the Startup folder"
        $startup = [Environment]::GetFolderPath('Startup')
        $launcher = Join-Path $startup ($TaskName + '.cmd')
        [IO.File]::WriteAllLines($launcher, @("@echo off", "chcp 65001 >nul", "call `"$Runner`""), [Text.UTF8Encoding]::new($false))
        Write-Info "startup entry: $launcher"
        return 'startup_folder'
    }
}

function Set-CurrentVersion([string]$InstallRoot, [string]$Version, [string]$Mode, [string]$Previous) {
    $payload = [ordered]@{ version = $Version; previous_version = $Previous; mode = $Mode; switched_at = (Get-Date -Format o) }
    $temporary = Join-Path $InstallRoot 'current.tmp'
    $payload | ConvertTo-Json | Set-Content -LiteralPath $temporary -Encoding UTF8
    Move-Item -LiteralPath $temporary -Destination (Join-Path $InstallRoot 'current.json') -Force
}

function Get-CurrentMode([string]$InstallRoot) {
    $path = Join-Path $InstallRoot 'current.json'
    if (-not (Test-Path -LiteralPath $path)) { return $null }
    $payload = Get-Content -LiteralPath $path -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($payload.mode) { return $payload.mode }
    return 'scheduled_task'
}

function Get-CurrentVersion([string]$InstallRoot) {
    $path = Join-Path $InstallRoot 'current.json'
    if (-not (Test-Path -LiteralPath $path)) { return $null }
    return (Get-Content -LiteralPath $path -Raw -Encoding UTF8 | ConvertFrom-Json).version
}

function Get-InstalledVersions([string]$InstallRoot) {
    $root = Join-Path $InstallRoot 'versions'
    if (-not (Test-Path -LiteralPath $root)) { return @() }
    return Get-ChildItem -LiteralPath $root -Directory | Where-Object { $_.Name -match '^\d+\.\d+\.\d+$' } | Sort-Object { [version]$_.Name } -Descending | Select-Object -ExpandProperty Name
}

function Invoke-WorkerEndpoint($ConfigHost, [string]$Path, [string]$Method = 'GET', [int]$TimeoutSeconds = 20) {
    $url = "http://$($ConfigHost.host):$($ConfigHost.port)$Path"
    try {
        return Invoke-RestMethod -Method $Method -Uri $url -TimeoutSec $TimeoutSeconds
    } catch {
        return $null
    }
}

function Test-LocalPipeline([string]$Python) {
    # The single-machine entry runs the public pipeline (build + independent verification)
    # from this same runtime, so the installed venv must carry the consumer dependencies
    # (MuJoCo) and not only the collection worker's.
    if (-not (Test-Path -LiteralPath $Python)) {
        return @{ status = 'missing'; detail = 'runtime interpreter not installed' }
    }
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $probe = & $Python -c "import mujoco, description_pipeline; print(mujoco.__version__)" 2>&1 | Out-String
        $code = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previous
    }
    if ($code -ne 0) {
        return @{ status = 'missing'; detail = "mujoco is not importable in this runtime" }
    }
    return @{ status = 'ok'; detail = ('mujoco ' + $probe.Trim()) }
}

function Publish-VersionRuntime([string]$VersionDir, [string]$Venv) {
    # The installed runtime has to serve the public CLI outside run-worker.cmd: an author
    # runs `description model init`, tool locking and builds before any submission.  Point
    # the venv at this version's own src/ (no copy, so the packaged source identity stays
    # intact) and expose a console entry next to the venv's python.
    $source = Join-Path $VersionDir 'src'
    if (-not (Test-Path -LiteralPath $source)) { Stop-WithError "bundle has no src directory: $source" }
    $sitePackages = Join-Path $Venv 'Lib\site-packages'
    if (-not (Test-Path -LiteralPath $sitePackages)) { Stop-WithError "venv has no site-packages: $sitePackages" }
    $pth = Join-Path $sitePackages 'description_pipeline_source.pth'
    [IO.File]::WriteAllText($pth, ($source -replace '\\', '/') + "`n", [Text.UTF8Encoding]::new($false))
    $scriptDirectory = Join-Path $Venv 'Scripts'
    $shim = Join-Path $scriptDirectory 'description.cmd'
    $lines = @(
        '@echo off',
        'chcp 65001 >nul',
        '"%~dp0python.exe" -m description_pipeline %*',
        'exit /b %ERRORLEVEL%'
    )
    [IO.File]::WriteAllLines($shim, $lines, [Text.UTF8Encoding]::new($false))
    Write-Info "runtime entry: $shim"
}

function Wait-Worker($ConfigHost, [string]$ExpectedVersion, [int]$Seconds = 60) {
    $deadline = (Get-Date).AddSeconds($Seconds)
    while ((Get-Date) -lt $deadline) {
        $health = Invoke-WorkerEndpoint $ConfigHost '/health'
        if ($health) {
            if (-not $ExpectedVersion -or $health.worker_version -eq $ExpectedVersion) { return $health }
        }
        Start-Sleep -Seconds 2
    }
    return $null
}

function Get-TaskState([string]$TaskName) {
    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if (-not $task) { return 'not_registered' }
    return (Get-ScheduledTaskInfo -TaskName $TaskName).LastTaskResult.ToString() + '/' + $task.State
}

function Test-SolidWorksProcess {
    $process = Get-Process SLDWORKS -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $process) { return @{ running = $false } }
    return @{ running = $true; id = $process.Id; window = $process.MainWindowTitle }
}

function Get-DoctorAdvisory($Report) {
    # ``/doctor`` carries the notices the Python CLI prints - today ``cad_save_flag_set``,
    # the documents SolidWorks would prompt to save.  They are advisory: the collection
    # stays collectable.  A worker that answers without the field has no notices.
    if (-not $Report) { return @() }
    $property = $Report.PSObject.Properties['advisories']
    if (-not $property) { return @() }
    return @(@($property.Value) | Where-Object { $_ })
}

function Format-DoctorAdvisory($Report) {
    # The primary Windows entry renders the notices, because the first-use guide tells the
    # operator that Doctor reports them.
    $lines = @()
    foreach ($note in @(Get-DoctorAdvisory $Report)) {
        $text = if ($note.message) { $note.message } else { $note.code }
        $lines += ('notice    : {0}' -f $text)
        $documents = @($note.documents)
        foreach ($document in ($documents | Select-Object -First 5)) { $lines += ('            {0}' -f $document) }
        if ($documents.Count -gt 5) { $lines += ('            and {0} more' -f ($documents.Count - 5)) }
    }
    return $lines
}

function Invoke-Doctor($ConfigHost, [string]$Assembly) {
    $host_ = @{
        name = 'host'; status = 'passed'
        python = $PSVersionTable.PSVersion.ToString()
        os = [System.Environment]::OSVersion.VersionString
        session_id = (Get-Process -Id $PID).SessionId
    }
    $current = Get-CurrentVersion $ConfigHost.install_root
    $venv = Join-Path $ConfigHost.install_root "versions\$current\venv\Scripts\python.exe"
    $venvCheck = @{ name = 'venv'; status = if (Test-Path -LiteralPath $venv) { 'passed' } else { 'failed' }; path = $venv }
    $pipeline = Test-LocalPipeline $venv
    $sw = Test-SolidWorksProcess
    $solidworks = @{
        name = 'solidworks'
        status = if ($sw.running) { 'passed' } else { 'unavailable' }
        process_id = $sw.id
        window = $sw.window
        hint = if ($sw.running) { '' } else { 'open SolidWorks in the logged-in desktop session' }
    }
    $health = Invoke-WorkerEndpoint $ConfigHost '/health'
    $worker = @{
        name = 'worker'
        status = if ($health) { 'passed' } else { 'unavailable' }
        version = if ($health) { $health.worker_version } else { '' }
        task = Get-TaskState $ConfigHost.task_name
    }
    $collection = $null
    if ($Assembly -and $health) {
        $query = '/doctor?assembly=' + [uri]::EscapeDataString($Assembly)
        if ($ConfigHost.configuration) { $query += '&configuration=' + [uri]::EscapeDataString($ConfigHost.configuration) }
        $collection = Invoke-WorkerEndpoint $ConfigHost $query 'GET' 300
    }
    Write-Step 'doctor'
    Write-Info ("host      : {0}" -f $host_.os)
    Write-Info ("venv      : {0}" -f $venvCheck.status)
    Write-Info ("pipeline  : {0} {1}" -f $pipeline.status, $pipeline.detail)
    Write-Info ("solidworks: {0} {1}" -f $solidworks.status, $solidworks.window)
    Write-Info ("worker    : {0} version={1} task={2}" -f $worker.status, $worker.version, $worker.task)
    if ($collection) {
        Write-Info ("collection: install={0} worker={1} solidworks={2} collectable={3}" -f `
            $collection.installed, $collection.worker_alive, $collection.solidworks_reachable, $collection.cad_collectable)
        foreach ($line in @(Format-DoctorAdvisory $collection)) { Write-Info $line }
    } else {
        Write-Info 'collection: not_run (worker unavailable or no -Assembly given)'
    }
    return @{
        installed = ($venvCheck.status -eq 'passed')
        worker_alive = ($worker.status -eq 'passed')
        local_pipeline = ($pipeline.status -eq 'ok')
        solidworks_reachable = [bool]($collection -and $collection.solidworks_reachable)
        cad_collectable = [bool]($collection -and $collection.cad_collectable)
        advisories = @(Get-DoctorAdvisory $collection)
        checks = @($host_, $venvCheck, $solidworks, $worker)
    }
}

function Start-Worker([string]$TaskName, $ConfigHost, [string]$ExpectedVersion, [string]$Mode) {
    Write-Step "starting task $TaskName"
    $existing = Invoke-WorkerEndpoint $ConfigHost '/health'
    if ($existing) {
        if ($existing.worker_version -ne $ExpectedVersion) { Stop-WithError 'another version is still running' }
        if ($existing.maintenance) { Invoke-WorkerEndpoint $ConfigHost '/resume' 'POST' | Out-Null }
        return $existing
    }
    if ($Mode -eq 'startup_folder') {
        if ((Get-Process -Id $PID).SessionId -eq 0 -or -not [Environment]::UserInteractive) {
            Stop-WithError 'Startup mode requires running Start/Update/Rollback from the logged-in desktop'
        }
        $runner = Join-Path $ConfigHost.install_root "versions\$ExpectedVersion\run-worker.cmd"
        Start-Process -FilePath $env:ComSpec -ArgumentList @('/c', "`"$runner`"") -WindowStyle Minimized | Out-Null
    } else {
        Start-ScheduledTask -TaskName $TaskName
    }
    $health = Wait-Worker $ConfigHost $ExpectedVersion 90
    if (-not $health) { Stop-WithError 'worker did not answer on /health; run -Action Doctor' }
    Write-Info ("worker alive: version={0} pid={1}" -f $health.worker_version, $health.pid)
    return $health
}

function Stop-Worker($ConfigHost) {
    function Test-InstalledWorkerProcess($Process, $Host_, [string]$Version) {
        # Python's venv launcher re-executes the base interpreter, so a legitimate worker may
        # report the base python.exe as its executable path.  Accept it only when the command
        # line also proves it is this worker on this jobs root; everything else is refused.
        if (-not $Process) { return $false }
        $expected = Join-Path $Host_.install_root "versions\$Version\venv\Scripts\python.exe"
        $sameInterpreter = [string]$Process.ExecutablePath -ieq $expected
        $commandLine = [string]$Process.CommandLine
        $ourCommandLine = ($commandLine -like '*description_pipeline.sources.solidworks.worker*') -and
                          ($commandLine -like '*--jobs-root*') -and
                          ($commandLine -like "*$($Host_.jobs_root)*")
        return ($sameInterpreter -or $ourCommandLine)
    }
    function Get-EndpointOwner($Host_) {
        # The process that holds the port is the authority: /health.pid can lag behind a
        # restart, and killing the wrong process is exactly what this check must prevent.
        $connection = Get-NetTCPConnection -LocalPort ([int]$Host_.port) -State Listen -ErrorAction SilentlyContinue |
            Select-Object -First 1
        if (-not $connection) { return $null }
        return Get-CimInstance Win32_Process -Filter "ProcessId=$($connection.OwningProcess)"
    }
    $health = Invoke-WorkerEndpoint $ConfigHost '/health'
    if (-not $health) {
        # An unavailable endpoint is not evidence that a worker is safe to kill.
        $active = Get-CimInstance Win32_Process | Where-Object {
            $_.CommandLine -like '*description_pipeline.sources.solidworks.worker*' -and
            ($_.ExecutablePath -like "$($ConfigHost.install_root)\versions\*\venv\Scripts\python.exe" -or
             ($_.CommandLine -like '*--jobs-root*' -and $_.CommandLine -like "*$($ConfigHost.jobs_root)*"))
        }
        if ($active) { Stop-WithError 'worker process exists but cannot prove it is idle; run Doctor from the desktop' }
        return
    }
    if (Test-WorkerBusy $health) { Stop-WithError 'worker has active or queued work; wait before stopping' }
    $process = Get-CimInstance Win32_Process -Filter "ProcessId=$($health.pid)"
    if (-not (Test-InstalledWorkerProcess $process $ConfigHost $health.worker_version)) {
        Stop-WithError 'refusing to stop a process outside this versioned worker installation'
    }
    $targets = @($process)
    $owner = Get-EndpointOwner $ConfigHost
    if ($owner -and $owner.ProcessId -ne $process.ProcessId) {
        if (-not (Test-InstalledWorkerProcess $owner $ConfigHost $health.worker_version)) {
            Stop-WithError (
                "the endpoint on port $($ConfigHost.port) is served by pid $($owner.ProcessId) " +
                "($($owner.ExecutablePath)), which does not look like this installation; " +
                "verify its command line before stopping anything")
        }
        $targets += $owner
    }
    $paused = Invoke-WorkerEndpoint $ConfigHost '/maintenance' 'POST'
    if (-not $paused -or -not $paused.maintenance) { Stop-WithError 'worker could not enter idle maintenance state' }
    if ((Get-CurrentMode $ConfigHost.install_root) -eq 'scheduled_task') {
        Stop-ScheduledTask -TaskName $ConfigHost.task_name -ErrorAction SilentlyContinue
    }
    # Only verified worker PIDs may be terminated; SLDWORKS is never targeted.
    foreach ($target in $targets) {
        if (Get-Process -Id $target.ProcessId -ErrorAction SilentlyContinue) { Stop-Process -Id $target.ProcessId }
    }
    # Terminating a process is not the same as the port being free; give it a bounded moment
    # instead of failing on the first look, which is how a healthy worker looked unstoppable.
    foreach ($attempt in 1..20) {
        if (-not (Invoke-WorkerEndpoint $ConfigHost '/health')) {
            Write-Info 'verified idle worker stopped; SolidWorks left running'
            return
        }
        Start-Sleep -Milliseconds 500
    }
    Invoke-WorkerEndpoint $ConfigHost '/resume' 'POST' | Out-Null
    $still = Get-EndpointOwner $ConfigHost
    $detail = if ($still) {
        "pid $($still.ProcessId) ($($still.ExecutablePath)): $($still.CommandLine)"
    } else {
        'the endpoint answers but no listening process could be identified'
    }
    Stop-WithError (
        "worker still answers after stop: $detail; " +
        'stop that process from the logged-in desktop session after verifying its command line, then run this action again')
}

function Activate-Version($ConfigHost, [string]$Version) {
    $previous = Get-CurrentVersion $ConfigHost.install_root
    if ($previous -eq $Version) { $previous = $null }  # re-applying a version has no fallback to point at
    Stop-Worker $ConfigHost
    try {
        $runner = New-Runner $ConfigHost.install_root $Version $ConfigHost
        $mode = Register-WorkerTask $ConfigHost.task_name $runner $ConfigHost
        Start-Worker $ConfigHost.task_name $ConfigHost $Version $mode | Out-Null
        Set-CurrentVersion $ConfigHost.install_root $Version $mode $previous
    } catch {
        $failure = $_
        if ($previous) {
            Stop-Worker $ConfigHost
            $oldRunner = New-Runner $ConfigHost.install_root $previous $ConfigHost
            $oldMode = Register-WorkerTask $ConfigHost.task_name $oldRunner $ConfigHost
            Start-Worker $ConfigHost.task_name $ConfigHost $previous $oldMode | Out-Null
            Write-Info "restored previous worker $previous after activation failure"
        }
        throw $failure
    }
}

function Prune-Versions([string]$InstallRoot, [int]$Keep, [string]$Current) {
    $versions = Get-InstalledVersions $InstallRoot
    $state = Get-Content (Join-Path $InstallRoot 'current.json') -Raw -Encoding UTF8 | ConvertFrom-Json
    $Keep = [Math]::Max(2, $Keep)
    $index = 0
    foreach ($version in $versions) {
        $index++
        if ($index -le $Keep -or $version -eq $Current -or $version -eq $state.previous_version) { continue }
        $path = Join-Path $InstallRoot "versions\$version"
        Write-Info "pruning old version $version"
        Remove-Item -LiteralPath $path -Recurse -Force
    }
}

# ---------------------------------------------------------------------------

if (-not $Config) { $Config = Join-Path $PSScriptRoot 'worker-host.json' }

# Setup runs on a machine that has no worker-host.json yet - that is the whole point of it - so it
# cannot load a configuration or take the deployment lock the other actions use.  It writes the
# file and then hands over to Install and Doctor in child processes, passing their exit codes on.
if ($Action -eq 'Setup') {
    if (-not $Bundle) { Stop-WithError '-Bundle is required for Setup' }
    $setup = New-HostConfig -Path $Config -Bundle $Bundle -BundleSha256 $BundleSha256 -InstallRoot $InstallRoot `
        -Python $Python -Assembly $Assembly -AssemblyConfiguration $AssemblyConfiguration `
        -Port $Port -TaskName $TaskName -Force:$Force
    if ($NoInstall) {
        Write-Info "next: powershell -File .\worker.ps1 -Action Install -Bundle $Bundle -Config $Config"
        exit 0
    }
    $powershell = Get-PowerShellPath
    $worker = if ($PSCommandPath) { $PSCommandPath } else { Join-Path $PSScriptRoot 'worker.ps1' }
    $current = Get-CurrentVersion $setup.install_root
    $installedConfig = Join-Path $setup.install_root 'worker-host.json'
    if ($current -and (Test-Path -LiteralPath (Join-Path $setup.install_root "versions\$current")) -and
        (Test-SameConfig $Config $installedConfig)) {
        # Nothing to install and nothing to reconfigure: Setup can be repeated as often as wanted.
        Write-Info "version $current is already installed with these settings; skipping Install"
    } else {
        Write-InstalledEndpoint $installedConfig $setup
        & $powershell -NoProfile -ExecutionPolicy Bypass -File $worker -Action Install -Bundle $Bundle -Config $Config
        if ($LASTEXITCODE -ne 0) {
            Write-Info "install failed with exit code $LASTEXITCODE; fix the error above and run the same command again"
            exit $LASTEXITCODE
        }
    }
    if (-not $Assembly) {
        Write-Info "next: powershell -File .\worker.ps1 -Action Doctor -Config $Config"
        exit 0
    }
    & $powershell -NoProfile -ExecutionPolicy Bypass -File $worker -Action Doctor -Config $Config
    exit $LASTEXITCODE
}

$host_ = Get-HostConfig $Config
New-Item -ItemType Directory -Force -Path $host_.install_root | Out-Null
$deploymentLock = [IO.File]::Open((Join-Path $host_.install_root 'deployment.lock'), 'OpenOrCreate', 'ReadWrite', 'None')

try {
switch ($Action) {
    'Install' {
        if (-not $Bundle) { Stop-WithError '-Bundle is required for Install' }
        $expected = if ($BundleSha256) { $BundleSha256 } else { $host_.bundle_sha256 }
        Assert-Bundle $Bundle $expected | Out-Null
        $payload = Read-BundleVersion $Bundle
        $versionDir = Join-Path $host_.install_root "versions\$($payload.version)"
        if (Test-Path -LiteralPath $versionDir) {
            # Repeating Install applies the configuration in use to the installed version.  The
            # files themselves are never overwritten, so a rebuilt bundle needs a new version.
            $installedConfig = Join-Path $host_.install_root 'worker-host.json'
            $installedDigest = if (Test-Path -LiteralPath $installedConfig) {
                (Get-HostConfig $installedConfig).bundle_sha256
            } else {
                $null
            }
            if ($installedDigest -and $installedDigest -ne $expected) {
                Stop-WithError ("version $($payload.version) is already installed from a bundle with digest " +
                    "$installedDigest, not $expected; use -Action Update with a new version for a different build")
            }
            Write-Info "version $($payload.version) is already installed; applying the configuration"
            Activate-Version $host_ $payload.version
        } else {
            New-Item -ItemType Directory -Force -Path $host_.install_root | Out-Null
            Install-WorkerVersion $Bundle $host_ $payload.version $Config $expected | Out-Null
            Activate-Version $host_ $payload.version
            Write-Info "installed version $($payload.version)"
            Prune-Versions $host_.install_root $host_.keep_versions $payload.version
        }
        Write-Info 'install complete'
    }
    'Start' {
        $current = Get-CurrentVersion $host_.install_root
        if (-not $current) { Stop-WithError 'nothing installed; run -Action Install first' }
        $mode = Get-CurrentMode $host_.install_root
        Start-Worker $host_.task_name $host_ $current $mode | Out-Null
    }
    'Stop' {
        Stop-Worker $host_
    }
    'Status' {
        Write-Step 'status'
        Write-Info ("installed versions: {0}" -f ((Get-InstalledVersions $host_.install_root) -join ', '))
        $current = Get-CurrentVersion $host_.install_root
        Write-Info ("current version   : {0}" -f $current)
        Write-Info ("start mode        : {0}" -f (Get-CurrentMode $host_.install_root))
        Write-Info ("task              : {0}" -f (Get-TaskState $host_.task_name))
        if ($current) {
            $runtime = Join-Path $host_.install_root "versions\$current\venv\Scripts\python.exe"
            Write-Info ("runtime python    : {0}" -f $runtime)
            Write-Info ("runtime entry     : {0}" -f (Join-Path $host_.install_root "versions\$current\venv\Scripts\description.cmd"))
            $pipeline = Test-LocalPipeline $runtime
            Write-Info ("local pipeline    : {0} {1}" -f $pipeline.status, $pipeline.detail)
        }
        $health = Invoke-WorkerEndpoint $host_ '/health'
        Write-Info ("worker            : {0}" -f $(if ($health) { $health.worker_version } else { 'unavailable' }))
    }
    'Doctor' {
        $report = Invoke-Doctor $host_ $host_.assembly
        if (-not $report.installed) { exit 1 }
        if (-not $report.worker_alive) { exit 1 }
        if ($host_.assembly -and -not $report.cad_collectable) { exit 2 }
    }
    'Update' {
        if (-not $Bundle) { Stop-WithError '-Bundle is required for Update' }
        # ``worker-host.json`` records the digest of the version that is installed, so using it here
        # would refuse every update: a different version is a different archive.  The expectation for
        # the new archive comes from its own release instead.
        $expected = Resolve-BundleDigest $Bundle $BundleSha256 -Strict
        $payload = Read-BundleVersion $Bundle
        $running = Invoke-WorkerEndpoint $host_ '/health'
        if (Test-WorkerBusy $running) { Stop-WithError 'worker has active or queued work; update when it is idle' }
        Install-WorkerVersion $Bundle $host_ $payload.version $Config $expected | Out-Null
        Activate-Version $host_ $payload.version
        Prune-Versions $host_.install_root $host_.keep_versions $payload.version
        Write-Info "updated to $($payload.version)"
    }
    'Rollback' {
        $current = Get-CurrentVersion $host_.install_root
        $state = Get-Content (Join-Path $host_.install_root 'current.json') -Raw -Encoding UTF8 | ConvertFrom-Json
        $previous = $state.previous_version
        if (-not $previous) { Stop-WithError 'no other version is installed to roll back to' }
        $running = Invoke-WorkerEndpoint $host_ '/health'
        if (Test-WorkerBusy $running) { Stop-WithError 'worker has active or queued work; roll back when it is idle' }
        Activate-Version $host_ $previous
        Write-Info "rolled back to $previous"
    }
}
} finally { $deploymentLock.Dispose() }
