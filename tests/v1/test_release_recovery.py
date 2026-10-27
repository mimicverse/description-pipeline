"""Unit gates for the release-to-recovery binding."""

from __future__ import annotations

import hashlib
import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

HERE = Path(__file__).resolve().parent
REPOSITORY = HERE.parents[1]
sys.path.insert(0, str(REPOSITORY / "deploy" / "operator"))

import release_recovery  # noqa: E402
import promotion  # noqa: E402
from tests.v1 import test_disaster_recovery as recovery_tests  # noqa: E402
from tests.v1.windows_fixtures import operator_status_sample  # noqa: E402


def digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


class Result:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class ReleaseRecoveryTests(unittest.TestCase):
    def freeze_steps(self, receipt):
        root = self.current_receipt_path.parent
        for index, row in enumerate(receipt["steps"], 1):
            row["seq"] = index
            row["at"] = "2026-10-22T00:00:00Z"
            if "evidence" not in row:
                source = root / f"{row['verb']}-{row['step']}.json"
                source.write_text(json.dumps({"step": row["step"]}))
                files = promotion.freeze_evidence([source], root / "evidence-immutable", row["verb"], row["step"])
                row["evidence"] = [str(path) for path in files]
                row["evidence_digests"] = [{"path": str(path), "sha256": digest(path.read_bytes())} for path in files]
        return receipt

    def add_promotion_history(self, receipt):
        names = [
            "linux-generation",
            "windows-generation",
            "close-admission",
            "drain",
            "backup",
            "stop-services",
            "schema-migration",
            "windows-switch",
            "endpoint-identity",
            "switch-pointer",
            "config-reference",
            "start-services",
            "verify",
            "open-admission",
        ]
        receipt["plan"] = names + receipt["plan"]
        prefix = [{"verb": "promote", "step": name, "status": "completed"} for name in names]
        receipt["steps"] = prefix + receipt["steps"]
        for index, row in enumerate(receipt["steps"], 1):
            row["seq"] = index
        return self.freeze_steps(receipt)

    def test_status_identity_rejects_crosswired_source_context(self):
        install_id = "win-" + "a" * 32
        config_digest = "a" * 64
        release = {
            "version": "1.5.0",
            "source_commit": "b" * 40,
            "source_sha256": "c" * 64,
            "role": "native",
        }
        sample = operator_status_sample(install_id, config_digest, release)
        self.assertTrue(
            release_recovery._status_identity_matches(
                sample, install_id, config_digest
            )
        )
        for field, value in (
            ("install_id", "win-" + "d" * 32),
            ("config_digest", "e" * 64),
        ):
            with self.subTest(field=field):
                crosswired = copy.deepcopy(sample)
                crosswired["source"][field] = value
                self.assertFalse(
                    release_recovery._status_identity_matches(
                        crosswired, install_id, config_digest
                    )
                )

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
        recovery = root / "recovery.py"
        recovery.write_text("# test recovery entry\n", encoding="utf-8")
        operatorctl = root / "operatorctl.sh"
        operatorctl.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        operatorctl.chmod(0o700)
        target_plan = root / "target-plan.json"
        target_plan.write_text("{}\n", encoding="utf-8")
        manifest = root / "recovery-manifest.json"
        manifest.write_text(
            json.dumps({"release": release, "windows": {"state_sha256": "d" * 64}}),
            encoding="utf-8",
        )
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

    def completed_finalize_inputs(self, root: Path) -> SimpleNamespace:
        status = root / "status"
        restore = self.restore_args(root, status_dir=status)
        harness = recovery_tests.RecoveryControlFlowTests("test_backup_and_restore_control_flow")
        harness.setUp()
        self.addCleanup(harness.doCleanups)
        self.addCleanup(harness.tearDown)
        out, manifest_document = harness.backup()
        Path(restore.manifest).write_bytes((out / "recovery-manifest.json").read_bytes())
        Path(restore.expected_release).write_text(
            json.dumps(
                {
                    "release": {
                        key: manifest_document["release"][key] for key in ("version", "source_commit", "source_sha256")
                    }
                }
            )
        )
        roots = {row["name"]: row["path"] for row in manifest_document["roots"] if "reconstructible_runtime" not in row}
        target = harness.target_plan(digest(Path(restore.manifest).read_bytes()), roots)
        Path(restore.target_plan).write_bytes(target.read_bytes())
        state_root = Path(roots["state_root"])
        Path(restore.config).write_text(
            json.dumps({"storage": {"state_root": str(state_root)}}),
            encoding="utf-8",
        )

        def restoring(argv, **_kwargs):
            if "restore" in argv:
                run_dir = Path(argv[argv.index("--status-dir") + 1])
                run_dir.mkdir(parents=True, exist_ok=True)
                windows = {
                    "task_state": "Disabled",
                    "endpoint": "stopped",
                    "cad_ownership": "free",
                    "state_sha256": manifest_document["windows"]["state_sha256"],
                }
                (run_dir / "receipt.json").write_text(
                    json.dumps(
                        {
                            "schema_version": "solidworks-to-urdf.recovery-receipt/v1",
                            "operation": "restore",
                            "status": "succeeded",
                            "manifest_sha256": digest(Path(restore.manifest).read_bytes()),
                            "target_plan_sha256": digest(Path(restore.target_plan).read_bytes()),
                            "closure": manifest_document["closure"],
                            "windows": windows,
                        }
                    ),
                    encoding="utf-8",
                )
                (run_dir / "recovery-marker.json").write_text(
                    json.dumps({"release": args_release(restore), "closure": manifest_document["closure"]}),
                    encoding="utf-8",
                )
            return Result()

        restore.runner = restoring
        self.assertEqual(release_recovery.restore(restore), 0)

        install_id = "gen-release-rollback"
        source = state_root / "install-records/rollback-source.json"
        source_snapshot = state_root / "snapshots/rollback-source.json"
        source_platform = (
            state_root / "promotions" / install_id / "source-platform.json"
        )
        source_platform.parent.mkdir(parents=True, exist_ok=True)
        source_platform.write_text('{"schema":"source"}\n', encoding="utf-8")
        source_snapshot.write_text(
            json.dumps(
                {
                    "install_id": "rollback-source",
                    "sources": {
                        "platform_json_sha256": digest(source_platform.read_bytes())
                    },
                }
            )
        )
        source.write_text(
            json.dumps(recovery_tests.install_record_fixture(state_root, "rollback-source", source_snapshot))
        )
        target_snapshot = state_root / "snapshots" / (install_id + ".json")
        target_snapshot.write_text(json.dumps({"install_id": install_id}))
        code_rollback = state_root / "promotions" / install_id / "receipt.json"
        self.current_receipt_path = code_rollback
        code_rollback.parent.mkdir(parents=True, exist_ok=True)
        code_rollback.write_text(
            json.dumps(
                self.freeze_steps(
                    {
                        "schema_version": "solidworks-to-urdf.promotion/v1",
                        "install_id": install_id,
                        "from": str(source),
                        "from_sha256": digest(source.read_bytes()),
                        "to": str(target_snapshot),
                        "to_sha256": digest(target_snapshot.read_bytes()),
                        "release_manifest": {
                            "path": restore.expected_release,
                            "sha256": digest(Path(restore.expected_release).read_bytes()),
                        },
                        "plan": list(release_recovery.ROLLBACK_STEPS),
                        "steps": [
                            {
                                "seq": index,
                                "verb": "rollback",
                                "step": step,
                                "status": "completed",
                            }
                            for index, step in enumerate(release_recovery.ROLLBACK_STEPS, 1)
                        ],
                    }
                )
            ),
            encoding="utf-8",
        )
        return SimpleNamespace(
            config=restore.config,
            operatorctl=restore.operatorctl,
            recovery_py=restore.recovery_py,
            expected_release=restore.expected_release,
            restore_status_dir=str(status),
            code_rollback_receipt=str(code_rollback),
            install_id=install_id,
            runner=None,
        )

    def test_snapshot_binds_release_and_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            expected, config, key, operatorctl, _plan, release = self.make_inputs(root)
            out = root / "snapshot"
            source_config = root / "source-platform.json"
            source_config.write_text('{"source":true}\n', encoding="utf-8")

            def runner(argv, **_kwargs):
                self.assertIn("backup", argv)
                self.assertEqual(
                    argv[argv.index("--source-config") + 1],
                    str(source_config),
                )
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
                source_config=str(source_config),
                runner=runner,
            )
            self.assertEqual(release_recovery.snapshot(args), 0)
            gate = json.loads((out / release_recovery.RECEIPT).read_text(encoding="utf-8"))
            self.assertEqual(gate["database_snapshot"], True)
            self.assertEqual(gate["release"], release)
            self.assertEqual(
                gate["source_config_sha256"],
                digest(source_config.read_bytes()),
            )

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
                                    "cad_ownership": "free",
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
            with mock.patch.object(
                release_recovery.windows_identity,
                "__file__",
                args.recovery_py,
            ), self.assertRaisesRegex(
                release_recovery.GateError,
                "differs from the fixed release path",
            ):
                release_recovery.restore(args)
            self.assertEqual(len(calls), before)
            receipt = json.loads((status / release_recovery.RECEIPT).read_text(encoding="utf-8"))
            self.assertEqual(receipt["database_rollback"], True)
            self.assertEqual(receipt["database_restore_executed"], True)
            self.assertEqual(receipt["database_recovery_verified"], True)
            self.assertEqual(receipt["runtime_restart"], False)
            self.assertEqual(receipt["services_stopped"], True)
            self.assertEqual(receipt["admission"], "closed")
            self.assertEqual(receipt["health"], "NOT_RUN")
            self.assertFalse(any("start" in row for row in calls))

    def test_parser_refuses_windows_identity_override(self):
        with self.assertRaises(SystemExit):
            release_recovery.parser().parse_args(
                [
                    "--windows-identity-py",
                    "/fixed/windows_identity.py",
                    "verify",
                    "--manifest",
                    "manifest.json",
                    "--key",
                    "key",
                    "--target-plan",
                    "target.json",
                    "--status-dir",
                    "status",
                    "--config",
                    "platform.json",
                    "--operatorctl",
                    "operatorctl.sh",
                    "--expected-release",
                    "release.json",
                ]
            )

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
                                    "cad_ownership": "free",
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
            self.assertFalse(any("start" in row for row in calls))
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
                                    "cad_ownership": "free",
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
            self.assertEqual(gate["database_restore_executed"], False)
            self.assertEqual(gate["database_recovery_verified"], True)
            self.assertEqual(gate["runtime_restart"], False)
            self.assertEqual(gate["services_stopped"], True)
            self.assertEqual(gate["admission"], "closed")

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
                                    "cad_ownership": "free",
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

    def test_windows_state_hash_must_match_manifest(self):
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
                                    "cad_ownership": "free",
                                    "state_sha256": "not-a-hash",
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
            with self.assertRaisesRegex(release_recovery.GateError, "state-identical"):
                release_recovery.restore(args)
            gate = json.loads((status / release_recovery.RECEIPT).read_text(encoding="utf-8"))
            self.assertEqual(gate["status"], "failed")
            self.assertNotIn("windows_verified", gate)

    def test_release_plan_cannot_replace_fixed_authorities(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan = root / "plan.json"
            plan.write_text(
                json.dumps(
                    {
                        "config": str(root / "config.json"),
                        "manifest": str(root / "manifest.json"),
                        "key_file": str(root / "key"),
                        "target_plan": str(root / "target.json"),
                        "operatorctl": str(root / "fake-operatorctl"),
                        "recovery_py": str(root / "fake-recovery.py"),
                        "expected_release": str(root / "fake-release.json"),
                    }
                ),
                encoding="utf-8",
            )
            args = SimpleNamespace(plan=str(plan), runner=lambda *_a, **_k: Result(), timeout=1)
            with self.assertRaisesRegex(release_recovery.GateError, "fixed release authorities"):
                release_recovery.restore_from_plan(args)
            with self.assertRaisesRegex(release_recovery.GateError, "fixed release authorities"):
                release_recovery.finalize_from_plan(args)

    def test_release_plan_uses_fixed_release_root_authorities(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            release_root = root / "release"
            operator_root = release_root / "deploy" / "operator"
            operator_root.mkdir(parents=True)
            release = {
                "version": "1.5.0",
                "source_commit": "a" * 40,
                "source_sha256": "b" * 64,
            }
            (release_root / "release.json").write_text(
                json.dumps({"release": release}),
                encoding="utf-8",
            )
            (operator_root / "recovery.py").write_text("# fixed recovery\n", encoding="utf-8")
            (operator_root / "operatorctl.sh").write_text("#!/bin/sh\n", encoding="utf-8")
            (operator_root / "windows_identity.py").write_text(
                "# fixed identity\n", encoding="utf-8"
            )
            config = root / "platform.json"
            config.write_text("{}\n", encoding="utf-8")
            plan = root / "plan.json"
            status = root / "status"
            plan.write_text(
                json.dumps(
                    {
                        "config": str(config),
                        "manifest": str(root / "manifest.json"),
                        "key_file": str(root / "key"),
                        "target_plan": str(root / "target.json"),
                        "restore_status_dir": str(status),
                    }
                ),
                encoding="utf-8",
            )
            args = SimpleNamespace(plan=str(plan), runner=lambda *_a, **_k: Result(), timeout=1)
            original = release_recovery.RELEASE_ROOT
            release_recovery.RELEASE_ROOT = release_root
            try:
                parsed = release_recovery.restore_from_plan(args)
            finally:
                release_recovery.RELEASE_ROOT = original
            self.assertEqual(Path(parsed.recovery_py), operator_root / "recovery.py")
            self.assertEqual(Path(parsed.operatorctl), operator_root / "operatorctl.sh")
            self.assertEqual(Path(parsed.expected_release), release_root / "release.json")
            self.assertEqual(parsed.status_dir, str(status))

    def test_finalize_requires_restore_then_starts_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            args = self.completed_finalize_inputs(root)
            history_path = Path(args.code_rollback_receipt)
            history = self.add_promotion_history(json.loads(history_path.read_text()))
            history_path.write_text(json.dumps(history), encoding="utf-8")
            calls = []

            def runner(argv, **_kwargs):
                calls.append(argv)
                action = argv[1]
                if action == "start":
                    return Result(stdout="start complete: admission open\n")
                if action == "status":
                    return Result(
                        stdout=(
                            "description-portal.service active\n"
                            + json.dumps(
                                operator_status_sample(
                                    "win-" + "a" * 32,
                                    "a" * 64,
                                    args_release(args),
                                )
                            )
                            + "\n"
                        )
                    )
                if action == "health":
                    return Result(stdout="PASS endpoint identity\n")
                self.fail(f"unexpected command: {argv}")

            args.runner = runner
            self.assertEqual(release_recovery.finalize(args), 0)
            self.assertEqual([argv[1] for argv in calls], ["start", "status", "health"])
            source_config = (
                Path(args.code_rollback_receipt).parent / "source-platform.json"
            )
            for argv in calls:
                self.assertEqual(
                    argv[argv.index("--source-config") + 1],
                    str(source_config.resolve()),
                )
            status = Path(args.restore_status_dir)
            gate = json.loads((status / release_recovery.FINALIZE_RECEIPT).read_text(encoding="utf-8"))
            restore_gate = json.loads((status / release_recovery.RECEIPT).read_text(encoding="utf-8"))
            self.assertEqual(gate["operation"], "release-finalize")
            self.assertEqual(gate["database_restore_executed"], True)
            self.assertEqual(gate["database_recovery_verified"], True)
            self.assertEqual(gate["runtime_restart"], True)
            self.assertEqual(gate["services_running"], True)
            self.assertEqual(gate["admission"], "open")
            self.assertEqual(gate["windows_verified"], True)
            self.assertEqual(gate["health"], "PASS")
            self.assertEqual(gate["operator_status"], "PASS")
            self.assertEqual(gate["restore"]["operation"], "recovery-restore")
            self.assertEqual(
                gate["inputs"]["restore_gate_sha256"],
                digest((status / release_recovery.RECEIPT).read_bytes()),
            )
            self.assertEqual(
                gate["inputs"]["source_config_sha256"],
                digest(source_config.read_bytes()),
            )
            self.assertFalse(restore_gate["runtime_restart"])
            before = len(calls)
            self.assertEqual(release_recovery.finalize(args), 0)
            self.assertEqual(len(calls), before)
            for field, value in (("install_id", args.install_id), ("config_digest", "f" * 64), ("task_state", "Ready")):
                with self.subTest(cached_identity_field=field):
                    gate_path = status / release_recovery.FINALIZE_RECEIPT
                    changed = json.loads(gate_path.read_text())
                    changed["operator_identity"][field] = value
                    gate_path.write_text(json.dumps(changed), encoding="utf-8")
                    before = len(calls)
                    self.assertEqual(release_recovery.finalize(args), 0)
                    self.assertEqual(len(calls), before + 3)

    def test_code_rollback_accepts_phase_history_and_latest_completion(self):
        with tempfile.TemporaryDirectory() as directory:
            args = self.completed_finalize_inputs(Path(directory))
            path = Path(args.code_rollback_receipt)
            rollback_only = json.loads(path.read_text())
            mixed = self.add_promotion_history(copy.deepcopy(rollback_only))
            retried = copy.deepcopy(mixed)
            retried["steps"].insert(14, {"verb": "rollback", "step": "rollback-close", "status": "failed"})
            for index, row in enumerate(retried["steps"], 1):
                row["seq"] = index
            for history in (rollback_only, mixed, retried):
                with self.subTest(rows=len(history["steps"])):
                    path.write_text(json.dumps(history), encoding="utf-8")
                    self.assertEqual(
                        release_recovery.validate_code_rollback(path, args.install_id, args_release(args)), history
                    )

    def test_code_rollback_rejects_invalid_history_and_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            args = self.completed_finalize_inputs(Path(directory))
            path = Path(args.code_rollback_receipt)
            original = self.add_promotion_history(json.loads(path.read_text()))
            mutations = {
                "foreign_verb": lambda d: d["steps"][0].update(verb="foreign"),
                "bad_promote": lambda d: d["steps"][0].update(status="foreign"),
                "empty_step": lambda d: d["steps"][0].update(step=""),
                "unknown_step": lambda d: d["steps"][0].update(step="not-in-plan"),
                "failed_rollback": lambda d: d["steps"][-1].update(status="failed"),
                "unknown_rollback": lambda d: d["steps"][-1].update(status="unknown"),
                "missing_step": lambda d: d["steps"].pop(),
                "later_failure": lambda d: d["steps"].append(
                    {"seq": 22, "verb": "rollback", "step": "rollback-close", "status": "failed"}
                ),
                "bad_seq_type": lambda d: d["steps"][0].update(seq="1"),
                "boolean_seq": lambda d: d["steps"][0].update(seq=True),
                "zero_seq": lambda d: d["steps"][0].update(seq=0),
                "duplicate_seq": lambda d: d["steps"][1].update(seq=1),
                "reordered": lambda d: d["steps"].reverse(),
                "bad_verb_type": lambda d: d["steps"][0].update(verb=[]),
                "bad_status_type": lambda d: d["steps"][0].update(status=[]),
                "promote_cannot_complete_rollback": lambda d: d["steps"][-1].update(verb="promote"),
                "wrong_install": lambda d: d.update(install_id="foreign"),
                "wrong_from": lambda d: d.update({"from": str(Path(directory) / "missing")}),
                "wrong_hash": lambda d: d.update(from_sha256="0" * 64),
            }
            for name, mutate in mutations.items():
                with self.subTest(name=name):
                    history = copy.deepcopy(original)
                    mutate(history)
                    path.write_text(json.dumps(history), encoding="utf-8")
                    with self.assertRaises(release_recovery.GateError):
                        release_recovery.validate_code_rollback(path, args.install_id, args_release(args))
            path.write_text(json.dumps(original), encoding="utf-8")
            with self.assertRaises(release_recovery.GateError):
                release_recovery.validate_code_rollback(
                    path, args.install_id, {**args_release(args), "source_commit": "c" * 40}
                )

    def test_finalize_rejects_crosswired_windows_identity(self):
        for use_generation_id in (True, False):
            with self.subTest(use_generation_id=use_generation_id), tempfile.TemporaryDirectory() as directory:
                args = self.completed_finalize_inputs(Path(directory))
                calls = []

                def runner(argv, calls=calls, args=args, use_generation_id=use_generation_id, **_kwargs):
                    action = argv[1]
                    calls.append(action)
                    if action == "status":
                        return Result(
                            stdout=json.dumps(
                                operator_status_sample(
                                    args.install_id
                                    if use_generation_id
                                    else "win-" + "a" * 32,
                                    "a" * 64 if use_generation_id else "b" * 64,
                                    args_release(args),
                                )
                            )
                        )
                    return Result(stdout="admission open\n")

                args.runner = runner
                with self.assertRaisesRegex(release_recovery.GateError, "active Windows identity"):
                    release_recovery.finalize(args)
                self.assertNotIn("health", calls)

    def test_finalize_rejects_malformed_source_windows_identity_before_start(self):
        mutations = {
            "missing_ends": lambda d: d.pop("ends"),
            "nonobject_ends": lambda d: d.update(ends=[]),
            "missing_windows": lambda d: d["ends"].pop("windows"),
            "nonobject_windows": lambda d: d["ends"].update(windows=[]),
            "nonstring_id": lambda d: d["ends"]["windows"].update(install_id=1),
            "empty_id": lambda d: d["ends"]["windows"].update(install_id=""),
            "uppercase_id": lambda d: d["ends"]["windows"].update(install_id="win-" + "A" * 32),
            "wrong_prefix": lambda d: d["ends"]["windows"].update(install_id="gen-" + "a" * 32),
            "short_id": lambda d: d["ends"]["windows"].update(install_id="win-" + "a" * 31),
            "nonhex_id": lambda d: d["ends"]["windows"].update(install_id="win-" + "g" * 32),
            "bad_digest": lambda d: d["ends"]["windows"].update(config_digest="short"),
            "nonstring_digest": lambda d: d["ends"]["windows"].update(config_digest=1),
            "wrong_release": lambda d: d["ends"]["windows"]["release"].update(source_commit="c" * 40),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                args = self.completed_finalize_inputs(Path(directory))
                rollback_path = Path(args.code_rollback_receipt)
                rollback = json.loads(rollback_path.read_text())
                source_path = Path(rollback["from"])
                source = json.loads(source_path.read_text())
                mutate(source)
                source_path.write_text(json.dumps(source), encoding="utf-8")
                rollback["from_sha256"] = digest(source_path.read_bytes())
                rollback_path.write_text(json.dumps(rollback), encoding="utf-8")
                calls = []
                args.runner = lambda argv, calls=calls, **_kwargs: calls.append(argv) or Result()
                with self.assertRaisesRegex(release_recovery.GateError, "source Windows identity"):
                    release_recovery.finalize(args)
                self.assertEqual(calls, [])

    def test_finalize_refuses_verify_only_gate_before_start(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            args = self.completed_finalize_inputs(root)
            gate_path = Path(args.restore_status_dir) / release_recovery.RECEIPT
            gate = json.loads(gate_path.read_text(encoding="utf-8"))
            gate["operation"] = "recovery-verify-only"
            gate_path.write_text(json.dumps(gate), encoding="utf-8")
            calls = []
            args.runner = lambda argv, **_kwargs: calls.append(argv) or Result()
            with self.assertRaisesRegex(release_recovery.GateError, "restore operation"):
                release_recovery.finalize(args)
            self.assertEqual(calls, [])

    def test_finalize_refuses_incomplete_code_rollback_before_start(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            args = self.completed_finalize_inputs(root)
            rollback_path = Path(args.code_rollback_receipt)
            receipt = json.loads(rollback_path.read_text(encoding="utf-8"))
            receipt["steps"][-1]["status"] = "failed"
            rollback_path.write_text(json.dumps(receipt), encoding="utf-8")
            calls = []
            args.runner = lambda argv, **_kwargs: calls.append(argv) or Result()
            with self.assertRaisesRegex(release_recovery.GateError, "has not completed"):
                release_recovery.finalize(args)
            self.assertEqual(calls, [])

    def test_finalize_requires_the_preserved_source_config_before_start(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            args = self.completed_finalize_inputs(root)
            source_config = (
                Path(args.code_rollback_receipt).parent / "source-platform.json"
            )
            source_config.write_text('{"schema":"forged"}\n', encoding="utf-8")
            calls = []
            args.runner = lambda argv, **_kwargs: calls.append(argv) or Result()
            with self.assertRaisesRegex(
                release_recovery.GateError,
                "no preserved source configuration",
            ):
                release_recovery.finalize(args)
            self.assertEqual(calls, [])

    def test_finalize_rejects_noncanonical_code_rollback_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            args = self.completed_finalize_inputs(root)
            fake = root / "fake-rollback.json"
            fake.write_bytes(Path(args.code_rollback_receipt).read_bytes())
            args.code_rollback_receipt = str(fake)
            calls = []
            args.runner = lambda argv, **_kwargs: calls.append(argv) or Result()
            with self.assertRaisesRegex(release_recovery.GateError, "canonical receipt"):
                release_recovery.finalize(args)
            self.assertEqual(calls, [])

    def test_finalize_start_failure_preserves_gate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            args = self.completed_finalize_inputs(root)
            calls = []

            def runner(argv, **_kwargs):
                calls.append(argv)
                return Result(returncode=1, stderr="start failed\n")

            args.runner = runner
            with self.assertRaisesRegex(release_recovery.GateError, "exited with 1"):
                release_recovery.finalize(args)
            self.assertEqual([argv[1] for argv in calls], ["start"])
            status = Path(args.restore_status_dir)
            gate = json.loads((status / release_recovery.FINALIZE_RECEIPT).read_text(encoding="utf-8"))
            self.assertEqual(gate["status"], "failed")
            self.assertEqual(gate["preserved"], True)
            self.assertIsNone(gate["failure_drain"])

    def test_finalize_post_start_failure_drains_and_preserves_identity_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            args = self.completed_finalize_inputs(root)
            calls = []

            def runner(argv, **_kwargs):
                calls.append(argv)
                if argv[1] == "start":
                    return Result(stdout="start complete: admission open\n")
                if argv[1] == "status":
                    return Result(
                        stdout=json.dumps(
                            operator_status_sample(
                                "another-generation",
                                "b" * 64,
                                args_release(args),
                            )
                        )
                        + "\n"
                    )
                if argv[1] == "drain":
                    return Result(stdout="admission=closed revision=2\n")
                self.fail(f"unexpected command: {argv}")

            args.runner = runner
            with self.assertRaisesRegex(release_recovery.GateError, "Windows identity"):
                release_recovery.finalize(args)
            self.assertEqual([argv[1] for argv in calls], ["start", "status", "drain"])
            status = Path(args.restore_status_dir)
            gate = json.loads((status / release_recovery.FINALIZE_RECEIPT).read_text(encoding="utf-8"))
            self.assertEqual(gate["status"], "failed")
            self.assertEqual(gate["failure_drain"]["returncode"], 0)
            self.assertTrue(gate["failure_drain"]["stdout_sha256"])


def args_release(args) -> dict:
    document = json.loads(Path(args.expected_release).read_text(encoding="utf-8"))
    return document.get("release", document)


if __name__ == "__main__":
    unittest.main()
