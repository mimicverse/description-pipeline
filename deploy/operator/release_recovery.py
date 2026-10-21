#!/usr/bin/env python3
"""Bind release promotion rollback to the repository recovery controls."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

SCHEMA = "description-pipeline.release-recovery-gate/v1"
RECEIPT = "release-recovery-gate.json"


class GateError(Exception):
    """A stable release-recovery refusal."""

    def __init__(self, code: str, message: str, detail: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail or {}


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def require_file(path: Path, label: str) -> Path:
    if path.is_symlink() or not path.is_file():
        raise GateError("release_recovery_input_missing", f"{label} is missing", {"path": str(path)})
    return path


def load_json(path: Path) -> dict:
    require_file(path, "JSON input")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise GateError("release_recovery_input_invalid", f"cannot read {path}", {"error": str(error)}) from error
    if not isinstance(value, dict):
        raise GateError("release_recovery_input_invalid", f"{path} must be an object")
    return value


def expected_identity(path: Path) -> dict:
    document = load_json(path)
    release = document.get("release", document)
    identity = {
        key: release.get(key)
        for key in ("version", "source_commit", "source_sha256")
    }
    if not all(isinstance(value, str) and value for value in identity.values()):
        raise GateError("release_recovery_release_identity_invalid", str(path))
    return identity


def run(argv: list[str], *, runner=subprocess.run) -> subprocess.CompletedProcess:
    try:
        result = runner(argv, capture_output=True, text=True, check=False)
    except (OSError, subprocess.SubprocessError) as error:
        raise GateError(
            "release_recovery_command_failed",
            f"cannot execute {Path(argv[0]).name}",
            {"program": Path(argv[0]).name, "error": str(error)},
        ) from error
    if result.returncode != 0:
        raise GateError(
            "release_recovery_command_failed",
            f"{Path(argv[0]).name} exited with {result.returncode}",
            {
                "program": Path(argv[0]).name,
                "stderr": "\n".join(result.stderr.strip().splitlines()[-12:]),
            },
        )
    return result


def key_values(stdout: str) -> dict[str, str]:
    result = {}
    for line in stdout.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            result[key] = value
    return result


def write_once(path: Path, document: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(document, handle, indent=2, sort_keys=True)
        handle.write("\n")


def release_from_manifest(path: Path) -> dict:
    manifest = load_json(path)
    release = manifest.get("release")
    if not isinstance(release, dict):
        raise GateError("release_recovery_manifest_invalid", "recovery manifest has no release identity")
    identity = {
        key: release.get(key)
        for key in ("version", "source_commit", "source_sha256")
    }
    if not all(isinstance(value, str) and value for value in identity.values()):
        raise GateError("release_recovery_manifest_invalid", "recovery manifest release identity is malformed")
    return identity


def snapshot(args) -> int:
    config = require_file(Path(args.config), "platform config")
    key = require_file(Path(args.key_file), "recovery key")
    operatorctl = require_file(Path(args.operatorctl), "operatorctl")
    identity = expected_identity(Path(args.expected_release))
    out = Path(args.out)
    if out.exists() or out.is_symlink():
        raise GateError("release_recovery_output_exists", "snapshot output must be absent", {"path": str(out)})
    result = run(
        [
            sys.executable,
            args.recovery_py,
            "backup",
            "--config",
            str(config),
            "--backup-id",
            args.backup_id,
            "--key-file",
            str(key),
            "--out",
            str(out),
            "--operatorctl",
            str(operatorctl),
        ],
        runner=args.runner,
    )
    values = key_values(result.stdout)
    manifest = Path(values.get("manifest", ""))
    if not manifest.is_file():
        raise GateError("release_recovery_snapshot_missing", "recovery backup did not produce a manifest")
    if values.get("manifest_sha256") != sha256_file(manifest):
        raise GateError("release_recovery_snapshot_hash_mismatch", "snapshot manifest hash differs")
    manifest_release = release_from_manifest(manifest)
    if manifest_release != identity:
        raise GateError("release_recovery_release_identity_mismatch", "snapshot release identity differs")
    write_once(
        out / RECEIPT,
        {
            "schema": SCHEMA,
            "operation": "snapshot",
            "status": "succeeded",
            "release": identity,
            "manifest": str(manifest),
            "manifest_sha256": values["manifest_sha256"],
            "archive": values.get("archive"),
            "archive_sha256": values.get("archive_sha256"),
            "database_snapshot": True,
            "admission": "closed",
        },
    )
    print(json.dumps({"status": "succeeded", "manifest": str(manifest)}, sort_keys=True))
    return 0


def restore(args, *, verify_only: bool = False) -> int:
    manifest = require_file(Path(args.manifest), "recovery manifest")
    key = require_file(Path(args.key), "recovery key")
    plan = require_file(Path(args.target_plan), "target plan")
    operatorctl = require_file(Path(args.operatorctl), "operatorctl")
    status_dir = Path(args.status_dir)
    receipt = status_dir / RECEIPT
    identity = expected_identity(Path(args.expected_release))
    if release_from_manifest(manifest) != identity:
        raise GateError(
            "release_recovery_release_identity_mismatch",
            "recovery manifest release identity differs from the expected release",
        )
    inputs = {
        "manifest_sha256": sha256_file(manifest),
        "key_sha256": sha256_file(key),
        "target_plan_sha256": sha256_file(plan),
        "release": identity,
    }
    if receipt.is_file():
        prior = load_json(receipt)
        if prior.get("status") == "succeeded" and prior.get("inputs") == inputs:
            print(json.dumps({"status": "succeeded", "idempotent": True}, sort_keys=True))
            return 0
        raise GateError("release_recovery_status_exists", "recovery status already contains different evidence")
    if status_dir.exists() and any(status_dir.iterdir()):
        raise GateError("release_recovery_status_not_empty", "recovery status directory is not empty")
    status_dir.mkdir(parents=True, exist_ok=True)
    action = "verify-only" if verify_only else "restore"
    try:
        result = run(
            [
                sys.executable,
                args.recovery_py,
                action,
                "--manifest",
                str(manifest),
                "--key",
                str(key),
                "--target-plan",
                str(plan),
                "--status-dir",
                str(status_dir),
            ],
            runner=args.runner,
        )
        recovery_receipt = load_json(status_dir / "receipt.json")
        if recovery_receipt.get("status") != "succeeded":
            raise GateError(
                "release_recovery_database_unverified",
                "recovery receipt did not verify the restored database and runtime",
            )
        marker = load_json(status_dir / "recovery-marker.json")
        if marker.get("release") != identity:
            raise GateError(
                "release_recovery_release_identity_mismatch",
                "recovery marker release identity differs from the expected release",
            )
        windows = recovery_receipt.get("windows")
        if (
            not isinstance(windows, dict)
            or windows.get("task_state") != "Disabled"
            or windows.get("endpoint") != "stopped"
            or windows.get("state_sha256") is None
        ):
            raise GateError(
                "release_recovery_windows_unverified",
                "Windows recovery did not return a verified disabled, stopped state",
            )
        if not verify_only:
            run(
                [str(operatorctl), "start", "--config", args.config],
                runner=args.runner,
            )
        run(
            [str(operatorctl), "health", "--config", args.config],
            runner=args.runner,
        )
    except GateError as error:
        failure = {
            "schema": SCHEMA,
            "operation": action,
            "status": "failed",
            "inputs": inputs,
            "error": {"code": error.code, "message": error.message, "detail": error.detail},
            "preserved": True,
        }
        if (status_dir / "receipt.json").is_file():
            failure["recovery_receipt_sha256"] = sha256_file(status_dir / "receipt.json")
        write_once(receipt, failure)
        raise
    write_once(
        receipt,
        {
            "schema": SCHEMA,
            "operation": action,
            "status": "succeeded",
            "inputs": inputs,
            "recovery_receipt_sha256": sha256_file(status_dir / "receipt.json"),
            "database_rollback": not verify_only,
            "runtime_restart": not verify_only,
            "health": "PASS",
            "windows": recovery_receipt.get("windows"),
            "stdout": result.stdout.strip().splitlines()[-1:] if result.stdout.strip() else [],
        },
    )
    print(json.dumps({"status": "succeeded", "operation": action}, sort_keys=True))
    return 0


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="release_recovery.py")
    root.add_argument("--recovery-py", default=str(Path(__file__).with_name("recovery.py")))
    sub = root.add_subparsers(dest="action", required=True)
    snapshot_parser = sub.add_parser("snapshot")
    snapshot_parser.add_argument("--config", required=True)
    snapshot_parser.add_argument("--backup-id", required=True)
    snapshot_parser.add_argument("--key-file", required=True)
    snapshot_parser.add_argument("--out", required=True)
    snapshot_parser.add_argument("--operatorctl", required=True)
    snapshot_parser.add_argument("--expected-release", required=True)
    restore_parser = sub.add_parser("restore")
    verify_parser = sub.add_parser("verify")
    for item in (restore_parser, verify_parser):
        item.add_argument("--manifest", required=True)
        item.add_argument("--key", required=True)
        item.add_argument("--target-plan", required=True)
        item.add_argument("--status-dir", required=True)
        item.add_argument("--config", required=True)
        item.add_argument("--operatorctl", required=True)
        item.add_argument("--expected-release", required=True)
    plan_parser = sub.add_parser("release-restore")
    plan_parser.add_argument("--plan", required=True)
    return root


def restore_from_plan(args):
    plan = load_json(Path(args.plan))
    required = {
        "recovery_py",
        "config",
        "operatorctl",
        "manifest",
        "key_file",
        "target_plan",
        "expected_release",
    }
    if not required.issubset(plan):
        raise GateError(
            "release_recovery_plan_invalid",
            "release recovery plan is missing required fields",
            {"missing": sorted(required - set(plan))},
        )
    status_dir = plan.get("release_restore_status_dir", plan.get("restore_status_dir"))
    if not isinstance(status_dir, str) or not status_dir:
        raise GateError(
            "release_recovery_plan_invalid",
            "release recovery plan has no restore status directory",
        )
    return SimpleNamespace(
        recovery_py=plan["recovery_py"],
        manifest=plan["manifest"],
        key=plan["key_file"],
        target_plan=plan["target_plan"],
        status_dir=status_dir,
        config=plan["config"],
        operatorctl=plan["operatorctl"],
        expected_release=plan["expected_release"],
        runner=args.runner,
    )


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    args.runner = subprocess.run
    try:
        if args.action == "snapshot":
            return snapshot(args)
        if args.action == "release-restore":
            return restore(restore_from_plan(args))
        return restore(args, verify_only=args.action == "verify")
    except GateError as error:
        print(
            json.dumps(
                {"status": "failed", "code": error.code, "message": error.message, "detail": error.detail},
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
