"""The Linux health gate's bounded waiting semantics (offline; stub probes only)."""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
GATE = ROOT / "deploy" / "operator" / "health_gate.sh"


def write_stub(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


def run_gate(
    gate: Path, evidence: Path, *, deadline: str, probe_timeout: str, interval: str
) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(gate), str(evidence), deadline, probe_timeout, interval],
        capture_output=True,
        text=True,
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(Path.home())},
    )


class HealthGateTests(unittest.TestCase):
    def _tree(self, tmp: Path, probe_body: str) -> tuple[Path, Path]:
        # The gate resolves its probe as the sibling health.sh; the temp copy keeps the real
        # deployment untouched while the gate logic itself runs for real.
        op = tmp / "operator"
        op.mkdir()
        (op / "health_gate.sh").write_bytes(GATE.read_bytes())
        write_stub(op / "health.sh", probe_body)
        return op / "health_gate.sh", tmp / "evidence"

    def test_a_passing_probe_passes_with_one_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            gate, evidence = self._tree(tmp_path, "#!/usr/bin/env bash\necho all-good\nexit 0\n")
            result = run_gate(gate, evidence, deadline="2", probe_timeout="1", interval="0.1")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("linux_health_gate=PASS attempts=1", result.stdout)
            attempts = sorted((evidence / "attempts").glob("attempt-*.out"))
            self.assertEqual([path.name for path in attempts], ["attempt-1.out"])
            self.assertIn("all-good", attempts[0].read_text(encoding="utf-8"))

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
            result = run_gate(gate, evidence, deadline="0.6", probe_timeout="1", interval="0.1")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("linux_health_gate=FAIL", result.stderr)
            self.assertIn("reason=deadline-reached", result.stderr)
            attempts = sorted((evidence / "attempts").glob("attempt-*.out"))
            self.assertGreaterEqual(len(attempts), 2)
            for index, path in enumerate(attempts, start=1):
                self.assertIn(f"attempt-body-{index}", path.read_text(encoding="utf-8"))
            self.assertIn("rc=3", (evidence / "summary.txt").read_text(encoding="utf-8"))

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
            result = run_gate(gate, evidence, deadline="5", probe_timeout="1", interval="0.1")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("linux_health_gate=PASS attempts=3", result.stdout)

    def test_a_probe_that_never_passes_times_out_at_the_deadline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            gate, evidence = self._tree(tmp_path, "#!/usr/bin/env bash\nexit 9\n")
            result = run_gate(gate, evidence, deadline="0.5", probe_timeout="1", interval="0.1")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("reason=deadline-reached", result.stderr)
            self.assertNotEqual((evidence / "summary.txt").read_text(encoding="utf-8"), "")

    def test_a_slow_probe_is_cut_by_the_probe_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            gate, evidence = self._tree(tmp_path, "#!/usr/bin/env bash\nsleep 5\nexit 0\n")
            result = run_gate(gate, evidence, deadline="0.8", probe_timeout="0.3", interval="0.1")
            self.assertNotEqual(result.returncode, 0)
            summary = (evidence / "summary.txt").read_text(encoding="utf-8")
            self.assertIn("rc=124", summary)
            self.assertIn("probe-timeout", summary)

    def test_a_success_after_the_deadline_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            gate, evidence = self._tree(tmp_path, "#!/usr/bin/env bash\nsleep 0.8\nexit 0\n")
            result = run_gate(gate, evidence, deadline="0.3", probe_timeout="2", interval="0.1")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("reason=success-after-deadline", result.stderr)


if __name__ == "__main__":
    unittest.main()
