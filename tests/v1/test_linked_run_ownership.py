"""Current DispatchConf creation authorization cannot be bypassed by direct API calls."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from description_pipeline.orchestration import request_context as context
from description_pipeline.orchestration.run_ownership import (
    CreateDispatchBinding,
    owner_create_allowed,
    recorded_create_binding,
)
from description_pipeline.pipeline.contracts import DispatchConf
from description_pipeline.schemas import AIRFLOW_DISPATCH_CONF_SCHEMA

RUN_ID = "run-worker"
FREEZE_COMMAND_ID = "freeze:worker"
DAG_RUN_ID = "portal-20261028T120000-1234abcd"
CONF = {
    "schema_version": AIRFLOW_DISPATCH_CONF_SCHEMA,
    "run_id": RUN_ID,
    "freeze_command_id": FREEZE_COMMAND_ID,
}


def create_body(
    *,
    conf: object = CONF,
    dag_run_id: object = DAG_RUN_ID,
    logical_date: object = None,
    extras: dict | None = None,
) -> bytes:
    body = {
        "conf": conf,
        "dag_run_id": dag_run_id,
        "logical_date": logical_date,
    }
    if extras:
        body.update(extras)
    return json.dumps(body).encode()


def binding(**overrides) -> CreateDispatchBinding:
    values = {
        "principal": "cli_app:tenant-a:ou_worker",
        "run_id": RUN_ID,
        "parent_run_id": None,
        "parent_dag_run_id": None,
        "dag_id": "solidworks_to_urdf",
        "dag_run_id": DAG_RUN_ID,
        "freeze_command_id": FREEZE_COMMAND_ID,
    }
    values.update(overrides)
    return CreateDispatchBinding(**values)


class LinkedRunOwnershipTests(unittest.TestCase):
    def candidate(self, raw: bytes) -> dict:
        return context.evaluate_create_candidate(raw, oversize=False)

    def test_current_dispatch_conf_is_the_only_admitted_create_body(self) -> None:
        self.assertEqual(
            self.candidate(create_body()),
            {
                "safe": True,
                "dag_run_id": DAG_RUN_ID,
                "conf": CONF,
            },
        )
        invalid = (
            create_body(logical_date="2026-10-28T12:00:00Z"),
            create_body(
                conf={
                    "handoff_path": "/input",
                    "purpose": "engineering",
                }
            ),
            create_body(conf={**CONF, "owner": "forged"}),
            create_body(conf={**CONF, "run_id": ""}),
            create_body(conf={**CONF, "freeze_command_id": ""}),
            create_body(conf={**CONF, "schema_version": "forged"}),
            create_body(dag_run_id="../escape"),
            create_body(extras={"purpose": "engineering"}),
            b"[]",
        )
        for raw in invalid:
            with self.subTest(raw=raw):
                self.assertIs(self.candidate(raw)["safe"], False)
        self.assertIs(
            self.candidate(
                b'{"dag_run_id":"a","dag_run_id":"b","logical_date":null,"conf":{}}'
            )["safe"],
            False,
        )
        self.assertIs(context.evaluate_create_candidate(b"{}", oversize=True)["safe"], False)

    def test_create_ownership_binds_admission_dispatch_and_authenticated_user(self) -> None:
        user = SimpleNamespace(get_id=lambda: "cli_app:tenant-a:ou_worker")
        candidate = self.candidate(create_body())
        request = SimpleNamespace(scope={context.CREATE_BODY_SCOPE_KEY: candidate})
        accepted = binding()
        cases = (
            (accepted, True),
            (binding(principal="cli_app:tenant-a:ou_other"), False),
            (binding(dag_id="other_dag"), False),
            (binding(run_id="run-other"), False),
            (binding(dag_run_id="portal-other"), False),
            (binding(freeze_command_id="freeze:other"), False),
            (
                binding(
                    parent_run_id="parent-platform-run",
                    parent_dag_run_id=None,
                ),
                False,
            ),
            (None, False),
        )
        for recorded, allowed in cases:
            with (
                self.subTest(recorded=recorded),
                context.use_request(request),
                patch(
                    "description_pipeline.orchestration.run_ownership.recorded_create_binding",
                    return_value=recorded,
                ) as lookup,
            ):
                self.assertIs(owner_create_allowed(user, "solidworks_to_urdf"), allowed)
                lookup.assert_called_once()

        malformed = SimpleNamespace(
            scope={
                context.CREATE_BODY_SCOPE_KEY: {
                    "safe": True,
                    "dag_run_id": DAG_RUN_ID,
                    "conf": {},
                }
            }
        )
        with (
            context.use_request(malformed),
            patch(
                "description_pipeline.orchestration.run_ownership.recorded_create_binding",
            ) as lookup,
        ):
            self.assertFalse(owner_create_allowed(user, "solidworks_to_urdf"))
            lookup.assert_not_called()

    def test_recorded_binding_reads_the_exact_operator_view(self) -> None:
        cursor = MagicMock()
        cursor.__enter__.return_value = cursor
        cursor.fetchall.return_value = [
            (
                "cli_app:tenant-a:ou_worker",
                RUN_ID,
                None,
                "solidworks_to_urdf",
                DAG_RUN_ID,
                FREEZE_COMMAND_ID,
                None,
                None,
            )
        ]
        connection = MagicMock()
        connection.__enter__.return_value = connection
        connection.cursor.return_value = cursor
        psycopg2 = SimpleNamespace(connect=MagicMock(return_value=connection))
        conf = DispatchConf.from_record(CONF)
        with (
            patch.dict(os.environ, {"PLATFORM_OPERATOR_DSN": "operator-ro-dsn"}),
            patch.dict(sys.modules, {"psycopg2": psycopg2}),
        ):
            binding = recorded_create_binding(DAG_RUN_ID, conf)
        self.assertEqual(
            binding,
            CreateDispatchBinding(
                principal="cli_app:tenant-a:ou_worker",
                run_id=RUN_ID,
                parent_run_id=None,
                parent_dag_run_id=None,
                dag_id="solidworks_to_urdf",
                dag_run_id=DAG_RUN_ID,
                freeze_command_id=FREEZE_COMMAND_ID,
            ),
        )
        psycopg2.connect.assert_called_once_with(
            "operator-ro-dsn",
            connect_timeout=5,
        )
        self.assertEqual(
            cursor.execute.call_args.args[1],
            (RUN_ID, DAG_RUN_ID, FREEZE_COMMAND_ID),
        )

    def test_recorded_binding_resolves_the_parent_airflow_run_id(self) -> None:
        cursor = MagicMock()
        cursor.__enter__.return_value = cursor
        cursor.fetchall.return_value = [
            (
                "cli_app:tenant-a:ou_worker",
                RUN_ID,
                "parent-platform-run",
                "solidworks_to_urdf",
                DAG_RUN_ID,
                FREEZE_COMMAND_ID,
                "solidworks_to_urdf",
                "parent-airflow-dag-run",
            )
        ]
        connection = MagicMock()
        connection.__enter__.return_value = connection
        connection.cursor.return_value = cursor
        psycopg2 = SimpleNamespace(connect=MagicMock(return_value=connection))
        with (
            patch.dict(os.environ, {"PLATFORM_OPERATOR_DSN": "operator-ro-dsn"}),
            patch.dict(sys.modules, {"psycopg2": psycopg2}),
        ):
            binding = recorded_create_binding(
                DAG_RUN_ID,
                DispatchConf.from_record(CONF),
            )
        self.assertIsNotNone(binding)
        self.assertEqual(binding.parent_run_id, "parent-platform-run")
        self.assertEqual(binding.parent_dag_run_id, "parent-airflow-dag-run")

    def test_successor_still_requires_owned_terminal_parent(self) -> None:
        user = SimpleNamespace(get_id=lambda: "cli_app:tenant-a:ou_worker")
        candidate = self.candidate(create_body())
        request = SimpleNamespace(scope={context.CREATE_BODY_SCOPE_KEY: candidate})
        successor = binding(
            parent_run_id="parent-platform-run",
            parent_dag_run_id="parent-airflow-dag-run",
        )
        for facts, allowed in (
            (("cli_app:tenant-a:ou_worker", "success", {}), True),
            (("cli_app:tenant-a:ou_worker", "failed", {}), True),
            (("cli_app:tenant-a:ou_other", "success", {}), False),
            ((None, "failed", {}), False),
            (("cli_app:tenant-a:ou_worker", "running", {}), False),
            (None, False),
        ):
            with (
                self.subTest(facts=facts),
                context.use_request(request),
                patch(
                    "description_pipeline.orchestration.run_ownership.recorded_create_binding",
                    return_value=successor,
                ),
                patch(
                    "description_pipeline.orchestration.run_ownership.recorded_run_facts",
                    return_value=facts,
                ) as lookup,
            ):
                self.assertIs(owner_create_allowed(user, "solidworks_to_urdf"), allowed)
                lookup.assert_called_once_with(
                    "solidworks_to_urdf",
                    "parent-airflow-dag-run",
                )

    def test_buffer_replays_the_exact_current_dispatch_body(self) -> None:
        raw = create_body()
        messages = [
            {"type": "http.request", "body": raw[:15], "more_body": True},
            {"type": "http.request", "body": raw[15:], "more_body": False},
        ]

        async def exercise():
            remaining = iter(messages)

            async def receive():
                return next(remaining)

            replay, candidate, oversized = await context._buffer_candidate(
                receive,
                context.evaluate_create_candidate,
            )
            self.assertFalse(oversized)
            self.assertEqual(candidate["dag_run_id"], DAG_RUN_ID)
            self.assertEqual([await replay(), await replay()], messages)

        asyncio.run(exercise())

    def test_create_route_shape_is_not_itself_an_authorization(self) -> None:
        for path in (
            "/api/v2/dags/solidworks_to_urdf/dagRuns",
            "/dags/solidworks_to_urdf/dagRuns",
        ):
            self.assertTrue(context._selects_create_candidate({"method": "POST", "path": path}))
        self.assertFalse(
            context._selects_create_candidate(
                {"method": "GET", "path": "/dags/solidworks_to_urdf/dagRuns"}
            )
        )
        user = SimpleNamespace(get_id=lambda: "cli_app:tenant-a:ou_worker")
        with context.use_request(SimpleNamespace(scope={})):
            self.assertFalse(owner_create_allowed(user, "solidworks_to_urdf"))


if __name__ == "__main__":
    unittest.main()
