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

from description_pipeline.io import PipelineError
from description_pipeline.orchestration import portal as portal_module
from description_pipeline.orchestration.airflow_client import EndpointError, native_run_id
from description_pipeline.orchestration.feishu_oauth import build_actor_name
from description_pipeline.platform import flow
from description_pipeline.stages import STAGE_IDS, require_complete, stage_view

from .portal_read_support import RegisteredPlatform
from .protocol_support import protocol_events

RUN = "7c9b44b3-0b9c-5ffe-9f4f-4bbdafb4a865"
SUBJECT = "a" * 64
HANDOFF = "b" * 64


def _publish_receipt(*, submission_state: str = "published", quality_subject: str = SUBJECT) -> dict:
    return {
        "run_id": RUN,
        "stage": "publish",
        "state": submission_state,
        "passed": True,
        "subject_sha256": SUBJECT,
        "handoff_sha256": HANDOFF,
        "quality": {"passed": True, "subject_sha256": quality_subject},
        "submission": {
            "passed": True,
            "state": submission_state,
            "subject_sha256": SUBJECT,
            "commit": "c" * 40,
            "base": "feature/robot",
            "repository_slug": "example/robot",
            "branch": "work/description/robot/0123456789abcdef01234567",
            "url": "https://github.com/example/robot/pull/1",
        },
    }


def _register_delivery(
    platform: RegisteredPlatform,
    *,
    run_id: str = RUN,
    submission_state: str = "published",
    quality_subject: str = SUBJECT,
) -> None:
    """One registered six-stage delivery: records, sealed capture identity and check events."""
    platform.admit(run_id)
    platform.register_capture(run_id)
    for stage in ("freeze", "discover", "generate"):
        platform.stage(run_id, stage)
    platform.stage(
        run_id,
        "verify",
        receipt={
            "subject_sha256": SUBJECT,
            "handoff_sha256": HANDOFF,
            "quality": {"passed": True, "subject_sha256": SUBJECT},
        },
    )
    platform.stage(
        run_id, "publish", receipt=_publish_receipt(submission_state=submission_state, quality_subject=quality_subject)
    )
    platform.events(run_id, protocol_events(subject=SUBJECT))


class RegisteredDeliveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="registered-delivery-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.platform = RegisteredPlatform(self.tmp / "platform")

    def test_snapshot_reads_the_registered_records_only(self) -> None:
        _register_delivery(self.platform)
        self.platform.conclude(RUN, "succeeded")
        snapshot = flow.registered_delivery_snapshot(self.platform.store, RUN)
        self.assertEqual(snapshot["run_id"], RUN)
        self.assertEqual(snapshot["status"], "passed")
        self.assertEqual(snapshot["result"]["passed"], True)
        self.assertEqual(snapshot["result"]["quality"]["subject_sha256"], SUBJECT)
        self.assertEqual(snapshot["result"]["submission"]["state"], "published")
        self.assertEqual(snapshot["request"]["handoff_sha256"], HANDOFF)
        # The registered capture tree binds hardware/revision; no chain is consulted here.
        self.assertEqual((snapshot["hardware_id"], snapshot["revision"]), ("robot", "r1"))
        # Check observations and the stage outcomes both came off the recorded stream.
        stages = {stage["id"]: stage for stage in stage_view(snapshot)["stages"]}
        self.assertEqual({stage["state"] for stage in stages.values()}, {"completed"})
        self.assertEqual(stages["verify"]["checks_passed"], stages["verify"]["checks_total"])
        require_complete(stage_view(snapshot))

    def test_confirmation_rows_render_as_stage_state_events(self) -> None:
        """A stage confirmation row without a stage key renders as its stage's state event."""
        _register_delivery(self.platform)
        self.platform.world.events.append((RUN, 999, "stage", "freeze", None, json.dumps({"state": "completed"})))
        events = flow.registered_delivery_events(self.platform.store, RUN)
        freeze = [event for event in events if event.get("stage") == "freeze"]
        self.assertTrue(freeze)
        self.assertEqual(freeze[-1]["state"], "completed")
        self.assertIsInstance(freeze[-1].get("at"), str)
        self.assertTrue(all(event.get("stage") for event in events))

    def test_an_incomplete_registration_is_refused(self) -> None:
        self.platform.admit(RUN)
        _ = self.platform.tree(RUN, "capture")
        for stage in ("freeze", "discover", "capture"):
            self.platform.stage(RUN, stage)
        snapshot = flow.registered_delivery_snapshot(self.platform.store, RUN)
        with self.assertRaises(PipelineError):
            require_complete(stage_view(snapshot))

    def test_a_run_without_registration_is_refused(self) -> None:
        with self.assertRaisesRegex(PipelineError, "no platform registration"):
            flow.registered_delivery_snapshot(self.platform.store, RUN)

    def test_the_bound_submission_chain_decides_the_result(self) -> None:
        _register_delivery(self.platform, submission_state="failed")
        self.platform.conclude(RUN, "succeeded")
        snapshot = flow.registered_delivery_snapshot(self.platform.store, RUN)
        self.assertEqual(snapshot["result"]["passed"], False)
        _register_delivery(self.platform, quality_subject="f" * 64)
        self.platform.conclude(RUN, "succeeded")
        snapshot = flow.registered_delivery_snapshot(self.platform.store, RUN)
        self.assertEqual(snapshot["result"]["passed"], False)

    def test_a_long_recorded_history_is_read_whole(self) -> None:
        """The reader pages the full recorded history: a fixed page never truncates it."""
        _register_delivery(self.platform)
        self.platform.conclude(RUN, "succeeded")
        protocol = list(self.platform.world.events)
        prefix = [
            (RUN, index, "activity", "capture", None, json.dumps({"stage": "capture", "state": "running"}))
            for index in range(1, 251)
        ]
        self.platform.world.events = prefix + [
            (run, seq + len(prefix), kind, stage, op, payload) for run, seq, kind, stage, op, payload in protocol
        ]
        events = flow.registered_delivery_events(self.platform.store, RUN)
        self.assertEqual(len(events), len(self.platform.world.events))
        snapshot = flow.registered_delivery_snapshot(self.platform.store, RUN)
        self.assertEqual(len(snapshot["events"]), len(self.platform.world.events))
        require_complete(stage_view(snapshot))

    def test_the_registered_run_outcome_is_authoritative(self) -> None:
        """A passing receipt never flips a non-succeeded registered outcome."""
        for state, expected_status in (
            ("failed", "failed"),
            ("cancelled", "failed"),
            ("unresolved", "failed"),
            ("running", "running"),
        ):
            with self.subTest(state=state):
                platform = RegisteredPlatform(self.tmp / f"outcome-{state}")
                _register_delivery(platform)
                platform.conclude(RUN, state)
                snapshot = flow.registered_delivery_snapshot(platform.store, RUN)
                self.assertEqual(snapshot["result"]["passed"], False)
                self.assertEqual(snapshot["status"], expected_status)

    def test_a_damaged_sealed_capture_withholds_identity(self) -> None:
        """The identity is consumed only through the sealed rebinding: forged bytes
        grant nothing, and restoring the registered bytes restores the identity."""
        _register_delivery(self.platform)
        self.platform.conclude(RUN, "succeeded")
        source = self.platform.sealed(f"store://sealed/{RUN}/capture") / "input/source.json"
        original = source.read_bytes()
        snapshot = flow.registered_delivery_snapshot(self.platform.store, RUN)
        self.assertEqual((snapshot["hardware_id"], snapshot["revision"]), ("robot", "r1"))
        source.write_bytes(
            json.dumps({"identity": {"hardware_id": "forged"}, "metadata": {"revision": "forged"}}).encode()
        )
        snapshot = flow.registered_delivery_snapshot(self.platform.store, RUN)
        self.assertEqual((snapshot["hardware_id"], snapshot["revision"]), (None, None))
        source.write_bytes(original)
        snapshot = flow.registered_delivery_snapshot(self.platform.store, RUN)
        self.assertEqual((snapshot["hardware_id"], snapshot["revision"]), ("robot", "r1"))

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
            for stage in ("freeze", "discover", "capture", "generate", "verify", "publish")
        ]


class _Endpoint:
    def __init__(self, error):
        self.error = error

    def get_job(self, run_id):
        assert run_id == WSGI_RUN
        if self.error is None:
            raise EndpointError("local unavailable Windows observation")
        return {"run_id": run_id, "status": "failed", "error": self.error}


class RegisteredFailureReadoutTests(unittest.TestCase):
    """The registered failure is the error authority the run page reads."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="registered-failure-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _failed_platform(self, stage: str, **failure_fields) -> tuple[RegisteredPlatform, str]:
        platform = RegisteredPlatform(self.tmp / f"failure-{stage}")
        platform.admit(WSGI_RUN)
        original = f"RuntimeError: registered {stage} failure before products"
        platform.store.confirm_stage(
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
            platform.store.confirm_stage(run_id=WSGI_RUN, stage=later, state="blocked")
        return platform, original

    def _get(self, platform: RegisteredPlatform, endpoint) -> tuple[int, bytes]:
        app = portal_module.PortalApp(
            portal_module.PortalConfig(
                airflow=_PermissiveAirflow(),
                endpoint=lambda: endpoint,
                platform_store=platform.store,
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
        """Windows-down, matching-native and stale-native all read the registration."""
        for stage in ("freeze", "generate"):
            for mode in ("down", "matching", "stale"):
                with self.subTest(stage=stage, mode=mode):
                    platform, original = self._failed_platform(stage)
                    native_error = {
                        "down": None,
                        "matching": original,
                        "stale": "RuntimeError: stale native failure",
                    }[mode]
                    status, body = self._get(platform, _Endpoint(native_error))
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
        status, body = self._get(platform, _Endpoint(None))
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(payload["job"]["detail"], "remote: denied")
        self.assertEqual(payload["job"]["diagnostic_path"], "failure/retained")
        self.assertEqual(payload["job"]["failure_diagnostics"], diagnostic)
        self.assertEqual(payload["report"]["failure"]["evidence_path"], "failure/retained")
        self.assertEqual(payload["report"]["failure"]["cause"], "remote: denied")

    def test_a_registered_success_keeps_its_closure_and_observes_the_native_error(self) -> None:
        platform = RegisteredPlatform(self.tmp / "success")
        _register_delivery(platform, run_id=WSGI_RUN)
        platform.conclude(WSGI_RUN, "succeeded")
        status, body = self._get(platform, _Endpoint(None))
        payload = json.loads(body)
        self.assertEqual(status, 200)
        self.assertEqual(payload["job"]["status"], "passed")
        self.assertIsNone(payload["job"]["error"])
        self.assertEqual(payload["pr"]["url"], "https://github.com/example/robot/pull/1")
        # A stale native error stays an observation and never flips the outcome.
        status, body = self._get(platform, _Endpoint("RuntimeError: stale native failure"))
        payload = json.loads(body)
        self.assertEqual(payload["job"]["status"], "passed")
        self.assertEqual(payload["job"]["error"], "RuntimeError: stale native failure")
        self.assertEqual(payload["pr"]["url"], "https://github.com/example/robot/pull/1")


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
            for stage in ("freeze", "discover", "capture", "generate", "verify", "publish")
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


class _RunningEndpoint:
    def get_job(self, run_id):
        assert run_id == WSGI_RUN
        return {"run_id": run_id, "status": "running", "events": []}


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
                endpoint=_RunningEndpoint(),
                platform_store=platform.store,
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
                platform.store.confirm_stage(
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
        self.assertEqual(status, 409, payload)
        self.assertEqual(payload.get("reason"), "registration_missing")
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

        def get_retry(platform: RegisteredPlatform) -> dict:
            airflow = _RetryAirflow()
            app = portal_module.PortalApp(
                portal_module.PortalConfig(
                    airflow=airflow,
                    endpoint=_RunningEndpoint(),
                    platform_store=platform.store,
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
            return json.loads(body)["retry"]

        unregistered = RegisteredPlatform(self.tmp / "advice-missing")
        advice = get_retry(unregistered)
        self.assertEqual(advice["eligible"], False, advice)
        self.assertEqual(advice["reason"], "registration_missing")
        running = RegisteredPlatform(self.tmp / "advice-running")
        running.admit(WSGI_RUN)
        advice = get_retry(running)
        self.assertEqual(advice["eligible"], True, advice)
        unresolved = RegisteredPlatform(self.tmp / "advice-unresolved")
        unresolved.admit(WSGI_RUN)
        unresolved.store.confirm_stage(
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
