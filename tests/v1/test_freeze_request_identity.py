"""Explicit Freeze successor request identity and immutable replay contracts."""
from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from description_pipeline.pipeline.contracts import ProtocolError
from description_pipeline.pipeline.ingestion import ProductionAdmissionFactory, ProductionFreezeSuccessorRequest
from tests.v1.test_pipeline_admission import AdmissionHarness
from tests.v1 import test_portal as portal_tests
from tests.v1.test_production_admission import _admission_outcome


class FreezeRequestIdentityTests(unittest.TestCase):
    def setUp(self):
        self.case = portal_tests.PortalTests()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.case._seed_retryable_run()
        self.case.client.login()
        self.body = {"stage": "freeze", "target_hardware": "m3.1",
                     "change_summary": "Current inputs", "main_assembly": "parts/arm.SLDASM"}

    def submit(self, key):
        return self.case.client.request("POST", "/api/runs/"+portal_tests.DAG_RUN_ID+"/attempts", self.body,
                                        headers={} if key is None else {"Idempotency-Key": key})

    def test_missing_or_invalid_key_is_rejected_before_admission(self):
        for key in (None, "../unsafe", "x"*121):
            with self.subTest(key=key):
                self.assertEqual(self.submit(key)[0], 400)
        self.assertEqual(self.case.production_application.freeze_successor_requests, [])

    def test_replay_uses_same_id_and_new_intent_uses_a_new_id(self):
        first = self.submit("first-key")
        replay = self.submit("first-key")
        second = self.submit("second-key")
        self.assertEqual([first[0], replay[0], second[0]], [202, 202, 202])
        self.assertEqual(json.loads(first[2])["dag_run_id"], "portal-freeze-first-key")
        self.assertEqual(json.loads(replay[2])["dag_run_id"], "portal-freeze-first-key")
        self.assertEqual(json.loads(second[2])["dag_run_id"], "portal-freeze-second-key")
        self.assertEqual(len(self.case.airflow.trigger_payloads), 2)

    def test_freeze_post_replay_recovers_same_admitted_dispatch(self):
        self.case.airflow.trigger_fail_times = 1
        first = self.submit("recover-freeze")
        self.assertEqual(first[0], 502)
        self.assertEqual(len(self.case.airflow.trigger_payloads), 1)
        self.assertEqual(self.case.production_application.dispatch_retry_modes, [False])
        second = self.submit("recover-freeze")
        self.assertEqual(second[0], 202, second[2])
        self.assertEqual(json.loads(second[2])["dag_run_id"], "portal-freeze-recover-freeze")
        self.assertEqual(len(self.case.airflow.trigger_payloads), 2)
        self.assertEqual(self.case.production_application.dispatch_retry_modes, [False, True])

    def test_upload_post_replay_recovers_same_admitted_dispatch(self):
        self.case.airflow.trigger_fail_times = 1
        body = portal_tests._upload_body(fields=list(portal_tests.UPLOAD_CHOICES))
        def submit():
            return self.case.client.request_raw(
                "POST", "/api/runs", body, "multipart/form-data; boundary=portal-boundary",
                headers={"Idempotency-Key": "recover-upload"},
            )
        first = submit()
        self.assertEqual(first[0], 502)
        self.assertEqual(len(self.case.airflow.trigger_payloads), 1)
        self.assertEqual(self.case.production_application.dispatch_retry_modes, [False])
        second = submit()
        self.assertEqual(second[0], 201, second[2])
        self.assertEqual(json.loads(second[2])["dag_run_id"], "portal-recover-upload")
        self.assertEqual(len(self.case.airflow.trigger_payloads), 2)
        self.assertEqual(self.case.production_application.dispatch_retry_modes, [False, True])


class FreezeFactoryReplayTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="freeze-replay-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.harness = AdmissionHarness(self.root)
        self.spec = replace(self.harness.spec, parent_run_id="parent", from_stage="freeze")
        self.outcome = _admission_outcome(self.harness.store, self.spec)
        self.application = SimpleNamespace(
            load_verified_freeze_origins=lambda run_id: self.harness.originals,
            load_admission_outcome=lambda run_id: self.outcome if run_id == self.spec.run_id else None,
        )
        (self.root/"work").mkdir()
        self.factory = ProductionAdmissionFactory(
            artifacts=self.harness.store, platform_snapshot=self.root/"unavailable-current-config",
            native_tool_provider=Mock(side_effect=AssertionError("Replay must not read current tools")),
            portable_tool_provider=Mock(side_effect=AssertionError("Replay must not read current tools")),
            work_root=self.root/"work",
        )
        self.request = ProductionFreezeSuccessorRequest(
            run_id=self.spec.run_id, request_id=self.spec.request_id, parent_run_id=self.spec.parent_run_id,
            main_assembly=self.spec.main_assembly, target_hardware=self.spec.target_hardware,
            purpose=self.spec.purpose, change_summary=self.spec.change_summary, principal=self.spec.principal,
        )

    def test_same_request_reuses_sealed_inputs_after_current_config_changes(self):
        self.assertEqual(self.factory.admit_from_frozen_freeze(self.application, self.request), self.outcome)

    def test_same_key_with_changed_parent_or_parameters_conflicts(self):
        for changed in (replace(self.request, parent_run_id="another-parent"),
                        replace(self.request, change_summary="Different intent")):
            with self.subTest(request=changed), self.assertRaises(ProtocolError):
                self.factory.admit_from_frozen_freeze(self.application, changed)

    def test_new_key_enters_current_preparation_instead_of_returning_old_outcome(self):
        prepared = Mock()
        prepared.register.return_value = "new-outcome"
        with patch.object(self.factory, "_prepare", return_value=prepared) as prepare:
            changed = replace(self.request, run_id="new-run", request_id="new-request")
            self.assertEqual(self.factory.admit_from_frozen_freeze(self.application, changed), "new-outcome")
            prepare.assert_called_once()
            prepared.register.assert_called_once_with(self.application)
