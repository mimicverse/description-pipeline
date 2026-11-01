"""Explicit Freeze successor request identity and immutable replay contracts."""
from __future__ import annotations

import json
import unittest

from tests.v1 import test_portal as portal_tests


class FreezeRequestIdentityTests(unittest.TestCase):
    def setUp(self):
        self.case = portal_tests.PortalTests()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.case._seed_retryable_run()
        self.case.client.login()
        self.body = {"from_stage": "freeze", "target_hardware": "m3.1",
                     "change_summary": "Current inputs", "main_assembly": "parts/arm.SLDASM"}

    def submit(self, key):
        return self.case.client.request("POST", "/api/runs/"+portal_tests.DAG_RUN_ID+"/attempts", self.body,
                                        headers={} if key is None else {"Idempotency-Key": key})

    def test_missing_or_invalid_key_is_rejected_before_admission(self):
        for key in (None, "../unsafe", "x"*121):
            with self.subTest(key=key):
                self.assertEqual(self.submit(key)[0], 400)
        self.assertEqual(self.case.production_application.stage_rerun_requests, [])

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
