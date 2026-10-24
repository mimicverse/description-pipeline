"""Canonical fact-store transactions: immutable registration and fenced claims."""

from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta

from description_pipeline.io import digest
from description_pipeline.pipeline.contracts import ArtifactRef, StageCommand
from description_pipeline.platform.facts import FactConflict, FactStore


class _Reader:
    def read_result(self, artifact):
        raise AssertionError("This test does not read a StageResult")


class _Connection:
    def __init__(self, results):
        self.cursor_value = _Cursor(results)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.cursor_value.closed = True
        return False

    def cursor(self):
        return self.cursor_value

    def close(self):
        self.cursor_value.closed = True


class _Cursor:
    def __init__(self, results):
        self.results = list(results)
        self.current = None
        self.closed = False
        self.queries = []

    def execute(self, query, parameters=None):
        normalized = " ".join(query.split())
        self.current = None if normalized.startswith("SELECT pg_advisory_xact_lock") else (
            self.results.pop(0) if self.results else None
        )
        self.queries.append((normalized, parameters))

    def fetchone(self):
        if isinstance(self.current, list):
            return self.current.pop(0) if self.current else None
        return self.current

    def fetchall(self):
        if self.current is None:
            return []
        if isinstance(self.current, list):
            return self.current
        return [self.current]


def artifact(name: str) -> ArtifactRef:
    return ArtifactRef(
        artifact_id=name,
        kind="input_set",
        locator=f"cas://artifact/{name}",
        manifest_sha256=digest({"manifest": name}),
        set_sha256=digest({"set": name}),
        producer_run_id="run-source",
        producer_stage="freeze",
        producer_tool_sha256=digest({"tool": name}),
    )


def artifact_row(value: ArtifactRef) -> tuple:
    return (
        value.artifact_id,
        value.kind,
        value.locator,
        value.manifest_sha256,
        value.set_sha256,
        value.producer_run_id,
        value.producer_stage,
        value.producer_tool_sha256,
    )


def freeze_command() -> StageCommand:
    return StageCommand.create(
        command_id="command-freeze",
        run_id="run-1",
        stage="freeze",
        idempotency_key="run-1/freeze",
        request={"source": "upload-1"},
        input_artifacts=(),
        config_snapshot_sha256=digest({"config": 1}),
        contract_sha256=digest({"contract": 1}),
        policy_sha256=digest({"policy": 1}),
        expected_executor_sha256=digest({"executor": "freeze"}),
        predecessor_decision_id=None,
    )


class ArtifactRegistrationTests(unittest.TestCase):
    def test_identical_registration_is_idempotent(self) -> None:
        value = artifact("input-1")
        connection = _Connection([None, artifact_row(value)])
        store = FactStore(lambda: connection, _Reader())
        self.assertEqual(store.register_artifact(value), value)
        self.assertTrue(connection.cursor_value.closed)

    def test_identity_or_locator_rebinding_is_refused(self) -> None:
        expected = artifact("input-1")
        changed = ArtifactRef(
            artifact_id=expected.artifact_id,
            kind=expected.kind,
            locator=expected.locator,
            manifest_sha256=digest({"different": 1}),
            set_sha256=expected.set_sha256,
            producer_run_id=expected.producer_run_id,
            producer_stage=expected.producer_stage,
            producer_tool_sha256=expected.producer_tool_sha256,
        )
        connection = _Connection([None, artifact_row(changed)])
        store = FactStore(lambda: connection, _Reader())
        with self.assertRaises(FactConflict):
            store.register_artifact(expected)


class ClaimTests(unittest.TestCase):
    def setUp(self) -> None:
        self.command = freeze_command()
        self.owner = digest({"process": "claim-owner"})
        self.executor = self.command.expected_executor_sha256
        self.now = datetime(2026, 10, 25, tzinfo=UTC)

    def command_row(self):
        return (
            self.command.command_id,
            self.command.run_id,
            self.command.kind,
            self.command.stage,
            self.executor,
            self.command.predecessor_decision_id,
        )

    def test_same_owner_reentry_cannot_acquire_the_open_claim_twice(self) -> None:
        expiry = self.now + timedelta(minutes=5)
        connection = _Connection(
            [
                self.command_row(),
                [],
                None,
                (
                    "claim-1",
                    self.command.command_id,
                    1,
                    self.owner,
                    self.executor,
                    None,
                    expiry,
                    "execution",
                    None,
                ),
            ]
        )
        store = FactStore(lambda: connection, _Reader())
        with self.assertRaises(FactConflict):
            store.acquire_claim(
                self.command.command_id,
                claim_id="claim-2",
                owner_sha256=self.owner,
                executor_sha256=self.executor,
                now=self.now,
                expires_at=expiry,
            )

    def test_live_foreign_owner_is_refused(self) -> None:
        connection = _Connection(
            [
                self.command_row(),
                [],
                None,
                (
                    "claim-1",
                    self.command.command_id,
                    1,
                    self.owner,
                    self.executor,
                    None,
                    self.now + timedelta(minutes=5),
                    "execution",
                    None,
                ),
            ]
        )
        store = FactStore(lambda: connection, _Reader())
        with self.assertRaises(FactConflict):
            store.acquire_claim(
                self.command.command_id,
                claim_id="claim-2",
                owner_sha256=digest({"executor": "other"}),
                executor_sha256=self.executor,
                now=self.now,
                expires_at=self.now + timedelta(minutes=5),
            )

    def test_expired_claim_can_be_replaced_only_without_effect_activity(self) -> None:
        connection = _Connection(
            [
                self.command_row(),
                [],
                None,
                (
                    "claim-1",
                    self.command.command_id,
                    1,
                    self.owner,
                    self.executor,
                    None,
                    self.now - timedelta(seconds=1),
                    "execution",
                    None,
                ),
                None,
                (2,),
                None,
            ]
        )
        store = FactStore(lambda: connection, _Reader())
        claim = store.acquire_claim(
            self.command.command_id,
            claim_id="claim-2",
            owner_sha256=digest({"executor": "replacement"}),
            executor_sha256=self.executor,
            now=self.now,
            expires_at=self.now + timedelta(minutes=5),
        )
        self.assertEqual(claim.fence, 2)

    def test_expired_claim_with_effect_activity_requires_reconciliation(self) -> None:
        created = self.now - timedelta(minutes=1)
        connection = _Connection(
            [
                self.command_row(),
                [("effect-1",)],
                (
                    "effect-1",
                    self.command.command_id,
                    "native",
                    "native_operation",
                    {"host": "native"},
                    {"state": "complete"},
                    digest({"state": "complete"}),
                    "outbound-1",
                    digest({"outbound": 1}),
                    created,
                ),
                [
                    (
                        "fact-1",
                        "effect-1",
                        1,
                        "claim-1",
                        1,
                        "dispatch_authorized",
                        "test",
                        None,
                        None,
                        digest({"fact": 1}),
                        None,
                        created,
                        None,
                    )
                ],
                None,
                (
                    "claim-1",
                    self.command.command_id,
                    1,
                    self.owner,
                    self.executor,
                    None,
                    self.now - timedelta(seconds=1),
                    "execution",
                    None,
                ),
                None,
            ]
        )
        store = FactStore(lambda: connection, _Reader())
        with self.assertRaises(FactConflict):
            store.acquire_claim(
                self.command.command_id,
                claim_id="claim-2",
                owner_sha256=digest({"executor": "replacement"}),
                executor_sha256=self.executor,
                now=self.now,
                expires_at=self.now + timedelta(minutes=5),
            )


if __name__ == "__main__":
    unittest.main()
