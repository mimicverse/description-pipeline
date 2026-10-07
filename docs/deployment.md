# Deployment

## Deployment contract

One RTX 4080 Linux server hosts the operator page, Apache Airflow and its
private PostgreSQL database. An HTTPS reverse proxy exposes one authenticated
operator URL on the team's network. The operator page submits to the Airflow
DAG and uses the same login; Airflow administration remains restricted to
platform maintainers. One reachable Windows worker holds licensed SolidWorks
and executes native jobs serially.

The operator contract itself — one URL, one path, concise stages, verified URDF
with joint/limit interaction, quality decision and PR — is documented in
[operations.md](operations.md) and [../README.md](../README.md); this guide owns
infrastructure, authentication, roots, routing, the Windows worker and the
server/operator-page parameters.

### Folder resolution

The target DAG accepts one field:

```json
{"handoff_path": "/srv/handoffs/arm/r2"}
```

| Folder location | Resolution |
|---|---|
| Absolute Linux path | The Airflow task must be able to read it; archive and stream it to Windows, then verify the received inventory digest |
| Absolute Windows path | The configured Windows worker must be able to read it; copy into the managed handoff store |
| Relative path | Resolve under the worker's configured `package_root`, then copy into that store |

A folder on an operator's separate computer must first reach one of these
locations. The browser does not grant server access to arbitrary local files.
Transport changes the location, not the package contents or its relative paths.
The selected folder contains SolidWorks engineering files, not an authored
`robot.yaml` or revision manifest. The platform generates these artifacts after
native reading and records their provenance.

```mermaid
sequenceDiagram
  participant M as Operator page
  participant A as Linux Airflow
  participant W as Windows endpoint
  participant G as GitHub model repository
  M->>A: One handoff folder path
  opt Folder on Linux
    A->>W: Authenticated archive plus inventory identity
  end
  A->>W: Resolve and freeze handoff
  W->>W: Freeze native file inventory
  W-->>A: Frozen input identity
  A->>W: Authenticated job request with stable UUID
  W->>W: Read CAD identity, assembly, mates, datums and properties
  W->>W: Resolve hardware route and controlled specifications
  W->>W: Generate robot definition, build, independently verify
  W->>G: Push verified bundle and create/update PR
  A->>W: Poll persistent job result
  W-->>A: Quality, verified URDF assets, commit and PR URL
  A-->>M: Progress, interactive URDF and review result
```

The pipeline freezes the received native files, computes their inventory
digest, and binds it to the job request. Hardware and structural identity are
read from CAD engineering records; manifests and robot definitions are
generated outputs. The worker resolves the hardware identity against `targets`:
each target may declare its hardware ID, with
the target key as the default. Exactly one match is required. Unknown or
ambiguous hardware blocks publication; the operator never chooses a route.
Mechanical identity may require native reading, so this check is not claimed
to happen before opening CAD. Unsafe paths and malformed transport are rejected
before native execution.

The Airflow connection is configured by `SOLIDWORKS_ENDPOINT_CONN_ID`, default
`solidworks_windows`. Repository clones and `feature/<hardware>` bases belong
to the worker configuration. Queued jobs run serially and recheck frozen files
before capture. Task retries preserve the native UUID; a UUID reused with
different content returns HTTP 409. Restart marks an interrupted running job
failed. A corrected handoff uses a new DAG run.

### Verified preview

The viewer loads the delivery's verified URDF and meshes, with per-joint
position and limit controls. It displays the independent quality decision and
submission receipt; it does not calculate an alternative pass/fail result.
The server-side client reads authenticated `GET /v1/jobs/<uuid>/preview` and
`GET /v1/jobs/<uuid>/artifacts/<relative>` endpoints. Only assets bound to a
passing delivery may be served. CAD files, evidence and worker paths are not
viewer assets. Tokens stay server-side, and viewer dependencies are bundled.

The DAG run maps to a native UUID through
`uuid5(NAMESPACE_URL, "solidworks_to_urdf:" + dag_run_id)`. This is an internal
correlation rule, not an operator input.

## Release and deployment status

| Capability | Status |
|---|---|
| Published v1.0.0 | Native capture, verified URDF, governed PR submission, frozen replay and the existing Airflow DAG are released |
| CAD-only input and automatic robot definition | Required target; native semantic discovery and generated-input verification are not yet released |
| One-folder resolution, transfer and routing | Under development; prepared-package transport alone does not establish CAD-only operation |
| Operator page and embedded URDF viewer | Planned; not commissioned |
| RTX 4080 server and shared operator URL | Not commissioned; no live address is asserted here |

The instructions below apply to the released v1.0.0 deployment. They provide
the working Airflow/Windows foundation; they do not install the planned page
or CAD-only workflow. This release still requires its legacy prepared package,
including `robot.yaml` and `cad-revision.json`; that is an existing platform
limitation, not a delivery requirement on the mechanical team. Existing
v1.0.0 assets and their tag remain unchanged.

## Windows endpoint

Install the same pipeline release used locally. Create separate handoff,
delivery, state and model-clone directories. Generate a random token of at
least 32 characters and save it outside the repositories with access restricted
to the execution user. Configuration contains the token-file path, not its value.

```json
{
  "schema_version": "solidworks-to-urdf.endpoint/v1",
  "package_root": "C:/handoffs",
  "output_root": "C:/deliveries",
  "state_root": "C:/description/state",
  "token_file": "C:/description/secrets/endpoint-token.txt",
  "host": "127.0.0.1",
  "port": 8765,
  "targets": {
    "arm": {
      "repository": "C:/description/models-arm",
      "base": "feature/arm"
    }
  }
}
```

```powershell
description serve --config C:\description\endpoint.json
```

Run it as the logged-in SolidWorks user, including after login if automatic
startup is configured. Do not run native CAD jobs as a Session 0 Windows service.
The endpoint owns only its pipeline jobs and CAD sessions. It does not dismiss
dialogs in other applications or manipulate their processes.

For automatic startup, register an interactive task under that same execution
account after verifying the foreground command:

```powershell
$account = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$action = New-ScheduledTaskAction -Execute C:\description\.venv\Scripts\description.exe -Argument 'serve --config C:\description\endpoint.json'
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $account
$principal = New-ScheduledTaskPrincipal -UserId $account -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName description-solidworks-endpoint -Action $action -Trigger $trigger -Principal $principal -Settings $settings
Start-ScheduledTask -TaskName description-solidworks-endpoint
```

Ensure Git/GitHub authentication and executable paths are available to this
account. Keep the desktop logged in and the computer awake while processing.
`description doctor` checks runtime availability without opening CAD; it does
not qualify a particular handoff.

Loopback HTTP must travel through an authenticated SSH tunnel from Linux. For a
remote bind, configure `tls_cert` and `tls_key`; plaintext remote binding is
rejected. Keep the bearer token in the Airflow connection, never a DAG, command
argument, model bundle or committed configuration.

## Linux scheduler

Follow the [deployment installation guide](../deploy/airflow/README.md) to set
up the dedicated Python 3.12 environment, PostgreSQL, authentication,
connection and services. It includes the exact tested dependency lock and
token-file connection helper. Airflow dependencies stay in its own environment.
The connection points to the tunneled endpoint and supplies the bearer token.

After login, open `solidworks_to_urdf`, enable it, and select **Trigger DAG**.
The released v1.0.0 DAG requires the following configuration, supplied by the
platform maintainer. This legacy form is replaced by `handoff_path` when the
target deployment is accepted:

```json
{
  "package": "arm/r2",
  "revision_sha256": "<SHA-256 of the exact cad-revision.json file>",
  "target": "arm",
  "repository_slug": "<owner>/<model-repository>",
  "base": "feature/arm",
  "conn_id": "solidworks_windows"
}
```

In v1.0.0, `package` is relative to the Windows `package_root`; the handoff must
already be there. `target` selects a configured clone, while `repository_slug`
and `base` bind the expected destination. This version does not automatically
transfer a Linux folder or resolve hardware routing.

The DAG validates this request, starts the job, waits for its terminal result,
and requires a passing quality report plus a successful submission receipt and
PR URL. The `wait_for_job` task logs native stages and failure diagnostics;
`confirm_job` returns the events, verified subject, commit and PR URL. Reusing
a DAG-run identity preserves the endpoint UUID across task retries. A corrected
handoff starts a new DAG run.

## Deployment acceptance

Before service handoff, demonstrate authenticated connectivity, a complete
native passing delivery and PR, a repeated request with no second capture, a
rejected revision digest, a rejected path escape, a failed quality gate with no
publication, and restart recovery. Verify the actual Airflow DAG imports and
runs in its isolated deployment environment. For the target interface, also
exercise all three folder locations, inventory verification after transfer,
unknown/ambiguous hardware rejection, single-page login and submission, and
actual URDF joint/limit interaction. Begin with native SolidWorks files only
and demonstrate automatic identity, rigid-body/joint recognition, parameter
resolution and generated definitions. Independently verify each derived fact
against native evidence; reject ambiguity and missing sources without
inventing parameters. Confirm that failed or changed assets and
non-viewer files cannot be served. A mocked endpoint tests DAG control behavior;
it does not replace the native end-to-end rehearsal.

## Model repository setup

Configure Git identity and GitHub authentication under the Windows execution
account, then create a dedicated clean clone:

```powershell
gh auth status
gh auth setup-git
git clone https://github.com/<owner>/<model-repository>.git C:\description\models-arm
```

The model owner creates a new hardware branch once if it does not exist. In
this dedicated clean clone:

```powershell
git -C C:\description\models-arm switch --orphan feature/arm
Set-Content -Encoding utf8 C:\description\models-arm\README.md "# arm model"
git -C C:\description\models-arm add README.md
git -C C:\description\models-arm commit -m "Initialize arm model branch"
git -C C:\description\models-arm push origin feature/arm
```

Existing hardware branches need no initialization. Keep tool source in the
public tool repository and native CAD/model history in the private model
repository. A passing pipeline delivery creates or updates the hardware PR;
model review and release approval remain separate steps.
