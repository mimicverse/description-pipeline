"""Unit gates for the release-to-recovery binding."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
REPOSITORY = HERE.parents[1]
sys.path.insert(0, str(REPOSITORY / "deploy" / "operator"))

import release_recovery  # noqa: E402


def digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


class Result:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class ReleaseRecoveryTests(unittest.TestCase):
    def make_inputs(self, root: Path) -> tuple[Path, Path, Path, Path, Path, dict]:
        release = {
            "version": "1.5.0",
            "source_commit": "a" * 40,
            "source_sha256": "b" * 64,
        }
        expected = root / "release.json"
        expected.write_text(json.dumps({"release": release}), encoding="utf-8")
        config = root / "platform.json"
        config.write_text("{}\n", encoding="utf-8")
        key = root / "recovery.key"
        key.write_bytes(b"key")
        operatorctl = root / "operatorctl.sh"
        operatorctl.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        operatorctl.chmod(0o700)
        target_plan = root / "target-plan.json"
        target_plan.write_text("{}\n", encoding="utf-8")
        manifest = root / "recovery-manifest.json"
        manifest.write_text(json.dumps({"release": release}), encoding="utf-8")
        return expected, config, key, operatorctl, target_plan, release

    def restore_args(self, root: Path, *, status_dir: Path | None = None):
        expected, config, key, operatorctl, plan, _release = self.make_inputs(root)
        manifest = root / "recovery-manifest.json"
        return SimpleNamespace(
            recovery_py=str(root / "recovery.py"),
            manifest=str(manifest),
            key=str(key),
            target_plan=str(plan),
            status_dir=str(status_dir or (root / "status")),
            config=str(config),
            operatorctl=str(operatorctl),
            expected_release=str(expected),
            runner=None,
        )

    def test_snapshot_binds_release_and_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            expected, config, key, operatorctl, _plan, release = self.make_inputs(root)
            out = root / "snapshot"

            def runner(argv, **_kwargs):
                self.assertIn("backup", argv)
                out.mkdir(parents=True, exist_ok=True)
                manifest = out / "manifest.json"
                manifest.write_text(json.dumps({"release": release}), encoding="utf-8")
                return Result(
                    stdout=(
                        f"manifest={manifest}\n"
                        f"manifest_sha256={digest(manifest.read_bytes())}\n"
                        "archive=archive.enc\n"
                        "archive_sha256=" + "c" * 64 + "\n"
                        "maintenance_state=stopped\n"
                    )
                )

            args = SimpleNamespace(
                recovery_py=str(root / "recovery.py"),
                config=str(config),
                key_file=str(key),
                out=str(out),
                operatorctl=str(operatorctl),
                expected_release=str(expected),
                backup_id="backup-1",
                runner=runner,
            )
            self.assertEqual(release_recovery.snapshot(args), 0)
            gate = json.loads((out / release_recovery.RECEIPT).read_text(encoding="utf-8"))
            self.assertEqual(gate["database_snapshot"], True)
            self.assertEqual(gate["release"], release)

    def test_restore_success_and_idempotence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            status = root / "status"
            args = self.restore_args(root, status_dir=status)
            calls = []

            def runner(argv, **_kwargs):
                calls.append(argv)
                if "restore" in argv:
                    run_dir = Path(argv[argv.index("--status-dir") + 1])
                    run_dir.mkdir(parents=True, exist_ok=True)
                    (run_dir / "receipt.json").write_text(
                        json.dumps(
                            {
                                "status": "succeeded",
                                "windows": {
                                    "task_state": "Disabled",
                                    "endpoint": "stopped",
                                    "state_sha256": "d" * 64,
                                },
                            }
                        ),
                        encoding="utf-8",
                    )
                    (run_dir / "recovery-marker.json").write_text(
                        json.dumps({"release": args_release(args)}),
                        encoding="utf-8",
                    )
                return Result()

            args.runner = runner
            self.assertEqual(release_recovery.restore(args), 0)
            before = len(calls)
            self.assertEqual(release_recovery.restore(args), 0)
            self.assertEqual(len(calls), before)
            receipt = json.loads((status / release_recovery.RECEIPT).read_text(encoding="utf-8"))
            self.assertEqual(receipt["database_rollback"], True)
            self.assertEqual(receipt["health"], "PASS")

    def test_restore_release_mismatch_refuses_before_runner(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            args = self.restore_args(root)
            manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
            manifest["release"]["source_commit"] = "e" * 40
            Path(args.manifest).write_text(json.dumps(manifest), encoding="utf-8")
            called = []
            args.runner = lambda *a, **k: called.append(a) or Result()
            with self.assertRaisesRegex(release_recovery.GateError, "identity differs"):
                release_recovery.restore(args)
            self.assertEqual(called, [])

    def test_database_failure_preserves_gate_and_recovery_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            status = root / "status"
            args = self.restore_args(root, status_dir=status)

            def runner(argv, **_kwargs):
                if "restore" in argv:
                    run_dir = Path(argv[argv.index("--status-dir") + 1])
                    run_dir.mkdir(parents=True, exist_ok=True)
                    (run_dir / "receipt.json").write_text(
                        json.dumps({"status": "failed", "code": "database_restore_failed"}),
                        encoding="utf-8",
                    )
                    return Result(returncode=1, stderr="database failed\n")
                return Result()

            args.runner = runner
            with self.assertRaisesRegex(release_recovery.GateError, "exited with 1"):
                release_recovery.restore(args)
            gate = json.loads((status / release_recovery.RECEIPT).read_text(encoding="utf-8"))
            self.assertEqual(gate["status"], "failed")
            self.assertEqual(gate["preserved"], True)

    def test_windows_failure_refuses_before_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            status = root / "status"
            args = self.restore_args(root, status_dir=status)
            calls = []

            def runner(argv, **_kwargs):
                calls.append(argv)
                if "restore" in argv:
                    run_dir = Path(argv[argv.index("--status-dir") + 1])
                    run_dir.mkdir(parents=True, exist_ok=True)
                    (run_dir / "receipt.json").write_text(
                        json.dumps(
                            {
                                "status": "succeeded",
                                "windows": {
                                    "task_state": "Ready",
                                    "endpoint": "running",
                                    "state_sha256": "f" * 64,
                                },
                            }
                        ),
                        encoding="utf-8",
                    )
                    (run_dir / "recovery-marker.json").write_text(
                        json.dumps({"release": args_release(args)}),
                        encoding="utf-8",
                    )
                return Result()

            args.runner = runner
            with self.assertRaisesRegex(release_recovery.GateError, "Windows recovery"):
                release_recovery.restore(args)
            self.assertFalse(any("start" in row for row in calls))

    def test_command_oserror_preserves_failure_gate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            status = root / "status"
            args = self.restore_args(root, status_dir=status)

            def runner(_argv, **_kwargs):
                raise OSError("cannot spawn")

            args.runner = runner
            with self.assertRaisesRegex(release_recovery.GateError, "cannot execute"):
                release_recovery.restore(args)
            gate = json.loads((status / release_recovery.RECEIPT).read_text(encoding="utf-8"))
            self.assertEqual(gate["preserved"], True)

    def test_snapshot_rejects_existing_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            expected, config, key, operatorctl, _plan, _release = self.make_inputs(root)
            out = root / "existing"
            out.mkdir()
            args = SimpleNamespace(
                recovery_py=str(root / "recovery.py"),
                config=str(config),
                key_file=str(key),
                out=str(out),
                operatorctl=str(operatorctl),
                expected_release=str(expected),
                backup_id="backup-1",
                runner=lambda *_a, **_k: Result(),
            )
            with self.assertRaisesRegex(release_recovery.GateError, "must be absent"):
                release_recovery.snapshot(args)

    def test_verify_success_does_not_authorize_restore(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            status = root / "status"
            args = self.restore_args(root, status_dir=status)
            calls = []

            def runner(argv, **_kwargs):
                calls.append(argv)
                if "verify-only" in argv or "restore" in argv:
                    run_dir = Path(argv[argv.index("--status-dir") + 1])
                    run_dir.mkdir(parents=True, exist_ok=True)
                    (run_dir / "receipt.json").write_text(
                        json.dumps(
                            {
                                "status": "succeeded",
                                "windows": {
                                    "task_state": "Disabled",
                                    "endpoint": "stopped",
                                    "state_sha256": "d" * 64,
                                },
                            }
                        ),
                        encoding="utf-8",
                    )
                    if "restore" in argv:
                        (run_dir / "recovery-marker.json").write_text(
                            json.dumps({"release": args_release(args)}),
                            encoding="utf-8",
                        )
                return Result()

            args.runner = runner
            self.assertEqual(release_recovery.restore(args, verify_only=True), 0)
            calls.clear()
            self.assertEqual(release_recovery.restore(args), 0)
            self.assertTrue(any("restore" in row for row in calls))
            self.assertFalse(any("verify-only" in row for row in calls))

    def test_verify_accepts_prior_failed_gate_without_marker(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            status = root / "status"
            args = self.restore_args(root, status_dir=status)

            def failing(argv, **_kwargs):
                if "restore" in argv:
                    run_dir = Path(argv[argv.index("--status-dir") + 1])
                    run_dir.mkdir(parents=True, exist_ok=True)
                    (run_dir / "receipt.json").write_text(
                        json.dumps({"status": "failed"}),
                        encoding="utf-8",
                    )
                    return Result(returncode=1, stderr="failed\n")
                return Result()

            args.runner = failing
            with self.assertRaises(release_recovery.GateError):
                release_recovery.restore(args)

            def verifying(argv, **_kwargs):
                if "verify-only" in argv:
                    run_dir = Path(argv[argv.index("--status-dir") + 1])
                    run_dir.mkdir(parents=True, exist_ok=True)
                    (run_dir / "receipt.json").write_text(
                        json.dumps(
                            {
                                "status": "succeeded",
                                "windows": {
                                    "task_state": "Disabled",
                                    "endpoint": "stopped",
                                    "state_sha256": "d" * 64,
                                },
                            }
                        ),
                        encoding="utf-8",
                    )
                return Result()

            args.runner = verifying
            self.assertEqual(release_recovery.restore(args, verify_only=True), 0)
            gate = json.loads((status / release_recovery.RECEIPT).read_text(encoding="utf-8"))
            self.assertEqual(gate["operation"], "recovery-verify-only")
            self.assertEqual(gate["database_rollback"], False)

    def test_timeout_preserves_failure_gate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            status = root / "status"
            args = self.restore_args(root, status_dir=status)

            def timeout(_argv, **_kwargs):
                raise subprocess.TimeoutExpired("recovery.py", 1)

            args.runner = timeout
            with self.assertRaisesRegex(release_recovery.GateError, "bounded timeout"):
                release_recovery.restore(args)
            gate = json.loads((status / release_recovery.RECEIPT).read_text(encoding="utf-8"))
            self.assertEqual(gate["status"], "failed")
            self.assertEqual(gate["preserved"], True)

    def test_missing_marker_and_key_are_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            status = root / "status"
            args = self.restore_args(root, status_dir=status)

            def runner(argv, **_kwargs):
                if "restore" in argv:
                    run_dir = Path(argv[argv.index("--status-dir") + 1])
                    run_dir.mkdir(parents=True, exist_ok=True)
                    (run_dir / "receipt.json").write_text(
                        json.dumps(
                            {
                                "status": "succeeded",
                                "windows": {
                                    "task_state": "Disabled",
                                    "endpoint": "stopped",
                                    "state_sha256": "d" * 64,
                                },
                            }
                        ),
                        encoding="utf-8",
                    )
                return Result()

            args.runner = runner
            with self.assertRaisesRegex(release_recovery.GateError, "JSON input"):
                release_recovery.restore(args)
            Path(args.key).unlink()
            with self.assertRaisesRegex(release_recovery.GateError, "recovery key"):
                release_recovery.restore(args)


def args_release(args) -> dict:
    document = json.loads(Path(args.expected_release).read_text(encoding="utf-8"))
    return document.get("release", document)


if __name__ == "__main__":
    unittest.main()
