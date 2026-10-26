"""Freeze/load behaviour of the SolidWorks adapter, driven by a fixture host."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from . import _paths  # noqa: F401  (import side effect: sys.path)

from description_pipeline.sources.solidworks.errors import BridgeError, ConfigError  # noqa: E402
from description_pipeline.sources.solidworks.freeze import (  # noqa: E402
    _bind_source_scope_origins,
    _relative_document_name,
    _source_inputs,
)
from description_pipeline.sources.solidworks.scene import load_scene  # noqa: E402

from . import support  # noqa: E402


def _Backend(document, components, **kwargs):  # fixture plumbing: supported native datum names
    """The fixture backend carrying this module's supported native datum names."""

    kwargs.setdefault(
        "coordinate_systems",
        {
            "CS_base_link": support.placement(),
            "CS_arm_link": support.placement((0.0, 0.0, 0.2)),
            "TCP_imu": support.placement((0.01, 0.0, 0.03)),
        },
    )
    return support.FixtureCadBackend(document, components, **kwargs)


def freeze(config, *args, **kwargs):  # noqa: F811 - fixture adapter for the request sets
    """Capture one prepared fixture through the production prepared-Capture entry."""

    if len(args) > 1:
        raise TypeError("fixture capture accepts at most one destination")
    backend = kwargs.pop("backend", None)
    if backend is None:
        raise TypeError("fixture capture requires a backend")
    destination = Path(args[0] if args else kwargs.pop("destination"))
    return support.capture_prepared_fixture(
        config,
        destination,
        backend=backend,
        worker_version=kwargs.pop("worker_version", "unknown"),
        on_activity=kwargs.pop("on_activity", None),
    )


def _native_record(*, with_arm: bool, with_joint: bool, files: dict[str, str]) -> dict:
    """The synthetic discovery record matching this module's freeze fixture.

    Carriers use the supported native semantics: a body owns its ``CS_<link>``
    datum, the released interface datum uses the ``TCP_`` prefix, and the
    hinge joint is a solved concentric+coincident mate pair whose name and
    limits travel through ``dp.joint.*`` claims and controlled records.
    """

    components = [support.native_component("base-1", document="base.SLDPRT")]
    datums = [
        support.native_datum("CS_base_link", "base-1", support.placement()),
        support.native_datum("TCP_imu", "base-1", support.placement((0.01, 0.0, 0.03))),
    ]
    masses = [{"component": "base-1", "mass_kg": 1.0, "material": "Alloy Steel"}]
    mates: list[dict] = []
    joint_claims = None
    if with_arm:
        components.append(
            support.native_component(
                "arm-1", document="arm.SLDPRT", transform=support.placement((0.0, 0.0, 0.2)), fixed=False
            )
        )
        datums.append(support.native_datum("CS_arm_link", "arm-1", support.placement((0.0, 0.0, 0.2))))
        masses.append({"component": "arm-1", "mass_kg": 0.5, "material": "Alloy Steel"})
    if with_joint:
        mates = support.native_hinge_mates(
            "base-1", "arm-1", left_point=(0.0, 0.0, 0.2), right_point=(0.0, 0.0, 0.0), radius=0.006
        )
        mates.append(support.native_limit_mate("base-1", "arm-1", point=(0.0, 0.0, 0.2)))
        joint_claims = support.native_joint_claims()
    return support.native_record(
        hardware_id="freeze_fixture",
        change_summary="freeze module fixture",
        control_reference="freeze-run",
        components=components,
        datums=datums,
        masses=masses,
        mates=mates,
        joint_claims=joint_claims,
        files=files,
    )


def _compose_scene_inputs(destination: Path, cad_root: Path, *, with_arm: bool, with_joint: bool) -> None:
    """Seal the consumer inputs the Linux scene rebuild reads (fixture plumbing)."""

    names = ["robot.SLDASM", "base.SLDPRT"] + (["arm.SLDPRT"] if with_arm else [])
    files = support.native_files(cad_root, names)
    record = _native_record(with_arm=with_arm, with_joint=with_joint, files=files)
    controlled = {"budget.json": support.native_budget_record(record)}
    record_keys = None
    if with_joint:
        controlled["joints/hinge.json"] = support.native_joint_record()
        record_keys = {"joints/hinge.json": ("limits", "drive")}
    support.seal_scene_inputs(
        destination,
        cad_root=cad_root,
        cad_names=names,
        record=record,
        controlled=controlled,
        record_keys=record_keys,
    )


class FreezeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="sw-freeze-"))
        self.assembly = support.make_cad_tree(self.tmp / "cad")
        self.parts = [self.tmp / "cad" / "base.SLDPRT", self.tmp / "cad" / "arm.SLDPRT"]
        self.backend = _Backend(
            self.assembly,
            [
                {
                    "name": "base-1",
                    "transform": support.placement((0.0, 0.0, 0.0)),
                    "mass": support.mass_payload(1.0, (0.0, 0.0, 0.0)),
                },
                {
                    "name": "arm-1",
                    "transform": support.placement((0.0, 0.0, 0.2)),
                    # Off-axis COM: the group's product signal must be able to
                    # prove the product convention at capture time (the z-only
                    # COM of the historical fixture cannot).
                    "mass": support.mass_payload(0.5, (0.01, 0.02, 0.05)),
                },
            ],
            dependencies=self.parts,
        )
        self.config = {
            "provider": "solidworks",
            "assembly": str(self.assembly),
            "configuration": "Default",
            "allowed_roots": [str(self.tmp / "cad")],
            "geometry": {"enabled": True, "format": "stl_binary"},
            "coordinate_systems": ["CS_base_link", "CS_arm_link", "TCP_imu"],
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
            "joints": [
                {
                    "id": "hinge",
                    "name": "hinge_joint",
                    "type": "revolute",
                    "parent": "base_link",
                    "child": "arm_link",
                    "axis": [0.0, 0.0, 1.0],
                    "limits": {"lower": -1.0, "upper": 1.0, "effort": 2.0, "velocity": 3.0},
                }
            ],
            "frames": [{"id": "imu", "parent": "base_link", "coordinate_system": "TCP_imu"}],
        }
        self.destination = self.tmp / "snapshot"

    def tearDown(self) -> None:
        support.cleanup(self.tmp)

    def test_source_scopes_bind_shared_copy_without_instance_rows(self) -> None:
        source_root = self.tmp / "collected"
        source_root.mkdir()
        source = source_root / "Part.SLDPRT"
        source.write_bytes(b"frozen-copy")
        copy_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
        original_sha256 = hashlib.sha256(b"original-source").hexdigest()
        source_path = r"C:\source\Part.SLDPRT"
        template = {
            "rule_version": "native-open-edge-scope-binding-v1",
            "component": "part-1",
            "source_document_path": str(source),
            "source_copy_path": "source/Part.SLDPRT",
            "source_document_sha256": copy_sha256,
            "source_copy_sha256": copy_sha256,
            "source_document_identity_sha256": copy_sha256,
            "source_document_identity_kind": "file_sha256",
            "referenced_configuration": "Default",
            "occurrence_scope": "part-1",
            "virtual_owner": None,
            "virtual_owner_sha256": None,
            "virtual_session_document_path": None,
            "virtual_session_temp_root": None,
            "virtual_native_api_pid": None,
        }
        scopes = {
            "part-1": template,
            "part-2": dict(
                template,
                component="part-2",
                occurrence_scope="part-2",
            ),
        }
        closure = {
            "mapping": [
                {
                    "instance": "part-1",
                    "copy": "source/Part.SLDPRT",
                    "copy_sha256": copy_sha256,
                    "source": source_path,
                    "source_sha256": copy_sha256,
                }
            ],
            "original_files": {source_path: original_sha256},
            "copy_files": {"source/Part.SLDPRT": copy_sha256},
        }

        bound = _bind_source_scope_origins(scopes, closure, source_root)

        self.assertEqual(set(bound), {"part-1", "part-2"})
        for scope in bound.values():
            self.assertEqual(
                scope["source_document_sha256"],
                original_sha256,
            )
            self.assertEqual(
                scope["source_document_identity_sha256"],
                original_sha256,
            )
            self.assertEqual(scope["source_copy_sha256"], copy_sha256)

    def test_source_scopes_do_not_match_same_basename_elsewhere(self) -> None:
        source_root = self.tmp / "collected"
        first = source_root / "first" / "Part.SLDPRT"
        second = source_root / "second" / "Part.SLDPRT"
        first.parent.mkdir(parents=True)
        second.parent.mkdir(parents=True)
        first.write_bytes(b"first")
        second.write_bytes(b"second")
        first_sha256 = hashlib.sha256(first.read_bytes()).hexdigest()
        second_sha256 = hashlib.sha256(second.read_bytes()).hexdigest()
        original_sha256 = hashlib.sha256(b"original").hexdigest()
        scope = {
            "rule_version": "native-open-edge-scope-binding-v1",
            "component": "part-1",
            "source_document_path": str(first),
            "source_copy_path": "source/first/Part.SLDPRT",
            "source_document_sha256": first_sha256,
            "source_copy_sha256": first_sha256,
            "source_document_identity_sha256": first_sha256,
            "source_document_identity_kind": "file_sha256",
            "referenced_configuration": "Default",
            "occurrence_scope": "part-1",
            "virtual_owner": None,
            "virtual_owner_sha256": None,
            "virtual_session_document_path": None,
            "virtual_session_temp_root": None,
            "virtual_native_api_pid": None,
        }
        closure = {
            "mapping": [
                {
                    "instance": "part-1",
                    "copy": "source/second/Part.SLDPRT",
                    "copy_sha256": second_sha256,
                    "source": r"C:\source\Part.SLDPRT",
                    "source_sha256": second_sha256,
                }
            ],
            "original_files": {
                r"C:\source\Part.SLDPRT": original_sha256
            },
            "copy_files": {
                "source/first/Part.SLDPRT": first_sha256,
                "source/second/Part.SLDPRT": second_sha256,
            },
        }

        with self.assertRaises(BridgeError):
            _bind_source_scope_origins(
                {"part-1": scope},
                closure,
                source_root,
            )

    def test_freeze_records_the_direct_link_group_read(self) -> None:
        freeze(self.config, self.destination, backend=self.backend)
        _compose_scene_inputs(self.destination, self.tmp / "cad", with_arm=True, with_joint=True)
        record = json.loads(
            (self.destination / "raw" / "link_group_mass_properties.json").read_text(encoding="utf-8")
        )
        self.assertEqual(record["schema_version"], "solidworks-to-urdf.link-group-mass-properties/v1")
        self.assertEqual(record["selection_rule"], "top_most_providers+uncovered_leaves")
        self.assertEqual(sorted(record["links"]), ["arm_link", "base_link"])
        base = record["links"]["base_link"]
        self.assertEqual(base["selection"], ["base-1"])
        self.assertEqual(len(base["reads"]), 2)
        self.assertAlmostEqual(base["reads"][0]["mass_kg"], 1.0, places=12)
        self.assertEqual(
            base["reproducibility"], {"mass_delta_kg": 0.0, "com_delta_norm_m": 0.0, "tensor_delta_max_abs": 0.0}
        )
        self.assertFalse((self.destination / "reports" / "diagnostics").exists())
        scene = load_scene(self.destination)
        link = next(row for row in scene["links"] if row["name"] == "base_link")
        self.assertAlmostEqual(link["inertial"]["mass"], 1.0, places=12)
        self.assertEqual(link["provenance"]["mass_properties"]["direct"]["selection"], ["base-1"])
        self.assertAlmostEqual(
            link["provenance"]["mass_properties"]["direct_vs_recombination"]["mass_delta_kg"], 0.0, places=12
        )

    def test_freeze_fails_closed_when_the_link_group_payload_echo_differs(self) -> None:
        self.backend.link_group_selection_override = {"base_link": ["arm-1"]}
        with self.assertRaises(BridgeError) as caught:
            freeze(self.config, self.destination, backend=self.backend)
        self.assertEqual(caught.exception.code, "cad_link_group_payload_invalid")
        self.assertEqual(caught.exception.detail["body"], "base_link")
        self.assertEqual(caught.exception.detail["expected"], ["base-1"])
        self.assertEqual(caught.exception.detail["received"], ["arm-1"])
        failure = self.destination.with_name(
            self.destination.name + ".failed-001"
        )
        observations = json.loads(
            (failure / "native_observations.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            observations["scene"]["link_group_mass_raw"]["links"]["base_link"][
                "selection"
            ],
            ["arm-1"],
        )

    def test_freeze_produces_a_verified_snapshot(self) -> None:
        manifest = freeze(self.config, self.destination, backend=self.backend, worker_version="test-worker")
        self.assertEqual(manifest["kind"], "solidworks")
        # the fixture backend must never be published as native CAD evidence
        self.assertEqual(manifest["evidence_class"], "fixture")
        self.assertTrue((self.destination / "manifest.json").is_file())
        self.assertFalse((self.destination / "scene.json").exists())
        self.assertEqual(manifest["scene"], "raw/scene_raw.json")
        # The full raw/source_config.json carrier is retired (356baed): the
        # snapshot binds its captured identity and keeps raw readings only.
        self.assertNotIn("raw/source_config.json", manifest["files"])
        self.assertEqual(manifest["identity"]["capture"]["evidence_class"], "fixture")
        self.assertIn("raw/material_provenance.json", manifest["files"])
        self.assertIn("raw/scene_raw.json", manifest["files"])
        self.assertTrue(any(name.startswith("geometry/") for name in manifest["files"]))

    def test_capture_retains_auxiliary_appearance_in_native_facts(self) -> None:
        appearance = {
            "component": "base-1",
            "document": {
                "material_property_values": [-1.0] * 9,
                "has_material_property_values": False,
                "read_errors": {},
            },
            "bodies": [
                {
                    "body_type": 0,
                    "body_index": 0,
                    "faces": [
                        {
                            "face_index": 0,
                            "appearance": {
                                "material_property_values": None,
                                "has_material_property_values": False,
                                "texture": {
                                    "configuration": "Default",
                                    "state": "absent",
                                    "material_name": None,
                                },
                                "read_errors": {},
                            },
                        }
                    ],
                }
            ],
        }
        capture_geometry = self.backend.capture_prepared_geometry

        def geometry_with_appearance(source_scopes, on_activity=None):
            rows = capture_geometry(source_scopes, on_activity=on_activity)
            component = next(iter(rows))
            appearance["component"] = component
            rows[component]["appearance_raw"] = appearance
            return rows

        self.backend.capture_prepared_geometry = geometry_with_appearance
        freeze(self.config, self.destination, backend=self.backend)
        facts = json.loads((self.destination / "raw/native_capture_facts.json").read_text())
        occurrence = next(
            row for row in facts["scene_raw"]["occurrences"]
            if row["name"] == appearance["component"]
        )
        self.assertEqual(occurrence["appearance_raw"], appearance)
        geometry = json.loads((self.destination / "raw/geometry.json").read_text())
        self.assertTrue(all("appearance_raw" not in row for row in geometry))

    def test_capture_never_builds_a_scene_and_linux_derivation_keeps_snapshot_immutable(self) -> None:
        from description_pipeline.io import inventory

        with (
            patch(
                "description_pipeline.sources.solidworks.scene.build_scene",
                side_effect=AssertionError("scene construction is portable work"),
            ),
        ):
            freeze(self.config, self.destination, backend=self.backend)
        _compose_scene_inputs(self.destination, self.tmp / "cad", with_arm=True, with_joint=True)
        before = inventory(self.destination)
        scene = load_scene(self.destination)
        self.assertEqual(scene["schema_version"], "solidworks-to-urdf.engineering-model/v1")
        self.assertEqual(inventory(self.destination), before)
        self.assertNotIn("scene.json", before)

    def test_fixture_derivation_keeps_bound_metadata_provenance(self) -> None:
        freeze(self.config, self.destination, backend=self.backend)
        _compose_scene_inputs(self.destination, self.tmp / "cad", with_arm=True, with_joint=True)

        manifest = json.loads((self.destination / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["evidence_class"], "fixture")
        self.assertIn("native-runtime.json", manifest["files"])

        scene = load_scene(self.destination)
        metadata = [
            warning
            for warning in scene["provenance"]["value_warnings"]
            if warning["code"] == "metadata.non_native"
        ]
        self.assertEqual(len(metadata), 1)
        self.assertTrue(metadata[0]["objects"])

    def test_fixture_derivation_rejects_partial_metadata_evidence(self) -> None:
        from description_pipeline.io import PipelineError
        from description_pipeline.sources.snapshot import write_manifest

        for missing in ("native-runtime.json",):
            with self.subTest(missing=missing):
                destination = self.tmp / ("partial-" + missing.replace("/", "-"))
                freeze(self.config, destination, backend=self.backend)
                _compose_scene_inputs(destination, self.tmp / "cad", with_arm=True, with_joint=True)
                manifest = json.loads((destination / "manifest.json").read_text(encoding="utf-8"))
                (destination / missing).unlink()
                write_manifest(
                    destination,
                    kind=manifest["kind"],
                    identity=manifest["identity"],
                    evidence_class=manifest["evidence_class"],
                )

                with self.assertRaisesRegex(PipelineError, "not bound by the manifest"):
                    load_scene(destination)

    def test_resealed_source_configuration_cannot_change_capture_identity(self) -> None:
        # The sealed capture identity replaced the retired raw/source_config.json
        # carrier (356baed): re-sealing a tampered assembly identity still refuses.
        from description_pipeline.io import read_data
        from description_pipeline.sources.snapshot import write_manifest

        freeze(self.config, self.destination, backend=self.backend)
        _compose_scene_inputs(self.destination, self.tmp / "cad", with_arm=True, with_joint=True)
        manifest = read_data(self.destination / "manifest.json")
        identity = dict(manifest["identity"])
        identity["assembly"] = "another-assembly.SLDASM"
        write_manifest(
            self.destination,
            kind=manifest["kind"],
            identity=identity,
            evidence_class=manifest["evidence_class"],
        )
        with self.assertRaisesRegex(ValueError, "names another assembly"):
            load_scene(self.destination)

    def test_resealed_duplicate_native_readings_cannot_disagree(self) -> None:
        from description_pipeline.io import write_json
        from description_pipeline.sources.snapshot import write_manifest

        manifest = freeze(self.config, self.destination, backend=self.backend)
        _compose_scene_inputs(self.destination, self.tmp / "cad", with_arm=True, with_joint=True)
        path = self.destination / "raw/mass_properties.json"
        masses = json.loads(path.read_text(encoding="utf-8"))
        masses["base-1"]["mass"] *= 2
        write_json(path, masses)
        write_manifest(
            self.destination,
            kind="solidworks",
            identity=manifest["identity"],
            evidence_class=manifest["evidence_class"],
        )
        with self.assertRaisesRegex(ValueError, "dedicated raw records"):
            load_scene(self.destination)

    def test_scene_carries_combined_mass_and_geometry(self) -> None:
        freeze(self.config, self.destination, backend=self.backend, worker_version="test-worker")
        _compose_scene_inputs(self.destination, self.tmp / "cad", with_arm=True, with_joint=True)
        scene = load_scene(self.destination)
        self.assertEqual(scene["schema_version"], "solidworks-to-urdf.engineering-model/v1")
        self.assertEqual(scene["units"], "SI")
        links = {link["name"]: link for link in scene["links"]}
        self.assertEqual(sorted(links), ["arm_link", "base_link"])
        self.assertAlmostEqual(links["base_link"]["inertial"]["mass"], 1.0, places=9)
        self.assertAlmostEqual(links["arm_link"]["inertial"]["mass"], 0.5, places=9)
        # the part sits at assembly z=0.2 with its COM at +0.05 in part axes, and
        # the arm_link frame is declared at assembly z=0.2: the COM is +0.05 m in
        # link axes, i.e. the numbers are re-expressed, not copied
        self.assertAlmostEqual(links["arm_link"]["inertial"]["xyz"][2], 0.05, places=9)
        self.assertEqual(len(links["arm_link"]["visuals"]), 1)
        self.assertEqual(scene["joints"][0]["axis"], [0.0, 0.0, 1.0])
        self.assertEqual(scene["frames"][0]["name"], "imu_tcp")
        self.assertEqual(scene["provenance"]["snapshot"]["evidence_class"], "fixture")

    def test_link_frame_rotation_is_applied_to_com_and_inertia(self) -> None:
        config = json.loads(json.dumps(self.config))
        # rotate the arm link frame 90 degrees about Z: the COM rotates with it,
        # and the inertia tensor has to be re-expressed in the rotated axes
        config["bodies"][1]["frame"] = {"coordinate_system": "CS_arm_link"}
        backend = _Backend(
            self.assembly,
            [
                {
                    "name": "base-1",
                    "transform": support.placement(),
                    # Off-axis COM: the product signal proves the convention at capture time.
                    "mass": support.mass_payload(1.0, (0.01, 0.02, 0.0)),
                },
                {
                    "name": "arm-1",
                    "transform": support.placement((0.1, 0.0, 0.0)),
                    "mass": support.mass_payload(
                        0.5, (0.1, 0.0, 0.0), [[0.002, 0.0, 0.0], [0.0, 0.001, 0.0], [0.0, 0.0, 0.001]]
                    ),
                },
            ],
            dependencies=self.parts,
        )
        backend.coordinate_system_matrices["CS_arm_link"] = support.placement(
            (-0.2, 0.0, 0.0), (0.0, 0.0, 1.5707963267948966)
        )
        freeze(config, self.destination, backend=backend)
        _compose_scene_inputs(self.destination, self.tmp / "cad", with_arm=True, with_joint=True)
        scene = load_scene(self.destination)
        arm = next(link for link in scene["links"] if link["name"] == "arm_link")
        inertial = arm["inertial"]
        # part COM at assembly (0.2, 0, 0); the link frame sits at assembly
        # (-0.2, 0, 0) and is rotated 90 deg about Z, so the 0.4 m offset lands
        # on -y in link axes - the numbers are re-expressed, not copied
        self.assertAlmostEqual(inertial["xyz"][0], 0.0, places=9)
        self.assertAlmostEqual(inertial["xyz"][1], -0.4, places=9)
        self.assertAlmostEqual(inertial["xyz"][2], 0.0, places=9)
        # the smallest principal axis follows the rotation: iyy < ixx afterwards
        self.assertAlmostEqual(inertial["inertia"][0], 0.001, places=9)  # ixx
        self.assertAlmostEqual(inertial["inertia"][3], 0.002, places=9)  # iyy

    def test_existing_empty_destination_is_used(self) -> None:
        self.destination.mkdir(parents=True)

        manifest = freeze(self.config, self.destination, backend=self.backend)

        self.assertEqual(manifest["kind"], "solidworks")
        self.assertTrue((self.destination / "manifest.json").is_file())
        self.assertTrue((self.destination / "raw/scene_raw.json").is_file())

    def test_non_empty_destination_is_refused(self) -> None:
        self.destination.mkdir(parents=True)
        leftover = self.destination / "leftover.txt"
        leftover.write_text("x", encoding="utf-8")

        with self.assertRaises(BridgeError) as raised:
            freeze(self.config, self.destination, backend=self.backend)

        self.assertEqual(raised.exception.code, "destination_not_empty")
        self.assertEqual(leftover.read_text(encoding="utf-8"), "x")

    def test_symlinked_destination_is_refused(self) -> None:
        real = self.tmp / "real-target"
        real.mkdir()
        link = self.tmp / "linked"
        link.symlink_to(real)

        with self.assertRaises(BridgeError) as raised:
            freeze(self.config, link, backend=self.backend)

        self.assertEqual(raised.exception.code, "destination_is_symlink")

    def test_dependency_outside_the_prepared_package_blocks_capture(self) -> None:
        outside = self.tmp / "outside.SLDPRT"
        outside.write_bytes(b"outside")
        backend = _Backend(
            self.assembly,
            [{"name": "base-1", "transform": support.placement(), "mass": support.mass_payload(1.0)}],
            dependencies=[outside],
        )

        with self.assertRaises(BridgeError) as raised:
            freeze(self.config, self.destination, backend=backend)

        self.assertEqual(raised.exception.code, "prepared_source_outside_root")
        self.assertEqual(raised.exception.detail["outside"], [str(outside)])
        self.assertFalse(self.destination.exists())

    def test_missing_configuration_is_refused(self) -> None:
        config = dict(self.config, configuration="Nope")

        with self.assertRaises(BridgeError) as raised:
            freeze(config, self.destination, backend=self.backend)

        self.assertEqual(raised.exception.code, "cad_prepared_capture_state")

    def test_authored_origin_is_not_a_kinematic_carrier(self) -> None:
        # The direct-config authored origin carrier is retired: the delivered
        # kinematics come from the sealed native record (groups/axis_requests),
        # so a stray xyz neither refuses the capture nor reaches the scene.
        config = json.loads(json.dumps(self.config))
        config["joints"][0]["xyz"] = [0.0, 0.0, 0.2]

        freeze(config, self.destination, backend=self.backend)
        _compose_scene_inputs(self.destination, self.tmp / "cad", with_arm=True, with_joint=True)
        scene = load_scene(self.destination)
        self.assertEqual(scene["joints"][0]["name"], "hinge_joint")
        self.assertEqual(scene["joints"][0]["axis"], [0.0, 0.0, 1.0])

    def test_tampered_snapshot_is_rejected(self) -> None:
        freeze(self.config, self.destination, backend=self.backend)
        target = self.destination / "raw/scene_raw.json"
        payload = json.loads(target.read_text(encoding="utf-8"))
        payload["name"] = "tampered"
        target.write_text(json.dumps(payload), encoding="utf-8")

        with self.assertRaises(ValueError):
            load_scene(self.destination)

    def test_extra_file_is_rejected(self) -> None:
        freeze(self.config, self.destination, backend=self.backend)
        (self.destination / "unused.txt").write_text("nope", encoding="utf-8")

        with self.assertRaises(ValueError):
            load_scene(self.destination)

    def _two_component_backend(self) -> tuple[support.FixtureCadBackend, dict]:
        """A readable two-component model whose components are real files."""

        backend = _Backend(
            self.assembly,
            [
                {
                    "name": "base-1",
                    "path": str(self.parts[0]),
                    "transform": support.placement(),
                    # Off-axis COM: the product signal proves the convention at capture time.
                    "mass": support.mass_payload(1.0, (0.01, 0.02, 0.0)),
                },
                {
                    "name": "arm-1",
                    "path": str(self.parts[1]),
                    "transform": support.placement((0.0, 0.0, 0.2)),
                    "mass": support.mass_payload(0.5),
                },
            ],
            dependencies=self.parts,
        )
        config = json.loads(json.dumps(self.config))
        config["bodies"] = [
            {
                "id": "base",
                "name": "base_link",
                "components": ["base-1"],
                "frame": {"coordinate_system": "CS_base_link"},
            },
            {"id": "arm", "name": "arm_link", "components": ["arm-1"], "frame": {"coordinate_system": "CS_arm_link"}},
        ]
        return backend, config

    def test_prepared_copy_changing_during_capture_blocks_freeze(self) -> None:
        backend, config = self._two_component_backend()
        backend.originals_change_during_capture = True

        with self.assertRaises(BridgeError) as raised:
            freeze(config, self.destination, backend=backend)

        self.assertEqual(raised.exception.code, "cad_copy_changed")
        self.assertFalse(self.destination.exists())

    def test_dependency_digest_identifies_the_source_not_the_job_directory(self) -> None:
        """The input identity must survive where the snapshot happened to be assembled."""

        backend, config = self._two_component_backend()
        first = freeze(config, self.destination, backend=backend)
        second_destination = self.tmp / "second-snapshot"
        second_backend, second_config = self._two_component_backend()
        second = freeze(second_config, second_destination, backend=second_backend)

        self.assertEqual(first["identity"]["dependency_digest"], second["identity"]["dependency_digest"])
        first_capture = json.loads((self.destination / "evidence" / "collection.json").read_text(encoding="utf-8"))
        second_capture = json.loads((second_destination / "evidence" / "collection.json").read_text(encoding="utf-8"))
        hashes = first_capture["capture"]["source_hashes"]
        self.assertEqual(sorted(hashes), ["arm.sldprt", "base.sldprt", "robot.sldasm"])
        self.assertEqual(hashes, second_capture["capture"]["source_hashes"])
        self.assertNotIn(str(self.tmp), json.dumps(hashes))

        # and the same names with one changed byte are a different input
        self.parts[0].write_bytes(b"fixture part changed after the first capture")
        third_destination = self.tmp / "third-snapshot"
        third_backend, third_config = self._two_component_backend()
        third = freeze(third_config, third_destination, backend=third_backend)
        self.assertNotEqual(first["identity"]["dependency_digest"], third["identity"]["dependency_digest"])

    def test_source_input_names_are_relative_and_host_independent(self) -> None:
        """A Windows snapshot has to re-derive the same names on a POSIX verifier.

        ``os.path.relpath`` only splits the host's separators and raises for Windows paths on
        different drives, so the capture parses them itself.
        """

        root = "C:\\models\\robot"
        self.assertEqual(_relative_document_name("C:\\models\\robot\\robot.SLDASM", root), "robot.SLDASM")
        self.assertEqual(_relative_document_name("C:\\models\\robot\\parts\\arm.SLDPRT", root), "parts/arm.SLDPRT")
        self.assertEqual(_relative_document_name("C:\\models\\library\\arm.SLDPRT", root), "../library/arm.SLDPRT")
        self.assertEqual(_relative_document_name("D:\\library\\arm.SLDPRT", root), "volume-d/library/arm.SLDPRT")
        self.assertEqual(_relative_document_name("c:/models/robot/parts/arm.SLDPRT", root), "parts/arm.SLDPRT")
        self.assertEqual(
            _relative_document_name("/srv/models/robot/parts/arm.SLDPRT", "/srv/models/robot"), "parts/arm.SLDPRT"
        )

    def test_source_inputs_bind_names_and_bytes_not_locations(self) -> None:
        """The same layout and bytes on another machine or drive is the same input."""

        windows = _source_inputs(
            {"assembly": "C:\\models\\robot\\robot.SLDASM"},
            {
                "original_files": {
                    "C:\\models\\robot\\robot.SLDASM": "a" * 64,
                    "C:\\models\\robot\\arm.SLDPRT": "b" * 64,
                }
            },
        )
        other_machine = _source_inputs(
            {"assembly": "D:\\work\\tree\\robot.SLDASM"},
            {"original_files": {"D:\\work\\tree\\robot.SLDASM": "a" * 64, "D:\\work\\tree\\arm.SLDPRT": "b" * 64}},
        )
        self.assertEqual(windows, other_machine)
        self.assertEqual(sorted(windows), ["arm.sldprt", "robot.sldasm"])
        changed = _source_inputs(
            {"assembly": "C:\\models\\robot\\robot.SLDASM"},
            {
                "original_files": {
                    "C:\\models\\robot\\robot.SLDASM": "a" * 64,
                    "C:\\models\\robot\\arm.SLDPRT": "c" * 64,
                }
            },
        )
        self.assertNotEqual(windows, changed)

    def test_one_document_spelled_two_ways_is_one_input(self) -> None:
        """The model's spelling and SolidWorks' spelling are the same document."""

        inputs = _source_inputs(
            {"assembly": "c:\\models\\robot\\ROBOT.SLDASM"},
            {
                "original_files": {
                    "C:\\models\\robot\\robot.SLDASM": "a" * 64,
                    "c:/models/robot/ROBOT.SLDASM": "a" * 64,
                }
            },
        )
        self.assertEqual(inputs, {"robot.sldasm": "a" * 64})

    def test_two_documents_with_one_relative_name_fail_closed(self) -> None:
        """A duplicate name is refused, never silently collapsed into one entry."""

        with self.assertRaises(BridgeError) as raised:
            _source_inputs(
                {"assembly": "C:\\models\\robot\\robot.SLDASM"},
                {
                    "original_files": {
                        "C:\\models\\robot\\arm.SLDPRT": "a" * 64,
                        "c:/models/robot/ARM.SLDPRT": "b" * 64,
                    }
                },
            )
        self.assertEqual(raised.exception.code, "dependency_identity_ambiguous")
        detail = raised.exception.detail
        self.assertIsInstance(detail, dict)
        self.assertEqual(detail["name"], "arm.sldprt")  # type: ignore[index]

    def test_requesting_a_configuration_that_is_not_active_blocks_freeze(self) -> None:
        backend = _Backend(
            self.assembly,
            [
                {
                    "name": "base-1",
                    "path": str(self.parts[0]),
                    "transform": support.placement(),
                    "mass": support.mass_payload(1.0),
                },
                {
                    "name": "arm-1",
                    "path": str(self.parts[1]),
                    "transform": support.placement((0.0, 0.0, 0.2)),
                    "mass": support.mass_payload(0.5),
                },
            ],
            dependencies=self.parts,
            configurations=("Default", "Other"),
        )
        config = json.loads(json.dumps(self.config))
        # the configuration exists in the document, but Default is the active one:
        # switching the user's document is not this adapter's job
        config["configuration"] = "Other"
        config["bodies"] = [
            {
                "id": "base",
                "name": "base_link",
                "components": ["base-1"],
                "frame": {"coordinate_system": "CS_base_link"},
            },
            {"id": "arm", "name": "arm_link", "components": ["arm-1"], "frame": {"coordinate_system": "CS_arm_link"}},
        ]

        with self.assertRaises(BridgeError) as raised:
            freeze(config, self.destination, backend=backend)

        self.assertEqual(raised.exception.code, "cad_prepared_capture_state")
        detail = raised.exception.detail
        self.assertIsInstance(detail, dict)
        self.assertEqual(detail["configuration"], "Other")
        self.assertEqual(
            detail["state"]["active_configuration"],
            "Default",
        )

    def test_failed_capture_keeps_its_collected_copy_and_a_record(self) -> None:
        backend, config = self._two_component_backend()
        backend.scene_document_override = str(self.assembly)

        with self.assertRaises(BridgeError):
            freeze(config, self.destination, backend=backend)

        failure = self.destination.with_name(self.destination.name + ".failed-001")
        self.assertTrue(failure.is_dir())
        record = json.loads((failure / "failure.json").read_text(encoding="utf-8"))
        self.assertEqual(record["code"], "capture_source_mismatch")
        self.assertEqual(record["stage"], "scene_projection")
        self.assertEqual(record["request"]["assembly"], str(self.assembly))
        self.assertEqual(record["request"]["configuration"], "Default")
        # the material that explains the failure survives: the collector's copy
        collected = failure / "partial" / "source" / self.assembly.name
        self.assertTrue(collected.is_file())
        self.assertTrue((failure / "partial" / "source" / self.parts[0].name).is_file())
        # ... and nothing was delivered
        self.assertFalse(self.destination.exists())

    def test_geometry_failure_retains_environment_without_masking_original_error(self) -> None:
        from description_pipeline.sources.solidworks.errors import CadError

        backend, config = self._two_component_backend()
        sessions = {"copy": {"pid": 42, "executable": "sldworks.exe", "ownership": "windows_job"}}
        backend.environment = lambda: {"revision": "34.0.0", "sessions": sessions}

        def unavailable_geometry(*args, **kwargs):
            raise CadError("cad_body_faces_unreadable", "native faces unavailable", {"api": "IBody2.GetFaces"})

        backend.capture_prepared_geometry = unavailable_geometry
        with self.assertRaises(CadError) as caught:
            freeze(config, self.destination, backend=backend)

        self.assertEqual(caught.exception.code, "cad_body_faces_unreadable")
        failure = self.destination.with_name(self.destination.name + ".failed-001")
        record = json.loads((failure / "failure.json").read_text())
        self.assertEqual(record["stage"], "geometry_facts")
        self.assertEqual(record["detail"]["api"], "IBody2.GetFaces")
        environment = json.loads((failure / "partial/evidence/environment.json").read_text())
        self.assertEqual(environment["solidworks"]["sessions"], sessions)
        self.assertFalse(self.destination.exists())

    def test_failed_readings_retain_exact_native_member_and_original_rpc_cause(self) -> None:
        from description_pipeline.sources.solidworks.errors import CadError
        from description_pipeline.sources.solidworks.native import _member, _method

        class RpcError(Exception):
            hresult = -2147023130

        class NoRepresentation:
            def __repr__(self):
                raise AssertionError("diagnostics must not inspect COM arguments")

        class Unreadable:
            def __init__(self):
                self.attempts = []

            @property
            def ConfigurationManager(self):
                self.attempts.append("property")
                raise RpcError("RPC failed at http://user:secret@192.0.2.10/api?token=abc")

            def ReadValue(self, argument):
                self.attempts.append("method")
                raise RpcError("RPC failed at http://user:secret@192.0.2.10/api?token=abc")

        for reader, name, arguments in (
            (_member, "ConfigurationManager", ()),
            (_method, "ReadValue", (NoRepresentation(),)),
        ):
            with self.subTest(reader=reader.__name__):
                backend, config = self._two_component_backend()
                native = Unreadable()

                def fail_reading(*args, reader=reader, native=native, member=name, arguments=arguments, **kwargs):
                    try:
                        reader(native, member, *arguments)
                    except RpcError as cause:
                        raise CadError("cad_source_state_unreadable", "native source unreadable") from cause

                backend.collect_scene = fail_reading
                destination = self.tmp / reader.__name__
                with self.assertRaises(CadError) as caught:
                    freeze(config, destination, backend=backend)

                self.assertEqual(caught.exception.code, "cad_source_state_unreadable")
                self.assertEqual(len(native.attempts), 1)
                failure = destination.with_name(destination.name + ".failed-001")
                payload = (failure / "failure.json").read_text()
                record = json.loads(payload)
                self.assertEqual(record["stage"], "readings")
                self.assertEqual([item["type"] for item in record["exceptions"]], ["CadError", "RpcError"])
                rpc = record["exceptions"][1]
                self.assertEqual(rpc["hresult"], -2147023130)
                site = next(item for item in rpc["frames"] if item["function"] == reader.__name__)
                self.assertEqual(site["member"], name)
                self.assertEqual(site["module"], "description_pipeline.sources.solidworks.native")
                self.assertGreater(site["line"], 0)
                self.assertNotIn("secret", payload)
                self.assertNotIn("token=abc", payload)
                self.assertTrue((failure / "partial/source" / self.assembly.name).is_file())
                self.assertFalse(destination.exists())

    def test_geometry_batch_cannot_omit_or_substitute_an_occurrence(self) -> None:
        for change in ("omit", "substitute"):
            with self.subTest(change=change):
                backend, config = self._two_component_backend()
                original = backend.capture_prepared_geometry

                def changed_result(source_scopes, on_activity=None, original=original, change=change):
                    entries = original(source_scopes, on_activity=on_activity)
                    name = next(iter(entries))
                    if change == "omit":
                        entries.pop(name)
                    else:
                        entries[name]["component"] = "foreign-occurrence"
                    return entries

                backend.capture_prepared_geometry = changed_result
                destination = self.tmp / change
                with self.assertRaises(BridgeError) as caught:
                    freeze(config, destination, backend=backend)
                self.assertEqual(
                    caught.exception.code,
                    (
                        "cad_source_scope_mismatch"
                        if change == "omit"
                        else "cad_mesh_export_failed"
                    ),
                )
                self.assertFalse(destination.exists())

    def test_successful_capture_leaves_no_failure_directories(self) -> None:
        backend, config = self._two_component_backend()
        freeze(config, self.destination, backend=backend)

        self.assertTrue((self.destination / "manifest.json").is_file())
        self.assertEqual(sorted(path.name for path in self.tmp.glob("snapshot*")), ["snapshot"])

    def test_failure_request_is_recorded_without_credentials(self) -> None:
        from description_pipeline.sources.solidworks.freeze import _redact

        redacted = _redact(
            {
                "token": "abc",
                "nested": {"password": "p", "port": 8765},
                "url": "http://user:pass@192.0.2.10:8765/jobs?token=abc",
                "assembly": "C:/cad/robot.SLDASM",
            }
        )
        self.assertEqual(redacted["token"], "<redacted>")
        self.assertEqual(redacted["nested"]["password"], "<redacted>")
        self.assertEqual(redacted["nested"]["port"], 8765)
        self.assertEqual(redacted["url"], "http://192.0.2.10:8765/jobs")
        self.assertEqual(redacted["assembly"], "C:/cad/robot.SLDASM")

    def test_removed_worker_configuration_is_rejected_without_echoing_credentials(self) -> None:
        backend, config = self._two_component_backend()
        backend.scene_document_override = str(self.assembly)
        config = json.loads(json.dumps(config))
        config["worker_url"] = "http://user:secret@192.0.2.10:8765/jobs?token=abc"

        with self.assertRaises(ConfigError) as raised:
            freeze(config, self.destination, backend=backend)
        message = str(raised.exception)
        self.assertIn("worker_url", message)
        self.assertNotIn("secret", message)
        self.assertNotIn("token=abc", message)
        self.assertFalse(self.destination.exists())

    def test_retained_failure_redacts_credentials_from_the_backend_detail(self) -> None:
        """A backend error may echo a URL; the retained detail must be redacted before it is written."""

        from description_pipeline.sources.solidworks.freeze import _retain_failure

        staging = self.destination.with_name(self.destination.name + ".partial")
        staging.mkdir()
        (staging / "partial.txt").write_text("collected copy stays\n", encoding="utf-8", newline="\n")
        error = BridgeError(
            "worker_unreachable",
            "the worker did not answer",
            {"url": "http://user:secret@192.0.2.10:8765/jobs?token=abc", "attempts": 3},
        )

        _retain_failure(staging, self.destination, error, "readings", {"assembly": "robot.SLDASM"})

        failure = self.destination.with_name(self.destination.name + ".failed-001")
        payload = (failure / "failure.json").read_text(encoding="utf-8")
        self.assertNotIn("secret", payload)
        self.assertNotIn("token=abc", payload)
        self.assertIn("192.0.2.10:8765", payload)
        self.assertEqual(json.loads(payload)["detail"]["attempts"], 3)
        self.assertTrue((failure / "partial" / "partial.txt").is_file())

    def test_capture_progress_reaches_the_activity_callback(self) -> None:
        """Prepared Capture notes must flow without changing the snapshot."""

        records: list[dict] = []
        freeze(self.config, self.destination, backend=self.backend, on_activity=records.append)
        self.assertTrue(records)
        self.assertTrue(all(item["phase"] == "capture" for item in records))
        actions = [item["action"] for item in records]
        for action in (
            "start",
            "prepared_source",
            "open_prepared",
            "readings",
            "geometry_facts",
            "scene_projection",
            "geometry",
            "release",
            "seal",
        ):
            self.assertIn(action, actions)
        self.assertIn("instance_reads", actions)  # the backend's own records travel through publish()
        self.assertIn("geometry_facts", actions)
        last_completed: dict[str, int] = {}
        for item in records:
            if "completed" not in item:
                continue
            key = str(item["action"])
            self.assertGreaterEqual(item["completed"], last_completed.get(key, 0))
            self.assertLessEqual(item["completed"], item["total"])
            last_completed[key] = item["completed"]

        def exploded(_record):
            raise RuntimeError("collector exploded")

        again = self.tmp / "snapshot-boom"
        freeze(self.config, again, backend=self.backend, on_activity=exploded)
        self.assertTrue((again / "manifest.json").is_file())

    def test_offline_geometry_failure_keeps_released_raw_facts(self) -> None:
        from description_pipeline.sources.solidworks import native as native_module

        backend, config = self._two_component_backend()
        destination = self.tmp / "offline-geometry-failure"
        with patch.object(
            native_module,
            "materialize_native_triangles",
            side_effect=BridgeError(
                "cad_mesh_serialization_failed",
                "offline STL materialization failed",
                {"component": "base-1"},
            ),
        ), self.assertRaises(BridgeError) as caught:
            freeze(config, destination, backend=backend)

        self.assertEqual(
            caught.exception.code,
            "cad_mesh_serialization_failed",
        )
        order = backend.order
        self.assertEqual(order.count("open_document"), 1)
        self.assertEqual(order.count("release"), 1)
        failure = destination.with_name(destination.name + ".failed-001")
        observations = json.loads(
            (failure / "native_observations.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            set(observations["geometry"]),
            {"base-1", "arm-1"},
        )
        self.assertTrue(
            all(
                facts["triangle_values"]
                for facts in observations["geometry"].values()
            )
        )

    def test_nonfinite_failure_detail_preserves_the_primary_error(self) -> None:
        from description_pipeline.sources.solidworks import native as native_module

        backend, config = self._two_component_backend()
        destination = self.tmp / "nonfinite-projection-failure"
        original = BridgeError(
            "cad_prepared_capture_projection",
            "offline projection failed",
            {"sample": float("nan")},
        )
        with patch.object(
            native_module,
            "project_prepared_capture",
            side_effect=original,
        ), self.assertRaises(BridgeError) as caught:
            freeze(config, destination, backend=backend)

        self.assertIs(caught.exception, original)
        failure = destination.with_name(destination.name + ".failed-001")
        payload = json.loads(
            (failure / "failure.json").read_text(encoding="utf-8")
        )
        self.assertEqual(payload["code"], "cad_prepared_capture_projection")
        self.assertEqual(
            payload["detail"]["sample"],
            {"native_non_finite_float": float("nan").hex()},
        )


if __name__ == "__main__":
    unittest.main()


class SharedModelContractTests(unittest.TestCase):
    """The snapshot scene must satisfy the pipeline-wide model contract."""

    def setUp(self) -> None:
        from description_pipeline.model import EngineeringModel

        self.model_cls = EngineeringModel
        self.tmp = Path(tempfile.mkdtemp(prefix="sw-contract-"))
        self.assembly = support.make_cad_tree(self.tmp / "cad")
        self.backend = _Backend(
            self.assembly,
            [
                {
                    "name": "base-1",
                    "transform": support.placement(),
                    # Off-axis COM: the product signal proves the convention at capture time.
                    "mass": support.mass_payload(1.0, (0.01, 0.02, 0.0)),
                },
                {
                    "name": "arm-1",
                    "transform": support.placement((0.0, 0.0, 0.2)),
                    "mass": support.mass_payload(0.5, (0.0, 0.0, 0.05)),
                },
            ],
            dependencies=[self.tmp / "cad" / "base.SLDPRT", self.tmp / "cad" / "arm.SLDPRT"],
        )
        self.config = {
            "provider": "solidworks",
            "assembly": str(self.assembly),
            "configuration": "Default",
            "allowed_roots": [str(self.tmp / "cad")],
            "robot_name": "robot",
            "geometry": {"enabled": True},
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
            "joints": [
                {
                    "id": "hinge",
                    "name": "hinge_joint",
                    "type": "revolute",
                    "parent": "base_link",
                    "child": "arm_link",
                    "axis": [0.0, 0.0, 1.0],
                    "limits": {"lower": -1.0, "upper": 1.0, "effort": 2.0, "velocity": 3.0},
                }
            ],
        }

    def tearDown(self) -> None:
        support.cleanup(self.tmp)

    def test_scene_satisfies_the_shared_model_schema(self) -> None:
        destination = self.tmp / "snapshot"
        freeze(self.config, destination, backend=self.backend)
        _compose_scene_inputs(destination, self.tmp / "cad", with_arm=True, with_joint=True)
        scene = load_scene(destination)

        model = self.model_cls.from_dict(scene)

        payload = model.to_dict()
        self.assertEqual(payload["schema_version"], "solidworks-to-urdf.engineering-model/v1")
        self.assertEqual(payload["units"], "SI")
        self.assertEqual({link["name"] for link in payload["links"]}, {"base_link", "arm_link"})
        self.assertEqual([joint["name"] for joint in payload["joints"]], ["hinge_joint"])

    def test_expected_entities_cover_the_assembly(self) -> None:
        destination = self.tmp / "snapshot"
        freeze(self.config, destination, backend=self.backend)
        _compose_scene_inputs(destination, self.tmp / "cad", with_arm=True, with_joint=True)
        scene = load_scene(destination)

        self.assertEqual(scene["provenance"]["expected_entities"], ["arm-1", "base-1"])
        assigned = [entity for link in scene["links"] for entity in link["provenance"]["source_entities"]]
        self.assertEqual(sorted(assigned), ["arm-1", "base-1"])
