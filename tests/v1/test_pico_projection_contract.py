"""Validate W4 design projections; these fixtures do not qualify a native run."""

from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from description_pipeline.simulation.resources import (
    SessionResourceError,
    validate_policy,
    validate_manifest_policy,
    validate_ready,
    validate_state,
)


def policy_fixture(root):
    return {
        "session_dir": str(root),
        "quota_bytes": 536870912,
        "minimum_duration_s": 3600.0,
        "control_period_s": 0.02,
        "maximum_control_ticks": 180000,
        "raw_rows_per_second": 64.0,
        "event_rows_per_second": 192.0,
        "traffic_headroom": 2.0,
        "raw_row_cap": 460800,
        "event_row_cap": 1382400,
        "record_row_cap": 183600,
        "setup_tick_cap": 3600,
        "max_row_bytes": 8388608,
        "finalization_reserve_bytes": 16777216,
        "failure_reserve_bytes": 16777216,
        "setup_reserve_bytes": 16777216,
        "source_bytes": 32,
        "compiled_bytes": 0,
        "stderr_request_session_bytes": 128,
        "fixed_bytes": 50331808,
        "evidence_byte_cap": 486539104,
        "declared_source_bytes": 32,
        "source_materialization_limit_bytes": 486539135,
        "formula": "evidence_byte_cap = quota_bytes - fixed_bytes",
    }


class PicoProjectionContractTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.policy = policy_fixture(self.root)
        self.ready = {
            "contract_revision": "w4-public-2",
            "phase": self.phase("ready_unpaired", 0),
            "resource_policy": deepcopy(self.policy),
            "timing_contract": {
                "clock": "time.monotonic_ns",
                "minimum_duration_s": 3600.0,
                "minimum_duration_ns": 3600000000000,
                "control_period_s": 0.02,
                "running_start": "first validated real frame applied at an actual control tick after ready",
                "running_end": (
                    "first actual control tick at or after start + minimum_duration_ns "
                    "at a requested normal stop boundary"
                ),
                "record_rule": (
                    "one canonical record for every actual control tick from tick zero "
                    "through the terminal control boundary"
                ),
                "stop_and_finalization_excluded_from_running_duration": True,
            },
            "files": {
                role: {"name": name, "encoding": "gzip-jsonl", "compresslevel": 6, "mtime": 0}
                for role, name in (
                    ("raw", "raw-input.jsonl.gz"),
                    ("events", "session-events.jsonl.gz"),
                    ("records", "records.jsonl.gz"),
                )
            },
        }
        self.ready["files"]["manifest"] = {
            "name": "session-manifest.json",
            "encoding": "json",
            "commit": "fsync-then-atomic-last",
        }

    @staticmethod
    def phase(name, tick):
        return {
            "name": name,
            "start_tick": tick,
            "start_monotonic_ns": 1000,
            "counts": {"raw_input": 0, "session_events": 0, "records": 0},
        }

    def state(self, *, running, elapsed=0):
        return {
            "contract_revision": "w4-public-2",
            "phase": self.phase("running" if running else "ready_unpaired", 3 if running else 0),
            "run_window": {
                "minimum_duration_s": 3600.0,
                "minimum_duration_ns": 3600000000000,
                "start_tick": 3 if running else None,
                "start_monotonic_ns": 1000 if running else None,
                "start_command_id": "frame-1" if running else None,
                "end_tick": None,
                "end_monotonic_ns": None,
                "elapsed_monotonic_ns": elapsed if running else None,
                "elapsed_monotonic_s": elapsed / 1000000000 if running else None,
                "minimum_duration_met": running and elapsed >= 3600000000000,
            },
        }

    def test_ready_requires_the_exact_resolver_policy_and_monotonic_timing(self):
        self.assertEqual(self.ready, validate_ready(self.ready, self.policy, self.root))
        for mutate in (
            lambda value: value.update({"contract_revision": "w4-public-1"}),
            lambda value: value["resource_policy"].update({"source_bytes": 33}),
            lambda value: value["timing_contract"].update({"clock": "time.time"}),
            lambda value: value["files"]["raw"].update({"mtime": False}),
        ):
            value = deepcopy(self.ready)
            mutate(value)
            with self.assertRaises(SessionResourceError):
                validate_ready(value, self.policy, self.root)

    def test_policy_rejects_boolean_caps_foreign_workspace_and_nonfinite_rates(self):
        for change in (
            {"raw_row_cap": True},
            {"session_dir": str(self.root / "foreign")},
            {"raw_rows_per_second": float("inf")},
            {"source_materialization_limit_bytes": 31},
        ):
            with self.subTest(change=change), self.assertRaises(SessionResourceError):
                validate_policy({**self.policy, **change}, self.root)

    def test_ready_rejects_boolean_policy_fields_equal_to_zero_or_one(self):
        for key, expected, received in (
            ("compiled_bytes", 0, False),
            ("compiled_bytes", 1, True),
            ("traffic_headroom", 1, True),
        ):
            policy = {**self.policy, key: expected}
            ready = deepcopy(self.ready)
            ready["resource_policy"] = {**policy, key: received}
            with self.subTest(key=key, expected=expected), self.assertRaises(SessionResourceError):
                validate_ready(ready, policy, self.root)

    def test_manifest_observations_cannot_change_or_replace_any_admitted_policy_field(self):
        observed = {**self.policy, "observed_evidence_bytes": 3711, "observed_session_bytes": 16551}
        self.assertEqual(observed, validate_manifest_policy(observed, self.policy, self.root))
        for change in ({"compiled_bytes": False}, {"raw_row_cap": self.policy["raw_row_cap"] + 1}):
            with self.subTest(change=change), self.assertRaises(SessionResourceError):
                validate_manifest_policy({**observed, **change}, self.policy, self.root)
        with self.assertRaises(SessionResourceError):
            validate_manifest_policy(observed, None, self.root)

    def test_idle_has_no_running_clock_and_short_running_is_not_duration_eligible(self):
        idle = self.state(running=False)
        self.assertEqual(idle, validate_state(idle, self.ready, tick=0, previous=None))
        running = self.state(running=True, elapsed=1_000_000_000)
        self.assertEqual(running, validate_state(running, self.ready, tick=4, previous=idle))
        self.assertFalse(running["run_window"]["minimum_duration_met"])
        invalid = deepcopy(idle)
        invalid["run_window"]["start_monotonic_ns"] = 1000
        with self.assertRaisesRegex(SessionResourceError, "unproven running clock"):
            validate_state(invalid, self.ready, tick=0, previous=None)

    def test_running_origin_and_phase_cannot_regress_after_first_applied_frame(self):
        previous = self.state(running=True, elapsed=2_000_000_000)
        for value in (self.state(running=False), self.state(running=True, elapsed=1_000_000_000)):
            with self.assertRaisesRegex(SessionResourceError, "regressed"):
                validate_state(value, self.ready, tick=5, previous=previous)
        value = self.state(running=True, elapsed=3_000_000_000)
        value["run_window"]["start_command_id"] = "another-frame"
        with self.assertRaisesRegex(SessionResourceError, "regressed"):
            validate_state(value, self.ready, tick=5, previous=previous)

    def test_live_progress_cannot_claim_a_final_end_or_false_duration_bit(self):
        for change in ({"end_tick": 5}, {"elapsed_monotonic_s": float("nan")}, {"minimum_duration_met": True}):
            value = self.state(running=True, elapsed=1_000_000_000)
            value["run_window"].update(change)
            with self.subTest(change=change), self.assertRaises(SessionResourceError):
                validate_state(value, self.ready, tick=4, previous=None)
        complete_duration = self.state(running=True, elapsed=3600_000_000_000)
        self.assertTrue(
            validate_state(complete_duration, self.ready, tick=180003, previous=None)["run_window"][
                "minimum_duration_met"
            ]
        )
