"""Real stage artifacts and PostgreSQL facts behind the MJCF portal boundary."""

from __future__ import annotations

import copy
import io
import json
import os
import unittest
from datetime import UTC, datetime
from unittest.mock import patch

from description_pipeline.io import canonical, PipelineError
from description_pipeline.orchestration.airflow_client import native_run_id
from description_pipeline.orchestration.feishu_oauth import build_actor_name
from description_pipeline.orchestration.portal import (
    PortalApp,
    PortalConfig,
    AirflowDispatchAdapter,
    _automatic_summary,
)
from description_pipeline.orchestration.run_ownership import (
    validated_canonical_projection,
    recorded_canonical_projection,
)
from description_pipeline.pipeline.ingestion import ProductionAdmissionFactory
from description_pipeline.platform.queries import read_run_projection
from tests.v1 import test_real_six_stage_chain as chain_tests
from description_pipeline.pipeline.contracts import DispatchConf
from description_pipeline.schemas import AIRFLOW_DISPATCH_CONF_SCHEMA


class DevelopmentAirflow:
    """Only the external transport and identity boundary is controlled in this test."""

    def __init__(self, application, principal):
        self.application = application
        self.principal = principal
        self.triggers = []

    def profile(self, _token):
        app, tenant, user = self.principal.split(":")
        return {
            "principal": self.principal,
            "app_id": app,
            "tenant_key": tenant,
            "open_id": user,
            "name": "Development operator",
            "role": "USER",
        }

    def dag_run(self, _token, dag_id, dag_run_id):
        admitted = self.application.load_admission_outcome(native_run_id(dag_run_id))
        return {
            "dag_id": dag_id,
            "dag_run_id": dag_run_id,
            "state": "queued",
            "conf": DispatchConf(
                AIRFLOW_DISPATCH_CONF_SCHEMA, admitted.admission.run_id, admitted.entry_command.command_id
            ).to_record(),
            "triggering_user_name": build_actor_name(self.principal, "Development operator"),
        }

    def trigger_dag_run(self, token, dag_id, dag_run_id, conf):
        self.triggers.append((dag_run_id, conf))
        return self.dag_run(token, dag_id, dag_run_id)

    def task_instances(self, *_args):
        return []


@unittest.skipUnless(
    chain_tests.pgserver is not None and chain_tests.psycopg2 is not None, "pgserver/psycopg2 are not installed"
)
class MJCFPortalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        chain_tests.RealSixStageChainTests.setUpClass()

    @classmethod
    def tearDownClass(cls):
        chain_tests.RealSixStageChainTests.tearDownClass()

    def test_real_preview_and_rerun_bind_original_inputs_and_current_output(self):
        chain = chain_tests.RealSixStageChainTests(methodName="runTest")
        chain.setUp()
        self.addCleanup(chain.doCleanups)
        application = chain._application()
        factory_root = chain.root / "portal-factory"
        factory_root.mkdir()
        factory = ProductionAdmissionFactory(
            artifacts=chain.store,
            platform_snapshot=chain._platform_snapshot(),
            work_root=factory_root,
            native_tool_provider=lambda: copy.deepcopy(chain.native),
            portable_tool_provider=lambda: copy.deepcopy(chain.portable),
            clock=lambda: datetime(2026, 10, 25, tzinfo=UTC),
        )
        dag_id = "portal-mjcf-source"
        source_id = native_run_id(dag_id)
        request = chain._production_request(run_id=source_id, request_id=dag_id)

        def projection(run_id):
            with chain._connect() as connection:
                return read_run_projection(connection, run_id, artifacts=chain.store)

        with (
            patch("description_pipeline.runtime.tool_record", return_value=copy.deepcopy(chain.native)),
            patch(
                "description_pipeline.pipeline.stages.generate.tool_record", return_value=copy.deepcopy(chain.portable)
            ),
            patch(
                "description_pipeline.pipeline.stages.publish.tool_record", return_value=copy.deepcopy(chain.portable)
            ),
        ):
            admitted = factory.admit(application, request)
            airflow = DevelopmentAirflow(application, request.principal)
            completed = application.execute_run(source_id)
            self.assertEqual("completed", completed.outcome)
            unsettled = projection(source_id)
            self.assertEqual("unresolved", unsettled["state"])
            self.assertEqual(unsettled["state"], chain.facts.get_admitted_run(source_id).state)
            application.dispatch(admitted.dispatch_command.command_id, AirflowDispatchAdapter(airflow, "dev-only"))
            airflow = DevelopmentAirflow(application, request.principal)
            portal = PortalApp(
                PortalConfig(
                    airflow=airflow,
                    artifact_store=chain.store,
                    run_projection_factory=projection,
                    run_control_store=chain.facts,
                    production_application=application,
                    production_mjcf_successor_loader=lambda req: factory.admit_mjcf(application, req),
                    session_cookie="mjcf_development_session",
                )
            )
            self.assertFalse(
                portal._mjcf_successor_row({**unsettled, "stages": {row["stage"]: row for row in unsettled["stages"]}})[
                    "eligible"
                ]
            )
            session = portal.sessions.create("Development operator", "development-only", principal=request.principal)

            def call(method, path, body=None, key="portal-boundary"):
                payload = canonical(body or {}) if body is not None else b""
                environ = {
                    "REQUEST_METHOD": method,
                    "PATH_INFO": path,
                    "QUERY_STRING": "",
                    "CONTENT_LENGTH": str(len(payload)),
                    "wsgi.input": io.BytesIO(payload),
                    "HTTP_COOKIE": f"mjcf_development_session={session.session_id}",
                    "HTTP_X_CSRF_TOKEN": session.csrf_token,
                    "HTTP_IDEMPOTENCY_KEY": key,
                }
                response = []
                result = b"".join(portal(environ, lambda status, headers: response.append((status, headers))))
                return int(response[0][0].split()[0]), result

            status, body = call("GET", f"/api/runs/{dag_id}/preview")
            self.assertEqual(200, status, body)
            preview = json.loads(body)
            self.assertTrue(preview["verified"])
            self.assertTrue(preview["mjcf"]["verified"])
            self.assertIn(preview["mjcf"]["entry"], preview["mjcf"]["files"])
            self.assertTrue(preview["mjcf"]["warnings"])
            original = projection(source_id)
            source_decisions = [row["decision"] for row in original["stages"]]
            status, body = call("POST", f"/api/runs/{dag_id}/attempts", {"stage": "mjcf", "simulation": "{}\n"})
            self.assertEqual(202, status, body)
            successor_dag = json.loads(body)["dag_run_id"]
            successor_id = native_run_id(successor_dag)
            status, repeated = call("POST", f"/api/runs/{dag_id}/attempts", {"stage": "mjcf", "simulation": "{}\n"})
            self.assertEqual((202, body), (status, repeated))
            completed = application.execute_run(successor_id)
            self.assertEqual("completed", completed.outcome)
            current = projection(successor_id)
            authenticated = validated_canonical_projection(
                current,
                dag_id="solidworks_to_urdf",
                dag_run_id=successor_dag,
                canonical_run_id=successor_id,
                mjcf_source=original,
                artifacts=chain.store,
            )
            self.assertIs(current, authenticated)
            real_connect = chain_tests.psycopg2.connect

            def isolated_operator_connection(dsn, **options):
                options["options"] = options.get("options", "") + f" -c search_path={chain.schema},public"
                return real_connect(dsn, **options)

            with (
                patch.dict(
                    os.environ,
                    {
                        "PLATFORM_OPERATOR_DSN": chain.server.get_uri(),
                        "PLATFORM_ARTIFACT_ROOT": str(chain.store.root),
                    },
                ),
                patch("psycopg2.connect", side_effect=isolated_operator_connection),
            ):
                self.assertIsNotNone(recorded_canonical_projection("solidworks_to_urdf", successor_dag))
            for name, eval_allowed in (("index.html", False), ("mjcf_worker.js", True)):
                headers = []
                portal._static(lambda _status, values, headers=headers: headers.extend(values), name)
                csp = dict(headers)["Content-Security-Policy"]
                self.assertEqual(eval_allowed, "'unsafe-eval'" in csp)
                self.assertIn("worker-src 'self'", csp)
            self.assertIsNone(
                validated_canonical_projection(
                    current,
                    dag_id="solidworks_to_urdf",
                    dag_run_id=successor_dag,
                    canonical_run_id=successor_id,
                )
            )
            status, body = call("GET", f"/api/runs/{successor_dag}/preview")
            self.assertEqual(200, status, body)
            self.assertTrue(json.loads(body)["mjcf"]["verified"])
            expanded = portal._read_projection(successor_id)
            self.assertEqual(source_id, expanded["stages"]["generate"]["reuse"]["source_run"])
            self.assertEqual(["mjcf", "publish"], current["execution_scope"])
            self.assertEqual(current["execution_scope"], portal._registered_stage_view(
                portal._fact_display_job(expanded), expanded,
            )["execution_scope"])
            for row in current["stages"][:5]:
                self.assertFalse(row["in_scope"])
                self.assertEqual("reused", row["state"])
                self.assertIsNone(row["command"])
                self.assertIsNone(row["decision"])
            self.assertEqual(source_decisions, [row["decision"] for row in projection(source_id)["stages"]])
            self.assertEqual({"mjcf", "publish"}, {row["stage"] for row in current["stages"] if row["decision"]})
            corrupted = copy.deepcopy(expanded)
            corrupted["by_stage"]["mjcf"]["mjcf/robot.xml"]["sha256"] = "f" * 64
            with self.assertRaises(PipelineError):
                portal._preview_from_projection(successor_id, corrupted)
            status, body = call("GET", f"/api/runs/{successor_dag}")
            self.assertEqual(200, status, body)
            view = json.loads(body)
            self.assertFalse(view["stage_reruns"][0]["eligible"])
            self.assertFalse(portal._mjcf_successor_row(expanded, principal="cli:tenant:other")["eligible"])
            self.assertEqual(["freeze", "mjcf"], [row["stage"] for row in view["stage_reruns"]])
            self.assertEqual("passed", view["report"]["overall"]["state"])
            failed_projection = {"stages": {"mjcf": {"state": "failed"}}}
            self.assertEqual("failed", _automatic_summary({"status": "failed"}, failed_projection)["state"])
