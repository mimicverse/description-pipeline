"""Run the real DAG and default scoped composition over isolated upstream fixtures."""

# Airflow reads its process configuration at import time.
# ruff: noqa: E402
from pathlib import Path
import os
import shutil
import sys

ROOT = Path(__file__).resolve().parents[2]
OUTPUT = Path(sys.argv[1]).resolve()
OUTPUT.mkdir(parents=True, exist_ok=True)
AIRFLOW_ROOT = OUTPUT / "airflow"
AIRFLOW_ROOT.mkdir(exist_ok=True)
DAGS = AIRFLOW_ROOT / "dags"
DAGS.mkdir(exist_ok=True)
shutil.copyfile(ROOT / "deploy/airflow/dags/solidworks_to_urdf.py", DAGS / "solidworks_to_urdf.py")
os.environ["AIRFLOW_HOME"] = str(AIRFLOW_ROOT)
os.environ["AIRFLOW__DATABASE__SQL_ALCHEMY_CONN"] = "sqlite:///" + str(AIRFLOW_ROOT / "metadata.db")
os.environ["AIRFLOW__CORE__LOAD_EXAMPLES"] = "False"
os.environ["AIRFLOW__CORE__DAGS_FOLDER"] = str(DAGS)

import copy
import importlib.util
import json
import sqlite3
import subprocess
import uuid
from contextlib import ExitStack
from datetime import UTC, datetime
from unittest.mock import patch

from psycopg2.extensions import make_dsn
from description_pipeline.orchestration.airflow_client import native_run_id
from description_pipeline.orchestration.portal import AirflowDispatchAdapter
from description_pipeline.pipeline.ingestion import ProductionAdmissionFactory, ProductionMJCFSuccessorRequest
from description_pipeline.platform.queries import read_run_projection
from tests.v1 import test_real_six_stage_chain as fixtures
from tests.v1.test_mjcf_portal import DevelopmentAirflow


def main():
    subprocess.run([sys.executable, "-m", "airflow", "db", "migrate"], check=True, timeout=90)
    from airflow.models.dagrun import DagRun
    from airflow.models.pool import Pool
    from airflow.utils.session import create_session

    with create_session() as session:
        Pool.create_or_update_pool(name="windows_native", slots=1, description="Isolated refactor fixture",
                                   include_deferred=True, session=session)
    fixtures.RealSixStageChainTests.setUpClass()
    fixture = fixtures.RealSixStageChainTests(methodName="runTest")
    try:
        fixture.setUp()
        now = datetime.now(UTC)
        fixture._bind_services(now=now)
        app = fixture._application(clock=now)
        work = fixture.root / "airflow-probe"
        work.mkdir()
        factory = ProductionAdmissionFactory(
            artifacts=fixture.store, platform_snapshot=fixture._platform_snapshot(), work_root=work,
            native_tool_provider=lambda: copy.deepcopy(fixture.native),
            portable_tool_provider=lambda: copy.deepcopy(fixture.portable), clock=lambda: now,
        )
        with ExitStack() as stack:
            for target, value in (
                ("description_pipeline.runtime.tool_record", fixture.native),
                ("description_pipeline.pipeline.stages.generate.tool_record", fixture.portable),
                ("description_pipeline.pipeline.stages.publish.tool_record", fixture.portable),
                ("description_pipeline.pipeline.application.tool_record", fixture.portable),
            ):
                stack.enter_context(patch(target, return_value=copy.deepcopy(value)))
            source_dag = "refactor-source-" + uuid.uuid4().hex
            source_request = fixture._production_request(run_id=native_run_id(source_dag), request_id=source_dag)
            source = factory.admit(app, source_request)
            transport = DevelopmentAirflow(app, source_request.principal)
            app.dispatch(source.dispatch_command.command_id, AirflowDispatchAdapter(transport, "isolated-review"))
            assert app.execute_run(source_request.run_id).outcome == "completed"
            dag_run_id = "refactor-mjcf-" + uuid.uuid4().hex
            rerun = factory.admit_mjcf(app, ProductionMJCFSuccessorRequest(
                run_id=native_run_id(dag_run_id), request_id=dag_run_id, parent_run_id=source_request.run_id,
                principal=source_request.principal, change_summary="Validate scoped Airflow execution",
            ))
            app.dispatch(rerun.dispatch_command.command_id, AirflowDispatchAdapter(transport, "isolated-review"))
            native_keys = ("SOLIDWORKS_ENDPOINT_URL", "TUNNEL_LOOPBACK_PORT",
                           "ENDPOINT_TOKEN_FILE", "PLATFORM_CONFIG_SNAPSHOT")
            for name in native_keys:
                os.environ.pop(name, None)
            os.environ["PLATFORM_SERVICE_DSN"] = make_dsn(
                fixture.server.get_uri(), options=f"-c search_path={fixture.schema},public",
            )
            os.environ["PLATFORM_ARTIFACT_ROOT"] = str(fixture.store.root)
            os.environ["PIPELINE_WORK_ROOT"] = str(work / "scoped-default-runtime")
            stack.enter_context(patch("description_pipeline.pipeline.application._native_client_from_environment",
                                      side_effect=AssertionError("MJCF-only scheduling loaded CAD credentials")))
            stack.enter_context(patch("description_pipeline.pipeline.stages.publish.GitHubCliPullRequestClient",
                                      return_value=fixture.pull_requests))
            stack.enter_context(patch("description_pipeline.repository.publish.GitHubCliPullRequestClient",
                                      return_value=fixture.pull_requests))
            spec = importlib.util.spec_from_file_location("refactor_actual_dag", DAGS / "solidworks_to_urdf.py")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            conf = fixture.store.read_dispatch_conf(rerun.dispatch_command.conf_artifact).to_record()
            with patch.object(DagRun, "generate_run_id", return_value=dag_run_id):
                observed = module.solidworks_to_urdf_dag.test(run_conf=conf, logical_date=datetime.now(UTC))
            with sqlite3.connect(AIRFLOW_ROOT / "metadata.db") as connection:
                tasks = dict(connection.execute(
                    "SELECT task_id,state FROM task_instance WHERE run_id = ?", (dag_run_id,),
                ))
            with fixture._connect() as connection:
                projection = read_run_projection(connection, rerun.admission.run_id, artifacts=fixture.store)
            proof = {
                "dag_run_id": dag_run_id, "state": observed.state, "tasks": tasks,
                "execution_scope": projection["execution_scope"],
                "stage_decisions": [row["stage"] for row in projection["stages"] if row.get("decision")],
                "cad_environment_present": any(os.environ.get(name) for name in native_keys),
                "composition": "Default production stage factory",
                "scope": "Offline upstream fixture, isolated PostgreSQL/Airflow, local Git and controlled PR transport",
            }
            (OUTPUT / "R4-AIRFLOW-RESULT.json").write_text(json.dumps(proof, indent=2) + "\n")
            print(json.dumps(proof, indent=2))
            assert observed.state == "success", proof
            assert {stage for stage, state in tasks.items() if state == "skipped"} == {
                "freeze", "discover", "capture", "generate", "verify",
            }, proof
            assert proof["stage_decisions"] == ["mjcf", "publish"], proof
    finally:
        fixture.doCleanups()
        fixtures.RealSixStageChainTests.tearDownClass()


if __name__ == "__main__":
    main()
