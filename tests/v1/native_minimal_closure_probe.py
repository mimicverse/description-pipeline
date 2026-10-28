"""Run the real native Capture route with only stdlib and packaging available."""

from __future__ import annotations

import argparse
import importlib
import importlib.abc
import importlib.machinery
import json
import shutil
import sys
import traceback
from pathlib import Path
from unittest import mock


DENIED_ROOTS = frozenset({"jsonschema", "mujoco", "numpy", "yaml"})
RUN_ID = "20261010-0000-4000-8000-000000000001"


def _native_tool() -> dict:
    from packaging.utils import canonicalize_name

    from description_pipeline import __version__
    from description_pipeline.delivery import PIPELINE_ID
    from description_pipeline.runtime import RUNTIME_VERSIONS, required_packages
    from description_pipeline.schemas import TOOL_SCHEMA

    return {
        "schema_version": TOOL_SCHEMA,
        "pipeline_id": PIPELINE_ID,
        "version": __version__,
        "source_sha256": "f" * 64,
        "release": None,
        "runtime": {
            "role": "native",
            "system": "Windows",
            "python": "3.12.10",
            "machine": "AMD64",
            "packages": {
                canonicalize_name(name): RUNTIME_VERSIONS[name]
                for name in required_packages("native")
            },
        },
    }


class PortableDependencyGuard(importlib.abc.MetaPathFinder):
    """Keep native verification from borrowing a portable runtime."""

    def __init__(self) -> None:
        self.attempts: list[str] = []

    def find_spec(self, fullname, path=None, target=None):  # noqa: ANN001
        root_name = fullname.partition(".")[0]
        if root_name in DENIED_ROOTS:
            self.attempts.append(fullname)
            raise ModuleNotFoundError(f"portable dependency is unavailable on the native runtime: {fullname}")
        return None


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True)
    parser.add_argument("--site", required=True)
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--receipt", required=True)
    return parser.parse_args()


def _install_minimal_paths(source: Path, site: Path) -> list[str]:
    stdlib = [
        entry
        for entry in sys.path
        if entry and "site-packages" not in entry.casefold() and "dist-packages" not in entry.casefold()
    ]
    sys.path[:] = [str(source / "src"), str(source), str(site), *stdlib]
    return list(sys.path)


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _fixture_backend(assembly: Path, parts: list[Path], masses: tuple[dict, dict]):
    from tests.sources import support

    class PreparedBackend(support.FixtureCadBackend):
        def source_scopes(self, components):
            prepared_root = Path(self._prepared_capture_root)
            package_root = Path(self.document).resolve().parents[1]
            scopes = {}
            for component in components:
                entry = next(item for item in self.components if str(item["name"]) == str(component))
                original = Path(entry["path"]).resolve()
                relative = original.relative_to(package_root)
                copied = prepared_root / relative
                checksum = support.file_digest(copied)
                scopes[str(component)] = support.fixture_source_scope(
                    str(component),
                    source_document_path=str(copied),
                    source_copy_path=f"source/{relative.as_posix()}",
                    source_document_sha256=checksum,
                    referenced_configuration=str(entry.get("configuration") or self.configurations[0]),
                )
            return scopes

        def collect_prepared_scene(self, *args, **kwargs):  # noqa: ANN002, ANN003
            raw = super().collect_prepared_scene(*args, **kwargs)
            prepared_root = Path(self._prepared_capture_root).resolve()
            package_root = Path(self.document).resolve().parents[1]
            prepared_paths = {
                str(entry["name"]): str(prepared_root / Path(entry["path"]).resolve().relative_to(package_root))
                for entry in self.components
            }
            for key in ("occurrences", "components"):
                for row in raw["scene_raw"][key]:
                    row["path"] = prepared_paths[row["name"]]
            return raw

    return PreparedBackend(
        assembly,
        [
            {
                "name": "base-1",
                "path": str(parts[0]),
                "transform": support.placement(),
                "mass": masses[0],
            },
            {
                "name": "arm-1",
                "path": str(parts[1]),
                "transform": support.placement((0.0, 0.0, 0.2)),
                "mass": masses[1],
            },
        ],
        dependencies=parts,
    )


def _build_prepared_package(
    workspace: Path,
) -> tuple[Path, object, dict[str, str], str, str, dict]:
    import hashlib

    from description_pipeline.io import digest, write_json
    from description_pipeline.schemas import NATIVE_DISCOVERY_SCHEMA
    from tests.sources import support
    from description_pipeline.schemas import CAPTURE_EVIDENCE_REQUESTS
    from tests.sources.selection_support import sealed_selection

    package = workspace / "prepared"
    cad_root = package / "cad"
    assembly = support.make_cad_tree(cad_root)
    parts = [cad_root / "base.SLDPRT", cad_root / "arm.SLDPRT"]
    base_mass = support.mass_payload(1.0, (0.01, 0.02, 0.0))
    base_mass["reference"]["configuration"] = "Default"
    arm_mass = support.mass_payload(0.5, (0.0, 0.0, 0.05))
    arm_mass["reference"]["configuration"] = "Default"
    backend = _fixture_backend(assembly, parts, (base_mass, arm_mass))
    backend.evidence_class = "cad"

    main_assembly = "cad/robot.SLDASM"
    native_files = {f"cad/{path.name}": hashlib.sha256(path.read_bytes()).hexdigest() for path in (assembly, *parts)}
    handoff_sha256 = digest(native_files)
    source_selection = sealed_selection(
        native_files,
        main_assembly,
        edges={
            main_assembly: ["cad/base.SLDPRT", "cad/arm.SLDPRT"],
            "cad/base.SLDPRT": [],
            "cad/arm.SLDPRT": [],
        },
    )
    components = [
        {
            "configuration": "Default",
            "document": document,
            "instance_id": name,
            "lightweight": False,
            "name2": name,
            "source_provenance": None,
            "suppressed": False,
            "suppression_state": 2,
        }
        for name, document in (
            ("base-1", "cad/base.SLDPRT"),
            ("arm-1", "cad/arm.SLDPRT"),
        )
    ]
    discovery = {
        "schema_version": NATIVE_DISCOVERY_SCHEMA,
        "run_id": RUN_ID,
        "derivation": {
            "configuration": None,
            "main_assembly": main_assembly,
            "record_roots": [],
        },
        "identity": {
            "change_summary": "native minimal dependency fixture",
            "control": {"reference": "fixture", "system": "handoff"},
            "delivery_configuration": "Default",
            "hardware_id": "m3.0",
            "main_assembly": main_assembly,
            "owner": "fixture",
            "parent_revision": None,
            "revision": "r1",
            "robot_name": "robot",
            "robot_name_technical": None,
        },
        "findings": [],
        "handoff_sha256": handoff_sha256,
        "native_files": native_files,
        "records": [],
        "raw": {
            "components": components,
            "files": dict(native_files),
            "saved_configuration": "Default",
            "source_selection": source_selection,
        },
    }
    (package / "discovery").mkdir()
    write_json(package / "discovery/native-discovery.json", discovery)
    write_json(
        package / "discovery/findings.json",
        {
            "schema_version": "solidworks-to-urdf.discovery-findings/v1",
            "findings": [],
        },
    )
    source = support.default_source_record(main_assembly=main_assembly)
    source["identity"]["handoff_digest"] = handoff_sha256
    source["source_revision_id"] = digest(source["identity"])
    write_json(package / "source.json", source)
    plan = {
        "axis_requests": [],
        "configuration": "Default",
        "datums": ["base_datum", "arm_datum", "imu_datum"],
        "discovery_sha256": hashlib.sha256((package / "discovery/native-discovery.json").read_bytes()).hexdigest(),
        "evidence_requests": list(CAPTURE_EVIDENCE_REQUESTS),
        "groups": [
            {"components": ["base-1"], "name": "base_link"},
            {"components": ["arm-1"], "name": "arm_link"},
        ],
        "handoff_sha256": handoff_sha256,
        "identity": {"hardware_id": "m3.0", "revision": "r1"},
        "main_assembly": main_assembly,
        "objects": [
            {"document": "cad/base.SLDPRT", "name": "base-1", "state": 2},
            {"document": "cad/arm.SLDPRT", "name": "arm-1", "state": 2},
        ],
        "run_id": RUN_ID,
        "schema_version": "solidworks-to-urdf.capture-plan/v1",
        "selection": {"files": native_files},
    }
    write_json(package / "capture-plan.json", plan)
    return (
        package,
        backend,
        native_files,
        handoff_sha256,
        main_assembly,
        _native_tool(),
    )


def _prior_native_events(
    workspace: Path,
    package: Path,
    native_files: dict[str, str],
    handoff_sha256: str,
    main_assembly: str,
) -> list[dict]:
    from description_pipeline.io import write_json
    from tests.v1.test_stage_transfer import _capture_events

    expected = workspace / "event-seed"
    (expected / "input/discovery").mkdir(parents=True)
    (expected / "input/cad").mkdir()
    (expected / "evidence").mkdir()
    for relative in native_files:
        target = expected / "input/cad" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(package / relative, target)
    for relative in (
        "source.json",
        "discovery/native-discovery.json",
        "discovery/findings.json",
    ):
        target = expected / "input" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(package / relative, target)
    shutil.copyfile(
        package / "discovery/native-discovery.json",
        expected / "input/discovery/native-discovery.json",
    )
    shutil.copyfile(
        package / "capture-plan.json",
        expected / "input/capture-plan.json",
    )
    write_json(expected / "evidence/manifest.json", {})
    write_json(
        expected / "input/handoff.json",
        {
            "cad_revision": {"hardware_id": "m3.0", "revision": "r1"},
            "package_files": dict(sorted(native_files.items())),
        },
    )
    events = _capture_events(
        expected,
        handoff=handoff_sha256,
        main=main_assembly,
        native_files=native_files,
    )
    return [event for event in events if event.get("stage") in {"freeze", "discover"}]


def _run(workspace: Path) -> dict:
    from description_pipeline import native_transfer
    from description_pipeline.interface_records import PURPOSE_ENGINEERING
    from description_pipeline.io import digest
    from description_pipeline.orchestration import stage_transfer
    from description_pipeline.pipeline.contracts import ArtifactRef
    from description_pipeline.pipeline.capture import _capture_native_transaction
    from description_pipeline.sources.solidworks.revision import package_inventory
    from description_pipeline.request_evidence import build_request_evidence

    capture_evidence = importlib.import_module("description_pipeline.verification.capture_evidence")
    facts_module = importlib.import_module("description_pipeline.verification.native_capture_facts")
    (
        package,
        backend,
        native_files,
        handoff_sha256,
        main_assembly,
        tool,
    ) = _build_prepared_package(workspace)
    producer_tool_sha256 = digest(tool)
    upstream_artifacts = tuple(
        ArtifactRef(
            artifact_id=f"minimal-{stage}-artifact",
            kind=f"stage_output:{stage}",
            locator=f"cas://artifact/minimal-{stage}-artifact",
            manifest_sha256=digest({"manifest": payload}),
            set_sha256=digest({stage: payload}),
            producer_run_id=RUN_ID,
            producer_stage=stage,
            producer_tool_sha256=producer_tool_sha256,
        ).to_record()
        for stage, payload in (
            ("freeze", {"state": "minimal"}),
            ("discover", {"state": "minimal"}),
        )
    )
    execution = {
        "claim_id": "minimal-capture-claim",
        "command_id": "minimal-capture-command",
        "effect_key": "native",
        "executor_sha256": digest({"executor": "capture"}),
        "fence": 1,
        "native_tool_sha256": producer_tool_sha256,
        "owner_sha256": digest({"owner": "minimal-capture"}),
        "run_id": RUN_ID,
    }
    request_evidence = build_request_evidence(
        purpose=PURPOSE_ENGINEERING,
        request_id="minimal-native-closure",
        target_hardware="m3.0",
        change_summary="Minimal native closure probe",
        main_assembly=main_assembly,
        handoff_digest=handoff_sha256,
        platform_config_digest="c" * 64,
    )
    capture = workspace / "capture"
    with mock.patch(
        "description_pipeline.runtime.tool_record",
        return_value=tool,
    ):
        receipt = _capture_native_transaction(
            package,
            capture,
            backend=backend,
            run_id=RUN_ID,
            handoff_sha256=handoff_sha256,
            upload_sha256=handoff_sha256,
            main_assembly=main_assembly,
            expected_inputs=package_inventory(package),
            on_event=lambda _event: None,
            native_tool_factory=_native_tool,
            upstream_artifacts=upstream_artifacts,
            execution=execution,
            request_evidence=request_evidence,
        )
    if receipt.get("state") != "native_complete":
        raise RuntimeError(f"solidworks.capture did not complete: {receipt.get('error')} {receipt.get('detail')}")
    archive = capture / stage_transfer.CAPTURE_ARCHIVE
    admitted = workspace / "admitted"
    stage_transfer.admit_capture(
        archive,
        admitted,
        expected_run_id=RUN_ID,
        expected_handoff_sha256=handoff_sha256,
        expected_upload_sha256=handoff_sha256,
        expected_main_assembly=main_assembly,
        expected_native_tool=_native_tool(),
        expected_upstream_artifacts=upstream_artifacts,
        expected_execution=execution,
    )
    transfer = native_transfer.verify_transfer(
        admitted,
        expected_upstream_artifacts=upstream_artifacts,
        expected_execution=execution,
    )

    scene = capture_evidence.scene(admitted)
    source_scopes = capture_evidence.source_scopes(admitted)
    mesh = capture_evidence.geometry(admitted)
    facts = facts_module.verify_native_capture_facts(admitted)
    return {
        "archive_files": transfer["files"],
        "capture_components": len(scene["components"]),
        "fact_components": len(native_files),
        "facts": facts,
        "mesh_components": len(mesh),
        "source_scope_components": len(source_scopes),
        "transfer_main_assembly": transfer["main_assembly"],
    }


def main() -> int:
    args = _arguments()
    source = Path(args.source).resolve()
    site = Path(args.site).resolve()
    workspace = Path(args.workspace).resolve()
    receipt_path = Path(args.receipt).resolve()
    guard = PortableDependencyGuard()
    sys.meta_path.insert(0, guard)
    sys.path[:] = _install_minimal_paths(source, site)
    workspace.mkdir(parents=True, exist_ok=True)
    receipt = {
        "banned_imports": [],
        "denied_roots": sorted(DENIED_ROOTS),
        "module_specs": {},
        "python": sys.version,
        "sys_path": list(sys.path),
    }
    try:
        import packaging

        receipt["packaging"] = {
            "module": packaging.__file__,
            "version": packaging.__version__,
        }
        receipt["module_specs"] = {
            name: (
                spec.origin
                if (
                    spec := importlib.machinery.PathFinder.find_spec(
                        name,
                        sys.path,
                    )
                )
                is not None
                else None
            )
            for name in ("yaml", "numpy", "jsonschema", "mujoco")
        }
        receipt["route"] = _run(workspace)
        receipt["status"] = "pass"
    except BaseException as error:
        receipt["status"] = "fail"
        receipt["error"] = f"{type(error).__name__}: {error}"
        receipt["traceback"] = traceback.format_exc()
        receipt["banned_imports"] = guard.attempts
        _write_json(receipt_path, receipt)
        raise
    else:
        receipt["banned_imports"] = guard.attempts
        _write_json(receipt_path, receipt)
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
