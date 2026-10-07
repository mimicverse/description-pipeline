# Airflow deployment (Linux orchestration, Windows execution)

This guide installs the released v1.0.0 Airflow services and documents their
configuration and API. The CAD-only one-folder operator page and embedded viewer are
the next deployment target, not installed by these commands. See the
[deployment contract and status](../../docs/deployment.md),
[operator workflow](../../docs/operations.md), and
[mechanical handoff specification](../../docs/mechanical-handoff-spec.md).

v1.0.0 still requires a legacy prepared package with `robot.yaml` and a revision
manifest. These are not requirements on the mechanical team; the target
workflow generates them from SolidWorks contents. Prepared-package transport
alone must not be described as the completed CAD-only interface.

The one-shot DAG `solidworks_to_urdf` submits a configured package to the bearer-authenticated Windows
endpoint, polls it with a bounded reschedule sensor and fails closed unless the passing result carries
quality and PR evidence.

Apache Airflow is the single production operator interface: handoff submission, run status,
diagnostic events and the PR result all come from the Airflow Web UI and the same DAG REST API
(`/api/v2`) behind it. The native `mimicverse-description` CLI and `scripts/scheduled_smoke.py` are
worker/local tooling for diagnostics and replay, not a second required operator path. There is one
DAG id (`solidworks_to_urdf`) and one pipeline id (`solidworks-to-urdf`). The
planned operator page uses this same DAG; it does not bypass Airflow or add
another workflow engine.

## Install

```sh
export AIRFLOW_VENV=$HOME/solidworks-urdf/airflow-venv
export AIRFLOW_HOME=$HOME/solidworks-urdf/airflow-home
export PIPELINE_WHEEL=$HOME/solidworks-urdf/wheels/mimicverse_description-<version>-py3-none-any.whl
export AIRFLOW_DB_URL='postgresql+psycopg2://solidworks@/airflow_meta?host=/abs/airflow-pg/socket&port=5433'
deploy/airflow/install.sh
```

Everything is explicit: the venv path, the Airflow home, the built `mimicverse_description-*.whl`
and the dedicated PostgreSQL DSN. The installer refuses relative paths and the shared roots
(`/`, `/usr`, `/etc`, `/opt`, `/var`, `$HOME` itself), and `AIRFLOW_VENV`/`AIRFLOW_HOME` must be
distinct — no system interpreter or site-packages tree is touched.

`requirements.lock` is a fully resolved stack, pinned with wheel SHA-256 hashes for Linux CPython
3.12 (`pip install --require-hashes --only-binary=:all:`): no floating versions, no build
dependencies, and no pip upgrade in the private venv. It covers the Airflow stack **and** the
wheel's runtime closure (numpy, mujoco, absl-py, etils, glfw, PyOpenGL, …); the pipeline wheel
itself is installed separately and never locked. `scripts/build_requirements_lock.py` regenerates
it in one pip transaction, constrained by the vendored Apache `constraints-3.12.txt`
(provenance in `constraints-3.12.source`):

```sh
scripts/build_requirements_lock.py --python "$AIRFLOW_VENV/bin/python" \
  --requirements deploy/airflow/requirements.txt \
  --constraints deploy/airflow/constraints-3.12.txt \
  --constraints deploy/airflow/requirements.lock \
  --wheel /dist/mimicverse_description-<version>-py3-none-any.whl \
  --output deploy/airflow/requirements.lock
```

The installer runs `pip check` after the wheel install, so a lock that misses the wheel's declared
closure fails the install. `AIRFLOW_PYTHON` must be a Python 3.12 with venv support. Rerunning the
installer preserves the existing Fernet key and `[api_auth] jwt_secret` in the 0600 `airflow.cfg`.

PostgreSQL is installed separately, sudo-free and socket-only (no TCP listener, no trust reachable
from the network):

```sh
export POSTGRES_ROOT=$HOME/solidworks-urdf/airflow-pg
deploy/airflow/scripts/install_postgres.sh   # prints the AIRFLOW_DB_URL to export above
```

## Services

For loopback Windows execution, configure an SSH host alias with a verified
host key and key-based authentication, then set `SOLIDWORKS_SSH_HOST`. The
managed tunnel reconnects after a network break and starts with the user services:

```sh
export SOLIDWORKS_SSH_HOST=windows-worker
```

```sh
deploy/airflow/services.sh render    # write the description-* unit files only (no systemctl)
deploy/airflow/services.sh install   # render + systemctl --user daemon-reload
deploy/airflow/services.sh start     # description-postgres (if POSTGRES_ROOT) + dag/scheduler/api
deploy/airflow/services.sh stop
deploy/airflow/services.sh status
```

The units are `description-postgres`, `description-solidworks-tunnel` when configured,
`description-airflow-dag-processor`, `description-airflow-scheduler`
and `description-airflow-api-server`; unrelated units in
`~/.config/systemd/user` are left alone.

The API server (UI + REST) binds `127.0.0.1:8791` only, and the units run with `UMask=0077` so the
generated password file is private. Remote operators reach the same UI through an SSH tunnel, or an
authenticated reverse proxy on the deployment host:

```sh
ssh -N -L 8788:127.0.0.1:8791 <deployment-host>   # then http://127.0.0.1:8788/
```

## Operator entry (Web UI / REST API)

Login user `operator` (role `admin`) is configured by `[core] simple_auth_manager_users` in the
rendered `airflow.cfg`. The Simple Auth Manager prints the generated password once on the first
`api-server` start (it is also stored, 0600, in
`$AIRFLOW_HOME/simple_auth_manager_passwords.json.generated`):

```sh
python3 -c 'import json, os; print(json.load(open(os.path.join(os.environ["AIRFLOW_HOME"], "simple_auth_manager_passwords.json.generated")))["operator"])'
```

That prints only the operator's value. The file itself is 0600 and holds every generated account;
do not print or copy it whole.

For v1.0.0 maintenance, trigger a handoff with **Trigger DAG w/ config** and
this JSON (identical to the API `conf` and CLI `--conf`):

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

The package must already exist under the Windows `package_root`. The target
alias selects a configured clone; `repository_slug` and `base` bind its expected
destination. These are the released DAG's six parameters. The target single-path
request, transport, hardware routing and server-side preview APIs are specified
in [deployment.md](../../docs/deployment.md#deployment-contract).

The same operations use the DAG REST API. Save the handoff JSON as
`handoff.json`. This example authenticates from the private password file and
submits it without placing credentials in arguments or printing the JWT:

```sh
"$AIRFLOW_VENV/bin/python" - <<'PY'
import json, os, uuid
from pathlib import Path
from urllib.request import Request, urlopen

base = "http://127.0.0.1:8791"
passwords = Path(os.environ["AIRFLOW_HOME"]) / "simple_auth_manager_passwords.json.generated"
credentials = {"username": "operator", "password": json.loads(passwords.read_text())["operator"]}
auth = Request(base + "/auth/token", data=json.dumps(credentials).encode(),
               headers={"Content-Type": "application/json"})
with urlopen(auth, timeout=30) as response:
    token = json.load(response)["access_token"]
payload = {"dag_run_id": "handoff-" + uuid.uuid4().hex, "logical_date": None,
           "conf": json.loads(Path("handoff.json").read_text())}
request = Request(base + "/api/v2/dags/solidworks_to_urdf/dagRuns",
                  data=json.dumps(payload).encode(),
                  headers={"Content-Type": "application/json", "Authorization": "Bearer " + token})
with urlopen(request, timeout=30) as response:
    print(json.dumps(json.load(response)))
PY
```

| Operator need | Airflow UI | Authenticated REST API |
| --- | --- | --- |
| Submit handoff | Trigger DAG w/ config | `POST /api/v2/dags/solidworks_to_urdf/dagRuns` with `{"dag_run_id", "logical_date", "conf"}` |
| Run status | Grid/Graph view of the run | `GET /api/v2/dags/solidworks_to_urdf/dagRuns/{run_id}` |
| Task/stage state | Grid task boxes | `GET …/dagRuns/{run_id}/taskInstances` |
| Diagnostic events | Task log of `wait_for_job` / `confirm_job` | `GET …/taskInstances/{task_id}/logs/{try_number}` |
| PR result | `confirm_job` XCom (`quality`, `submission`) | `GET …/taskInstances/confirm_job/xcomEntries/return_value` |

`confirm_job` fails the run unless the job echoes the bound request, the job's `repository_slug` /
`repository_base` match the conf, and the receipt carries a quality pass plus a GitHub pull URL.

Simple Auth Manager keeps a single local admin account; it is appropriate for this private,
loopback-only deployment (`airflow.cfg` is 0600, the password file is 0600, no shared users). Anyone
exposing the API server beyond loopback should put a real auth manager in front of it.

Pipeline id is `solidworks-to-urdf`; schema versions carry their own suffixes
(`solidworks-to-urdf.bundle/v1`, `solidworks-to-urdf.cad-revision/v1`, HTTP `/v1`).

## Connection

Keep the bearer token in a 0600 file (editor, `umask`-protected write or secret manager — never on a
command line) and let the helper upsert the connection:

```sh
export AIRFLOW_HOME=$HOME/solidworks-urdf/airflow-home
token_file="$AIRFLOW_HOME/windows-token"        # 0600, holds only the bearer token
"$AIRFLOW_VENV/bin/python" deploy/airflow/scripts/add_connection.py \
  --token-file "$token_file" --host 127.0.0.1 --port 18765
```

The v1.0.0 DAG selects the `solidworks_windows` connection through its `conn_id`
parameter. The target single-path DAG will read `SOLIDWORKS_ENDPOINT_CONN_ID`
from deployment configuration, defaulting to that same connection ID.
The managed tunnel forwards Linux `127.0.0.1:18765` to Windows `127.0.0.1:8765`.
The endpoint must stay loopback HTTP behind that tunnel or use TLS;
the client refuses remote `http://` URLs, so a plaintext remote bearer token cannot be configured.

## Local diagnostics / replay (worker tooling)

```sh
"$AIRFLOW_VENV/bin/airflow" dags test solidworks_to_urdf 2026-01-01 --conf "$(cat handoff.json)"
```

`deploy/airflow/scripts/scheduled_smoke.py --root … --venv … --airflow-home …` runs the same DAG
against a neutral mock endpoint with real dag-processor/scheduler/api-server processes; it is the
regression harness, not the production submission path.

Start the services first, then unpause the DAG once: the DAG row created by the DAG processor
starts paused and triggered runs stay queued while it is paused.

```sh
deploy/airflow/services.sh start
AIRFLOW_HOME=$AIRFLOW_HOME "$AIRFLOW_VENV/bin/airflow" dags unpause solidworks_to_urdf
```

The scheduler executes tasks through the API server, so `dag-processor`, `scheduler` and `api-server`
must all run (dedicated port **8791**; `[core] execution_api_server_url` is
`http://127.0.0.1:8791/execution` and the trailing `/execution` path is required). Airflow reaches
PostgreSQL only through the private UNIX socket. The installer writes a private Fernet key and
`[api_auth] jwt_secret` into the 0600 `airflow.cfg`.

## Windows endpoint setup

On the Windows host the native worker serves the JSON endpoint described by
`solidworks-to-urdf.endpoint/v1`:

* `package_root`, `output_root`, `state_root` — absolute, separate directories;
* `targets` — `{alias: {repository: <local clone>, base: feature/<hardware>}}`, so the DAG sends only an
  alias, never a path or command;
* `token_file` — bearer token file (keep it out of Git); `host` `127.0.0.1`, `port` `8765`,
  optional `tls_cert`/`tls_key`.

Expose it to the Linux scheduler through an SSH tunnel (`ssh -L 18765:127.0.0.1:8765 windows-host`)
or TLS. Jobs return `solidworks-to-urdf.job/v1` with `events=[{stage,state,at}]` and a run receipt:
`result.quality` (`passed`, `subject_sha256`, `checks`) and `result.submission`
(`passed`, `state`, `url`, `commit`). A job is `passed` only when both are passed and a PR URL exists.
