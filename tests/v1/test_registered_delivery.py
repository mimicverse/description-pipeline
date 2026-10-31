"""The registered delivery snapshot: the run/confirmation read path off the platform records.

The snapshot is what the delivery confirmation consumes: the registered run
row, the recorded event stream and the sealed stage receipts - never a store
working directory or a file projection.  These tests pin that read surface and
the six-stage completeness gate it feeds.
"""

from __future__ import annotations

import json
import io
import shutil
import tempfile
import unittest
from pathlib import Path

from description_pipeline.orchestration import portal as portal_module
from description_pipeline.orchestration.airflow_client import native_run_id
from description_pipeline.orchestration.feishu_oauth import build_actor_name
from description_pipeline.stages import STAGE_IDS
from description_pipeline.io import digest, read_data, write_json
from description_pipeline.schemas import PUBLICATION_RECEIPT_SCHEMA

from .portal_read_support import RegisteredPlatform, registered_check_fixture
from .protocol_support import protocol_events

RUN = "7c9b44b3-0b9c-5ffe-9f4f-4bbdafb4a865"
SUBJECT = "a" * 64
def _register_delivery(platform: RegisteredPlatform, *, run_id: str = RUN) -> None:
    """Register seven read-model stages with their sealed certificate and publication receipt."""
    platform.admit(run_id)
    capture = platform.register_capture(run_id)
    write_json(
        platform.tree(run_id, "discover") / "prepared/discovery/native-discovery.json",
        read_data(capture / "input/discovery/native-discovery.json"),
    )
    for stage in ("freeze", "discover", "generate", "verify", "mjcf", "publish"):
        platform.stage(run_id, stage)
    write_json(
        platform.tree(run_id, "verify") / "verification-certificate.json",
        {"passed": True, "decision": "PASS", "checks": [], "diagnostics": []},
    )
    write_json(platform.tree(run_id, "mjcf") / "receipt.json", {})
    command, _checks = registered_check_fixture(run_id, "publish")
    publication = {
        "schema_version": PUBLICATION_RECEIPT_SCHEMA,
        "run_id": run_id,
        "command_id": command["command_id"],
        "request_digest": command["request_digest"],
        "publication_subject_sha256": SUBJECT,
        "base_ref": "feature/robot",
        "repository": "example/robot",
        "branch_ref": "work/description/robot/0123456789abcdef01234567",
        "candidate_parents": ["d" * 40],
        "pr_effect": {
            "observation": {
                "number": 1,
                "head_oid": "c" * 40,
                "url": "https://github.com/example/robot/pull/1",
            },
        },
    }
    write_json(platform.tree(run_id, "publish") / "publication-receipt.json", publication)
    platform.stage(run_id, "publish", receipt={"checks_sha256": digest(publication)})
    platform.events(run_id, protocol_events(subject=SUBJECT))


DAG = "portal-20261016T000002-feedcafe"
WSGI_RUN = native_run_id(DAG)


class _PermissiveAirflow:
    def profile(self, token):
        return {
            "principal": "fixture:local:reader",
            "app_id": "fixture",
            "tenant_key": "local",
            "open_id": "reader",
            "name": "synthetic",
            "role": "USER",
        }

    def dag_run(self, token, dag_id, dag_run_id):
        return {
            "dag_run_id": dag_run_id,
            "state": "failed",
            "conf": {},
            "triggering_user_name": "synthetic",
        }

    def task_instances(self, token, dag_id, dag_run_id):
        return [
            {"task_id": stage, "state": "failed" if stage in {"freeze", "capture"} else "upstream_failed"}
            for stage in ("freeze", "discover", "capture", "generate", "verify", "mjcf", "publish")
        ]


class RegisteredFailureReadoutTests(unittest.TestCase):
    """The registered failure is the error authority the run page reads."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="registered-failure-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _failed_platform(self, stage: str, **failure_fields) -> tuple[RegisteredPlatform, str]:
        platform = RegisteredPlatform(self.tmp / f"failure-{stage}")
        platform.admit(WSGI_RUN)
        original = f"RuntimeError: registered {stage} failure before products"
        platform.facts.confirm_stage(
            run_id=WSGI_RUN,
            stage=stage,
            state="failed",
            receipt={
                "schema_version": "solidworks-to-urdf.bundle/v1",
                "pipeline_id": "solidworks-to-urdf",
                "run_id": WSGI_RUN,
                "stage": stage,
                "state": "failed",
                "passed": False,
                "error": original,
                "error_code": "fixture",
                "detail": {"where": stage},
                **failure_fields,
            },
        )
        # The convergence registers every later stage blocked after the failure: the trailing
        # events must never claim the error's stage.
        for later in STAGE_IDS[STAGE_IDS.index(stage) + 1 :]:
            platform.facts.confirm_stage(
                run_id=WSGI_RUN,
                stage=later,
                state="blocked",
            )
        return platform, original

    def _get(self, platform: RegisteredPlatform) -> tuple[int, bytes]:
        app = portal_module.PortalApp(
            portal_module.PortalConfig(
                airflow=_PermissiveAirflow(),
                run_control_store=platform.run_control,
                config_identity_provider=lambda: "c" * 64,
            )
        )
        session = app.sessions.create("user", "token", principal="fixture:local:reader")
        response: dict = {}

        def start(status, headers):
            response["status"] = int(status.split()[0])

        body = b"".join(
            app(
                {
                    "REQUEST_METHOD": "GET",
                    "PATH_INFO": f"/api/runs/{DAG}",
                    "HTTP_COOKIE": f"{portal_module.DEFAULT_SESSION_COOKIE}={session.session_id}",
                    "wsgi.input": io.BytesIO(b""),
                },
                start,
            )
        )
        return response["status"], body

    def test_the_registered_failure_is_the_readout_authority(self) -> None:
        """The registered failure and its failed stage own the run-page readout."""
        for stage in ("freeze", "generate"):
            with self.subTest(stage=stage):
                platform, original = self._failed_platform(stage)
                status, body = self._get(platform)
                payload = json.loads(body)
                self.assertEqual(status, 200)
                self.assertEqual(payload["job"]["error"], original)
                self.assertIn(original, body.decode("utf-8"))
                finding = next(
                    row for row in payload["findings"] if original in str(row.get("message", ""))
                )
                self.assertEqual(
                    finding["stage"],
                    stage,
                    "the registered failed stage owns the error; a later blocked event never does",
                )

    def test_registered_failure_preserves_string_detail_and_diagnostic_identity(self) -> None:
        diagnostic = {"path": "diagnostic.json", "sha256": "d" * 64}
        platform, _ = self._failed_platform(
            "publish", detail="remote: denied", diagnostic_path="failure/retained",
            failure_diagnostics=diagnostic,
        )
        status, body = self._get(platform)
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(payload["job"]["detail"], "remote: denied")
        self.assertEqual(payload["job"]["diagnostic_path"], "failure/retained")
        self.assertEqual(payload["job"]["failure_diagnostics"], diagnostic)
        self.assertEqual(payload["report"]["failure"]["evidence_path"], "failure/retained")
        self.assertEqual(payload["report"]["failure"]["cause"], "remote: denied")

    def test_a_registered_success_keeps_its_canonical_closure(self) -> None:
        platform = RegisteredPlatform(self.tmp / "success")
        _register_delivery(platform, run_id=WSGI_RUN)
        platform.conclude(WSGI_RUN, "succeeded")
        status, body = self._get(platform)
        payload = json.loads(body)
        self.assertEqual(status, 200)
        self.assertEqual(payload["job"]["status"], "passed")
        self.assertTrue(all(stage["state"] == "completed" for stage in payload["stage_view"]["stages"]))
        self.assertIsNone(payload["job"]["error"])
        self.assertEqual(payload["pr"]["url"], "https://github.com/example/robot/pull/1")


class RegisteredStagePresentationTests(unittest.TestCase):
    """The actual run API projects registered stage rows without a coarse override."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="registered-stage-display-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _platform(self, capture_state: str) -> RegisteredPlatform:
        platform = RegisteredPlatform(self.tmp / capture_state)
        platform.admit(WSGI_RUN)
        if capture_state == "completed":
            platform.register_capture(WSGI_RUN)
        states = {
            "freeze": "completed",
            "discover": "completed",
            "capture": capture_state,
            "generate": "blocked",
            "verify": "blocked",
            "mjcf": "blocked",
            "publish": "blocked",
        }
        for stage, state in states.items():
            platform.stage(WSGI_RUN, stage, state=state)
        platform.events(WSGI_RUN, protocol_events(subject=SUBJECT))
        return platform

    def _get(self, platform: RegisteredPlatform) -> dict:
        app = portal_module.PortalApp(
            portal_module.PortalConfig(
                airflow=_PermissiveAirflow(),
                run_control_store=platform.run_control,
                config_identity_provider=lambda: "c" * 64,
            )
        )
        session = app.sessions.create("user", "token", principal="fixture:local:reader")
        response: dict = {}

        def start(status, headers):
            response["status"] = int(status.split()[0])

        body = b"".join(
            app(
                {
                    "REQUEST_METHOD": "GET",
                    "PATH_INFO": f"/api/runs/{DAG}",
                    "HTTP_COOKIE": f"{portal_module.DEFAULT_SESSION_COOKIE}={session.session_id}",
                    "wsgi.input": io.BytesIO(b""),
                },
                start,
            )
        )
        self.assertEqual(response["status"], 200)
        return json.loads(body)

    def test_actual_run_api_preserves_canonical_stage_and_report_states(self) -> None:
        expected = {
            "active": ("running", "running", "In progress"),
            "waiting": ("waiting", "waiting", "Waiting"),
            "blocked": ("blocked", "blocked", "Blocked by incomplete predecessor"),
            "failed": ("failed", "failed", "Failed"),
            "cancelled": ("cancelled", "cancelled", "Cancelled"),
            "unresolved": ("unresolved", "unresolved", "Unresolved"),
        }
        for canonical, (stage_state, overall_state, stage_label) in expected.items():
            with self.subTest(state=canonical):
                payload = self._get(self._platform(canonical))
                stages = {
                    stage["id"]: stage for stage in payload["stage_view"]["stages"]
                }
                report_stages = {
                    stage["id"]: stage for stage in payload["report"]["stages"]
                }
                self.assertEqual(stages["capture"]["state"], stage_state)
                self.assertEqual(payload["report"]["overall"]["state"], overall_state)
                self.assertEqual(report_stages["capture"]["state_label"], stage_label)
                if canonical == "cancelled":
                    self.assertEqual(payload["job"]["status"], "cancelled")
                    self.assertEqual(payload["automatic"]["state"], "cancelled")
                    self.assertEqual(payload["activity"]["state"], "finished")
                for downstream in ("generate", "verify", "publish"):
                    self.assertEqual(stages[downstream]["state"], "blocked")
                    self.assertEqual(
                        report_stages[downstream]["state_label"],
                        "Blocked by incomplete predecessor",
                    )

    def test_actual_run_api_fails_closed_for_noncanonical_stage_states(self) -> None:
        for injected in ("queued", "ready", "invented"):
            with self.subTest(state=injected):
                payload = self._get(self._platform(injected))
                stages = {
                    stage["id"]: stage for stage in payload["stage_view"]["stages"]
                }
                report_stages = {
                    stage["id"]: stage for stage in payload["report"]["stages"]
                }
                self.assertEqual(stages["capture"]["state"], "not_run")
                self.assertEqual(report_stages["capture"]["state"], "not_run")

    def test_actual_run_api_prioritizes_unresolved_over_other_terminal_rows(self) -> None:
        platform = self._platform("unresolved")
        platform.stage(WSGI_RUN, "verify", state="failed")
        payload = self._get(platform)
        stages = {stage["id"]: stage for stage in payload["stage_view"]["stages"]}
        self.assertEqual(stages["capture"]["state"], "unresolved")
        self.assertEqual(stages["verify"]["state"], "failed")
        self.assertEqual(payload["report"]["overall"]["state"], "unresolved")

    def test_actual_run_api_keeps_canonical_decision_above_live_qc(self) -> None:
        platform = self._platform("completed")
        events = protocol_events(subject=SUBJECT)
        capture_checks = [
            event
            for event in events
            if event["stage"] == "capture" and isinstance(event.get("check"), dict)
        ]
        self.assertTrue(capture_checks)
        capture_checks[0]["check"]["state"] = "failed"
        platform.events(WSGI_RUN, events)

        payload = self._get(platform)
        stages = {stage["id"]: stage for stage in payload["stage_view"]["stages"]}
        self.assertEqual(stages["capture"]["state"], "completed")
        self.assertTrue(all(
            check["state"] == "passed"
            for check in stages["capture"]["input_qc"] + stages["capture"]["output_qc"]
        ))
        self.assertIsNone(payload["report"]["failure"])


class _RetryAirflow(_PermissiveAirflow):
    """A retry-capable Airflow double that records every clear it is asked for."""

    def __init__(self) -> None:
        self.clears: list[dict] = []

    def dag_run(self, token, dag_id, dag_run_id):
        return {
            "dag_run_id": dag_run_id,
            "state": "failed",
            "conf": {},
            "triggering_user_name": build_actor_name("fixture:local:reader", "synthetic"),
        }

    def task_instances(self, token, dag_id, dag_run_id):
        return [
            {
                "task_id": stage,
                "state": (
                    "failed"
                    if stage == "capture"
                    else "upstream_failed"
                    if stage in {"generate", "verify", "publish"}
                    else "success"
                ),
            }
            for stage in ("freeze", "discover", "capture", "generate", "verify", "mjcf", "publish")
        ]

    def clear_dag_run(self, token, dag_id, dag_run_id, *, dry_run):
        self.clears.append({"dry_run": dry_run})
        selected = [row for row in self.task_instances(token, dag_id, dag_run_id) if row["state"] != "success"]
        return {
            "dag_id": dag_id,
            "dag_run_id": dag_run_id,
            "state": "queued",
            "task_instances": selected,
            "total_entries": len(selected),
        }


class RegisteredRetryAuthorityTests(unittest.TestCase):
    """The registered terminal outcome owns the retry decision before any clear."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="registered-retry-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _post_retry(self, platform: RegisteredPlatform) -> tuple[int, dict, list]:
        airflow = _RetryAirflow()
        app = portal_module.PortalApp(
            portal_module.PortalConfig(
                airflow=airflow,
                run_control_store=platform.run_control,
                config_identity_provider=lambda: "c" * 64,
            )
        )
        session = app.sessions.create("user", "token", principal="fixture:local:reader")
        response: dict = {}

        def start(status, headers):
            response["status"] = int(status.split()[0])

        body = b"".join(
            app(
                {
                    "REQUEST_METHOD": "POST",
                    "PATH_INFO": f"/api/runs/{DAG}/retry",
                    "HTTP_COOKIE": f"{portal_module.DEFAULT_SESSION_COOKIE}={session.session_id}",
                    "HTTP_X_CSRF_TOKEN": session.csrf_token,
                    "CONTENT_LENGTH": "2",
                    "wsgi.input": io.BytesIO(b"{}"),
                },
                start,
            )
        )
        return response["status"], json.loads(body), airflow.clears

    def test_registered_terminal_outcomes_refuse_retry_without_clearing(self) -> None:
        """failed/cancelled/unresolved registrations all refuse before any clear."""
        for state in ("failed", "cancelled", "unresolved"):
            with self.subTest(state=state):
                platform = RegisteredPlatform(self.tmp / f"terminal-{state}")
                platform.admit(WSGI_RUN)
                platform.facts.confirm_stage(
                    run_id=WSGI_RUN,
                    stage="capture",
                    state=state,
                    receipt={
                        "schema_version": "solidworks-to-urdf.bundle/v1",
                        "pipeline_id": "solidworks-to-urdf",
                        "run_id": WSGI_RUN,
                        "stage": "capture",
                        "state": state,
                        "passed": False,
                        "error": "Registered authoritative native outcome",
                    },
                )
                status, payload, clears = self._post_retry(platform)
                self.assertEqual(status, 409, payload)
                self.assertEqual(payload.get("reason"), f"registered_{state}")
                self.assertEqual(clears, [], "no dry-run and no clear may touch a registered terminal run")

    def test_an_unregistered_failure_keeps_its_retry_positive(self) -> None:
        platform = RegisteredPlatform(self.tmp / "running")
        platform.admit(WSGI_RUN)
        status, payload, clears = self._post_retry(platform)
        self.assertEqual(status, 200, payload)
        self.assertEqual([entry["dry_run"] for entry in clears], [True, False])

    def test_missing_or_unknown_registration_refuses_retry_without_clearing(self) -> None:
        """Airflow and native facts never substitute for the platform admission."""
        platform = RegisteredPlatform(self.tmp / "not-admitted")
        status, payload, clears = self._post_retry(platform)
        self.assertEqual(status, 404, payload)
        self.assertEqual(payload.get("error"), "The run has no canonical admission")
        self.assertEqual(clears, [], "no dry-run and no clear without a platform registration")
        unknown = RegisteredPlatform(self.tmp / "unknown")
        unknown.admit(WSGI_RUN)
        unknown.world.run_states[WSGI_RUN] = "mystery"
        status, payload, clears = self._post_retry(unknown)
        self.assertEqual(status, 409, payload)
        self.assertEqual(payload.get("reason"), "registration_missing")
        self.assertEqual(clears, [])

    def test_the_displayed_retry_advice_matches_the_post_criterion(self) -> None:
        """The run page never offers an action the POST route would necessarily refuse."""

        def get_retry(platform: RegisteredPlatform, *, expected_status: int = 200) -> dict:
            airflow = _RetryAirflow()
            app = portal_module.PortalApp(
                portal_module.PortalConfig(
                    airflow=airflow,
                    run_control_store=platform.run_control,
                    config_identity_provider=lambda: "c" * 64,
                )
            )
            session = app.sessions.create("user", "token", principal="fixture:local:reader")
            response: dict = {}

            def start(status, headers):
                response["status"] = int(status.split()[0])

            body = b"".join(
                app(
                    {
                        "REQUEST_METHOD": "GET",
                        "PATH_INFO": f"/api/runs/{DAG}",
                        "HTTP_COOKIE": f"{portal_module.DEFAULT_SESSION_COOKIE}={session.session_id}",
                        "wsgi.input": io.BytesIO(b""),
                    },
                    start,
                )
            )
            self.assertEqual(response["status"], expected_status)
            payload = json.loads(body)
            return payload["retry"] if expected_status == 200 else payload

        unregistered = RegisteredPlatform(self.tmp / "advice-missing")
        advice = get_retry(unregistered, expected_status=404)
        self.assertEqual(advice["error"], "The run has no canonical admission")
        running = RegisteredPlatform(self.tmp / "advice-running")
        running.admit(WSGI_RUN)
        advice = get_retry(running)
        self.assertEqual(advice["eligible"], True, advice)
        unresolved = RegisteredPlatform(self.tmp / "advice-unresolved")
        unresolved.admit(WSGI_RUN)
        unresolved.facts.confirm_stage(
            run_id=WSGI_RUN,
            stage="capture",
            state="unresolved",
            receipt={
                "schema_version": "solidworks-to-urdf.bundle/v1",
                "pipeline_id": "solidworks-to-urdf",
                "run_id": WSGI_RUN,
                "stage": "capture",
                "state": "unresolved",
                "passed": False,
                "error": "Registered authoritative native outcome",
            },
        )
        advice = get_retry(unresolved)
        self.assertEqual(advice["eligible"], False, advice)
        self.assertEqual(advice["reason"], "registered_unresolved")
        # Stage-neutral: an unresolved outcome requires reconciliation; the
        # publication-specific classification belongs to publication evidence only.
        self.assertEqual(advice["classification"], "reconciliation_required")


if __name__ == "__main__":
    unittest.main()
