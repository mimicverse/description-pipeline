"""The Linux health gate's bounded waiting semantics (offline; stub probes only)."""

from __future__ import annotations

import os
import subprocess
import tempfile
import time
import unittest
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
GATE = ROOT / "deploy" / "operator" / "health_gate.sh"


def write_stub(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


def invoke(
    gate: Path, evidence: Path, args: list[str], env: dict[str, str] | None = None
) -> subprocess.CompletedProcess:
    environment = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(Path.home())}
    if env:
        environment.update(env)
    return subprocess.run(
        ["bash", str(gate), str(evidence), *args], capture_output=True, text=True, env=environment
    )


class HealthGateTests(unittest.TestCase):
    def _tree(self, tmp: Path, probe_body: str) -> tuple[Path, Path]:
        op = tmp / "operator"
        op.mkdir()
        (op / "health_gate.sh").write_bytes(GATE.read_bytes())
        write_stub(op / "health.sh", probe_body)
        return op / "health_gate.sh", tmp / "evidence"

    def _runs(self, evidence: Path) -> list[Path]:
        return sorted(evidence.glob("run-*"))

    def test_a_passing_probe_passes_with_one_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            gate, evidence = self._tree(Path(tmp), "#!/usr/bin/env bash\necho all-good\nexit 0\n")
            result = invoke(gate, evidence, ["2", "1", "0.1"])
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("linux_health_gate=PASS", result.stdout)
            runs = self._runs(evidence)
            self.assertEqual(len(runs), 1)
            attempt = runs[0] / "attempts" / "attempt-1.out"
            self.assertIn("all-good", attempt.read_text(encoding="utf-8"))

    def test_the_probe_timeout_never_outruns_the_total_budget(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            gate, evidence = self._tree(Path(tmp), "#!/usr/bin/env bash\nsleep 0.6\nexit 1\n")
            begun = time.monotonic()
            result = invoke(gate, evidence, ["0.15", "1", "0.05"])
            took = time.monotonic() - begun
            self.assertNotEqual(result.returncode, 0)
            elapsed = int(re.search(r"elapsed_ms=(\d+)", result.stderr).group(1))
            self.assertLess(elapsed, 300, f"strong finish must stay in the 0.15s budget, gate said {elapsed}ms")
            self.assertLess(took, 0.6, f"the gate must fail at budget scale, took {took:.3f}s")
            summary = (self._runs(evidence)[0] / "summary.txt").read_text(encoding="utf-8")
            self.assertIn("rc=124", summary)

    def test_a_probe_ignoring_term_is_force_finished_within_the_budget(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            gate, evidence = self._tree(
                Path(tmp), "#!/usr/bin/env bash\ntrap '' TERM\nsleep 5\nexit 1\n"
            )
            begun = time.monotonic()
            result = invoke(gate, evidence, ["0.5", "0.05", "0.05"])
            took = time.monotonic() - begun
            self.assertNotEqual(result.returncode, 0)
            elapsed = int(re.search(r"elapsed_ms=(\d+)", result.stderr).group(1))
            self.assertLess(elapsed, 800, f"a TERM-ignoring probe must finish near the budget, gate said {elapsed}ms")
            self.assertLess(took, 1.2, f"a TERM-ignoring probe must be bounded, took {took:.3f}s")

    def test_a_late_success_never_opens(self) -> None:
        # The probe ignores TERM and would exit 0 after the budget: the forced finish wins and
        # the gate stays failed (the rc0-past-deadline guard covers clock anomalies).
        with tempfile.TemporaryDirectory() as tmp:
            gate, evidence = self._tree(
                Path(tmp), "#!/usr/bin/env bash\ntrap '' TERM\nsleep 0.3\nexit 0\n"
            )
            result = invoke(gate, evidence, ["0.2", "0.2", "0.05"])
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn("linux_health_gate=PASS", result.stdout + result.stderr)

    def test_a_failing_probe_fails_at_the_deadline_and_preserves_attempts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            counter = tmp_path / "counter"
            gate, evidence = self._tree(
                tmp_path,
                "#!/usr/bin/env bash\n"
                f'n=$(cat "{counter}" 2>/dev/null || echo 0); n=$((n+1)); echo "$n" > "{counter}"\n'
                'echo "attempt-body-$n"\nexit 3\n',
            )
            result = invoke(gate, evidence, ["0.6", "1", "0.1"])
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("reason=deadline-reached", result.stderr)
            attempts = sorted((self._runs(evidence)[0] / "attempts").glob("attempt-*.out"))
            self.assertGreaterEqual(len(attempts), 2)
            for index, path in enumerate(attempts, start=1):
                self.assertIn(f"attempt-body-{index}", path.read_text(encoding="utf-8"))
            summary = (self._runs(evidence)[0] / "summary.txt").read_text(encoding="utf-8")
            self.assertIn("rc=3", summary)

    def test_a_delayed_success_passes_after_retries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            counter = tmp_path / "counter"
            gate, evidence = self._tree(
                tmp_path,
                "#!/usr/bin/env bash\n"
                f'n=$(cat "{counter}" 2>/dev/null || echo 0); n=$((n+1)); echo "$n" > "{counter}"\n'
                'echo "try-$n"\n[ "$n" -ge 3 ] && exit 0\nexit 7\n',
            )
            result = invoke(gate, evidence, ["5", "1", "0.1"])
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("attempts=3", result.stdout)

    def test_each_invocation_keeps_its_own_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            gate, evidence = self._tree(Path(tmp), "#!/usr/bin/env bash\necho ok\nexit 0\n")
            first = invoke(gate, evidence, ["2", "1", "0.1"])
            second = invoke(gate, evidence, ["2", "1", "0.1"])
            self.assertEqual(first.returncode, 0)
            self.assertEqual(second.returncode, 0)
            runs = self._runs(evidence)
            self.assertEqual(len(runs), 2, "two invocations must keep two evidence runs")
            for run in runs:
                self.assertTrue((run / "attempts" / "attempt-1.out").is_file())
                self.assertIn("linux_health_gate=PASS", (run / "summary.txt").read_text(encoding="utf-8"))

    def test_the_policy_cannot_be_changed_by_the_environment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            gate, evidence = self._tree(Path(tmp), "#!/usr/bin/env bash\nexit 0\n")
            result = invoke(
                gate,
                evidence,
                [],  # the lifecycle calls the gate with the evidence directory only
                {
                    "HEALTH_GATE_DEADLINE": "0",
                    "HEALTH_GATE_PROBE_TIMEOUT": "0",
                    "HEALTH_GATE_INTERVAL": "0",
                },
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("linux_health_gate=PASS", result.stdout)


if __name__ == "__main__":
    unittest.main()
