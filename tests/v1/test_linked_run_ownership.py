"""Linked-run authorization cannot be bypassed through direct Airflow creation."""

from __future__ import annotations

import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from description_pipeline.orchestration import request_context as context
from description_pipeline.orchestration.run_ownership import owner_create_allowed


class LinkedRunOwnershipTests(unittest.TestCase):
    def candidate(self, conf):
        return context.evaluate_create_candidate(json.dumps({"conf": conf}).encode(), oversize=False)

    def test_only_canonical_linkage_is_accepted(self):
        self.assertEqual(self.candidate({"handoff_path": "/input"}), {"safe": True, "parent_dag_run_id": None})
        self.assertEqual(
            self.candidate({"parent_dag_run_id": "original", "resume_from": "verify"}),
            {"safe": True, "parent_dag_run_id": "original"},
        )
        for conf in (
            {"parent_dag_run_id": "original"},
            {"resume_from": "capture"},
            {"parent_dag_run_id": "original", "resume_from": "arbitrary"},
            {"parent_dag_run_id": "original", "resume_from": ["verify"]},
            {"parent_dag_run_id": "original", "resume_from": "verify", "package": "other"},
            {"parent_dag_run_id": "~", "resume_from": "verify"},
            {"parent_dag_run_id": "original", "resume_from": "verify", "owner": "admin"},
        ):
            with self.subTest(conf=conf):
                self.assertIs(self.candidate(conf)["safe"], False)
        self.assertIs(
            context.evaluate_create_candidate(
                b'{"conf":{"parent_dag_run_id":"a","parent_dag_run_id":"b","resume_from":"verify"}}', oversize=False
            )["safe"],
            False,
        )
        self.assertIs(context.evaluate_create_candidate(b"{}", oversize=True)["safe"], False)

    def test_authority_comes_from_the_parent_record(self):
        user = SimpleNamespace(get_id=lambda: "trusted-user")
        self.assertFalse(owner_create_allowed(user, "workflow"))
        request = SimpleNamespace(
            scope={context.CREATE_BODY_SCOPE_KEY: {"safe": True, "parent_dag_run_id": "original"}}
        )
        for facts, allowed in (
            (("trusted-user", "success", {}), True),
            (("trusted-user", "failed", {}), True),
            (("other-user", "success", {}), False),
            ((None, "failed", {}), False),
            (("trusted-user", "running", {}), False),
            (None, False),
        ):
            with (
                self.subTest(facts=facts),
                context.use_request(request),
                patch(
                    "description_pipeline.orchestration.run_ownership.recorded_run_facts", return_value=facts
                ) as lookup,
            ):
                self.assertIs(owner_create_allowed(user, "workflow"), allowed)
                lookup.assert_called_once_with("workflow", "original")

    def test_buffer_replays_exact_create_bytes(self):
        raw = b'{"conf":{"parent_dag_run_id":"original","resume_from":"generate"}}'
        messages = [
            {"type": "http.request", "body": raw[:15], "more_body": True},
            {"type": "http.request", "body": raw[15:], "more_body": False},
        ]

        async def exercise():
            remaining = iter(messages)

            async def receive():
                return next(remaining)

            replay, candidate, oversized = await context._buffer_candidate(receive, context.evaluate_create_candidate)
            self.assertFalse(oversized)
            self.assertEqual(candidate["parent_dag_run_id"], "original")
            self.assertEqual([await replay(), await replay()], messages)

        asyncio.run(exercise())

    def test_create_route_shape_is_not_itself_an_authorization(self):
        for path in ("/api/v2/dags/workflow/dagRuns", "/dags/workflow/dagRuns"):
            self.assertTrue(context._selects_create_candidate({"method": "POST", "path": path}))
        self.assertFalse(context._selects_create_candidate({"method": "GET", "path": "/dags/workflow/dagRuns"}))
        user = SimpleNamespace(get_id=lambda: "trusted-user")
        with context.use_request(SimpleNamespace(scope={})):  # No evaluated body: deny.
            self.assertFalse(owner_create_allowed(user, "workflow"))
