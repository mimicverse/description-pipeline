"""Offline package integrity check for a swbridge export (upstream side).

Scope: packaging, manifest hashes, URDF structure and evidence-field presence.
This is deliberately NOT a physics/quality gate; downstream owns strict
URDF/mesh validation, MJCF conversion and mjlab verification.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import xml.etree.ElementTree as ET
from typing import Dict, List

REQUIRED_FILES = (
    "robot.urdf",
    "manifest.sha256",
    "native_source.json",
    "export_config.json",
    "cad_evidence.json",
    "cad_inertia_evidence.json",
)


def _sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fs_number(value) -> bool:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    return math.isfinite(number)


def verify_package(package_dir: str) -> Dict:
    root = os.path.abspath(package_dir)
    checks: List[Dict] = []
    warnings: List[str] = []
    errors: List[str] = []

    def check(name: str, ok: bool, detail=None) -> bool:
        entry = {"check": name, "ok": bool(ok)}
        if detail is not None:
            entry["detail"] = detail
        checks.append(entry)
        if not ok:
            errors.append(name)
        return bool(ok)

    check("package_exists", os.path.isdir(root), root)
    if not os.path.isdir(root):
        return {"ok": False, "package_dir": root, "checks": checks, "warnings": warnings, "errors": errors}

    for name in REQUIRED_FILES:
        check(f"required_file:{name}", os.path.isfile(os.path.join(root, name)))

    manifest_path = os.path.join(root, "manifest.sha256")
    listed = set()
    if os.path.isfile(manifest_path):
        with open(manifest_path, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    digest, rel = line.split("  ", 1)
                except ValueError:
                    check("manifest_line_format", False, line)
                    continue
                listed.add(rel)
                path = os.path.join(root, rel)
                if not os.path.isfile(path):
                    check(f"manifest_file_exists:{rel}", False)
                    continue
                check(f"manifest_hash:{rel}", _sha256(path) == digest)

    actual = set()
    for dirpath, _dirs, names in os.walk(root):
        for name in names:
            rel = os.path.relpath(os.path.join(dirpath, name), root).replace(os.sep, "/")
            if rel != "manifest.sha256":
                actual.add(rel)
    missing_from_manifest = sorted(actual - listed)
    if missing_from_manifest:
        warnings.append(f"files_not_in_manifest:{','.join(missing_from_manifest)}")

    urdf_path = os.path.join(root, "robot.urdf")
    link_names = set()
    mesh_files: List[str] = []
    urdf_links: Dict[str, ET.Element] = {}
    if os.path.isfile(urdf_path):
        try:
            tree = ET.parse(urdf_path)
            robot = tree.getroot()
            check("urdf_root_tag", robot.tag == "robot", robot.tag)
            for link in robot.findall("link"):
                link_name = link.get("name")
                if link_name:
                    link_names.add(link_name)
                    urdf_links[link_name] = link
                for mesh in link.iter("mesh"):
                    filename = mesh.get("filename")
                    if filename:
                        mesh_files.append(filename)
            parents = set()
            children = set()
            for joint in robot.findall("joint"):
                parent = joint.find("parent")
                child = joint.find("child")
                if parent is not None and parent.get("link"):
                    parents.add(parent.get("link"))
                if child is not None and child.get("link"):
                    children.add(child.get("link"))
            check(
                "urdf_joint_links_known", (parents | children) <= link_names, sorted((parents | children) - link_names)
            )
            roots = sorted(link_names - children)
            check("urdf_single_root", len(roots) == 1, roots)
        except ET.ParseError as exc:
            check("urdf_parses", False, str(exc))
    check("urdf_mesh_count", bool(mesh_files), len(mesh_files))

    def mesh_file(uri: str) -> str:
        """Repository layouts use "../meshes/..." while the package keeps meshes/ at its root."""
        while uri.startswith("../"):
            uri = uri[3:]
        return os.path.join(root, uri)

    missing_meshes = [name for name in sorted(set(mesh_files)) if not os.path.isfile(mesh_file(name))]
    check("urdf_mesh_files_exist", not missing_meshes, missing_meshes)

    for package_file in ("native_source.json", "cad_evidence.json"):
        path = os.path.join(root, package_file)
        if not os.path.isfile(path):
            continue
        try:
            with open(path, encoding="utf-8") as handle:
                payload = json.load(handle)
        except json.JSONDecodeError as exc:
            check(f"{package_file}_parses", False, str(exc))
            continue
        check(f"{package_file}_parses", True)
        check(
            f"{package_file}_evidence_class",
            payload.get("evidence_class") in ("real_cad", "synthetic"),
            payload.get("evidence_class"),
        )

    evidence_path = os.path.join(root, "cad_evidence.json")
    if os.path.isfile(evidence_path):
        with open(evidence_path, encoding="utf-8") as handle:
            evidence = json.load(handle)
        for link_name, payload in (evidence.get("links") or {}).items():
            if payload.get("kind") == "frame":
                # A frame link has no solid, so there are no mass properties to
                # cross-check; the URDF link must simply carry no inertial block.
                frame_link = urdf_links.get(link_name)
                check(
                    f"evidence_frame_has_no_inertial:{link_name}",
                    frame_link is not None and frame_link.find("inertial") is None,
                )
                continue
            combined = payload.get("combined") or {}
            ok_mass = _fs_number(combined.get("mass")) and float(combined["mass"]) > 0
            check(f"evidence_mass:{link_name}", ok_mass)
            com = combined.get("com")
            check(
                f"evidence_com:{link_name}", isinstance(com, list) and len(com) == 3 and all(_fs_number(v) for v in com)
            )
            inertia = combined.get("inertia")
            ok_inertia = (
                isinstance(inertia, list)
                and len(inertia) == 3
                and all(isinstance(row, list) and len(row) == 3 and all(_fs_number(v) for v in row) for row in inertia)
            )
            check(f"evidence_inertia:{link_name}", ok_inertia)

    urdf_actual = _sha256(urdf_path) if os.path.isfile(urdf_path) else None
    config_path = os.path.join(root, "export_config.json")
    config_actual = _sha256(config_path) if os.path.isfile(config_path) else None
    native_path = os.path.join(root, "native_source.json")
    if os.path.isfile(native_path):
        with open(native_path, encoding="utf-8") as handle:
            native = json.load(handle)
        check(
            "native_source_schema_version",
            native.get("schema_version") == "swbridge.native-source/v1",
            native.get("schema_version"),
        )
        check(
            "native_source_source_kind",
            native.get("source_kind") in ("solidworks", "synthetic"),
            native.get("source_kind"),
        )
        check("native_source_urdf_sha256", urdf_actual is not None and native.get("urdf_sha256") == urdf_actual)
        check(
            "native_source_export_config_sha256",
            config_actual is not None and native.get("export_config_sha256") == config_actual,
        )

    sidecar_path = os.path.join(root, "cad_inertia_evidence.json")
    if os.path.isfile(sidecar_path):
        try:
            with open(sidecar_path, encoding="utf-8") as handle:
                sidecar = json.load(handle)
        except json.JSONDecodeError as exc:
            check("sidecar_parses", False, str(exc))
        else:
            check("sidecar_parses", True)
            check(
                "sidecar_schema_version",
                sidecar.get("schema_version") == "solidworks-mass-properties/v1",
                sidecar.get("schema_version"),
            )
            check("sidecar_urdf_sha256", urdf_actual is not None and sidecar.get("urdf_sha256") == urdf_actual)
            check(
                "sidecar_units",
                sidecar.get("units") == {"length": "m", "mass": "kg", "inertia": "kg*m^2", "angle": "rad"},
                sidecar.get("units"),
            )
            source = sidecar.get("source") or {}
            check(
                "sidecar_source",
                bool(source.get("kind"))
                and bool(source.get("name"))
                and re.fullmatch(r"[0-9a-f]{64}", str(source.get("sha256", ""))) is not None,
            )
            evidence_hash = _sha256(evidence_path) if os.path.isfile(evidence_path) else None
            check(
                "sidecar_source_binding",
                source.get("name") == "cad_evidence.json"
                and evidence_hash is not None
                and source.get("sha256") == evidence_hash,
            )
            sidecar_links = sidecar.get("links") or {}
            check(
                "sidecar_links_cover_urdf",
                link_names == set(sidecar_links.keys()),
                sorted(set(sidecar_links.keys()) - link_names),
            )
            for link_name, record in sidecar_links.items():
                required = (
                    "output_frame",
                    "output_frame_in_link",
                    "frame_mapping_confirmed",
                    "component_selection_confirmed",
                    "product_convention",
                    "mass_kg",
                    "com_in_output_m",
                    "L_at_com",
                    "uncertainty",
                )
                check(
                    f"sidecar_record_fields:{link_name}",
                    all(key in record for key in required),
                    [key for key in required if key not in record],
                )
                check(
                    f"sidecar_record_flags:{link_name}",
                    type(record.get("frame_mapping_confirmed")) is bool
                    and type(record.get("component_selection_confirmed")) is bool,
                )
                tensor = record.get("L_at_com") or {}
                check(
                    f"sidecar_record_tensor:{link_name}",
                    set(tensor.keys()) == {"ixx", "iyy", "izz", "ixy", "ixz", "iyz"},
                    sorted(tensor.keys()),
                )

    return {
        "ok": not errors,
        "package_dir": root,
        "checks": checks,
        "warnings": warnings,
        "errors": errors,
    }
