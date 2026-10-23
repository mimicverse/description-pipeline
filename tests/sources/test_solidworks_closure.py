"""Fixtures for native component context and independent selection accounting."""

from __future__ import annotations

import unittest

from . import _paths  # noqa: F401  (import side effect: sys.path)

from description_pipeline.sources.solidworks.freeze import _component_context_record  # noqa: E402

from . import support  # noqa: E402

DEV = "board-1/Dev_Board-1"
EASYEDA = "board-1/EasyEDA PCB Model~Nz70-1"


def _placement(xyz=(0.0, 0.0, 0.0)):
    return [1.0, 0.0, 0.0, xyz[0], 0.0, 1.0, 0.0, xyz[1], 0.0, 0.0, 1.0, xyz[2], 0.0, 0.0, 0.0, 1.0]


def _flags(**active):
    flags = {"OverrideMass": False, "OverrideCenterOfMass": False, "OverrideMomentsOfInertia": False}
    flags.update(active)
    return flags


def _part_payload(mass, com=(0.0, 0.0, 0.0)):
    return {
        "mass": mass,
        "com": list(com),
        "inertia": [[1e-6, 0.0, 0.0], [0.0, 1e-6, 0.0], [0.0, 0.0, 1e-6]],
        "reference": {"used_api": "fixture", "product_convention": "solidworks_standard"},
    }


def _effective_payload(mass, com, *, role, document_type, name):
    return {
        "mode": "effective_instance" if document_type == "part" else "assembly_component_group",
        "state": "sheet_only" if name == EASYEDA else None,
        "measurement_basis": "cad_authored_effective_instance",
        "mass": mass,
        "com": list(com),
        "inertia": [[2e-6, 1e-8, 0.0], [1e-8, 2.5e-6, 0.0], [0.0, 0.0, 4e-6]],
        "role": role,
        "document_type": document_type,
        "reference": {
            "used_api": "IMassProperty2.GetMomentOfInertia(0)",
            "selection_api": "IMassProperty2 (SelectedItems = component instance)",
            "override_api": "IMassProperty2.GetOverrideOptions (same selection)",
            "scope": "assembly_component_group",
            "axes": "assembly_document_axes",
            "reference_point": "center_of_mass",
            "product_convention": "solidworks_standard",
            "effective_instance": True,
            "measurement_basis": "cad_authored_effective_instance",
            "selected_components": [name],
            "configuration": "Default",
            "part_document": "C:/collected/" + name.split("/")[-1] + ".SLDPRT",
            "assembly_document": "C:/collected/robot.SLDASM",
            "assembly_configuration": "Default",
            "overrides": _flags(OverrideMass=True),
        },
    }


def _instance(name, *, depth, parent, document_type, mass, overrides, effective=None, error=None):
    entry = {
        "name": name,
        "parent": parent,
        "depth": depth,
        "document": "C:/collected/robot.SLDASM",
        "document_type": document_type,
        "context_mass_kg": mass,
        "context_volume_m3": None,
        "overrides": overrides,
        "effective": effective,
        "effective_error": error,
    }
    return entry


def _carrier_transform(name: str) -> list[float]:
    """The recorded occurrence transform; EasyEDA's frame sits 0.01 m above the origin."""

    return _placement((0.0, 0.01, 0.0)) if name == EASYEDA else _placement()


def _hinge_point(name: str) -> list[float]:
    """Component-frame geometry that solves the hinge through the child datum origin.

    The assembly-space line runs through (0, 0.01, 0): the child link's datum
    origin, which the joint frame check requires the axis to pass through.
    """

    return [0.0, 0.0, 0.0] if name == EASYEDA else [0.0, 0.01, 0.0]


def _carrier_record(bodies: list[dict]) -> tuple[dict, dict[str, str]]:
    """Fixture plumbing: the native record the given bodies are rebuilt from.

    Rigid membership inside one body comes from a solved lock mate; two bodies
    hang off one solved concentric+coincident hinge so the reduced joint tree
    stays connected.  The capture always reads its base occurrence, even when
    a scenario's scene carries no mass-bearing components.
    """

    names = [str(member) for body in bodies for member in (body.get("components") or [])]
    if not names:
        names = [DEV]
    components = [
        support.native_component(
            name, document=name.split("/")[-1].split("~")[0] + ".SLDPRT", transform=_carrier_transform(name)
        )
        for name in dict.fromkeys(names)
    ]
    datums = [
        support.native_datum("CS_" + str(body["name"]), str(members[0]), _carrier_transform(str(members[0])))
        for body in bodies
        if (members := list(body.get("components") or []))
    ]
    if not datums:
        datums = [support.native_datum("CS_base_link", DEV, _carrier_transform(DEV))]
    mates: list[dict] = []
    joint_claims = None
    controlled: dict[str, str] = {}
    for body in bodies:
        members = [str(member) for member in (body.get("components") or [])]
        if len(members) > 1:
            mates.append(support.native_lock(members))
    named = [body for body in bodies if body.get("components")]
    if len(named) > 1:
        left, right = str(named[0]["components"][0]), str(named[1]["components"][0])
        hinge_mates = support.native_hinge_mates(
            left, right, left_point=_hinge_point(left), right_point=_hinge_point(right), radius=0.005
        )
        mates += hinge_mates
        mates.append(support.native_limit_mate(left, right))
        joint_claims = support.native_joint_claims()
        controlled["joints/hinge.json"] = support.native_joint_record()
    masses = [
        {"component": entry["name2"], "mass_kg": 0.035 if entry["name2"] == DEV else 0.026, "material": "FR-4"}
        for entry in components
    ]
    record = support.native_record(
        hardware_id="closure_fixture",
        change_summary="closure module fixture",
        control_reference="closure-run",
        components=components,
        datums=datums,
        masses=masses,
        mates=mates,
        joint_claims=joint_claims,
        budget=False,
        assembly_mass_kg=0.0632,
        assembly_extent_m=0.2,
    )
    return record, controlled


class ComponentContextRecordTests(unittest.TestCase):
    def test_native_record_keeps_observations_only(self):
        reading = {
            "assembly": "C:/collected/robot.SLDASM",
            "configuration": "Default",
            "instances": [
                _instance(
                    DEV, depth=1, parent="board-1", document_type="part", mass=0.035,
                    overrides=_flags(), effective=None,
                ),
                _instance(
                    EASYEDA, depth=1, parent="board-1", document_type="part", mass=0.026,
                    overrides=_flags(OverrideMass=True), effective={"mass": 0.026},
                    error="cad_effective_instance_unreadable",
                ),
            ],
            "errors": [{"name": "junk", "error": "cad_component_context_invalid", "message": "bad"}],
            "reference": {"method": "fixture"},
        }
        record = _component_context_record(reading, 0.0632)
        self.assertEqual(
            set(record),
            {"schema_version", "status", "assembly_mass_kg", "instances", "errors", "reference"},
        )
        self.assertEqual(record["status"], "partial")
        self.assertEqual(record["assembly_mass_kg"], 0.0632)
        self.assertEqual(len(record["errors"]), 1)
        self.assertEqual(
            set(record["instances"][0]),
            {
                "name",
                "parent",
                "depth",
                "document",
                "document_type",
                "context_mass_kg",
                "context_volume_m3",
                "overrides",
                "effective",
                "effective_error",
            },
        )
        self.assertEqual(record["instances"][1]["effective"], {"mass": 0.026})
        self.assertEqual(record["instances"][1]["effective_error"], "cad_effective_instance_unreadable")
        for banned in (
            "document_basis_mass_kg",
            "delta_kg",
            "totals",
            "overridden",
            "leaf_documents",
            "leaf_total",
            "effective_total",
            "blockers",
        ):
            self.assertNotIn(banned, record)


if __name__ == "__main__":
    unittest.main()
