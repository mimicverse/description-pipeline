"""Consumer implementation tests against the frozen W4 design, not native qualification."""

from copy import deepcopy
import hashlib
from pathlib import Path
import sys
import tracemalloc
from types import ModuleType
import unittest
from unittest.mock import patch

from description_pipeline.simulation.verify_recording import _verify_commands


class PicoStreamingContractTests(unittest.TestCase):
    def setUp(self):
        self.identifier = "operator-" + hashlib.sha256(b"estop-request").hexdigest()
        self.envelope = {
            "protocol": "teleop.upper-body.worker/1",
            "id": self.identifier,
            "source_session_id": "operator-session",
            "sequence": 1,
            "received_monotonic_ns": 1,
            "type": "estop",
        }
        self.response = {
            "effective_tick": 4,
            "effective_time_s": 0.08,
            "reason": "estop_latched",
            "state_digest": "a" * 64,
        }
        self.commands = [
            {
                "command_id": "estop-request",
                "result": {"state": "applied", "envelope": self.envelope, "response": self.response},
            }
        ]
        self.fact = {"command_id": self.identifier, "digest": "b" * 64, "status": "applied", **self.response}
        self.reconciliation = {
            "schema_version": "teleop-pico-session-reconciliation/1",
            "record_dir": "/contract-fixture",
            "commands": [self.fact],
            "unknown": [],
            "malformed": 0,
            "incomplete": [],
            "counts": {"raw_input": 1, "session_events": 2, "commands": 1, "unknown": 0, "malformed": 0},
            "complete": True,
        }

    def verify(self, rows, *, commands=None, reconciliation=None):
        # This explicit design double tests only the pipeline consumer. It does
        # not implement gzip, reconciliation, replay or native worker behavior.
        modules = {}
        for name in ("teleop", "teleop.robots", "teleop.robots.m3_0"):
            module = ModuleType(name)
            module.__path__ = []
            modules[name] = module
        api = ModuleType("teleop.robots.m3_0.session_evidence")
        api.iter_raw_input_evidence = lambda _path: iter(rows)
        requested = []

        def reconcile(_path, *, expected_command_ids):
            requested.append(expected_command_ids)
            return self.reconciliation if reconciliation is None else reconciliation

        api.reconcile_session_evidence = reconcile
        modules[api.__name__] = api
        with patch.dict(sys.modules, modules):
            result, _counts = _verify_commands(
                Path("/contract-fixture"), self.commands if commands is None else commands
            )
        self.assertEqual([result], requested)
        return result

    def test_streams_unregistered_traffic_without_retaining_it_and_filters_reconciliation(self):
        def rows():
            for index in range(20000):
                yield {"raw": {"id": f"frame-{index}", "payload": "x" * 2048}}
            yield {"raw": self.envelope}
            yield {"raw": deepcopy(self.envelope)}

        tracemalloc.start()
        try:
            self.reconciliation["counts"]["raw_input"] = 20002
            self.assertEqual((self.identifier,), self.verify(rows()))
            _current, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertLess(peak, 1024 * 1024)

    def test_rejects_changed_duplicate_envelope_and_missing_dispatch(self):
        altered = {**self.envelope, "received_monotonic_ns": 2}
        for rows in ([], [{"raw": self.envelope}, {"raw": altered}]):
            with self.subTest(rows=rows), self.assertRaisesRegex(ValueError, "immutable registered envelope"):
                self.verify(iter(rows))

    def test_not_sent_cannot_have_dispatch_evidence(self):
        commands = [{"command_id": "estop-request", "result": {"state": "not_sent"}}]
        with self.assertRaisesRegex(ValueError, "not sent"):
            self.verify(iter([{"raw": self.envelope}]), commands=commands)
        reconciliation = {
            **self.reconciliation,
            "commands": [],
            "counts": {**self.reconciliation["counts"], "raw_input": 0},
        }
        self.assertEqual((), self.verify(iter([]), commands=commands, reconciliation=reconciliation))

    def test_propagates_public_iterator_failure_after_complete_rows(self):
        class IncompleteContractStream(RuntimeError):
            pass

        def rows():
            yield {"raw": self.envelope}
            raise IncompleteContractStream("Missing footer")

        with self.assertRaisesRegex(IncompleteContractStream, "Missing footer"):
            self.verify(rows())

    def test_global_unknown_malformed_and_incomplete_cannot_be_hidden_by_filtering(self):
        for field, value in (
            ("complete", False),
            ("unknown", ["unregistered-frame"]),
            ("malformed", 1),
            ("incomplete", [{"stream": "raw", "reason": "footer", "complete_rows": 1}]),
        ):
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "unknown or malformed"):
                self.verify(iter([{"raw": self.envelope}]), reconciliation={**self.reconciliation, field: value})

    def test_reconciliation_requires_the_exact_registered_subset(self):
        for facts in ([], [self.fact, self.fact], [self.fact, {**self.fact, "command_id": "unrequested"}]):
            with self.subTest(facts=facts), self.assertRaisesRegex(ValueError, "registered command set"):
                self.verify(iter([{"raw": self.envelope}]), reconciliation={**self.reconciliation, "commands": facts})

    def test_checks_compact_terminal_receipt_and_state_digest(self):
        for field, value, error in (
            ("status", "rejected", "receipt differs"),
            ("effective_tick", 5, "receipt differs"),
            ("state_digest", "0" * 64, "state digest differs"),
        ):
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, error):
                self.verify(
                    iter([{"raw": self.envelope}]),
                    reconciliation={**self.reconciliation, "commands": [{**self.fact, field: value}]},
                )

    def test_rejects_unreconciled_or_duplicate_registered_identity(self):
        for commands, error in (
            ([{"command_id": "estop-request", "result": None}], "no dispatch reconciliation"),
            (self.commands * 2, "identity is duplicated"),
        ):
            with self.subTest(error=error), self.assertRaisesRegex(ValueError, error):
                self.verify(iter([{"raw": self.envelope}]), commands=commands)
