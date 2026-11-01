"""Current DispatchConf creation authorization cannot be bypassed by direct API calls."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from description_pipeline.orchestration import request_context as context
from description_pipeline.orchestration.airflow_client import native_run_id
from description_pipeline.orchestration.run_ownership import (
    CreateDispatchBinding,
    owner_create_allowed,
    owner_retry_allowed,
    recorded_create_binding,
    recorded_canonical_projection,
    registered_recovery_block,
    retry_allowed,
    validated_canonical_projection,
)
from description_pipeline.io import digest
from description_pipeline.pipeline.admission import (
    build_freeze_command,
    build_run_admission,
)
from description_pipeline.pipeline.contracts import (
    DispatchCommand,
    DispatchConf,
)
from description_pipeline.schemas import AIRFLOW_DISPATCH_CONF_SCHEMA

RUN_ID = "run-worker"
FREEZE_COMMAND_ID = "freeze:worker"
DAG_RUN_ID = "portal-20261028T120000-1234abcd"
PRINCIPAL = "cli_app:tenant-a:ou_worker"
CONF = {
    "schema_version": AIRFLOW_DISPATCH_CONF_SCHEMA,
    "run_id": RUN_ID,
    "entry_command_id": FREEZE_COMMAND_ID,
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
        "entry_command_id": FREEZE_COMMAND_ID,
    }
    values.update(overrides)
    return CreateDispatchBinding(**values)


def canonical_projection(
    *,
    dag_id: str = "solidworks_to_urdf",
    dag_run_id: str = DAG_RUN_ID,
    run_id: str = RUN_ID,
    principal: str = PRINCIPAL,
    state: str = "running",
) -> dict:
    return {
        "run_id": run_id,
        "request": {"principal": principal},
        "state": state,
        "technical": {
            "commands": [
                {
                    "kind": "dispatch",
                    "run_id": run_id,
                    "request": {
                        "run_id": run_id,
                        "dag_id": dag_id,
                        "dag_run_id": dag_run_id,
                    },
                }
            ]
        },
    }


def typed_canonical_projection(*, state: str = "running") -> dict:
    from tests.v1.test_pipeline_admission import AdmissionHarness  # noqa: PLC0415

    with tempfile.TemporaryDirectory(prefix="canonical-projection-") as directory:
        harness = AdmissionHarness(Path(directory))
        run_id = native_run_id(DAG_RUN_ID)
        spec = replace(
            harness.spec,
            run_id=run_id,
            request_id=DAG_RUN_ID,
            principal=PRINCIPAL,
        )
        admission = build_run_admission(spec)
        freeze = build_freeze_command(spec)
        conf = DispatchConf(
            schema_version=AIRFLOW_DISPATCH_CONF_SCHEMA,
            run_id=run_id,
            entry_command_id=freeze.command_id,
        )
        conf_artifact = harness.store.seal_dispatch_conf(
            conf,
            producer_run_id=run_id,
            producer_tool_sha256=spec.dispatch_executor_sha256,
        )
        dispatch = DispatchCommand.create(
            command_id="dispatch:"
            + digest(
                {
                    "admission_sha256": spec.admission_sha256,
                    "dag_id": "solidworks_to_urdf",
                    "dag_run_id": DAG_RUN_ID,
                    "entry_command_id": freeze.command_id,
                    "run_id": run_id,
                }
            ),
            run_id=run_id,
            entry_command_id=freeze.command_id,
            idempotency_key=f"{run_id}/dispatch",
            dag_id="solidworks_to_urdf",
            dag_run_id=DAG_RUN_ID,
            conf_artifact=conf_artifact,
            expected_executor_sha256=spec.dispatch_executor_sha256,
        )
        freeze_row = {
            "command_id": freeze.command_id,
            "run_id": freeze.run_id,
            "kind": freeze.kind,
            "stage": freeze.stage,
            "request": freeze.request,
            "request_digest": freeze.request_digest,
            "idempotency_key": freeze.idempotency_key,
            "config_snapshot_sha256": freeze.config_snapshot_sha256,
            "contract_sha256": freeze.contract_sha256,
            "policy_sha256": freeze.policy_sha256,
            "expected_executor_sha256": freeze.expected_executor_sha256,
            "predecessor_decision_id": freeze.predecessor_decision_id,
        }
        dispatch_row = {
            "command_id": dispatch.command_id,
            "run_id": dispatch.run_id,
            "kind": "dispatch",
            "stage": None,
            "request": dispatch.to_record(),
            "request_digest": dispatch.request_digest,
            "idempotency_key": dispatch.idempotency_key,
            "config_snapshot_sha256": freeze.config_snapshot_sha256,
            "contract_sha256": freeze.contract_sha256,
            "policy_sha256": freeze.policy_sha256,
            "expected_executor_sha256": dispatch.expected_executor_sha256,
            "predecessor_decision_id": None,
        }
    return {
        "run_id": run_id,
        "request_id": admission.request_id,
        "parent_run_id": admission.parent_run_id,
        "from_stage": admission.from_stage,
        "request": spec.to_record(),
        "request_digest": admission.request_digest,
        "created_at": admission.created_at.isoformat(),
        "state": state,
        "stages": [{"stage": "freeze", "command": freeze.to_record()}],
        "technical": {"commands": [freeze_row, dispatch_row]},
    }


def clone_projection(projection: dict) -> dict:
    return json.loads(json.dumps(projection, default=dict))


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
            create_body(conf={**CONF, "entry_command_id": ""}),
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
            (binding(entry_command_id="freeze:other"), False),
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
                entry_command_id=FREEZE_COMMAND_ID,
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

    def test_owner_retry_requires_the_canonical_registration_gate(self) -> None:
        user = SimpleNamespace(get_id=lambda: PRINCIPAL)
        facts = (
            PRINCIPAL,
            "failed",
            {
                "freeze": "success",
                "discover": "success",
                "capture": "success",
                "generate": "success",
                "verify": "failed",
            },
        )
        cases = (
            ("running", True),
            ("queued", True),
            ("failed", False),
            ("cancelled", False),
            ("succeeded", False),
            ("unresolved", False),
            ("unknown", False),
            (None, False),
        )
        for state, expected in cases:
            projection = canonical_projection(state=state) if state is not None else None
            with (
                self.subTest(state=state),
                patch(
                    "description_pipeline.orchestration.run_ownership.recorded_run_facts",
                    return_value=facts,
                ),
                patch(
                    "description_pipeline.orchestration.run_ownership.recorded_canonical_projection",
                    return_value=projection,
                ),
            ):
                self.assertIs(
                    owner_retry_allowed(user, "solidworks_to_urdf", DAG_RUN_ID),
                    expected,
                )

    def test_owner_retry_rejects_principal_and_dispatch_binding_drift(self) -> None:
        user = SimpleNamespace(get_id=lambda: PRINCIPAL)
        facts = (
            PRINCIPAL,
            "failed",
            {"generate": "failed", "verify": "upstream_failed"},
        )
        projection = canonical_projection(principal="cli_app:tenant-a:ou_other")
        with (
            patch(
                "description_pipeline.orchestration.run_ownership.recorded_run_facts",
                return_value=facts,
            ),
            patch(
                "description_pipeline.orchestration.run_ownership.recorded_canonical_projection",
                return_value=projection,
            ),
        ):
            self.assertFalse(owner_retry_allowed(user, "solidworks_to_urdf", DAG_RUN_ID))

    def test_retry_without_owner_still_requires_the_canonical_safety_gate(self) -> None:
        user = SimpleNamespace(get_id=lambda: "cli_app:tenant-a:ou_admin")
        facts = (
            PRINCIPAL,
            "failed",
            {
                "freeze": "success",
                "discover": "success",
                "capture": "success",
                "generate": "success",
                "verify": "failed",
            },
        )
        cases = (
            ("queued", True),
            ("running", True),
            ("failed", False),
            ("cancelled", False),
            ("succeeded", False),
            ("unresolved", False),
            ("unknown", False),
            (None, False),
        )
        for state, expected in cases:
            projection = canonical_projection(state=state) if state is not None else None
            with (
                self.subTest(state=state),
                patch(
                    "description_pipeline.orchestration.run_ownership.recorded_run_facts",
                    return_value=facts,
                ),
                patch(
                    "description_pipeline.orchestration.run_ownership.recorded_canonical_projection",
                    return_value=projection,
                ),
            ):
                self.assertIs(
                    retry_allowed(
                        user,
                        "solidworks_to_urdf",
                        DAG_RUN_ID,
                        require_owner=False,
                    ),
                    expected,
                )

    def test_registered_recovery_block_matches_canonical_state_authority(self) -> None:
        from description_pipeline.orchestration import portal  # noqa: PLC0415

        self.assertIsNone(registered_recovery_block({"state": "queued"}))
        self.assertIsNone(registered_recovery_block({"state": "running"}))
        self.assertEqual(
            registered_recovery_block({"state": "failed"}),
            ("registered_failed", "engineering_terminal_failure"),
        )
        self.assertEqual(
            registered_recovery_block({"state": "unresolved"}),
            ("registered_unresolved", "reconciliation_required"),
        )
        self.assertEqual(
            registered_recovery_block(None),
            ("registration_missing", "temporary_disconnection"),
        )
        for state in (
            "queued",
            "running",
            "failed",
            "cancelled",
            "succeeded",
            "unresolved",
            "unknown",
            None,
        ):
            record = None if state is None else {"state": state}
            self.assertEqual(
                registered_recovery_block(record),
                portal._registered_recovery_block(record),
            )

    def test_canonical_projection_read_is_read_only_and_bound_to_airflow(self) -> None:
        run_id = native_run_id(DAG_RUN_ID)
        projection = typed_canonical_projection()
        connection = MagicMock()
        connection.__enter__.return_value = connection
        psycopg2 = SimpleNamespace(connect=MagicMock(return_value=connection))
        with (
            patch.dict(os.environ, {"PLATFORM_OPERATOR_DSN": "operator-ro-dsn"}),
            patch.dict(sys.modules, {"psycopg2": psycopg2}),
            patch(
                "description_pipeline.platform.queries.read_run_projection",
                return_value=projection,
            ) as read,
        ):
            result = recorded_canonical_projection(
                "solidworks_to_urdf",
                DAG_RUN_ID,
            )
        self.assertEqual(result, projection)
        psycopg2.connect.assert_called_once_with(
            "operator-ro-dsn",
            connect_timeout=5,
            options="-c statement_timeout=5000",
        )
        connection.set_session.assert_called_once_with(readonly=True)
        read.assert_called_once_with(connection, run_id)

        drift = clone_projection(projection)
        drift["technical"]["commands"][1]["request"]["dag_run_id"] = "another-dag-run"
        with (
            patch.dict(os.environ, {"PLATFORM_OPERATOR_DSN": "operator-ro-dsn"}),
            patch.dict(sys.modules, {"psycopg2": psycopg2}),
            patch(
                "description_pipeline.platform.queries.read_run_projection",
                return_value=drift,
            ),
        ):
            self.assertIsNone(
                recorded_canonical_projection(
                    "solidworks_to_urdf",
                    DAG_RUN_ID,
                )
            )

    def test_canonical_projection_reuses_admission_dispatch_and_freeze_validators(
        self,
    ) -> None:
        run_id = native_run_id(DAG_RUN_ID)
        projection = typed_canonical_projection()

        def validate(candidate: object) -> dict | None:
            return validated_canonical_projection(
                candidate,
                dag_id="solidworks_to_urdf",
                dag_run_id=DAG_RUN_ID,
                canonical_run_id=run_id,
            )

        self.assertIs(validate(projection), projection)
        cases: list[tuple[str, dict]] = []

        invalid_admission = clone_projection(projection)
        invalid_admission["request"]["unknown"] = "field"
        cases.append(("invalid admission", invalid_admission))

        invalid_dispatch_request = clone_projection(projection)
        invalid_dispatch_request["technical"]["commands"][1]["request"][
            "entry_command_id"
        ] = "freeze:drifted"
        cases.append(("dispatch validator", invalid_dispatch_request))

        dispatch_dag_drift = clone_projection(projection)
        dispatch_dag_drift["technical"]["commands"][1]["request"]["dag_id"] = (
            "another_dag"
        )
        cases.append(("dispatch dag", dispatch_dag_drift))

        dispatch_run_drift = clone_projection(projection)
        dispatch_run_drift["technical"]["commands"][1]["request"][
            "dag_run_id"
        ] = "another-dag-run"
        cases.append(("dispatch run", dispatch_run_drift))

        dispatch_identity_drift = clone_projection(projection)
        dispatch_identity_drift["technical"]["commands"][1]["command_id"] = (
            "dispatch:drifted"
        )
        cases.append(("dispatch identity", dispatch_identity_drift))

        request_digest_drift = clone_projection(projection)
        request_digest_drift["technical"]["commands"][1]["request_digest"] = "0" * 64
        cases.append(("dispatch row digest", request_digest_drift))

        expected_executor_drift = clone_projection(projection)
        expected_executor_drift["technical"]["commands"][1][
            "expected_executor_sha256"
        ] = "0" * 64
        cases.append(("dispatch executor", expected_executor_drift))

        duplicate_dispatch = clone_projection(projection)
        duplicate_dispatch["technical"]["commands"].append(
            clone_projection(duplicate_dispatch["technical"]["commands"][1])
        )
        cases.append(("duplicate dispatch", duplicate_dispatch))

        missing_technical_freeze = clone_projection(projection)
        missing_technical_freeze["technical"]["commands"].pop(0)
        cases.append(("missing technical freeze", missing_technical_freeze))

        missing_freeze_stage = clone_projection(projection)
        missing_freeze_stage["stages"] = []
        cases.append(("missing freeze stage", missing_freeze_stage))

        freeze_identity_drift = clone_projection(projection)
        freeze_identity_drift["stages"][0]["command"]["command_id"] = "freeze:drifted"
        cases.append(("freeze stage identity", freeze_identity_drift))

        for name, candidate in cases:
            with self.subTest(name=name):
                self.assertIsNone(validate(candidate))

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
