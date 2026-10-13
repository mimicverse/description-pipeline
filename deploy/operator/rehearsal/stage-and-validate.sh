#!/usr/bin/env bash
# Stage (or replace) the release in place and validate the candidate runtime.
# No live change: only the release dir, the inactive <tag> venv and its isolated
# AIRFLOW_HOME are touched; production paths, configs and the AirFlow DB are never touched.
#
#   stage-and-validate.sh <release-dir> <tag> <wheel> <expected-wheel-sha256> \
#                         <expected-source-sha256> [<deployment-zip> <expected-zip-sha256>]
#
# EXPECTED_DAG_TASKS (env, default 8) pins the imported DAG's task count for the final source.
set -euo pipefail

rel="$1"; tag="$2"; wheel="$3"; want_wheel="$4"; want_source="$5"
zip_path="${6:-}"; want_zip="${7:-}"
OWNER="${OPERATOR_ROOT:-/home/andy/operator}"
venv="$OWNER/airflow-venv-$tag"
LIVE_TAG="${LIVE_TAG:?set LIVE_TAG to the live rollback venv tag}"
live_venv="$OWNER/airflow-venv-$LIVE_TAG"
candidate_airflow_home="$OWNER/airflow-home-$tag"
expected_tasks="${EXPECTED_DAG_TASKS:-8}"
[ "$venv" != "$live_venv" ] || { echo "refusing to stage into the live rollback venv"; exit 1; }
[ -x "$venv/bin/python" ] || { echo "candidate venv missing: run prepare-venv.sh <release-dir> $tag first"; exit 1; }

cd "$rel"
got_wheel="$(sha256sum "$wheel" | cut -d' ' -f1)"
[ "$got_wheel" = "$want_wheel" ] || { echo "wheel sha256 mismatch: $got_wheel"; exit 1; }
if [ -n "$zip_path" ]; then
  got_zip="$(sha256sum "$zip_path" | cut -d' ' -f1)"
  [ "$got_zip" = "$want_zip" ] || { echo "deployment zip sha256 mismatch: $got_zip"; exit 1; }
  unzip -q -o "$zip_path"
fi

echo "--- wheel install ---"
"$venv/bin/python" -m pip install --disable-pip-version-check --no-index --no-deps \
  --force-reinstall "$wheel" 2>&1 | tail -2
"$venv/bin/python" -m pip check

echo "--- source identity ---"
"$venv/bin/python" - "$want_source" <<'PY'
import json, sys
from description_pipeline import __version__
from description_pipeline.runtime import tool_record
record = tool_record(role="portable")
print(json.dumps({"version": __version__, "source_sha256": record["source_sha256"]}))
if record["source_sha256"] != sys.argv[1]:
    raise SystemExit("source identity differs from the expected release identity")
PY

echo "--- isolated DAG import ---"
mkdir -p "$candidate_airflow_home"
AIRFLOW_HOME="$candidate_airflow_home" AIRFLOW__CORE__LOAD_EXAMPLES=false EXPECTED_DAG_TASKS="$expected_tasks" \
  "$venv/bin/python" - <<'PY'
import hashlib, json, os, pathlib
from airflow.models import DagBag
bag = DagBag("deploy/airflow/dags")
dag = bag.dags.get("solidworks_to_urdf")
path = pathlib.Path("deploy/airflow/dags/solidworks_to_urdf.py")
expected = int(os.environ["EXPECTED_DAG_TASKS"])
print(json.dumps({
    "import_errors": bag.import_errors,
    "tasks": sorted(dag.task_dict) if dag else None,
    "dag_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
}, indent=1))
if bag.import_errors or dag is None or len(dag.task_dict) != expected:
    raise SystemExit(f"DAG import validation failed (expected {expected} tasks)")
PY

echo "stage-and-validate: OK"
