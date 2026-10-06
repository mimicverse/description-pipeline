# Airflow deployment (Linux orchestration, Windows execution)

The one-shot DAG `solidworks_to_urdf` submits a configured package to the bearer-authenticated Windows
endpoint, polls it with a bounded reschedule sensor and fails closed unless the passing result carries
quality and PR evidence.

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
dependencies, and no pip upgrade in the private venv. `constraints-3.12.txt` is the vendored
resolve basis (provenance in `constraints-3.12.source`); `scripts/build_requirements_lock.py`
regenerates the lock from its own `name==version` head. `AIRFLOW_PYTHON` must be a Python 3.12
with venv support. Rerunning the installer preserves the existing Fernet key and
`[api_auth] jwt_secret` in the 0600 `airflow.cfg`.

PostgreSQL is installed separately, sudo-free and socket-only (no TCP listener, no trust reachable
from the network):

```sh
export POSTGRES_ROOT=$HOME/solidworks-urdf/airflow-pg
deploy/airflow/scripts/install_postgres.sh   # prints the AIRFLOW_DB_URL to export above
```

## Services

```sh
deploy/airflow/services.sh render    # write the description-* unit files only (no systemctl)
deploy/airflow/services.sh install   # render + systemctl --user daemon-reload
deploy/airflow/services.sh start     # description-postgres (if POSTGRES_ROOT) + dag/scheduler/api
deploy/airflow/services.sh stop
deploy/airflow/services.sh status
```

Only `description-postgres`, `description-airflow-dag-processor`, `description-airflow-scheduler`
and `description-airflow-api-server` are ever written; unrelated units in
`~/.config/systemd/user` are left alone.

## Connection

Keep the bearer token in a 0600 file (editor, `umask`-protected write or secret manager — never on a
command line) and let the helper upsert the connection:

```sh
export AIRFLOW_HOME=$HOME/solidworks-urdf/airflow-home
token_file="$AIRFLOW_HOME/windows-token"        # 0600, holds only the bearer token
"$AIRFLOW_VENV/bin/python" deploy/airflow/scripts/add_connection.py \
  --token-file "$token_file" --host 127.0.0.1 --port 18765
```

The default connection id is `solidworks_windows`, matching the DAG's `conn_id` parameter.
The endpoint must stay loopback HTTP behind an SSH tunnel (`ssh -L 18765:127.0.0.1:8765 …`) or TLS;
the client refuses remote `http://` URLs, so a plaintext remote bearer token cannot be configured.

## Run

```sh
"$AIRFLOW_VENV/bin/airflow" dags test solidworks_to_urdf 2026-01-01 --conf '{
  "package": "handoff/m3.0",
  "revision_sha256": "<sha256 of the sealed cad-revision.json>",
  "target": "m3",
  "repository_slug": "<owner>/<repo>",
  "base": "feature/<hardware>"
}'
```

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

Pipeline id is `solidworks-to-urdf`; schema versions carry their own suffixes
(`solidworks-to-urdf.bundle/v1`, `solidworks-to-urdf.cad-revision/v1`, HTTP `/v1`).

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
