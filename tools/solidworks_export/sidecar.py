"""Downstream interface: ``solidworks-mass-properties/v1`` CAD evidence sidecar.

Values are derived from the CAD raw mass properties plus component placement
transforms (the same computation that feeds the URDF) -- never re-read from the
URDF text. The raw, unconverted readings stay in ``cad_evidence.json``.

``frame_mapping_confirmed`` and ``component_selection_confirmed`` are written
as ``false`` unless a human/independent check has confirmed them; a synthetic
package therefore never claims real-machine acceptance.
"""

from __future__ import annotations

from typing import Dict

from . import __version__

SCHEMA = "solidworks-mass-properties/v1"
UNITS = {"length": "m", "mass": "kg", "inertia": "kg*m^2", "angle": "rad"}


def build_sidecar(
    model,
    evidence,
    cfg,
    urdf_sha256: str,
    export_config_sha256: str,
    source_kind: str,
    document: str,
    cad_evidence_sha256: str,
) -> Dict:
    links: Dict[str, Dict] = {}
    for link in model.links:
        payload = evidence["links"][link.name]
        if getattr(link, "is_frame", False):
            # Frames carry kinematics only; the sidecar says so instead of
            # reporting invented mass properties.
            links[link.name] = {
                "output_frame": f"{link.name}_link_frame",
                "output_frame_in_link": {"xyz_m": [0.0, 0.0, 0.0], "rpy_rad": [0.0, 0.0, 0.0]},
                "frame_mapping_confirmed": False,
                "component_selection_confirmed": False,
                "components_declared": [],
                "link_frame_source": payload.get("link_frame_source"),
                "kind": "frame",
                "product_convention": "not_applicable",
                "mass_kg": 0.0,
                "com_in_output_m": [0.0, 0.0, 0.0],
                "L_at_com": {"ixx": 0.0, "iyy": 0.0, "izz": 0.0, "ixy": 0.0, "ixz": 0.0, "iyz": 0.0},
                "uncertainty": {"mass_kg": 0.0, "com_m": [0.0, 0.0, 0.0], "inertia_kg_m2": 0.0, "principal_kg_m2": 0.0},
            }
            continue
        inertia = payload["combined"]["inertia"]
        links[link.name] = {
            "output_frame": f"{link.name}_link_frame",
            "output_frame_in_link": {
                "xyz_m": [0.0, 0.0, 0.0],
                "rpy_rad": [0.0, 0.0, 0.0],
            },
            "frame_mapping_confirmed": False,
            "component_selection_confirmed": False,
            "components_declared": list(payload.get("components", [])),
            "link_frame_source": payload.get("link_frame_source"),
            "product_convention": "negative_products",
            "mass_kg": float(link.mass),
            "com_in_output_m": [float(value) for value in link.com],
            "L_at_com": {
                "ixx": float(inertia[0][0]),
                "iyy": float(inertia[1][1]),
                "izz": float(inertia[2][2]),
                "ixy": float(inertia[0][1]),
                "ixz": float(inertia[0][2]),
                "iyz": float(inertia[1][2]),
            },
            "uncertainty": {
                "mass_kg": 0.0,
                "com_m": [0.0, 0.0, 0.0],
                "inertia_kg_m2": 0.0,
                "principal_kg_m2": 0.0,
            },
        }
    synthetic = source_kind != "solidworks"
    return {
        "schema_version": SCHEMA,
        "urdf_sha256": urdf_sha256,
        "export_config_sha256": export_config_sha256,
        "units": dict(UNITS),
        "source": {
            "kind": source_kind,
            "name": "cad_evidence.json",
            "sha256": cad_evidence_sha256,
            "cad_document": document,
            "sha256_basis": (
                "SHA256 of the in-package cad_evidence.json (CAD API raw "
                "readings and placement transforms); not a hash of the CAD "
                "binary. The native document path is recorded in "
                "native_source.json.document and in source.cad_document."
            ),
            "synthetic": synthetic,
        },
        "generator": {"tool": "solidworks_export", "version": __version__},
        "notes": (
            "Generated from CAD raw mass properties and placement transforms. "
            "Values are converted to the standard inertia convention "
            "(negative products) in the original part axes before rotation into "
            "the link frame. Frame mapping and component selection are NOT "
            "independently confirmed; synthetic packages are software fixtures."
        ),
        "links": links,
    }
