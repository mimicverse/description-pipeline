#!/usr/bin/env python3
"""Bind release promotion rollback to the repository recovery controls."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import windows_identity

SCHEMA = "solidworks-to-urdf.release-recovery-gate/v1"
RECEIPT = "release-recovery-gate.json"
FINALIZE_RECEIPT = "release-finalize-gate.json"
DEFAULT_TIMEOUT_SECONDS = 3600
ROLLBACK_STEPS = (
    "rollback-close",
    "rollback-drain",
    "rollback-stop",
    "rollback-windows",
    "rollback-linux",
    "rollback-config-reference",
    "rollback-verify",
)
HEX64 = re.compile(r"^[0-9a-f]{64}$")
RELEASE_ROOT = Path(__file__).resolve().parents[2]


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
        import recovery

        value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=recovery._pairs_no_duplicates)
    except (OSError, ValueError) as error:
        raise GateError("release_recovery_input_invalid", f"cannot read {path}", {"error": str(error)}) from error
    if not isinstance(value, dict):
        raise GateError("release_recovery_input_invalid", f"{path} must be an object")
    return value


def expected_identity(path: Path) -> dict:
    document = load_json(path)
    release = document.get("release", document)
    identity = {key: release.get(key) for key in ("version", "source_commit", "source_sha256")}
    if not all(isinstance(value, str) and value for value in identity.values()):
        raise GateError("release_recovery_release_identity_invalid", str(path))
    return identity


def run(
    argv: list[str],
    *,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
    runner=subprocess.run,
) -> subprocess.CompletedProcess:
    try:
        result = runner(argv, capture_output=True, text=True, check=False, timeout=timeout)
    except subprocess.TimeoutExpired as error:
        raise GateError(
            "release_recovery_command_timeout",
            f"{Path(argv[0]).name} exceeded its bounded timeout",
            {"program": Path(argv[0]).name, "timeout": timeout},
        ) from error
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
    identity = {key: release.get(key) for key in ("version", "source_commit", "source_sha256")}
    if not all(isinstance(value, str) and value for value in identity.values()):
        raise GateError("release_recovery_manifest_invalid", "recovery manifest release identity is malformed")
    return identity


def require_directory(path: Path, label: str) -> Path:
    if path.is_symlink() or not path.is_dir():
        raise GateError(
            "release_recovery_status_invalid",
            f"{label} must be a real directory",
            {"path": str(path)},
        )
    return path


def require_success(value: object, label: str, expected: object) -> None:
    if value != expected:
        raise GateError(
            "release_recovery_gate_invalid",
            f"{label} does not match the release recovery contract",
            {"actual": value, "expected": expected},
        )


def validate_restore_gate(status_dir: Path, identity: dict) -> tuple[Path, dict]:
    gate_path = require_file(status_dir / RECEIPT, "release restore gate")
    gate = load_json(gate_path)
    require_success(gate.get("schema"), "restore gate schema", SCHEMA)
    require_success(gate.get("operation"), "restore operation", "recovery-restore")
    require_success(gate.get("status"), "restore status", "succeeded")
    inputs = gate.get("inputs")
    if not isinstance(inputs, dict):
        raise GateError("release_recovery_gate_invalid", "restore gate inputs are malformed")
    require_success(inputs.get("release"), "restore release identity", identity)
    manifest = require_file(Path(str(inputs.get("manifest", ""))), "restore manifest")
    target_plan = require_file(Path(str(inputs.get("target_plan", ""))), "restore target plan")
    require_success(inputs.get("manifest_sha256"), "restore manifest hash", sha256_file(manifest))
    require_success(inputs.get("target_plan_sha256"), "restore target plan hash", sha256_file(target_plan))
    expected_release = require_file(
        Path(str(inputs.get("expected_release", ""))),
        "restore release manifest",
    )
    require_success(
        inputs.get("release_manifest_sha256"),
        "restore release manifest hash",
        sha256_file(expected_release),
    )
    require_success(gate.get("database_restore_executed"), "database_restore_executed", True)
    require_success(gate.get("database_recovery_verified"), "database_recovery_verified", True)
    require_success(gate.get("database_rollback"), "database_rollback", True)
    require_success(gate.get("runtime_restart"), "pre-finalize runtime_restart", False)
    require_success(gate.get("services_stopped"), "pre-finalize services_stopped", True)
    require_success(gate.get("admission"), "pre-finalize admission", "closed")
    require_success(gate.get("windows_verified"), "windows_verified", True)
    require_success(gate.get("health"), "pre-finalize health", "NOT_RUN")

    recovery_run = Path(str(gate.get("recovery_run", "")))
    if recovery_run.is_symlink() or recovery_run.resolve().parent != status_dir.resolve():
        raise GateError(
            "release_recovery_gate_invalid",
            "restore gate names a recovery run outside its status directory",
            {"recovery_run": str(recovery_run)},
        )
    recovery_receipt_path = require_file(recovery_run / "receipt.json", "recovery receipt")
    if gate.get("recovery_receipt_sha256") != sha256_file(recovery_receipt_path):
        raise GateError(
            "release_recovery_gate_invalid",
            "recovery receipt hash differs from the restore gate",
        )
    recovery_receipt = load_json(recovery_receipt_path)
    require_success(recovery_receipt.get("operation"), "recovery receipt operation", "restore")
    require_success(recovery_receipt.get("status"), "recovery receipt status", "succeeded")
    require_success(
        recovery_receipt.get("manifest_sha256"),
        "recovery receipt manifest hash",
        inputs.get("manifest_sha256"),
    )
    require_success(
        recovery_receipt.get("target_plan_sha256"),
        "recovery receipt target plan hash",
        inputs.get("target_plan_sha256"),
    )
    require_success(recovery_receipt.get("windows"), "recovery receipt Windows state", gate.get("windows"))
    manifest_document = load_json(manifest)
    import recovery

    try:
        recovery.validate_manifest(manifest_document, expected_manifest_sha=sha256_file(manifest))
        recovery.validate_target_plan(load_json(target_plan), manifest_document, sha256_file(manifest))
    except recovery.RecoveryError as error:
        raise GateError("release_recovery_gate_invalid", str(error)) from error
    require_success(recovery_receipt.get("closure"), "recovery receipt closure", manifest_document["closure"])
    validate_windows_result(gate.get("windows"), manifest_document)

    marker = require_file(recovery_run / "recovery-marker.json", "recovery marker")
    if gate.get("marker_sha256") != sha256_file(marker):
        raise GateError(
            "release_recovery_gate_invalid",
            "recovery marker hash differs from the restore gate",
        )
    marker_document = load_json(marker)
    require_success(marker_document.get("release"), "recovery marker release identity", identity)
    require_success(marker_document.get("closure"), "recovery marker closure", manifest_document["closure"])
    return gate_path, gate


def validate_code_rollback(receipt_path: Path, install_id: str, identity: dict) -> dict:
    receipt_path = require_file(receipt_path, "code rollback receipt")
    receipt = load_json(receipt_path)
    require_success(
        receipt.get("schema_version"),
        "code rollback receipt schema",
        "solidworks-to-urdf.promotion/v1",
    )
    require_success(receipt.get("install_id"), "code rollback install_id", install_id)
    plan = receipt.get("plan")
    if not isinstance(plan, list) or not all(isinstance(item, str) for item in plan):
        raise GateError("release_recovery_gate_invalid", "code rollback plan is malformed")
    missing = sorted(set(ROLLBACK_STEPS) - set(plan))
    if missing:
        raise GateError(
            "release_recovery_gate_invalid",
            "code rollback receipt does not plan every required recovery step",
            {"missing": missing},
        )
    steps = receipt.get("steps")
    if not isinstance(steps, list):
        raise GateError("release_recovery_gate_invalid", "code rollback steps are malformed")
    latest = {}
    last_seq = 0
    for item in steps:
        if (
            not isinstance(item, dict)
            or type(item.get("seq")) is not int
            or item["seq"] <= last_seq
            or not isinstance(item.get("verb"), str)
            or item.get("verb") not in {"promote", "rollback"}
            or not isinstance(item.get("step"), str)
            or not item["step"]
            or item["step"] not in plan
            or not isinstance(item.get("status"), str)
            or item.get("status") not in {"completed", "failed", "unknown"}
        ):
            raise GateError("release_recovery_gate_invalid", "code rollback receipt has a malformed step")
        last_seq = item["seq"]
        latest[item["step"]] = (item["verb"], item["status"])
    incomplete = [step for step in ROLLBACK_STEPS if latest.get(step) != ("rollback", "completed")]
    if incomplete:
        raise GateError(
            "release_recovery_gate_invalid",
            "code rollback has not completed every required step",
            {"steps": incomplete},
        )

    source = require_file(Path(str(receipt.get("from", ""))), "code rollback source generation")
    if receipt.get("from_sha256") != sha256_file(source):
        raise GateError(
            "release_recovery_gate_invalid",
            "code rollback source generation hash differs from its receipt",
        )
    require_success(expected_identity(source), "code rollback source release identity", identity)
    return receipt


def _status_identity_matches(value: object, install_id: str, config_digest: str) -> bool:
    source = value.get("source") if isinstance(value, dict) else None
    return (
        isinstance(value, dict)
        and value.get("task_state") == "Running"
        and value.get("install_id") == install_id
        and value.get("config_digest") == config_digest
        and value.get("listener")
        == (source.get("listener") if isinstance(source, dict) else None)
        and isinstance(source, dict)
        and source.get("install_id") == install_id
        and source.get("config_digest") == config_digest
        and windows_identity.sealed_source_listener_matches(source)
    )


def status_identity(stdout: str, install_id: str, config_digest: str) -> dict:
    for line in reversed(stdout.splitlines()):
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if not isinstance(value, dict):
            continue
        if _status_identity_matches(value, install_id, config_digest):
            return value
        break
    raise GateError(
        "release_recovery_identity_unverified",
        "operatorctl status did not prove the active Windows identity",
    )


def fixed_release_paths() -> tuple[Path, Path, Path, Path]:
    release_manifest = require_file(RELEASE_ROOT / "release.json", "release manifest")
    recovery_py = require_file(RELEASE_ROOT / "deploy" / "operator" / "recovery.py", "recovery.py")
    operatorctl = require_file(
        RELEASE_ROOT / "deploy" / "operator" / "operatorctl.sh",
        "operatorctl.sh",
    )
    windows_identity_py = require_file(
        RELEASE_ROOT / "deploy" / "operator" / "windows_identity.py",
        "windows_identity.py",
    )
    return release_manifest, recovery_py, operatorctl, windows_identity_py


def bound_windows_identity_path() -> Path:
    expected = require_file(
        RELEASE_ROOT / "deploy" / "operator" / "windows_identity.py",
        "fixed windows identity validator",
    )
    actual = require_file(
        Path(windows_identity.__file__), "loaded windows identity validator"
    )
    if actual.resolve() != expected.resolve():
        raise GateError(
            "release_recovery_identity_unverified",
            "loaded Windows identity validator differs from the fixed release path",
            {"actual": str(actual), "expected": str(expected)},
        )
    return actual


def canonical_code_rollback_receipt(config: Path, install_id: str) -> Path:
    document = load_json(config)
    storage = document.get("storage")
    state_root = storage.get("state_root") if isinstance(storage, dict) else None
    if not isinstance(state_root, str) or not state_root:
        raise GateError(
            "release_recovery_config_invalid",
            "platform config has no storage state root",
        )
    return require_file(
        Path(state_root) / "promotions" / install_id / "receipt.json",
        "canonical code rollback receipt",
    )


def authority_inputs(
    *,
    config: Path,
    release_manifest: Path,
    recovery_py: Path,
    operatorctl: Path,
    manifest: Path,
    key: Path,
    target_plan: Path,
) -> dict:
    windows_identity_py = bound_windows_identity_path()
    return {
        "config": str(config.resolve()),
        "config_sha256": sha256_file(config),
        "expected_release": str(release_manifest.resolve()),
        "release_manifest_sha256": sha256_file(release_manifest),
        "recovery_py": str(recovery_py.resolve()),
        "recovery_py_sha256": sha256_file(recovery_py),
        "operatorctl": str(operatorctl.resolve()),
        "operatorctl_sha256": sha256_file(operatorctl),
        "windows_identity_py": str(windows_identity_py.resolve()),
        "windows_identity_py_sha256": sha256_file(windows_identity_py),
        "manifest": str(manifest.resolve()),
        "manifest_sha256": sha256_file(manifest),
        "key_sha256": sha256_file(key),
        "target_plan": str(target_plan.resolve()),
        "target_plan_sha256": sha256_file(target_plan),
        "release": expected_identity(release_manifest),
    }


def validate_windows_result(windows: object, manifest_document: dict) -> dict:
    manifest_windows = manifest_document.get("windows")
    if not isinstance(manifest_windows, dict):
        raise GateError(
            "release_recovery_manifest_invalid",
            "recovery manifest has no Windows recovery identity",
        )
    expected_state_sha256 = manifest_windows.get("state_sha256")
    if not isinstance(expected_state_sha256, str) or HEX64.fullmatch(expected_state_sha256) is None:
        raise GateError(
            "release_recovery_manifest_invalid",
            "recovery manifest Windows state hash is malformed",
        )
    if (
        not isinstance(windows, dict)
        or windows.get("task_state") != "Disabled"
        or windows.get("endpoint") != "stopped"
        or windows.get("cad_ownership") != "free"
        or windows.get("state_sha256") != expected_state_sha256
    ):
        raise GateError(
            "release_recovery_windows_unverified",
            "Windows recovery did not prove a disabled, stopped, CAD-free, state-identical endpoint",
        )
    return windows


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
        timeout=getattr(args, "timeout", DEFAULT_TIMEOUT_SECONDS),
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
    config = require_file(Path(args.config), "platform config")
    recovery_py = require_file(Path(args.recovery_py), "recovery.py")
    operatorctl = require_file(Path(args.operatorctl), "operatorctl")
    expected_release = require_file(Path(args.expected_release), "release manifest")
    status_dir = Path(args.status_dir)
    receipt = status_dir / RECEIPT
    action = "recovery-verify-only" if verify_only else "recovery-restore"
    identity = expected_identity(expected_release)
    manifest_document = load_json(manifest)
    if release_from_manifest(manifest) != identity:
        raise GateError(
            "release_recovery_release_identity_mismatch",
            "recovery manifest release identity differs from the expected release",
        )
    inputs = authority_inputs(
        config=config,
        release_manifest=expected_release,
        recovery_py=recovery_py,
        operatorctl=operatorctl,
        manifest=manifest,
        key=key,
        target_plan=plan,
    )
    if receipt.is_file():
        prior = load_json(receipt)
        if (
            prior.get("status") == "succeeded"
            and prior.get("operation") == action
            and prior.get("inputs") == inputs
            and prior.get("windows_verified") is True
            and prior.get("database_restore_executed") == (not verify_only)
            and prior.get("database_recovery_verified") is True
            and prior.get("runtime_restart") is False
            and prior.get("services_stopped") is True
            and prior.get("admission") == "closed"
        ):
            validate_windows_result(prior.get("windows"), manifest_document)
            print(
                json.dumps(
                    {"status": "succeeded", "operation": action, "idempotent": True},
                    sort_keys=True,
                )
            )
            return 0
        if prior.get("status") == "succeeded":
            verify_to_restore = (
                action == "recovery-restore"
                and prior.get("operation") == "recovery-verify-only"
                and prior.get("inputs") == inputs
            )
            if not verify_to_restore:
                raise GateError(
                    "release_recovery_status_mismatch",
                    "a successful recovery gate exists with different inputs",
                )
    if status_dir.is_symlink() or (status_dir.exists() and not status_dir.is_dir()):
        raise GateError("release_recovery_status_invalid", "recovery status path must be a real directory")
    status_dir.mkdir(parents=True, exist_ok=True)
    run_number = 1
    while True:
        run_dir = status_dir / f"{action}-{run_number}"
        if not run_dir.exists() and not run_dir.is_symlink():
            break
        run_number += 1
    timeout = getattr(args, "timeout", DEFAULT_TIMEOUT_SECONDS)
    try:
        result = run(
            [
                sys.executable,
                args.recovery_py,
                "verify-only" if verify_only else "restore",
                "--manifest",
                str(manifest),
                "--key",
                str(key),
                "--target-plan",
                str(plan),
                "--status-dir",
                str(run_dir),
            ],
            timeout=timeout,
            runner=args.runner,
        )
        recovery_receipt = load_json(run_dir / "receipt.json")
        if recovery_receipt.get("status") != "succeeded":
            raise GateError(
                "release_recovery_database_unverified",
                "recovery receipt did not verify the restored database and runtime",
            )
        marker_sha = None
        if not verify_only:
            marker = load_json(run_dir / "recovery-marker.json")
            if marker.get("release") != identity:
                raise GateError(
                    "release_recovery_release_identity_mismatch",
                    "recovery marker release identity differs from the expected release",
                )
            marker_sha = sha256_file(run_dir / "recovery-marker.json")
        windows = validate_windows_result(recovery_receipt.get("windows"), manifest_document)
    except GateError as error:
        failure = {
            "schema": SCHEMA,
            "operation": action,
            "status": "failed",
            "inputs": inputs,
            "error": {"code": error.code, "message": error.message, "detail": error.detail},
            "preserved": True,
        }
        if (run_dir / "receipt.json").is_file():
            failure["recovery_receipt_sha256"] = sha256_file(run_dir / "receipt.json")
        _replace_gate_receipt(receipt, failure)
        raise
    success = {
        "schema": SCHEMA,
        "operation": action,
        "status": "succeeded",
        "inputs": inputs,
        "recovery_run": str(run_dir),
        "recovery_receipt_sha256": sha256_file(run_dir / "receipt.json"),
        "database_restore_executed": not verify_only,
        "database_recovery_verified": True,
        "database_rollback": not verify_only,
        "runtime_restart": False,
        "services_stopped": True,
        "admission": "closed",
        "windows_verified": True,
        "health": "NOT_RUN",
        "operator_status": "NOT_RUN",
        "marker_sha256": marker_sha,
        "windows": windows,
        "stdout": result.stdout.strip().splitlines()[-1:] if result.stdout.strip() else [],
    }
    _replace_gate_receipt(receipt, success)
    print(json.dumps({"status": "succeeded", "operation": action}, sort_keys=True))
    return 0


def finalize(args) -> int:
    status_dir = require_directory(Path(args.restore_status_dir), "restore status directory")
    operatorctl = require_file(Path(args.operatorctl), "operatorctl")
    recovery_py = require_file(Path(args.recovery_py), "recovery.py")
    windows_identity_py = bound_windows_identity_path()
    config = require_file(Path(args.config), "platform config")
    expected_release = require_file(Path(args.expected_release), "release manifest")
    identity = expected_identity(expected_release)
    code_rollback_path = require_file(Path(args.code_rollback_receipt), "code rollback receipt")
    canonical_rollback = canonical_code_rollback_receipt(config, args.install_id)
    if code_rollback_path.resolve() != canonical_rollback.resolve():
        raise GateError(
            "release_recovery_gate_invalid",
            "code rollback receipt is not the canonical receipt for the configured state root",
        )
    restore_gate_path, restore_gate = validate_restore_gate(status_dir, identity)
    restore_inputs = restore_gate["inputs"]
    for key, path in (
        ("config", config),
        ("expected_release", expected_release),
        ("recovery_py", recovery_py),
        ("operatorctl", operatorctl),
        ("windows_identity_py", windows_identity_py),
    ):
        require_success(
            restore_inputs.get(key),
            f"restore {key} authority",
            str(path.resolve()),
        )
        require_success(
            restore_inputs.get(f"{key}_sha256")
            if key != "expected_release"
            else restore_inputs.get("release_manifest_sha256"),
            f"restore {key} authority hash",
            sha256_file(path),
        )
    rollback = validate_code_rollback(code_rollback_path, args.install_id, identity)
    import recovery

    manifest_document = load_json(Path(restore_inputs["manifest"]))
    state_root = Path(next(row["path"] for row in manifest_document["roots"] if row["name"] == "state_root"))
    source_path = require_file(Path(rollback["from"]), "rollback source install record")
    source_record = load_json(source_path)
    if sha256_file(source_path) != rollback["from_sha256"]:
        raise GateError("release_recovery_gate_invalid", "rollback source install record changed")
    source_ends = source_record.get("ends")
    windows_identity = source_ends.get("windows") if isinstance(source_ends, dict) else None
    if (
        not isinstance(windows_identity, dict)
        or not isinstance(windows_identity.get("install_id"), str)
        or re.fullmatch(r"win-[0-9a-f]{32}", windows_identity["install_id"]) is None
        or not isinstance(windows_identity.get("config_digest"), str)
        or not HEX64.fullmatch(windows_identity["config_digest"])
        or windows_identity.get("release") != {**identity, "role": "native"}
    ):
        raise GateError("release_recovery_identity_unverified", "rollback source Windows identity is invalid")
    try:
        recovery.journal_selection(state_root, expected=manifest_document["selection"]["journal_entries"])
        recovery.journal_selection(state_root, additional_receipts=(canonical_rollback,))
    except recovery.RecoveryError as error:
        raise GateError("release_recovery_gate_invalid", str(error)) from error
    receipt = status_dir / FINALIZE_RECEIPT
    inputs = {
        "restore_gate_sha256": sha256_file(restore_gate_path),
        "code_rollback_receipt_sha256": sha256_file(code_rollback_path),
        "config_sha256": sha256_file(config),
        "release_manifest_sha256": sha256_file(expected_release),
        "recovery_py_sha256": sha256_file(recovery_py),
        "operatorctl_sha256": sha256_file(operatorctl),
        "windows_identity_py_sha256": sha256_file(windows_identity_py),
        "release": identity,
        "install_id": args.install_id,
    }
    if receipt.is_file():
        prior = load_json(receipt)
        if (
            prior.get("schema") == SCHEMA
            and prior.get("operation") == "release-finalize"
            and prior.get("status") == "succeeded"
            and prior.get("inputs") == inputs
            and prior.get("database_restore_executed") is True
            and prior.get("database_recovery_verified") is True
            and prior.get("runtime_restart") is True
            and prior.get("services_running") is True
            and prior.get("admission") == "open"
            and prior.get("windows_verified") is True
            and prior.get("health") == "PASS"
            and prior.get("operator_status") == "PASS"
            and _status_identity_matches(
                prior.get("operator_identity"), windows_identity["install_id"], windows_identity["config_digest"]
            )
        ):
            print(
                json.dumps(
                    {"status": "succeeded", "operation": "release-finalize", "idempotent": True},
                    sort_keys=True,
                )
            )
            return 0

    run_number = 1
    while True:
        run_dir = status_dir / f"release-finalize-{run_number}"
        if not run_dir.exists() and not run_dir.is_symlink():
            break
        run_number += 1
    run_dir.mkdir(parents=True, mode=0o700)
    timeout = getattr(args, "timeout", DEFAULT_TIMEOUT_SECONDS)
    start_executed = False
    outputs = {}
    status_value = None
    try:
        start_result = run(
            [str(operatorctl), "start", "--config", str(config)],
            timeout=timeout,
            runner=args.runner,
        )
        start_executed = True
        (run_dir / "finalize-start.out").write_text(start_result.stdout, encoding="utf-8")
        outputs["start"] = sha256_file(run_dir / "finalize-start.out")
        if "admission open" not in start_result.stdout.lower():
            raise GateError(
                "release_recovery_admission_unverified",
                "operatorctl start did not prove that the platform admission opened",
            )
        status_result = run(
            [str(operatorctl), "status", "--config", str(config)],
            timeout=timeout,
            runner=args.runner,
        )
        (run_dir / "finalize-status.out").write_text(status_result.stdout, encoding="utf-8")
        outputs["status"] = sha256_file(run_dir / "finalize-status.out")
        status_value = status_identity(
            status_result.stdout, windows_identity["install_id"], windows_identity["config_digest"]
        )
        health_result = run(
            [str(operatorctl), "health", "--config", str(config)],
            timeout=timeout,
            runner=args.runner,
        )
        (run_dir / "finalize-health.out").write_text(health_result.stdout, encoding="utf-8")
        outputs["health"] = sha256_file(run_dir / "finalize-health.out")
    except GateError as error:
        drain_result = None
        if start_executed:
            try:
                completed = args.runner(
                    [str(operatorctl), "drain", "--config", str(config)],
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=timeout,
                )
                (run_dir / "finalize-failure-drain.out").write_text(
                    completed.stdout,
                    encoding="utf-8",
                )
                drain_result = {
                    "returncode": completed.returncode,
                    "stdout_sha256": sha256_file(run_dir / "finalize-failure-drain.out"),
                    "stderr_tail": "\n".join(completed.stderr.strip().splitlines()[-12:]),
                }
            except (OSError, subprocess.SubprocessError) as drain_error:
                drain_result = {"returncode": None, "error": str(drain_error)}
        failure = {
            "schema": SCHEMA,
            "operation": "release-finalize",
            "status": "failed",
            "inputs": inputs,
            "restore": {
                "operation": restore_gate.get("operation"),
                "status": restore_gate.get("status"),
                "database_restore_executed": restore_gate.get("database_restore_executed"),
                "database_recovery_verified": restore_gate.get("database_recovery_verified"),
                "runtime_restart": restore_gate.get("runtime_restart"),
                "windows_verified": restore_gate.get("windows_verified"),
            },
            "error": {"code": error.code, "message": error.message, "detail": error.detail},
            "outputs": outputs,
            "failure_drain": drain_result,
            "preserved": True,
        }
        _replace_gate_receipt(receipt, failure)
        raise

    success = {
        "schema": SCHEMA,
        "operation": "release-finalize",
        "status": "succeeded",
        "inputs": inputs,
        "finalize_run": str(run_dir),
        "restore": {
            "operation": restore_gate.get("operation"),
            "status": restore_gate.get("status"),
            "database_restore_executed": restore_gate.get("database_restore_executed"),
            "database_recovery_verified": restore_gate.get("database_recovery_verified"),
            "runtime_restart": restore_gate.get("runtime_restart"),
            "windows_verified": restore_gate.get("windows_verified"),
        },
        "database_restore_executed": True,
        "database_recovery_verified": True,
        "database_rollback": True,
        "runtime_restart": True,
        "services_running": True,
        "admission": "open",
        "windows_verified": True,
        "health": "PASS",
        "operator_status": "PASS",
        "operator_identity": status_value,
        "outputs": outputs,
    }
    _replace_gate_receipt(receipt, success)
    print(json.dumps({"status": "succeeded", "operation": "release-finalize"}, sort_keys=True))
    return 0


def _replace_gate_receipt(receipt: Path, document: dict) -> None:
    if receipt.exists():
        prior_sha = sha256_file(receipt)
        archive = receipt.with_name(f"release-recovery-gate.previous-{prior_sha[:16]}.json")
        if archive.exists():
            raise GateError(
                "release_recovery_status_exists",
                "prior recovery evidence has the same digest but a different filename",
                {"archive": str(archive)},
            )
        os.replace(receipt, archive)
    write_once(receipt, document)


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="release_recovery.py")
    root.add_argument("--recovery-py", default=str(Path(__file__).with_name("recovery.py")))
    root.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SECONDS)
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
    finalize_parser = sub.add_parser("release-finalize")
    finalize_parser.add_argument("--plan", required=True)
    finalize_parser.add_argument("--code-rollback-receipt")
    finalize_parser.add_argument("--install-id")
    return root


def restore_from_plan(args):
    plan = load_json(Path(args.plan))
    forbidden = {
        "expected_release",
        "operatorctl",
        "recovery_py",
        "windows_identity_py",
        "release_file",
        "release_root",
    }
    present = sorted(forbidden & set(plan))
    if present:
        raise GateError(
            "release_recovery_plan_invalid",
            "release recovery plan cannot replace fixed release authorities",
            {"forbidden": present},
        )
    release_manifest, recovery_py, operatorctl, _windows_identity_py = fixed_release_paths()
    required = {"config", "manifest", "key_file", "target_plan"}
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
        recovery_py=str(recovery_py),
        manifest=plan["manifest"],
        key=plan["key_file"],
        target_plan=plan["target_plan"],
        status_dir=status_dir,
        config=plan["config"],
        operatorctl=str(operatorctl),
        expected_release=str(release_manifest),
        runner=args.runner,
        timeout=args.timeout,
    )


def finalize_from_plan(args):
    plan = load_json(Path(args.plan))
    forbidden = {
        "expected_release",
        "operatorctl",
        "recovery_py",
        "windows_identity_py",
        "release_file",
        "release_root",
    }
    present = sorted(forbidden & set(plan))
    if present:
        raise GateError(
            "release_recovery_plan_invalid",
            "release finalize plan cannot replace fixed release authorities",
            {"forbidden": present},
        )
    release_manifest, recovery_py, operatorctl, _windows_identity_py = fixed_release_paths()
    required = {"config"}
    if not required.issubset(plan):
        raise GateError(
            "release_recovery_plan_invalid",
            "release recovery plan is missing required finalize fields",
            {"missing": sorted(required - set(plan))},
        )
    status_dir = plan.get("release_restore_status_dir", plan.get("restore_status_dir"))
    if not isinstance(status_dir, str) or not status_dir:
        raise GateError(
            "release_recovery_plan_invalid",
            "release recovery plan has no restore status directory",
        )
    code_rollback_receipt = args.code_rollback_receipt or plan.get("code_rollback_receipt")
    install_id = args.install_id or plan.get("install_id")
    if not isinstance(code_rollback_receipt, str) or not code_rollback_receipt:
        raise GateError(
            "release_recovery_plan_invalid",
            "release finalize has no code rollback receipt",
        )
    if not isinstance(install_id, str) or not install_id:
        raise GateError(
            "release_recovery_plan_invalid",
            "release finalize has no install_id",
        )
    return SimpleNamespace(
        config=plan["config"],
        operatorctl=str(operatorctl),
        recovery_py=str(recovery_py),
        expected_release=str(release_manifest),
        restore_status_dir=status_dir,
        code_rollback_receipt=code_rollback_receipt,
        install_id=install_id,
        runner=args.runner,
        timeout=args.timeout,
    )


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    args.runner = subprocess.run
    if args.timeout <= 0:
        print("release_recovery: timeout must be positive", file=sys.stderr)
        return 2
    try:
        if args.action == "snapshot":
            return snapshot(args)
        if args.action == "release-restore":
            return restore(restore_from_plan(args))
        if args.action == "release-finalize":
            return finalize(finalize_from_plan(args))
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
