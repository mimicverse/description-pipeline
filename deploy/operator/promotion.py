#!/usr/bin/env python3
"""Promotion receipts for the unified stage/promote/rollback protocol.

The install record is written once, at the pointer switch, and its journal references this
receipt.  The receipt is the single step authority: every step of one switch attempt (stage
included) is registered here as it happens - the plan, the actual status and the evidence -
and a failed attempt leaves it as the failure record rollback reads.  The bound generations
are immutable files with their real digests, never the mutable active pointer.  Everything
lives under ``<state root>/promotions/<install_id>/``; nothing here touches the active
pointer or the journal of an existing record.

Exit codes: 0 success; 1 error (message on stderr); 2 usage error.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import shutil
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from platform_config import valid_install_id

RECEIPT_SCHEMA = "solidworks-to-urdf.promotion/v1"
VERBS = ("stage", "reconcile", "promote", "rollback")
STATUSES = ("completed", "failed", "unknown")
PROMOTE_STEPS = (
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
)
ROLLBACK_STEPS = (
    "rollback-close",
    "rollback-drain",
    "rollback-stop",
    "rollback-windows",
    "rollback-linux",
    "rollback-config-reference",
    "rollback-verify",
)
STAGE_OWNED_STEPS = frozenset({"linux-generation", "windows-generation"})


def die(message: str) -> None:
    raise SystemExit(f"promotion: {message}")


def require(condition: bool, message: str) -> None:
    if not condition:
        die(message)


def utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def receipt_path(state_root: Path, install_id: str) -> Path:
    require(valid_install_id(install_id), "install_id is malformed")
    return Path(state_root) / "promotions" / install_id / "receipt.json"


def lexical_path(path: Path) -> Path:
    return Path(os.path.normpath(os.path.abspath(path)))


def no_symlink_ancestors(path: Path) -> bool:
    candidate = lexical_path(path)
    current = Path(candidate.anchor)
    for part in candidate.parts[1:]:
        current = current / part
        if current.is_symlink():
            return False
    return True


def prepare_receipt_parent(path: Path) -> None:
    parent = lexical_path(path.parent)
    require(no_symlink_ancestors(parent), "the promotion receipt path has a symlink ancestor")
    current = Path(parent.anchor)
    for part in parent.parts[1:]:
        current = current / part
        if current.is_symlink():
            die("the promotion receipt path has a symlink ancestor")
        if not current.exists():
            with contextlib.suppress(FileExistsError):
                current.mkdir(mode=0o700)
        require(
            current.is_dir() and not current.is_symlink(),
            f"the promotion receipt parent is not a real directory: {current}",
        )
    require(no_symlink_ancestors(parent), "the promotion receipt path has a symlink ancestor")


def canonical_evidence_root(receipt_file: Path) -> Path:
    return lexical_path(receipt_file.parent / "evidence-immutable")


def load_receipt(path: Path) -> dict:
    require(path.is_file(), f"promotion receipt not found: {path}")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        die(f"cannot parse promotion receipt {path}: {error}")
    require(document.get("schema_version") == RECEIPT_SCHEMA, f"unexpected receipt schema in {path}")
    require(isinstance(document.get("steps"), list), f"receipt steps are malformed in {path}")
    return document


def frozen_step_valid(row: dict, evidence_root: Path) -> bool:
    verb = row.get("verb")
    step = row.get("step")
    evidence = row.get("evidence")
    digests = row.get("evidence_digests")
    if (
        not isinstance(evidence, list)
        or not evidence
        or not isinstance(digests, list)
        or len(digests) != len(evidence)
    ):
        return False

    paths = []
    digest_paths = []
    for value, entry in zip(evidence, digests, strict=True):
        if not isinstance(value, str) or not value or not isinstance(entry, dict):
            return False
        if set(entry) != {"path", "sha256"} or entry["path"] != value:
            return False
        if not isinstance(entry["sha256"], str):
            return False
        path = Path(value)
        if not path.is_absolute() or path.is_symlink() or not path.is_file():
            return False
        real_path = Path(os.path.realpath(path))
        if Path(os.path.abspath(path)) != real_path:
            return False
        if real_path in paths:
            return False
        if hashlib.sha256(real_path.read_bytes()).hexdigest() != entry["sha256"]:
            return False
        paths.append(real_path)
        digest_paths.append(value)
    if digest_paths != evidence:
        return False

    maps = [path for path in paths if path.name == "evidence-map.json"]
    if len(maps) != 1 or paths[-1] != maps[0]:
        return False
    map_path = maps[0]
    freeze_dir = map_path.parent
    if (
        freeze_dir.is_symlink()
        or freeze_dir.parent != evidence_root
        or not freeze_dir.name.startswith(f"{verb}-{step}-")
    ):
        return False
    if any(path.parent != freeze_dir for path in paths):
        return False

    try:
        manifest = json.loads(map_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(manifest, list) or not manifest:
        return False
    sources = set()
    snapshots = set()
    for group in manifest:
        if not isinstance(group, list) or not group:
            return False
        for entry in group:
            if not isinstance(entry, dict) or set(entry) != {"source", "snapshot"}:
                return False
            source = entry["source"]
            snapshot = entry["snapshot"]
            if (
                not isinstance(source, str)
                or not source
                or source in sources
                or not isinstance(snapshot, str)
                or not snapshot
                or Path(snapshot).name != snapshot
                or snapshot in {".", "..", "evidence-map.json"}
                or any(character in snapshot for character in "\x00\r\n")
                or snapshot in snapshots
            ):
                return False
            sources.add(source)
            snapshots.add(snapshot)

    evidence_paths = [path for path in paths if path != map_path]
    if {path.name for path in evidence_paths} != snapshots:
        return False
    children = list(freeze_dir.iterdir())
    if any(child.is_symlink() or not child.is_file() for child in children):
        return False
    return {child.name for child in children} == {path.name for path in paths}


def recorded_steps_valid(
    document: dict,
    receipt_file: Path,
    expected_install_id: str | None = None,
) -> bool:
    try:
        install_id = document.get("install_id")
        plan = document.get("plan")
        if (
            not isinstance(install_id, str)
            or not valid_install_id(install_id)
            or (
                expected_install_id is not None
                and install_id != expected_install_id
            )
            or not isinstance(plan, list)
            or not plan
            or any(not isinstance(name, str) or not name for name in plan)
            or len(plan) != len(set(plan))
        ):
            return False
        steps = document.get("steps")
        if not isinstance(steps, list):
            return False
        canonical_receipt = lexical_path(receipt_file)
        evidence_root = canonical_evidence_root(canonical_receipt)
        if not no_symlink_ancestors(canonical_receipt):
            return False
        if steps and not no_symlink_ancestors(evidence_root):
            return False
        keys = set()
        evidence_paths = set()
        for index, row in enumerate(steps, 1):
            if not isinstance(row, dict):
                return False
            key = (row.get("verb"), row.get("step"))
            if (
                key in keys
                or row.get("verb") not in VERBS
                or not isinstance(key[1], str)
                or not key[1]
                or row.get("status") not in STATUSES
                or row.get("seq") != index
                or not isinstance(row.get("at"), str)
                or not row["at"]
            ):
                return False
            row_evidence = row.get("evidence")
            if not isinstance(row_evidence, list):
                return False
            for path in row_evidence:
                if not isinstance(path, str) or not path or path in evidence_paths:
                    return False
                evidence_paths.add(path)
            keys.add(key)
            if not frozen_step_valid(row, evidence_root):
                return False
        return True
    except (OSError, TypeError, ValueError):
        return False


def save_receipt(path: Path, document: dict) -> None:
    prepare_receipt_parent(path)
    require(not path.is_symlink(), "the promotion receipt must not be a symlink")
    payload = json.dumps(document, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False, encoding="utf-8") as handle:
        handle.write(payload)
        os.fchmod(handle.fileno(), 0o600)
        handle.flush()
        os.fsync(handle.fileno())
        temporary = Path(handle.name)
    os.replace(temporary, path)


def save_receipt_once(path: Path, document: dict) -> None:
    prepare_receipt_parent(path)
    payload = json.dumps(document, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False, encoding="utf-8") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
        temporary = Path(handle.name)
    try:
        os.link(temporary, path)
    except FileExistsError:
        die(f"preserve the existing promotion receipt: {path}")
    finally:
        temporary.unlink(missing_ok=True)


def freeze_evidence(evidence: list[Path], root: Path, verb: str, step: str) -> list[Path]:
    root = lexical_path(root)
    require(no_symlink_ancestors(root), "the evidence freeze path has a symlink ancestor")
    try:
        root.mkdir(mode=0o700)
    except FileExistsError:
        require(
            root.is_dir() and not root.is_symlink(),
            "the evidence freeze root is not a real directory",
        )
    require(no_symlink_ancestors(root), "the evidence freeze path has a symlink ancestor")
    destination = Path(tempfile.mkdtemp(prefix=f"{verb}-{step}-", dir=root))
    require(
        destination.parent == root and no_symlink_ancestors(destination),
        "the evidence freeze directory escaped its canonical root",
    )
    manifest = []
    frozen = []
    for index, source in enumerate(evidence):
        require(
            source.is_file() and not source.is_symlink(),
            f"evidence must be a regular non-link file: {source}",
        )
        snapshot = destination / f"{index:03d}-{source.name}"
        shutil.copy2(source, snapshot)
        frozen.append(snapshot)
        manifest.append(
            [{"source": str(Path(os.path.realpath(source))), "snapshot": snapshot.name}]
        )
    manifest_path = destination / "evidence-map.json"
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    frozen.append(manifest_path)
    return frozen


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="promotion.py", description=__doc__.splitlines()[0])
    subparsers = parser.add_subparsers(dest="action", required=True)
    init = subparsers.add_parser("init")
    init.add_argument("--state-root", type=Path, required=True)
    init.add_argument("--install-id", required=True)
    init.add_argument("--from", dest="source", type=Path, required=True)
    init.add_argument("--to", dest="target", type=Path, required=True)
    init.add_argument("--release-file", type=Path, required=True)
    init.add_argument("--plan", required=True, help="comma-separated planned step names")
    step = subparsers.add_parser("step")
    step.add_argument("--state-root", type=Path, required=True)
    step.add_argument("--install-id", required=True)
    step.add_argument("--verb", choices=sorted(VERBS), required=True)
    step.add_argument("--step", required=True)
    step.add_argument("--status", choices=sorted(STATUSES), required=True)
    step.add_argument("--at", default=None)
    step.add_argument("--evidence", action="append", required=True, help="repeatable evidence reference")
    step.add_argument("--freeze-root", type=Path, required=True)
    step_state = subparsers.add_parser("step-state")
    step_state.add_argument("--state-root", type=Path, required=True)
    step_state.add_argument("--install-id", required=True)
    step_state.add_argument("--verb", choices=sorted(VERBS), required=True)
    step_state.add_argument("--step", required=True)
    show = subparsers.add_parser("show")
    show.add_argument("--state-root", type=Path, required=True)
    show.add_argument("--install-id", required=True)
    replay = subparsers.add_parser("replay")
    replay.add_argument("--state-root", type=Path, required=True)
    replay.add_argument("--install-id", required=True)
    replay.add_argument("--verb", choices=("promote", "rollback"), required=True)
    replay.add_argument("--active-record", type=Path, required=True)
    verify_release = subparsers.add_parser("verify-release")
    verify_release.add_argument("--state-root", type=Path, required=True)
    verify_release.add_argument("--install-id", required=True)
    verify_release.add_argument("--release-file", type=Path, required=True)
    args = parser.parse_args(argv)

    path = receipt_path(args.state_root, args.install_id)
    if args.action == "step-state":
        document = load_receipt(path)
        if not recorded_steps_valid(document, path, args.install_id):
            print("step=partial")
            return 11
        rows = [
            row
            for row in document["steps"]
            if isinstance(row, dict) and row.get("verb") == args.verb and row.get("step") == args.step
        ]
        if not rows:
            print("step=absent")
            return 10
        require(len(rows) == 1, f"promotion step is duplicated: {args.verb}/{args.step}")
        print(f"step={rows[0].get('status')}")
        return 0
    if args.action == "init":
        plan = [name for name in args.plan.split(",") if name]
        require(plan, "the promotion plan is empty")
        require(len(plan) == len(set(plan)), "the promotion plan repeats a step")
        require(
            no_symlink_ancestors(path.parent),
            "the promotion receipt path has a symlink ancestor",
        )
        require(args.source.is_file(), f"the bound source generation is missing: {args.source}")
        require(args.target.is_file(), f"the bound target generation is missing: {args.target}")
        require(args.release_file.is_file(), f"the bound release manifest is missing: {args.release_file}")
        release_path = Path(os.path.realpath(args.release_file))
        release_sha256 = hashlib.sha256(release_path.read_bytes()).hexdigest()
        save_receipt_once(
            path,
            {
                "schema_version": RECEIPT_SCHEMA,
                "install_id": args.install_id,
                "from": str(args.source),
                "from_sha256": hashlib.sha256(args.source.read_bytes()).hexdigest(),
                "to": str(args.target),
                "to_sha256": hashlib.sha256(args.target.read_bytes()).hexdigest(),
                "release_manifest": {"path": str(release_path), "sha256": release_sha256},
                "plan": plan,
                "created_at": utc_now(),
                "steps": [],
            },
        )
        print(f"receipt={path}")
        return 0
    if args.action == "step":
        document = load_receipt(path)
        require(
            recorded_steps_valid(document, path, args.install_id),
            "the promotion receipt has invalid evidence provenance",
        )
        require(
            args.step in document["plan"],
            f"step {args.step!r} is not part of the recorded plan {document['plan']}",
        )
        evidence = [str(item) for item in args.evidence]
        require(all(item for item in evidence), "every journal step needs non-empty evidence")
        require(
            not any(
                row.get("verb") == args.verb and row.get("step") == args.step
                for row in document["steps"]
                if isinstance(row, dict)
            ),
            f"promotion step is already recorded: {args.verb}/{args.step}",
        )
        evidence_paths = [Path(item) for item in evidence]
        require(
            all(item.is_file() for item in evidence_paths),
            "every journal step evidence must reference an existing file",
        )
        expected_freeze_root = canonical_evidence_root(path)
        requested_freeze_root = lexical_path(args.freeze_root)
        require(
            requested_freeze_root == expected_freeze_root,
            "the promotion evidence freeze root is not canonical",
        )
        require(
            no_symlink_ancestors(path)
            and no_symlink_ancestors(expected_freeze_root),
            "the promotion evidence freeze path has a symlink ancestor",
        )
        evidence_paths = freeze_evidence(
            evidence_paths,
            requested_freeze_root,
            args.verb,
            args.step,
        )
        evidence_digests = [
            {
                "path": str(item),
                "sha256": hashlib.sha256(item.read_bytes()).hexdigest(),
            }
            for item in evidence_paths
        ]
        document["steps"].append(
            {
                "seq": len(document["steps"]) + 1,
                "verb": args.verb,
                "step": args.step,
                "status": args.status,
                "at": args.at or utc_now(),
                "evidence": [str(item) for item in evidence_paths],
                "evidence_digests": evidence_digests,
            }
        )
        require(
            recorded_steps_valid(document, path, args.install_id),
            "the appended promotion evidence provenance is invalid",
        )
        save_receipt(path, document)
        print(f"receipt={path}")
        return 0
    if args.action == "show":
        document = load_receipt(path)
        print(f"receipt={path}")
        print(f"steps={len(document['steps'])} plan={','.join(document['plan'])}")
        return 0
    if args.action == "replay":
        document = load_receipt(path)
        if not recorded_steps_valid(document, path, args.install_id):
            print("replay=partial")
            return 11
        if document.get("install_id") != args.install_id:
            print("replay=drift")
            return 11
        required = PROMOTE_STEPS if args.verb == "promote" else ROLLBACK_STEPS
        verb_rows = [
            row
            for row in document["steps"]
            if isinstance(row, dict) and row.get("verb") == args.verb
        ]
        names = [row.get("step") for row in verb_rows]
        if len(names) != len(set(names)):
            print("replay=partial")
            return 11
        by_name = {row["step"]: row for row in verb_rows}

        completed = all(
            by_name.get(name, {}).get("status") == "completed" for name in required
        ) and set(names) == set(required)
        fresh = (
            args.verb == "rollback" and not verb_rows
        ) or (
            args.verb == "promote"
            and {row.get("step") for row in verb_rows} == STAGE_OWNED_STEPS
            and all(row.get("status") == "completed" for row in verb_rows)
        )
        if not completed and not fresh:
            print("replay=partial")
            return 11
        if not args.active_record.is_file():
            print("replay=drift")
            return 11
        try:
            active = json.loads(args.active_record.read_text(encoding="utf-8"))
            source_path = Path(document["from"])
            target_path = Path(document["to"])
            source = json.loads(source_path.read_text(encoding="utf-8"))
            target = json.loads(target_path.read_text(encoding="utf-8"))
        except (KeyError, OSError, TypeError, ValueError):
            print("replay=drift")
            return 11
        if (
            hashlib.sha256(source_path.read_bytes()).hexdigest()
            != document.get("from_sha256")
            or hashlib.sha256(target_path.read_bytes()).hexdigest()
            != document.get("to_sha256")
            or target.get("install_id") != document["install_id"]
        ):
            print("replay=drift")
            return 11
        source_id = source.get("install_id")
        target_id = target.get("install_id")
        if args.verb == "promote":
            expected_install_id = target_id if completed else source_id
        else:
            expected_install_id = source_id if completed else target_id
        active_install_id = active.get("install_id")
        fresh_rollback_pointer = (
            args.verb == "rollback"
            and not completed
            and active_install_id in {source_id, target_id}
        )
        if not fresh_rollback_pointer and active_install_id != expected_install_id:
            print("replay=drift")
            return 11
        print("replay=completed" if completed else "replay=fresh")
        return 0 if completed else 10
    if args.action == "verify-release":
        document = load_receipt(path)
        binding = document.get("release_manifest")
        require(
            isinstance(binding, dict) and set(binding) == {"path", "sha256"},
            f"promotion receipt {path} carries no release manifest binding",
        )
        release_path = Path(os.path.realpath(args.release_file))
        require(release_path.is_file(), f"the release manifest is missing: {release_path}")
        require(binding["path"] == str(release_path), "the promotion receipt binds another release manifest")
        require(
            binding["sha256"] == hashlib.sha256(release_path.read_bytes()).hexdigest(),
            "the release manifest differs from the promotion receipt",
        )
        print(f"release_manifest={release_path}")
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
