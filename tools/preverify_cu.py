#!/usr/bin/env python3
"""CU pre-submission pre-verification (W6 parity with the W5 probe-v4 check set).

Two phases, both read-only:

* Linux byte checks over the frozen package folder: exact inventory (count and
  bytes), ``~$`` lock files, zero-byte files and the main-assembly rule, plus an
  optional receipt comparison (``sha256  path`` lines);
* the native side (``--sw``, run on the endpoint host): the same read-only
  closure walk the pipeline performs at staging
  (``native_selection.NativeSelectionReader``), whose ``preverify`` block is the
  authoritative verdict: SolidWorks open errors, reference closure, and the
  declared delivery identity (``dp.*``).

The pipeline gate itself lives in ``native_selection`` (a missing required
declaration refuses with ``selection_declarations_missing`` before any native
capture).  This tool is the manual fallback for window submissions, and it also
fixes the W5 args-file naming incident by construction: the canonical args file
is ``probe_v4_args.json`` and is only ever read through an explicit path
(``--args``); when it is missing the error names the candidate files it found
instead of guessing a suffixed copy.

Exit codes: 0 = verdict ``pass``; 2 = verdict ``attention`` (do not submit);
3 = usage or read error.  The machine-readable record is written to ``--out``
(default ``preverify.json`` in the current directory).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path

DEFAULT_ARGS_NAME = "probe_v4_args.json"
SCHEMA = "m303-probe-v4/preverify"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _stem(name: str) -> str:
    return re.sub(r"(?i)\.(sldasm|sldprt|step|stp)$", "", name).lower()


def linux_checks(folder: Path, *, receipt: Path | None = None, main: str | None = None) -> dict:
    """The W5 probe-v4 Linux checks, verbatim semantics (read-only)."""

    folder = Path(folder)
    files = sorted(name for name in os.listdir(folder) if (folder / name).is_file())
    inventory = {
        name: {"bytes": (folder / name).stat().st_size, "sha256": sha256_file(folder / name)} for name in files
    }
    check: dict = {
        "count": len(files),
        "bytes": sum(item["bytes"] for item in inventory.values()),
        "lock_files": [name for name in files if name.startswith("~$")],
        "zero_byte": [name for name in files if inventory[name]["bytes"] == 0],
        "assemblies": sorted(name for name in files if name.lower().endswith(".sldasm")),
    }
    if main:
        wanted = _stem(main)
        check["main"] = {
            "requested": main,
            "exact_name_present": main in files,
            "matches": [name for name in files if _stem(name) == wanted],
        }
        check["main_ok"] = bool(check["main"]["exact_name_present"])
    else:
        check["main"] = {"requested": None, "unique_assembly": len(check["assemblies"]) == 1}
        check["main_ok"] = bool(check["main"]["unique_assembly"])
    if receipt is not None:
        expected: dict[str, str] = {}
        for line in Path(receipt).read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if not line:
                continue
            digest, _, path = line.partition("  ")
            expected[os.path.basename(path.strip().lstrip("./"))] = digest.strip().lower()
        missing = sorted(name for name in expected if name not in inventory)
        mismatched = sorted(
            name
            for name, digest in expected.items()
            if name in inventory and inventory[name]["sha256"] != digest
        )
        check["receipt"] = {
            "file": str(receipt),
            "entries": len(expected),
            "missing": missing,
            "mismatched": mismatched,
        }
        check["receipt_ok"] = not missing and not mismatched
    else:
        check["receipt"] = None
        check["receipt_ok"] = None
    verdict = (
        check["count"] > 0
        and not check["lock_files"]
        and not check["zero_byte"]
        and check["main_ok"]
        and check["receipt_ok"] in (None, True)
    )
    check["verdict"] = "pass" if verdict else "attention"
    check["inventory"] = inventory
    return check


def load_args(path: Path, *, folder: str | None, main: str | None, target: str | None) -> dict:
    """Explicit args-file loading; a missing canonical file names its candidates."""

    path = Path(path)
    if not path.is_file():
        folder_candidates = sorted(p.name for p in path.parent.glob("probe_v4_args*.json"))
        raise FileNotFoundError(
            f"args file is missing: {path} (expected the canonical name {DEFAULT_ARGS_NAME!r}; "
            f"pass it explicitly with --args; candidates found: {folder_candidates})"
        )
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"args file is not an object: {path}")
    return {
        "folder": folder or payload.get("folder"),
        "main_assembly": main or payload.get("main_assembly"),
        "expected_hardware_id": target or payload.get("expected_hardware_id"),
        "tag": payload.get("tag"),
        "args_file": str(path),
    }


def sw_checks(folder: Path, main: str, *, index: dict[str, str] | None = None, reader=None) -> dict:
    """The read-only native phase; inject ``reader`` in tests."""

    if reader is None:
        from description_pipeline.sources.solidworks.native_selection import NativeSelectionReader

        reader = NativeSelectionReader()
    from description_pipeline.sources.solidworks.selection import SelectionError

    files = index
    if files is None:
        files = {
            name: sha256_file(Path(folder) / name)
            for name in sorted(os.listdir(folder))
            if (Path(folder) / name).is_file()
        }
    try:
        payload = reader.observe(Path(folder), main, files)
    except SelectionError as error:
        return {
            "verdict": "attention",
            "code": error.code,
            "message": str(error),
            "problems": error.detail.get("problems") if isinstance(error.detail, dict) else None,
        }
    return {"verdict": "pass", **dict(payload.get("preverify") or {})}


def build_report(folder: Path, main: str | None, *, receipt: Path | None = None, sw: bool = False, reader=None) -> dict:
    report: dict = {"schema": SCHEMA, "folder": str(folder), "main_assembly": main}
    report["linux"] = linux_checks(folder, receipt=receipt, main=main)
    if sw:
        if not main:
            raise ValueError("the native phase needs the main assembly name (--main or the args file)")
        report["sw"] = sw_checks(folder, main, reader=reader)
    verdicts = [report["linux"]["verdict"]] + ([report["sw"]["verdict"]] if "sw" in report else [])
    report["verdict"] = "pass" if all(item == "pass" for item in verdicts) else "attention"
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--folder")
    parser.add_argument("--main")
    parser.add_argument("--target")
    parser.add_argument("--receipt", type=Path)
    parser.add_argument("--args", type=Path, help=f"args file (canonical name {DEFAULT_ARGS_NAME})")
    parser.add_argument("--sw", action="store_true", help="also run the read-only native phase (endpoint host)")
    parser.add_argument("--out", type=Path, default=Path("preverify.json"))
    args = parser.parse_args(argv)

    folder, main, target = args.folder, args.main, args.target
    if args.args is not None:
        try:
            loaded = load_args(args.args, folder=folder, main=main, target=target)
        except (FileNotFoundError, ValueError) as error:
            print(f"preverify: {error}", file=sys.stderr)
            return 3
        folder, main, target = loaded["folder"], loaded["main_assembly"], loaded["expected_hardware_id"]
    elif folder is None:
        canonical = Path.cwd() / DEFAULT_ARGS_NAME
        if canonical.is_file():
            loaded = load_args(canonical, folder=None, main=None, target=None)
            folder, main, target = loaded["folder"], loaded["main_assembly"], loaded["expected_hardware_id"]
        else:
            parser.error(f"provide --folder or place {DEFAULT_ARGS_NAME} in the working directory")
    if not folder:
        print("preverify: args file carries no folder", file=sys.stderr)
        return 3
    folder_path = Path(folder)
    if not folder_path.is_dir():
        print(f"preverify: folder is not readable: {folder_path}", file=sys.stderr)
        return 3
    try:
        report = build_report(folder_path, main, receipt=args.receipt, sw=args.sw)
    except ValueError as error:
        print(f"preverify: {error}", file=sys.stderr)
        return 3
    report["expected_hardware_id"] = target
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(json.dumps({"verdict": report["verdict"], "out": str(args.out), "linux": report["linux"]["verdict"]}))
    return 0 if report["verdict"] == "pass" else 2


if __name__ == "__main__":
    raise SystemExit(main())
