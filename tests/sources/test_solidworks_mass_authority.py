"""CAD mass remains authoritative and manual replacement tables are rejected."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from . import _paths  # noqa: F401
from . import support
from description_pipeline.sources.solidworks.appearance import AppearanceError
from description_pipeline.sources.solidworks.errors import ConfigError
from description_pipeline.sources.solidworks.freeze import validate_source_config as _validate_source_config
from description_pipeline.sources.solidworks.scene import load_scene


def freeze(config, *args, **kwargs):
    return support.capture_prepared_fixture(config, *args, **kwargs)


def validate_source_config(config):
    return _validate_source_config(support.capture_requests(config))


def _native_record(*, files: dict[str, str]) -> dict:
    components = [
        support.native_component("base-1", document="base.SLDPRT"),
        support.native_component(
            "arm-1", document="arm.SLDPRT", transform=support.placement((0.0, 0.0, 0.2)), fixed=False
        ),
    ]
    datums = [
        support.native_datum("CS_base_link", "base-1", support.placement()),
        support.native_datum("CS_arm_link", "arm-1", support.placement((0.0, 0.0, 0.2))),
    ]
    masses = [
        {"component": "base-1", "mass_kg": 1.0, "material": "Alloy Steel"},
        {"component": "arm-1", "mass_kg": 0.5, "material": "Alloy Steel"},
    ]
    mates = support.native_hinge_mates(
        "base-1", "arm-1", left_point=(0.0, 0.0, 0.2), right_point=(0.0, 0.0, 0.0), radius=0.006
    )
    mates.append(support.native_limit_mate("base-1", "arm-1", point=(0.0, 0.0, 0.2)))
    return support.native_record(
        hardware_id="cad_mass_authority_fixture",
        change_summary="CAD mass authority fixture",
        control_reference="cad-mass-authority",
        components=components,
        datums=datums,
        masses=masses,
        mates=mates,
        joint_claims=support.native_joint_claims(),
        files=files,
    )


def _compose_scene_inputs(destination: Path, cad_root: Path) -> None:
    names = ["robot.SLDASM", "base.SLDPRT", "arm.SLDPRT"]
    record = _native_record(files=support.native_files(cad_root, names))
    support.seal_scene_inputs(
        destination,
        cad_root=cad_root,
        cad_names=names,
        record=record,
        controlled={
            "budget.json": support.native_budget_record(record),
            "joints/hinge.json": support.native_joint_record(),
        },
        record_keys={"joints/hinge.json": "limits"},
    )


def _appearance_owner(
    *,
    configuration: str,
    member: str,
    active: bool = False,
    values=None,
    has_values: bool = False,
    read_errors=None,
):
    material_call = (
        {
            "configuration_semantics": "active_configuration",
            "active_configuration": configuration,
        }
        if active
        else {
            "configuration_semantics": "requested_configuration",
            "configuration_option": 3,
            "configuration_names": [configuration],
        }
    )
    return {
        "used_api": member,
        "material_call": material_call,
        "presence_call": {
            "arguments": [],
            "configuration_semantics": "native_owner_context",
        },
        "material_property_values": values,
        "has_material_property_values": has_values,
        "texture": {
            "configuration": configuration,
            "state": "absent",
            "material_name": None,
        },
        "read_errors": dict(read_errors or {}),
    }


def _appearance_raw(component: str, scope: dict, *, claimed: bool = False):
    configuration = str(scope["referenced_configuration"])
    values = [0.2, 0.3, 0.4, 0.1, 0.2, 0.3, 0.4, 0.0, 0.0]
    return {
        "component": component,
        "source_document_path": scope["source_document_path"],
        "referenced_configuration": configuration,
        "native_active_configuration": configuration,
        "native_active_configuration_error": None,
        "document": _appearance_owner(
            configuration=configuration,
            member="IModelDocExtension.GetMaterialPropertyValues",
            values=values if claimed else None,
            has_values=claimed,
        ),
        "occurrence": _appearance_owner(
            configuration=configuration,
            member="IComponent2.GetMaterialPropertyValues2",
        ),
        "bodies": [
            {
                "component": component,
                "enumeration_api": "IComponent2.GetBodies2",
                "body_type": 0,
                "body_index": 0,
                "body_name": component,
                "appearance": _appearance_owner(
                    configuration=configuration,
                    member="IBody2.MaterialPropertyValues2",
                    active=True,
                ),
            }
        ],
    }


class CadMassAuthorityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="sw-cad-mass-"))
        self.assembly = support.make_cad_tree(self.tmp / "cad")
        self.backend = support.FixtureCadBackend(
            self.assembly,
            [
                {
                    "name": "base-1",
                    "transform": support.placement(),
                    "mass": support.mass_payload(1.0, (0.01, 0.02, 0.0)),
                },
                {"name": "arm-1", "transform": support.placement((0.0, 0.0, 0.2)), "mass": support.mass_payload(0.5)},
            ],
            coordinate_systems={
                "CS_base_link": support.placement(),
                "CS_arm_link": support.placement((0.0, 0.0, 0.2)),
            },
            dependencies=[self.tmp / "cad" / "base.SLDPRT", self.tmp / "cad" / "arm.SLDPRT"],
        )
        self.config = {
            "provider": "solidworks",
            "assembly": str(self.assembly),
            "configuration": "Default",
            "allowed_roots": [str(self.tmp / "cad")],
            "geometry": {"enabled": True},
            "coordinate_systems": ["CS_base_link", "CS_arm_link"],
            "bodies": [
                {
                    "id": "base",
                    "name": "base_link",
                    "components": ["base-1"],
                    "frame": {"coordinate_system": "CS_base_link"},
                },
                {
                    "id": "arm",
                    "name": "arm_link",
                    "components": ["arm-1"],
                    "frame": {"coordinate_system": "CS_arm_link"},
                },
            ],
            "joints": [],
        }

    def tearDown(self) -> None:
        support.cleanup(self.tmp)

    def _freeze(self, name: str) -> Path:
        snapshot = self.tmp / name
        freeze(dict(self.config), snapshot, backend=self.backend)
        return snapshot

    def _install_appearance(self, *, claimed: bool) -> None:
        capture_geometry = self.backend.capture_prepared_geometry

        def capture_with_appearance(source_scopes, on_activity=None):
            rows = capture_geometry(source_scopes, on_activity=on_activity)
            for component, row in rows.items():
                row["appearance_raw"] = _appearance_raw(
                    component,
                    row["source_scope"],
                    claimed=claimed and component == "arm-1",
                )
            return rows

        self.backend.capture_prepared_geometry = capture_with_appearance

    def test_manual_mass_configuration_is_rejected(self) -> None:
        for key, value in (
            ("documented_masses", {"arm-1": {"mass_kg": 0.5, "reason": "manual", "evidence": "table"}}),
            ("mass_evidence", {"reference": "manual", "file": "table.json", "sha256": "0" * 64}),
        ):
            with self.subTest(key=key), self.assertRaises(ConfigError):
                validate_source_config(dict(self.config, **{key: value}))
        with self.assertRaises(ConfigError):
            validate_source_config(dict(self.config, material_source="documented_table"))

    def test_freeze_preserves_unverified_cad_mass_and_records_warning(self) -> None:
        self.backend.components[1]["mass"]["reference"].update(
            part_document="C:/cad/arm.SLDPRT",
            scope="part_document",
            volume_m3=6.25e-5,
            density_kg_m3=7900.0,
            material_assignment={
                "schema_version": "swbridge.material-assignment/v1",
                "configuration": "默认",
                "unverified_reason": "cad_material_provenance_missing",
                "message": "Every solid requires an explicit physical material; default density is not evidence",
                "missing_body_indices": [0],
            },
        )
        snapshot = self._freeze("unverified")
        _compose_scene_inputs(snapshot, self.tmp / "cad")
        record = json.loads((snapshot / "raw/material_provenance.json").read_text(encoding="utf-8"))
        self.assertEqual([entry["code"] for entry in record["warnings"]], ["cad_material_provenance_missing"])
        self.assertFalse(record["warnings"][0]["blocking"])
        raw_rows = json.loads((snapshot / "raw/mass_properties.json").read_text(encoding="utf-8"))
        warning = record["warnings"][0]
        self.assertEqual(warning["material_assignment"], raw_rows["arm-1"]["reference"]["material_assignment"])
        self.assertEqual(warning["measured"]["mass_kg"], raw_rows["arm-1"]["mass"])
        scene = load_scene(snapshot)
        arm = next(link for link in scene["links"] if link["name"] == "arm_link")
        self.assertAlmostEqual(arm["inertial"]["mass"], 0.5, places=12)
        self.assertEqual(
            [entry["code"] for entry in scene["provenance"]["warnings"]],
            ["cad_material_provenance_missing"],
        )

    def test_verified_cad_material_has_no_warning_and_no_declared_mass_file(self) -> None:
        snapshot = self._freeze("verified")
        _compose_scene_inputs(snapshot, self.tmp / "cad")
        record = json.loads((snapshot / "raw/material_provenance.json").read_text(encoding="utf-8"))
        self.assertEqual(record["warnings"], [])
        self.assertFalse((snapshot / "raw/declared_masses.json").exists())
        scene = load_scene(snapshot)
        self.assertNotIn("warnings", scene["provenance"])

    def test_unobserved_appearance_keeps_the_warned_fallback_eligible(self) -> None:
        self._install_appearance(claimed=False)
        snapshot = self._freeze("appearance-unobserved")
        _compose_scene_inputs(snapshot, self.tmp / "cad")
        scene = load_scene(snapshot)
        visuals = [
            visual
            for link in scene["links"]
            for visual in link.get("visuals") or []
        ]
        self.assertTrue(visuals)
        self.assertTrue(
            all(
                visual["provenance"]["material_source"]
                == "platform_display_fallback"
                for visual in visuals
            )
        )

    def test_generate_rejects_claimed_appearance_without_effective_scope(self) -> None:
        self._install_appearance(claimed=True)
        snapshot = self._freeze("appearance-claimed")
        _compose_scene_inputs(snapshot, self.tmp / "cad")
        with self.assertRaises(AppearanceError) as caught:
            load_scene(snapshot)
        appearance = caught.exception.details["appearance"]
        self.assertEqual(appearance["component"], "arm-1")
        self.assertIn(
            "cad_appearance_effective_scope_unproven",
            {issue["code"] for issue in appearance["issues"]},
        )


if __name__ == "__main__":
    unittest.main()
