<#
.SYNOPSIS
    One command on Windows: verify locally and submit the model candidate as a pull request.

.DESCRIPTION
    Local mode is the default: the public pipeline runs on this machine with the runtime the
    worker installer already created, and the model's own `source` block decides where the
    data comes from - a loopback SolidWorks worker, the Onshape API, or an existing frozen
    snapshot.  This launcher never rewrites `source`.

        powershell -ExecutionPolicy Bypass -File .\submit.ps1

    submit-host.json next to this script (-Config overrides it):

        local : model_root (Windows path), profile, message, and optionally
                python (explicit interpreter) or install_root (installed worker root).
        remote: build_host (ssh alias), remote_python, model_root (build host path),
                profile, message, worker_port, remote_port, identity_file.

    A non-empty build_host selects remote mode and keeps the previous behaviour: one ssh
    session opens the reverse tunnel and runs the same command on the build host.  In local
    mode the installed runtime is <install_root>\versions\<current>\venv\Scripts\python.exe,
    Git and the GitHub CLI must be reachable (PATH or their default install location) and
    `git lfs version` must work.  The commit message travels on stdin.

.NOTES
    Config defaults to submit-host.json next to this script and holds no secrets.  In remote
    mode set identity_file to use a dedicated OpenSSH identity.
#>

[CmdletBinding()]
param(
    [string]$Config,
    [string]$Message,
    [string]$ModelRoot,
    [string]$Profile,
    [string]$MechanicalReference,
    [switch]$DescribeOnly
)

$ErrorActionPreference = 'Stop'

function Write-Step([string]$Text) { Write-Host "== $Text" }
function Write-Info([string]$Text) { Write-Host "   $Text" }
function Stop-WithError([string]$Text) { throw $Text }

function Quote-PosixArg([string]$Name, [string]$Value) {
    # POSIX single quoting only needs to reject the quote itself and line breaks.
    if ([string]::IsNullOrWhiteSpace($Value)) { Stop-WithError "$Name must not be empty" }
    if ($Value -match "['`r`n]") { Stop-WithError "$Name must not contain a quote or a line break" }
    return "'" + $Value + "'"
}

function Assert-AbsolutePosixPath([string]$Name, [string]$Value) {
    if (-not $Value.StartsWith('/')) { Stop-WithError "$Name must be an absolute path on the build host" }
    return $Value
}

function Assert-AbsoluteWindowsPath([string]$Name, [string]$Value) {
    if ([string]::IsNullOrWhiteSpace($Value)) { Stop-WithError "$Name must not be empty" }
    if ($Value -notmatch '^[A-Za-z]:[\\/]' -and -not $Value.StartsWith('\\')) {
        Stop-WithError "$Name must be an absolute Windows path (drive letter or UNC)"
    }
    return $Value
}

function Assert-SshHostName([string]$Name, [string]$Value) {
    # The host is a positional ssh argument, so an option-like value (for example
    # -oProxyCommand=...) must never reach the command line.
    if ($Value -notmatch '^[A-Za-z0-9][A-Za-z0-9._-]*$') {
        Stop-WithError "$Name must be a plain ssh host or alias (letters, digits, dot, dash, underscore)"
    }
    return $Value
}

function Assert-ProfileName([string]$Value) {
    # Same shape the CLI accepts, so a bad name fails before anything runs.
    # -cnotmatch: PowerShell's default matching ignores case and would accept "Kinematics".
    if ($Value -cnotmatch '^[a-z][a-z0-9_-]{0,63}$') {
        Stop-WithError "profile must match ^[a-z][a-z0-9_-]{0,63}$ (got '$Value')"
    }
    return $Value
}

function Get-DefaultInstallRoot {
    $hostConfig = Join-Path $PSScriptRoot 'worker-host.json'
    if (Test-Path -LiteralPath $hostConfig) {
        $payload = Get-Content -LiteralPath $hostConfig -Raw -Encoding UTF8 | ConvertFrom-Json
        if ($payload.install_root) { return [string]$payload.install_root }
    }
    return (Join-Path $env:LOCALAPPDATA 'DescriptionWorker')
}

function Read-HostConfig([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path)) { Stop-WithError "submit config not found: $Path" }
    $config = Get-Content -LiteralPath $Path -Raw -Encoding UTF8 | ConvertFrom-Json
    foreach ($key in @('model_root', 'profile')) {
        if (-not $config.$key) { Stop-WithError "submit-host.json needs '$key'" }
    }
    $config.profile = Assert-ProfileName ([string]$config.profile)
    if ($config.message -and $config.message -isnot [string]) { Stop-WithError "'message' must be a string" }
    if ($config.python) {
        $runtime = [Environment]::ExpandEnvironmentVariables([string]$config.python)
        if (-not (Test-Path -LiteralPath $runtime)) { Stop-WithError "python does not exist: $runtime" }
        $config.python = $runtime
    }
    if ($config.install_root) {
        $config.install_root = [Environment]::ExpandEnvironmentVariables([string]$config.install_root)
    }
    if ($config.build_host) {
        # Remote mode: model_root names a path on the build host.
        foreach ($key in @('remote_python', 'remote_port')) {
            if (-not $config.$key) { Stop-WithError "submit-host.json needs '$key' for remote mode" }
        }
        $config.build_host = Assert-SshHostName 'build_host' ([string]$config.build_host)
        Assert-AbsolutePosixPath 'remote_python' ([string]$config.remote_python) | Out-Null
        Assert-AbsolutePosixPath 'model_root' ([string]$config.model_root) | Out-Null
        if ($config.identity_file) {
            # values like %USERPROFILE% are expanded here; Test-Path itself does not expand them
            $config.identity_file = [Environment]::ExpandEnvironmentVariables([string]$config.identity_file)
            if (-not (Test-Path -LiteralPath $config.identity_file)) {
                Stop-WithError "identity_file does not exist: $($config.identity_file)"
            }
        }
    } else {
        # Local mode: the pipeline runs here and the model keeps its own source block.
        $config.model_root = Assert-AbsoluteWindowsPath 'model_root' ([string]$config.model_root)
    }
    foreach ($key in @('worker_port', 'remote_port')) {
        if (-not $config.$key) { continue }
        $port = 0
        if (-not [int]::TryParse([string]$config.$key, [ref]$port) -or $port -lt 1 -or $port -gt 65535) {
            Stop-WithError "submit-host.json '$key' must be a TCP port"
        }
    }
    return $config
}

function Test-RemoteMode($ConfigHost) { return [bool]$ConfigHost.build_host }

function Test-WorkerHealth($ConfigHost, [string]$BaseUrl = 'http://127.0.0.1') {
    $url = "$BaseUrl`:$($ConfigHost.worker_port)/health"
    try {
        $health = Invoke-RestMethod -Method GET -Uri $url -TimeoutSec 10
    } catch {
        Stop-WithError "worker health unreachable at $url ($($_.Exception.Message))"
    }
    $busy = $health.runner.current -or $health.runner.queued -or
        ($health.jobs.queued -gt 0) -or ($health.jobs.running -gt 0)
    $problems = @()
    if ($health.status -ne 'ok') { $problems += "status=$($health.status)" }
    if ($health.maintenance) { $problems += 'maintenance' }
    if ($health.cad_recovery_required) { $problems += 'cad_recovery_required' }
    if ($health.runner.alive -eq $false) { $problems += 'runner_alive=false' }
    if ($busy) { $problems += 'active_or_queued_work' }
    if ($problems.Count) { Stop-WithError "worker is not ready for a clean run: $($problems -join ', ')" }
    return $health
}

function Get-Message($ConfigHost, [string]$Override) {
    $text = if ($Override) { $Override } elseif ($ConfigHost.message) { [string]$ConfigHost.message } else { '' }
    if (-not $text.Trim()) { Stop-WithError 'no commit message: pass -Message or set "message" in submit-host.json' }
    return $text
}

function Resolve-Plan($ConfigHost, [string]$ModelRoot, [string]$Profile, [string]$MechanicalReference) {
    # Command-line overrides are validated exactly like the configuration file.
    $root = if ($ModelRoot) { [string]$ModelRoot } else { [string]$ConfigHost.model_root }
    $name = if ($Profile) { [string]$Profile } else { [string]$ConfigHost.profile }
    if (Test-RemoteMode $ConfigHost) {
        Assert-AbsolutePosixPath 'model_root' $root | Out-Null
    } else {
        Assert-AbsoluteWindowsPath 'model_root' $root | Out-Null
    }
    Assert-ProfileName $name | Out-Null
    $reference = if ($MechanicalReference) { $MechanicalReference } else { [string]$ConfigHost.mechanical_reference }
    if ($reference) {
        if (Test-RemoteMode $ConfigHost) {
            Assert-AbsolutePosixPath 'mechanical_reference' $reference | Out-Null
        } else {
            $reference = [Environment]::ExpandEnvironmentVariables($reference)
            Assert-AbsoluteWindowsPath 'mechanical_reference' $reference | Out-Null
        }
    }
    return [pscustomobject]@{ model_root = $root; profile = $name; mechanical_reference = $reference }
}

function Resolve-LocalRuntime($ConfigHost) {
    # An explicit interpreter wins; otherwise use the runtime the installer created.
    if ($ConfigHost.python) { return [string]$ConfigHost.python }
    $installRoot = if ($ConfigHost.install_root) { [string]$ConfigHost.install_root } else { Get-DefaultInstallRoot }
    $current = Join-Path $installRoot 'current.json'
    if (-not (Test-Path -LiteralPath $current)) {
        Stop-WithError ("no installed runtime at $installRoot; install it once with " +
            "worker.ps1 -Action Install -Bundle <zip> -Config <worker-host.json>, " +
            "or set 'python' in submit-host.json")
    }
    $version = [string](Get-Content -LiteralPath $current -Raw -Encoding UTF8 | ConvertFrom-Json).version
    if (-not $version) { Stop-WithError "no current version recorded in $current" }
    $python = Join-Path $installRoot "versions\$version\venv\Scripts\python.exe"
    if (-not (Test-Path -LiteralPath $python)) {
        Stop-WithError "runtime interpreter missing: $python; run worker.ps1 -Action Install (or -Action Update)"
    }
    return $python
}

function Find-Executable([string]$Name, [string[]]$Candidates) {
    $command = Get-Command $Name -ErrorAction SilentlyContinue
    if ($command -and $command.Source) { return [string]$command.Source }
    foreach ($candidate in $Candidates) {
        $expanded = [Environment]::ExpandEnvironmentVariables($candidate)
        if ($expanded -match '[*?]') {
            # Versioned portable installs (for example %LOCALAPPDATA%\description-tools\git-<version>)
            $match = Get-Item -Path $expanded -ErrorAction SilentlyContinue | Select-Object -First 1
            if ($match) { return $match.FullName }
            continue
        }
        if (Test-Path -LiteralPath $expanded) { return $expanded }
    }
    return $null
}

function Initialize-LocalTools {
    # The pipeline shells out to git, git-lfs and gh.  A fresh machine often has them
    # installed outside PATH, so look in the default locations and make them visible to the
    # child process before running the update.
    $git = Find-Executable 'git' @(
        '%ProgramFiles%\Git\cmd\git.exe',
        '%ProgramFiles(x86)%\Git\cmd\git.exe',
        '%LOCALAPPDATA%\Programs\Git\cmd\git.exe',
        '%ProgramFiles%\Git\bin\git.exe',
        '%LOCALAPPDATA%\description-tools\git-*\cmd\git.exe'
    )
    if (-not $git) {
        Stop-WithError ('Git is required: install Git for Windows, then rerun. ' +
            'No git.exe on PATH or in the default install locations.')
    }
    $gh = Find-Executable 'gh' @(
        '%ProgramFiles%\GitHub CLI\gh.exe',
        '%ProgramFiles(x86)%\GitHub CLI\gh.exe',
        '%LOCALAPPDATA%\Programs\GitHub CLI\gh.exe',
        '%LOCALAPPDATA%\description-tools\gh-*\bin\gh.exe'
    )
    if (-not $gh) {
        Stop-WithError ('GitHub CLI (gh) is required: install it and run "gh auth login", then rerun. ' +
            'No gh.exe on PATH or in the default install locations.')
    }
    $lfs = Find-Executable 'git-lfs' @(
        '%ProgramFiles%\Git\mingw64\bin\git-lfs.exe',
        '%ProgramFiles(x86)%\Git\mingw64\bin\git-lfs.exe',
        '%LOCALAPPDATA%\Programs\Git\mingw64\bin\git-lfs.exe',
        '%LOCALAPPDATA%\description-tools\git-*\mingw64\bin\git-lfs.exe'
    )
    $directories = @((Split-Path -Parent $git), (Split-Path -Parent $gh))
    if ($lfs) { $directories += (Split-Path -Parent $lfs) }
    $env:PATH = (($directories | Select-Object -Unique) -join ';') + ';' + $env:PATH
    $probe = Invoke-NativeCommand -FilePath $git -Arguments @('lfs', 'version') -InputText ''
    if ($probe.exit_code -ne 0) {
        $detail = if ($probe.stderr) { $probe.stderr } else { $probe.stdout }
        Stop-WithError ("Git LFS is required but 'git lfs version' failed: " + $detail.Trim())
    }
    return [pscustomobject]@{ git = $git; gh = $gh; git_lfs = $probe.stdout.Trim() }
}

function Build-RemoteCommand($ConfigHost, [string]$ModelRoot, [string]$Profile, [string]$MechanicalReference) {
    $parts = @(
        (Quote-PosixArg 'remote_python' ([string]$ConfigHost.remote_python)),
        '-m description_pipeline model update',
        '--root', (Quote-PosixArg 'model_root' $ModelRoot),
        '--profile', (Quote-PosixArg 'profile' $Profile),
        '--message-file -',
        '--expect-worker-url', (Quote-PosixArg 'tunnel url' "http://127.0.0.1:$($ConfigHost.remote_port)")
    )
    if ($MechanicalReference) {
        $parts += @('--mechanical-reference', (Quote-PosixArg 'mechanical_reference' $MechanicalReference))
    }
    return ($parts -join ' ')
}

function Build-LocalArguments([string]$ModelRoot, [string]$Profile, [string]$MechanicalReference) {
    # Same public command as the remote path.  The model's own `source` block selects the
    # capture endpoint, so the launcher passes neither --expect-worker-url nor a worker host.
    $parts = @('-m', 'description_pipeline', 'model', 'update', '--root', $ModelRoot,
        '--profile', $Profile, '--message-file', '-')
    if ($MechanicalReference) { $parts += @('--mechanical-reference', $MechanicalReference) }
    return $parts
}

function Invoke-NativeCommand([string]$FilePath, [string[]]$Arguments, [string]$InputText) {
    # Run one native process with $InputText on its stdin, and keep stdout, stderr and the
    # exit code apart.  PowerShell 5.1 only forwards a pipeline into a native process when
    # that process is the pipeline target itself, so the native call happens right here.
    $previousConsole = [Console]::OutputEncoding
    $previousOutput = $global:OutputEncoding
    $previousPreference = $ErrorActionPreference
    $utf8 = New-Object System.Text.UTF8Encoding $false
    try {
        [Console]::OutputEncoding = $utf8
        $global:OutputEncoding = $utf8
        $ErrorActionPreference = 'Continue'
        $global:LASTEXITCODE = 0
        $raw = @($InputText | & $FilePath @Arguments 2>&1)
        $code = $global:LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previousPreference
        [Console]::OutputEncoding = $previousConsole
        $global:OutputEncoding = $previousOutput
    }
    $stdout = @()
    $stderr = @()
    foreach ($item in $raw) {
        if ($item -is [System.Management.Automation.ErrorRecord]) {
            if ($item.Exception -is [System.Management.Automation.CommandNotFoundException]) {
                Stop-WithError "cannot run '$FilePath': $($item.Exception.Message)"
            }
            $stderr += $item.ToString()
        } else {
            $stdout += [string]$item
        }
    }
    return [pscustomobject]@{
        exit_code = [int]$code
        stdout    = ($stdout -join "`n")
        stderr    = ($stderr -join "`n")
    }
}

function Invoke-LocalUpdate([string]$RuntimePython, [string]$ModelRoot, [string]$Profile, [string]$MessageText, [string]$MechanicalReference) {
    $arguments = Build-LocalArguments $ModelRoot $Profile $MechanicalReference
    return (Invoke-NativeCommand -FilePath $RuntimePython -Arguments $arguments -InputText $MessageText)
}

function Invoke-RemoteUpdate($ConfigHost, [string]$RemoteCommand, [string]$MessageText) {
    $arguments = @(
        '-T', '-o', 'BatchMode=yes', '-o', 'ExitOnForwardFailure=yes', '-o', 'ConnectTimeout=10',
        '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=4',
        '-R', "127.0.0.1:$($ConfigHost.remote_port):127.0.0.1:$($ConfigHost.worker_port)",
        [string]$ConfigHost.build_host, $RemoteCommand
    )
    if ($ConfigHost.identity_file) {
        $arguments = @('-i', [string]$ConfigHost.identity_file, '-o', 'IdentitiesOnly=yes') + $arguments
    }
    return (Invoke-NativeCommand -FilePath 'ssh' -Arguments $arguments -InputText $MessageText)
}

function ConvertFrom-Record([string]$Text) {
    # The record shares its stream with log lines, and lines can follow it (a warning
    # written to stderr, for instance).  The whole text is tried first; after that the CLI
    # writes its record at column 0, so objects that start a line are preferred over
    # anything indented inside them.
    if ($null -eq $Text) { return $null }
    $trimmed = $Text.Trim()
    if (-not $trimmed) { return $null }
    try { return ($trimmed | ConvertFrom-Json) } catch { }
    $atLineStart = @()
    $elsewhere = @()
    for ($i = 0; $i -lt $trimmed.Length; $i++) {
        if ($trimmed[$i] -ne '{') { continue }
        if ($i -eq 0 -or $trimmed[$i - 1] -eq "`n") { $atLineStart += $i } else { $elsewhere += $i }
    }
    foreach ($start in (@($atLineStart) + @($elsewhere))) {
        $depth = 0
        $inString = $false
        $escaped = $false
        $end = -1
        for ($j = $start; $j -lt $trimmed.Length; $j++) {
            $character = $trimmed[$j]
            if ($inString) {
                if ($escaped) { $escaped = $false }
                elseif ($character -eq '\') { $escaped = $true }
                elseif ($character -eq '"') { $inString = $false }
                continue
            }
            if ($character -eq '"') { $inString = $true; continue }
            if ($character -eq '{') { $depth++ }
            elseif ($character -eq '}') { $depth--; if ($depth -eq 0) { $end = $j; break } }
        }
        if ($end -lt 0) { continue }
        $candidate = $trimmed.Substring($start, $end - $start + 1)
        try { return ($candidate | ConvertFrom-Json) } catch { }
    }
    return $null
}

function Show-RemoteResult([string]$StdOut, [string]$StdErr, [int]$ExitCode) {
    if ($StdOut) { Write-Output $StdOut }
    if ($StdErr) { Write-Output $StdErr }
    Write-Step 'result'
    $record = ConvertFrom-Record $StdOut
    if ($null -eq $record) {
        # A refusal is reported as a JSON record on stderr; anything else leaves the review
        # state unknown, and an exit code alone is never success.
        $failure = ConvertFrom-Record $StdErr
        if ($null -ne $failure) {
            $detail = if ($failure.message) { [string]$failure.message } else { [string]$failure.error }
            Stop-WithError "model update refused the change (exit $ExitCode): $detail"
        }
        Stop-WithError "model update returned no parsable JSON record (exit $ExitCode); review state is unknown"
    }
    if ($record.error) {
        $detail = if ($record.message) { [string]$record.message } else { [string]$record.error }
        Stop-WithError "model update reported $($record.error): $detail"
    }
    $pullRequest = [string]$record.pull_request
    if ($pullRequest) { Write-Info "pull request : $pullRequest" }
    if ($record.central_validation -and $record.central_validation.state -ne 'not_requested') {
        Write-Info "central      : $($record.central_validation.state)"
        if ($record.central_validation.retry) { Write-Info "retry with   : $($record.central_validation.retry)" }
    }
    if ($record.diagnostic_path) { Write-Info "diagnostics  : $($record.diagnostic_path)" }
    if ($ExitCode -ne 0) { Stop-WithError "model update exited $ExitCode" }
    if ($record.ok -ne $true -and $record.passed -ne $true) {
        Stop-WithError 'the model update did not confirm success (ok/passed is not true)'
    }
    if (-not $pullRequest) {
        Stop-WithError 'the record carries no pull request URL, so no review request exists'
    }
    Write-Info 'done: review request is up to date'
}

$configPath = if ($Config) { $Config } else { Join-Path $PSScriptRoot 'submit-host.json' }
$host_ = Read-HostConfig $configPath
$remote = Test-RemoteMode $host_
$plan = Resolve-Plan $host_ $ModelRoot $Profile $MechanicalReference
$messageText = Get-Message $host_ $Message
$runtimePython = if ($remote) { $null } else { Resolve-LocalRuntime $host_ }
$remoteCommand = if ($remote) { Build-RemoteCommand $host_ $plan.model_root $plan.profile $plan.mechanical_reference } else { $null }

Write-Step 'plan'
if ($remote) {
    Write-Info "mode         : remote (build host $($host_.build_host))"
    Write-Info "remote python: $($host_.remote_python)"
    Write-Info "model root   : $($plan.model_root) (build host path)"
    Write-Info "tunnel       : build host 127.0.0.1:$($host_.remote_port) -> worker 127.0.0.1:$($host_.worker_port)"
} else {
    Write-Info "mode         : local (this machine)"
    Write-Info "runtime      : $runtimePython"
    Write-Info "model root   : $($plan.model_root)"
    Write-Info "source       : selected by the model's own source block (worker, Onshape or frozen)"
}
Write-Info "profile      : $($plan.profile)"
if ($plan.mechanical_reference) { Write-Info "reference    : $($plan.mechanical_reference)" }

if ($DescribeOnly) {
    if ($remote) {
        try { Test-WorkerHealth $host_ | Out-Null; Write-Info 'worker       : ready' }
        catch { Write-Info "worker       : $($_.Exception.Message)" }
    } else {
        try {
            $tools = Initialize-LocalTools
            Write-Info "git          : $($tools.git)"
            Write-Info "gh           : $($tools.gh)"
            Write-Info "git lfs      : $($tools.git_lfs)"
        } catch {
            Write-Info "tools        : $($_.Exception.Message)"
        }
    }
    Write-Info 'describe-only: nothing was connected or submitted'
    exit 0
}

if ($remote) {
    Write-Step 'worker health'
    $health = Test-WorkerHealth $host_
    Write-Info "worker       : version $($health.worker_version), pid $($health.pid), queue empty"
    Write-Step 'submit (single ssh session with -R)'
    $result = Invoke-RemoteUpdate $host_ $remoteCommand $messageText
} else {
    Write-Step 'tools (git, git lfs, gh)'
    $tools = Initialize-LocalTools
    Write-Info "git          : $($tools.git)"
    Write-Info "gh           : $($tools.gh)"
    Write-Info "git lfs      : $($tools.git_lfs)"
    Write-Step 'submit (local pipeline)'
    $result = Invoke-LocalUpdate $runtimePython $plan.model_root $plan.profile $messageText $plan.mechanical_reference
}
Show-RemoteResult -StdOut $result.stdout -StdErr $result.stderr -ExitCode $result.exit_code
