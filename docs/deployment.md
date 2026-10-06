# Apache Airflow deployment

Linux runs Apache Airflow. A Windows endpoint with licensed SolidWorks runs the
complete local `description run` workflow and serializes CAD execution.

```mermaid
sequenceDiagram
  participant M as Mechanical team
  participant A as Linux Airflow
  participant W as Windows endpoint
  participant G as GitHub model repository
  M->>W: Sealed CAD package and robot definition
  M->>A: Package path, revision digest, target alias
  A->>W: Authenticated job request with stable UUID
  W->>W: Inspect, capture, generate, independently verify
  W->>G: Push verified bundle and create/update PR
  A->>W: Poll persistent job result
  W-->>A: Gate decision, subject, commit and PR URL
```

The DAG is manually triggered for a mechanical handoff. The endpoint rechecks
the requested CAD revision and author inventory before execution. Queued jobs
run serially. Identical request retries return the existing job; a UUID reused
with different content returns HTTP 409. An interrupted running job fails
explicitly on restart and needs a new reviewed request.

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

Loopback HTTP must travel through an authenticated SSH tunnel from Linux. For a
remote bind, configure `tls_cert` and `tls_key`; plaintext remote binding is
rejected. Keep the bearer token in the Airflow connection, never a DAG, command
argument, model bundle or committed configuration.

## Linux scheduler

Use the deployment files under `deploy/airflow/` to install a dedicated Airflow
environment, its exact tested constraints, connection and DAG. Airflow
dependencies do not belong in the local pipeline runtime. The connection points
to the tunneled endpoint and supplies the bearer token.

Trigger a handoff with:

```json
{
  "package": "arm/r2",
  "revision_sha256": "<SHA-256 of the exact cad-revision.json file>",
  "target": "arm"
}
```

The DAG validates this request, starts the job, waits for its terminal result,
and requires a passing quality report plus a successful submission receipt and
PR URL. Job events expose the five local stages. Reusing a DAG-run identity
preserves the endpoint UUID across task retries.

## Deployment acceptance

Before service handoff, demonstrate authenticated connectivity, a complete
native passing delivery and PR, a repeated request with no second capture, a
rejected revision digest, a rejected path escape, a failed quality gate with no
publication, and restart recovery. Verify the actual Airflow DAG imports and
runs in its isolated deployment environment. A mocked endpoint tests DAG
control behavior; it does not replace the native end-to-end rehearsal.
