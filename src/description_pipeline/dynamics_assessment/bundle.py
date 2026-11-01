"""Bounded archive admission and one shared execution-bundle byte contract."""

from __future__ import annotations

import hashlib
import io
import stat
import tempfile
import zipfile
from pathlib import Path

from ..artifacts.store import MANIFEST_NAME
from ..io import artifact_path_parts, canonical
from .contracts import (
    BUNDLE_MEMBER,
    BUNDLE_SCHEMA,
    AssessmentConflict,
    MemberRef,
    object_record,
    parse_json,
    relative,
    require,
)


def validate_bundle(source, destination: Path) -> tuple[Path, dict]:
    envelope = object_record(
        parse_json((destination / BUNDLE_MEMBER).read_bytes()),
        {"schema", "source_sha256", "plan", "model_derivation"},
        "Execution bundle",
    )
    require(
        envelope["schema"] == BUNDLE_SCHEMA and envelope["source_sha256"] == source.sha256,
        "Execution bundle belongs to another engineering source/certificate closure",
    )
    plan_path = destination / relative(envelope["plan"], "Execution plan path")
    plan = parse_json(plan_path.read_bytes())
    require(
        plan.get("evaluation_mode") == "continuous_dynamics",
        "Optional dynamics assessment requires a continuous_dynamics plan",
    )
    closure = plan.get("input_closure")
    require(isinstance(closure, dict), "Execution plan has no input closure")
    inputs = plan_path.parent / relative(closure.get("root"), "Execution input root")
    urdf = inputs / relative((closure.get("urdf") or {}).get("path"), "Execution URDF")
    mjcf = inputs / relative((closure.get("mjcf") or {}).get("path"), "Execution MJCF")
    original_urdf = MemberRef.from_record(source.record["urdf"])
    require(
        urdf.stat().st_size == original_urdf.bytes
        and hashlib.sha256(urdf.read_bytes()).hexdigest() == original_urdf.sha256,
        "Execution URDF differs from the exact engineering URDF",
    )
    derivation = object_record(
        envelope["model_derivation"],
        {"kind", "source_mjcf_sha256", "execution_mjcf_sha256", "provenance"},
        "Execution model derivation",
    )
    original_mjcf = MemberRef.from_record(source.record["mjcf"])
    execution_sha = hashlib.sha256(mjcf.read_bytes()).hexdigest()
    require(
        derivation["source_mjcf_sha256"] == original_mjcf.sha256
        and derivation["execution_mjcf_sha256"] == execution_sha,
        "Execution MJCF derivation does not bind its original and actual bytes",
    )
    require(
        derivation["kind"] in {"source_exact", "sealed_execution_input"},
        "Execution model derivation kind is unsupported",
    )
    if derivation["kind"] == "source_exact":
        require(execution_sha == original_mjcf.sha256, "Exact execution MJCF differs from its source")
    else:
        require(
            isinstance(derivation["provenance"], dict) and bool(derivation["provenance"]),
            "Derived execution MJCF needs explicit sealed provenance",
        )
    return plan_path, derivation


def seal_upload(service, access, source, archive: bytes, *, should_cancel):
    """Extract regular data files only, then seal against the authorized source."""
    require(
        isinstance(archive, bytes) and 0 < len(archive) <= service.input_limit,
        "Execution archive exceeds its input budget",
    )

    def check_cancel():
        if should_cancel():
            raise AssessmentConflict("Execution input upload was cancelled")

    check_cancel()
    with tempfile.TemporaryDirectory(prefix="assessment-upload-", dir=service.work_root) as temporary:
        root = Path(temporary)
        total = 0
        names = {}
        directories = {}
        with zipfile.ZipFile(io.BytesIO(archive)) as zipped:
            members = zipped.infolist()
            require(0 < len(members) <= 4096, "Execution archive member count is invalid")
            for item in members:
                check_cancel()
                name = item.filename.rstrip("/") if item.is_dir() else item.filename
                parts = artifact_path_parts(name)
                require(tuple(name.split("/")) == parts, "Execution archive path is not canonical")
                require(len(name) <= 1024 and all(len(p) <= 255 for p in parts), "Execution archive path is too long")
                require(name not in {MANIFEST_NAME, "admission.json"}, "Execution archive carries reserved metadata")
                mode = item.external_attr >> 16
                require(
                    stat.S_IFMT(mode) in {0, stat.S_IFDIR if item.is_dir() else stat.S_IFREG},
                    "Execution archive contains a link or special file",
                )
                require(
                    not item.flag_bits & 1 and item.compress_type in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED},
                    "Execution archive compression is unsupported",
                )
                key = name.casefold()
                require(
                    key not in names and (key not in directories or item.is_dir()),
                    "Execution archive has a path collision",
                )
                if key in directories:
                    require(directories[key] == name, "Execution archive directory case differs")
                for index in range(1, len(parts)):
                    prefix = "/".join(parts[:index])
                    folded = prefix.casefold()
                    require(
                        folded not in names and directories.get(folded, prefix) == prefix,
                        "Execution archive has a path collision",
                    )
                    directories[folded] = prefix
                target = root.joinpath(*parts)
                if item.is_dir():
                    require(item.file_size == 0, "Execution archive directory contains data")
                    directories[key] = name
                    target.mkdir(mode=0o700, parents=True, exist_ok=True)
                    continue
                names[key] = name
                require(
                    0 <= item.file_size <= service.input_limit - total,
                    "Expanded execution archive exceeds its input budget",
                )
                target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                count = 0
                with zipped.open(item) as incoming, target.open("xb") as outgoing:
                    while chunk := incoming.read(1024 * 1024):
                        check_cancel()
                        total += len(chunk)
                        count += len(chunk)
                        require(
                            total <= service.input_limit and count <= item.file_size,
                            "Expanded execution archive exceeds its declared budget",
                        )
                        outgoing.write(chunk)
                require(count == item.file_size, "Execution archive member is incomplete")
        check_cancel()
        validate_bundle(source, root)
        current = service._source(access, source.record["dag_run_id"], control=True)
        require(current.sha256 == source.sha256, "Execution input source changed during admission")
        source.verify(service.store.artifacts)
        (root / "admission.json").write_bytes(
            canonical(
                {
                    "schema": "description.dynamics-bundle-admission/v1",
                    "principal": access.principal,
                    "source_sha256": source.sha256,
                    "archive_sha256": hashlib.sha256(archive).hexdigest(),
                    "bytes": total,
                    "files": len(names),
                    "scope": "Byte binding only; Teleop retains input and physics qualification.",
                }
            )
            + b"\n"
        )
        check_cancel()
        artifact = service.store.artifacts.seal_tree(
            root,
            kind="dynamics_execution_bundle",
            producer_run_id=source.record["run_id"],
            producer_stage=None,
            producer_tool_sha256=service.executor_sha256,
        )
        check_cancel()
        service.store.platform.register_artifact(artifact)
        return {"bundle": artifact.to_record(), "source_sha256": source.sha256, "bytes": total, "files": len(names)}
