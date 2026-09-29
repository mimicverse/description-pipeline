"""Export pipeline: collect scene -> mass properties -> meshes -> URDF -> metadata."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import platform
import shutil
import sys
import tempfile
import time
import uuid
from typing import Callable, Dict, List, Optional

from . import __version__
from .backends import Backend
from .config import ExportConfig
from .errors import BridgeError, CadError, UsageError
from .package_check import verify_package
from .scene_build import build_robot_model
from .sidecar import build_sidecar
from .stl import merge_binary_stl
from .urdf import write_urdf


def _sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json(path: str, payload: dict) -> None:
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")


def _config_to_dict(cfg: ExportConfig) -> dict:
    return {
        "schema_version": cfg.schema_version,
        "model": cfg.model,
        "source_length_unit": cfg.source_length_unit,
        "inertia_product_convention": cfg.inertia_product_convention,
        "mesh": {"format": "stl_binary", "merge": cfg.mesh_merge, "path_prefix": cfg.mesh_path_prefix},
        "links": [
            {
                "name": link.name,
                "components": list(link.components),
                "frame_component": link.frame_component,
                "frame": link.frame,
            }
            for link in cfg.links
        ],
        "joints": [
            {
                "name": joint.name,
                "type": joint.type,
                "parent": joint.parent,
                "child": joint.child,
                "coordinate_system": joint.coordinate_system,
                "origin": joint.origin,
                "axis": list(joint.axis) if joint.axis else None,
                "limits": joint.limits,
                "dynamics": joint.dynamics,
            }
            for joint in cfg.joints
        ],
        "coordinate_system_transforms": cfg.coordinate_system_transforms,
        "component_masses": cfg.component_masses,
        "mass_provenance": cfg.mass_provenance,
        "notes": cfg.notes,
    }


def _source_kind(evidence_class: str) -> str:
    return "solidworks" if evidence_class == "real_cad" else "synthetic"


def _check_cancelled(job) -> None:
    if job is not None and getattr(job, "cancel_requested", False):
        raise BridgeError("job_cancelled", "job was cancelled")


def _preserve_failed_dir(tmp_dir: str, parent: str, log_lines: List[str]) -> str:
    """Keep the failure state on disk (architecture doc, section 5.8)."""

    target = os.path.join(
        parent,
        f".swbridge-failed-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:4]}",
    )
    try:
        os.replace(tmp_dir, target)
    except OSError:
        target = tmp_dir
    try:
        os.makedirs(target, exist_ok=True)
        with open(os.path.join(target, "failure.log"), "w", encoding="utf-8", newline="\n") as handle:
            handle.write("\n".join(log_lines) + "\n")
    except OSError:
        pass
    return target


def _environment_block(backend: Backend) -> Dict:
    """Export environment metadata recorded in native_source.json."""

    info = {
        "bridge_version": __version__,
        "python": sys.version.split()[0],
        "platform": platform.platform(),
    }
    try:
        health = backend.health()
        info["backend"] = health.get("backend") or ""
        info["sw_version"] = health.get("sw_version") or ""
        info["active_document"] = health.get("active_document") or ""
    except Exception as exc:
        info["backend_health_error"] = str(exc)
    return info


def run_export(
    backend: Backend,
    cfg: ExportConfig,
    out_dir: str,
    doc_path: str,
    log: Optional[Callable[[str], None]] = None,
    job=None,
    evidence_class: str = "synthetic",
    progress: Optional[Callable[[Dict], None]] = None,
) -> Dict:
    """Run one full export; output is written atomically to ``out_dir``."""

    if evidence_class not in ("real_cad", "synthetic"):
        raise UsageError("evidence_class must be 'real_cad' or 'synthetic'", {"evidence_class": evidence_class})
    if evidence_class == "real_cad" and backend.name != "solidworks":
        raise UsageError("Only the native SolidWorks backend can claim real_cad")
    if backend.name == "solidworks" and (
        cfg.source_length_unit != "m" or cfg.inertia_product_convention != "solidworks_positive"
    ):
        raise UsageError(
            "The calibrated native backend returns SI / positive products; do not rescale or reinterpret it"
        )
    if backend.name == "solidworks":
        raise UsageError(
            "Legacy native export is retired; use description source freeze and description build "
            "with the Windows worker"
        )
    log = log or (lambda message: None)
    out_dir = os.path.abspath(out_dir)
    if os.path.exists(out_dir) and os.listdir(out_dir):
        raise UsageError("output directory exists and is not empty", {"out_dir": out_dir})
    parent = os.path.dirname(out_dir) or "."
    os.makedirs(parent, exist_ok=True)
    tmp_dir = tempfile.mkdtemp(prefix=".swbridge-export-", dir=parent)
    log_lines: List[str] = []
    environment = _environment_block(backend)

    def report(payload: Dict) -> None:
        if progress is None:
            return
        try:
            progress(payload)
        except Exception as exc:  # a reporter must never break an export
            log_lines.append(f"progress callback failed: {exc}")

    def note(message: str) -> None:
        log_lines.append(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {message}")
        log(message)

    note(f"evidence_class: {evidence_class}")
    note(f"environment: {json.dumps(environment, ensure_ascii=False, sort_keys=True)}")

    try:
        cs_names = sorted({joint.coordinate_system for joint in cfg.joints if joint.coordinate_system})
        report({"stage": "collecting_scene", "percent": 0})
        note(f"collecting scene from {doc_path}")
        documented = bool(cfg.component_masses)
        if documented:
            # Documented mass tables make CAD materials optional; backends that do
            # not know the flag are reported instead of failing obscurely.
            try:
                scene = backend.collect_scene(doc_path, cs_names, progress=note, require_material=False)
            except TypeError as exc:
                raise UsageError(
                    "this backend does not support component_masses",
                    {"hint": "use a backend with require_material support", "error": str(exc)},
                ) from exc
        else:
            scene = backend.collect_scene(doc_path, cs_names, progress=note)
        _check_cancelled(job)
        report({"stage": "building_model", "percent": 10})
        api_contact = {
            key: value
            for key, value in scene.notes.items()
            if key == "backend" or key.startswith(("mass_property:", "coordinate_system:", "mesh:"))
        }

        model, evidence, mesh_transforms = build_robot_model(cfg, scene)
        note(f"model built: {len(model.links)} links, {len(model.joints)} joints")

        mesh_dir = os.path.join(tmp_dir, "meshes")
        parts_dir = os.path.join(tmp_dir, "parts-scratch")
        os.makedirs(mesh_dir, exist_ok=True)
        os.makedirs(parts_dir, exist_ok=True)
        mesh_report: Dict[str, Dict] = {}
        for index, link in enumerate(cfg.links, start=1):
            _check_cancelled(job)
            if link.is_frame:
                # A massless frame has no solid, so there is nothing to mesh.
                mesh_report[link.name] = {"triangles": 0, "merged_from": [], "note": "frame link: no mesh"}
                continue
            report(
                {
                    "stage": "exporting_meshes",
                    "link": link.name,
                    "index": index,
                    "total": len(cfg.links),
                    "percent": 10 + int(70 * (index - 1) / max(1, len(cfg.links))),
                }
            )
            part_files = []
            for part_index, component in enumerate(link.components):
                # Native nested names contain '/', and are identifiers, never
                # paths. Opaque transaction-local names also prevent traversal.
                part_path = os.path.join(parts_dir, f"{index}-{part_index}.STL")
                info = backend.export_component_mesh(component, part_path, progress=note)
                part_files.append(part_path)
                mesh_report[component] = {
                    "triangles": info.get("triangles"),
                    "used_api": info.get("used_api"),
                    "sha256": _sha256(part_path),
                }
            target = os.path.join(mesh_dir, f"{link.name}.STL")
            if cfg.mesh_merge == "per_link":
                triangles = merge_binary_stl(
                    part_files,
                    target,
                    source_note=link.name,
                    transforms=[mesh_transforms.get(name) for name in link.components],
                )
                mesh_report[link.name] = {"triangles": triangles, "merged_from": list(link.components)}
            else:
                if len(part_files) != 1:
                    raise UsageError(
                        "mesh.merge='none' requires exactly one component per link",
                        {"link": link.name},
                    )
                # 'none' means no component merging, not no frame conversion.
                triangles = merge_binary_stl(
                    part_files,
                    target,
                    source_note=link.name,
                    transforms=[mesh_transforms[link.components[0]]],
                )
                mesh_report[link.name] = {"triangles": triangles, "merged_from": []}
        shutil.rmtree(parts_dir, ignore_errors=True)
        note(f"meshes written: {len(model.links)} link mesh(es)")

        report({"stage": "writing_package", "percent": 85})
        urdf_path = os.path.join(tmp_dir, "robot.urdf")
        write_urdf(urdf_path, model)
        # Provenance is part of the contract: a reader must be able to tell, without
        # asking, whether these mass/inertia numbers came from CAD or from weighing.
        mass_provenance = {
            "schema_version": "swbridge.mass-provenance/v1",
            "kind": ("documented_source" if documented else ("cad" if evidence_class == "real_cad" else "synthetic")),
            "documented_components": sorted(cfg.component_masses) if documented else [],
            "documented_table": dict(cfg.mass_provenance) if documented else {},
            "values_from": "solidworks_mass_properties" if evidence_class == "real_cad" else "synthetic_fixture",
            "density_source": (
                "documented_mass_table"
                if documented
                else (
                    "solidworks_explicit_material_assignment" if evidence_class == "real_cad" else "synthetic_fixture"
                )
            ),
            "measured_override": False,
            "policy": "pure_cad_v1",
            "note": (
                "CAD mass properties with explicit per-solid material assignment; "
                "not proof of physical material correctness or hardware acceptance."
                if evidence_class == "real_cad"
                else "Synthetic mathematical fixture; no CAD materials or physical hardware verified."
            ),
        }
        _write_json(
            os.path.join(tmp_dir, "cad_evidence.json"),
            {
                "schema_version": "swbridge.cad-evidence/v1",
                "evidence_class": evidence_class,
                "model": cfg.model,
                "document": doc_path,
                "environment": environment,
                "api_contact": api_contact,
                "mass_provenance": mass_provenance,
                "links": evidence["links"],
                "joints": evidence["joints"],
                "notes": evidence.get("notes", {}),
                "mesh_report": mesh_report,
            },
        )
        _write_json(
            os.path.join(tmp_dir, "mass_properties.json"),
            {
                link_name: payload.get(
                    "raw", {"kind": payload.get("kind", "unknown"), "mass_properties": payload.get("mass_properties")}
                )
                for link_name, payload in evidence["links"].items()
            },
        )
        _write_json(os.path.join(tmp_dir, "export_config.json"), _config_to_dict(cfg))
        urdf_sha256 = _sha256(urdf_path)
        export_config_sha256 = _sha256(os.path.join(tmp_dir, "export_config.json"))
        cad_evidence_sha256 = _sha256(os.path.join(tmp_dir, "cad_evidence.json"))
        native_files = {}
        if evidence_class == "real_cad":
            verify_sources = getattr(backend, "verify_sources_unchanged", None)
            if verify_sources is None:
                raise CadError(
                    "cad_sources_unbound",
                    "该后端不支持源哈希复核，不能用于 real_cad 导出",
                )
            native_files = verify_sources()
            if not native_files:
                raise CadError("cad_sources_unbound", "real CAD export requires saved source hashes")
        _write_json(
            os.path.join(tmp_dir, "native_source.json"),
            {
                "schema_version": "swbridge.native-source/v1",
                "document": doc_path,
                "model": cfg.model,
                "bridge_version": __version__,
                "exported_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "source_length_unit": cfg.source_length_unit,
                "source_kind": _source_kind(evidence_class),
                "cad_source_files": native_files,
                "evidence_class": evidence_class,
                "urdf_sha256": urdf_sha256,
                "export_config_sha256": export_config_sha256,
                "environment": environment,
                "api_contact": api_contact,
                "mass_provenance": mass_provenance,
                "notes": cfg.notes,
            },
        )
        _write_json(
            os.path.join(tmp_dir, "cad_inertia_evidence.json"),
            build_sidecar(
                model,
                evidence,
                cfg,
                urdf_sha256,
                export_config_sha256,
                _source_kind(evidence_class),
                doc_path,
                cad_evidence_sha256,
            ),
        )
        with open(os.path.join(tmp_dir, "export.log"), "w", encoding="utf-8", newline="\n") as handle:
            handle.write("\n".join(log_lines) + "\n")

        files: List[tuple] = []
        for root, _dirs, names in os.walk(tmp_dir):
            for name in sorted(names):
                path = os.path.join(root, name)
                files.append((path, os.path.relpath(path, tmp_dir).replace(os.sep, "/")))
        files.sort(key=lambda item: item[1])
        with open(os.path.join(tmp_dir, "manifest.sha256"), "w", encoding="utf-8", newline="\n") as handle:
            for path, rel in files:
                handle.write(f"{_sha256(path)}  {rel}\n")

        report({"stage": "integrity_check", "percent": 95})
        shallow = verify_package(tmp_dir)
        status = "passed" if shallow["ok"] else "failed"
        note(
            f"post-export integrity check: {status} ({len(shallow['checks'])} checks, "
            f"{len(shallow['warnings'])} warnings)"
        )
        if not shallow["ok"]:
            raise CadError(
                "export_shallow_check_failed",
                "post-export integrity check failed",
                {"errors": shallow["errors"], "warnings": shallow["warnings"]},
            )

        if os.path.exists(out_dir):
            os.rmdir(out_dir)
        os.replace(tmp_dir, out_dir)
        note(f"export committed to {out_dir}")
        report({"stage": "done", "percent": 100})
        return {
            "out_dir": out_dir,
            "links": len(model.links),
            "joints": len(model.joints),
            "files": [rel for _path, rel in files] + ["manifest.sha256"],
            "shallow_check": {
                "ok": True,
                "checks": len(shallow["checks"]),
                "warnings": shallow["warnings"],
            },
        }
    except BaseException as exc:
        failed_dir = _preserve_failed_dir(tmp_dir, parent, log_lines)
        with contextlib.suppress(Exception):  # some exception types forbid attributes
            exc.failed_dir = failed_dir  # type: ignore[attr-defined]
        raise
