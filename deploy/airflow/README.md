# Airflow installation

This guide installs the released v1.0.0 orchestration: Linux Airflow submits a
prepared package to the authenticated Windows endpoint, waits for its result,
and requires verified quality and PR evidence. The pipeline ID is
`solidworks-to-urdf`; the DAG ID is `solidworks_to_urdf`.

The CAD-only folder interface, operator page, viewer and per-item engineering
report are target requirements, not installed by these steps. See
[deployment status](../../docs/deployment.md#release-and-deployment-status) and
[the target contract](../../docs/deployment.md#deployment-contract).

Run Linux commands from the tool repository root or the extracted deployment
archive containing `deploy/airflow/`. Use a dedicated execution account and
Python 3.12. The provided PostgreSQL installer requires Debian/Ubuntu package
tools and repositories supplying PostgreSQL 18.

## 1. Prepare PostgreSQL

The local database uses a private UNIX socket and has no TCP listener. The
installer extracts packages under a dedicated directory without sudo:

```sh
export POSTGRES_ROOT="$HOME/solidworks-urdf/airflow-pg"
deploy/airflow/scripts/install_postgres.sh
export AIRFLOW_DB_URL="postgresql+psycopg2://solidworks@/airflow_meta?host=$POSTGRES_ROOT/socket&port=5433"
```

The script prints the DSN; use that value if the root differs. An existing
compatible database may be configured instead through `AIRFLOW_DB_URL`.

## 2. Install the isolated Airflow runtime

Obtain the matching pipeline wheel from the release and set its absolute path:

```sh
export AIRFLOW_VENV="$HOME/solidworks-urdf/airflow-venv"
export AIRFLOW_HOME="$HOME/solidworks-urdf/airflow-home"
export PIPELINE_WHEEL="$HOME/solidworks-urdf/wheels/mimicverse_description-1.0.0-py3-none-any.whl"
deploy/airflow/install.sh
```

`AIRFLOW_VENV` and `AIRFLOW_HOME` must be distinct absolute private paths.
The installer rejects shared roots and does not change the system interpreter.
`AIRFLOW_PYTHON` may select another Python 3.12 interpreter with working venv support.

The hash-pinned Linux CPython 3.12 `requirements.lock` covers Airflow and the
pipeline wheel's runtime dependencies. Installation uses wheel-only artifacts
and `--require-hashes`; the pipeline wheel is installed separately, followed
by `pip check`. Rerunning preserves the existing Fernet and JWT secrets in
the private `airflow.cfg`.

## 3. Connect the Windows worker

First configure the [Windows endpoint](../../docs/deployment.md#windows-endpoint)
and model repository. Set up an SSH alias with a verified host key and key-based
authentication. The managed tunnel forwards Linux `127.0.0.1:18765` to Windows
`127.0.0.1:8765` and reconnects after network interruptions:

```sh
export SOLIDWORKS_SSH_HOST=windows-worker
```

Copy the endpoint's token securely into a private file with mode 0600. Keep
its value out of commands and repositories. Upsert the Airflow connection:

```sh
token_file="$AIRFLOW_HOME/windows-token"
"$AIRFLOW_VENV/bin/python" deploy/airflow/scripts/add_connection.py \
  --token-file "$token_file" --host 127.0.0.1 --port 18765
```

The helper defaults to connection ID `solidworks_windows`. Released v1.0.0
selects that ID through the DAG's `conn_id` field. Remote endpoint connections
require HTTPS; loopback HTTP is used only through the authenticated tunnel.

## 4. Start services and log in

Keep the exported paths available when rendering or managing the services:

```sh
deploy/airflow/services.sh install
deploy/airflow/services.sh start
deploy/airflow/services.sh status
"$AIRFLOW_VENV/bin/airflow" dags unpause solidworks_to_urdf
```

The managed units are `description-postgres` when `POSTGRES_ROOT` is set,
`description-solidworks-tunnel` when `SOLIDWORKS_SSH_HOST` is set, and the Airflow
DAG processor, scheduler and API server. The API server provides both UI and
REST at `127.0.0.1:8791`. All three Airflow processes must run; task execution
uses `http://127.0.0.1:8791/execution`. Unpause after the DAG processor creates
the DAG row, otherwise triggered runs remain queued.

Open `http://127.0.0.1:8791/` on the deployment host. For access from another
computer, tunnel that same UI:

```sh
ssh -N -L 8788:127.0.0.1:8791 deployment-host
```

Replace `deployment-host` with the configured host, then open
`http://127.0.0.1:8788/`. The supplied Simple Auth Manager creates
one local `operator` account with the admin role. There is no self-registration.
After the API server first starts, the execution account can read its password:

```sh
"$AIRFLOW_VENV/bin/python" - <<'PY'
import json, os
from pathlib import Path
path = Path(os.environ["AIRFLOW_HOME"]) / "simple_auth_manager_passwords.json.generated"
print(json.loads(path.read_text())["operator"])
PY
```

Generated configuration and passwords are private (0600, service `UMask=0077`).
The supplied login is for a single-account loopback deployment. A shared
operator service requires managed user authentication and an HTTPS reverse
proxy as specified in the deployment contract.

## Run and inspect a delivery

Platform maintainers prepare the v1.0.0 package, including `robot.yaml` and
`cad-revision.json`, under the Windows `package_root`. Mechanical engineers
supply native engineering only. In Airflow, open `solidworks_to_urdf` and choose
**Trigger DAG w/ config** with:

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

`package` is relative to the Windows `package_root`. `target` selects a configured
clone; `repository_slug` and `base` bind its expected destination. This released
form does not transfer Linux folders or infer hardware identity.

| Result | Airflow UI | Authenticated REST API |
|---|---|---|
| Submit | Trigger DAG w/ config | `POST /api/v2/dags/solidworks_to_urdf/dagRuns` |
| Run status | Grid/Graph view | `GET /api/v2/dags/solidworks_to_urdf/dagRuns/{run_id}` |
| Task state | Run task boxes | `GET …/dagRuns/{run_id}/taskInstances` |
| Native stages and failures | `wait_for_job` and `confirm_job` logs | `GET …/taskInstances/{task_id}/logs/{try_number}` |
| Quality, commit and PR | `confirm_job` return-value XCom | `GET …/taskInstances/confirm_job/xcomEntries/return_value` |

`confirm_job` requires a matching request and destination, a passing quality
report and a successful submission receipt with a GitHub PR URL. Quality
failure prevents publication; a corrected package starts a new run. Retries
reuse the native UUID derived from the Airflow run ID.

### REST submission

Save the configuration above as `handoff.json`. This example reads the private
password file, authenticates and submits without printing the JWT:

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

CLI and API access use the same Airflow DAG. They are maintenance interfaces,
not additional workflow engines or mechanical-team requirements.

## Deployment maintenance

`deploy/airflow/services.sh render` writes unit files without calling systemctl;
`stop` disables and stops the managed services. Unrelated units are left alone.

For an isolated test deployment with its database and configuration prepared,
stop its Airflow services before running the smoke harness. The harness starts
real Airflow processes on port 8791 against a neutral mock endpoint:

```sh
"$AIRFLOW_VENV/bin/python" deploy/airflow/scripts/scheduled_smoke.py \
  --root "$PWD" --venv "$AIRFLOW_VENV" --airflow-home "$AIRFLOW_HOME"
```

It verifies orchestration, not native CAD. Actual deployment acceptance is in
[deployment.md](../../docs/deployment.md#deployment-acceptance).

Maintainers regenerate the deployment lock after changes to direct pins or the
pipeline wheel. Resolution uses the vendored Apache constraints with provenance
in `constraints-3.12.source`, plus the previously tested deployment versions:

```sh
"$AIRFLOW_VENV/bin/python" deploy/airflow/scripts/build_requirements_lock.py \
  --python "$AIRFLOW_VENV/bin/python" \
  --requirements deploy/airflow/requirements.txt \
  --constraints deploy/airflow/constraints-3.12.txt \
  --constraints deploy/airflow/requirements.lock \
  --wheel "$PIPELINE_WHEEL" \
  --output deploy/airflow/requirements.lock
```

The helper resolves Airflow and the wheel together and checks the generated
hash lock. The wheel itself remains outside that lock to avoid a circular
source-identity dependency. Rehearse installation and acceptance before releasing
an updated environment.
