"""Freeze/load behaviour of the SolidWorks adapter, driven by a fixture host."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from . import _paths  # noqa: F401  (import side effect: sys.path)

from description_pipeline.sources.solidworks.errors import BridgeError, ConfigError  # noqa: E402
from description_pipeline.sources.solidworks.freeze import (  # noqa: E402
    _relative_document_name,
    _source_inputs,
    freeze,
)
from description_pipeline.sources.solidworks.scene import load_scene  # noqa: E402

from . import support  # noqa: E402


class FreezeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="sw-freeze-"))
        self.assembly = support.make_cad_tree(self.tmp / "cad")
        self.parts = [self.tmp / "cad" / "base.SLDPRT", self.tmp / "cad" / "arm.SLDPRT"]
        self.backend = support.FixtureCadBackend(
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
                    "mass": support.mass_payload(0.5, (0.0, 0.0, 0.05)),
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
            "coordinate_systems": ["base_datum", "arm_datum", "imu_datum"],
            "bodies": [
                {
                    "id": "base",
                    "name": "base_link",
                    "components": ["base-1"],
                    "frame": {"coordinate_system": "base_datum"},
                },
                {
                    "id": "arm",
                    "name": "arm_link",
                    "components": ["arm-1"],
                    "frame": {"coordinate_system": "arm_datum"},
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
            "frames": [{"id": "imu", "parent": "base_link", "coordinate_system": "imu_datum"}],
        }
        self.destination = self.tmp / "snapshot"

    def tearDown(self) -> None:
        support.cleanup(self.tmp)

    def test_freeze_produces_a_verified_snapshot(self) -> None:
        manifest = freeze(self.config, self.destination, backend=self.backend, worker_version="test-worker")
        self.assertEqual(manifest["kind"], "solidworks")
        # the fixture backend must never be published as native CAD evidence
        self.assertEqual(manifest["evidence_class"], "fixture")
        self.assertTrue((self.destination / "manifest.json").is_file())
        self.assertTrue((self.destination / "scene.json").is_file())
        self.assertIn("scene.json", manifest["files"])
        self.assertIn("raw/scene_raw.json", manifest["files"])
        self.assertTrue(any(name.startswith("geometry/") for name in manifest["files"]))

    def test_nested_unicode_instance_names_fit_native_filesystem_limits(self) -> None:
        from description_pipeline.sources.solidworks.freeze import _export_geometry

        names = ["装配/" + "nested-component/" * 30 + "part-1", "装配/" + "nested-component/" * 30 + "part-2"]
        files = _export_geometry(self.backend, self.config, self.tmp / "geometry", names)
        self.assertEqual([item["component"] for item in files], names)
        self.assertEqual(len({item["path"] for item in files}), 2)
        for item in files:
            self.assertLess(len(Path(item["path"]).name), 40)
            self.assertTrue((self.tmp / item["path"]).is_file())

    def test_unsaved_capture_cannot_be_enabled_by_configuration(self) -> None:
        for value in (False, 0, "false"):
            with self.subTest(value=value), self.assertRaisesRegex(ConfigError, "cannot be disabled"):
                freeze({**self.config, "require_saved": value}, self.destination, backend=self.backend)
        self.assertFalse(self.destination.exists())

    def test_scene_carries_combined_mass_and_geometry(self) -> None:
        freeze(self.config, self.destination, backend=self.backend, worker_version="test-worker")
        scene = load_scene(self.destination)
        self.assertEqual(scene["schema_version"], "description.scene/v1")
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
        self.assertEqual(scene["frames"][0]["name"], "imu")
        self.assertEqual(scene["provenance"]["snapshot"]["evidence_class"], "fixture")

    def test_link_frame_rotation_is_applied_to_com_and_inertia(self) -> None:
        config = json.loads(json.dumps(self.config))
        # rotate the arm link frame 90 degrees about Z: the COM rotates with it,
        # and the inertia tensor has to be re-expressed in the rotated axes
        config["bodies"][1]["frame"] = {"coordinate_system": "arm_datum"}
        backend = support.FixtureCadBackend(
            self.assembly,
            [
                {
                    "name": "base-1",
                    "transform": support.placement(),
                    "mass": support.mass_payload(1.0),
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
        backend.coordinate_system_matrices["arm_datum"] = support.placement(
            (-0.2, 0.0, 0.0), (0.0, 0.0, 1.5707963267948966)
        )
        freeze(config, self.destination, backend=backend)
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
        self.assertTrue((self.destination / "scene.json").is_file())

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

    def test_copy_missing_a_component_document_blocks_freeze(self) -> None:
        backend = support.FixtureCadBackend(
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
                    "transform": support.placement((0, 0, 0.2)),
                    "mass": support.mass_payload(0.5),
                },
            ],
            dependencies=self.parts,
        )
        backend.pack_drop_files = {self.parts[1].name}
        config = json.loads(json.dumps(self.config))
        config["bodies"] = [
            {"id": "base", "name": "base_link", "components": ["base-1"], "frame": {"coordinate_system": "base_datum"}},
            {"id": "arm", "name": "arm_link", "components": ["arm-1"], "frame": {"coordinate_system": "arm_datum"}},
        ]

        with self.assertRaises(BridgeError) as raised:
            freeze(config, self.destination, backend=backend)

        self.assertEqual(raised.exception.code, "dependency_collection_incomplete")
        self.assertFalse(self.destination.exists())

    def test_empty_resolution_blocks_freeze(self) -> None:
        backend = support.FixtureCadBackend(
            self.assembly,
            [
                {
                    "name": "base-1",
                    "path": str(self.parts[0]),
                    "transform": support.placement(),
                    "mass": support.mass_payload(1.0),
                }
            ],
            dependencies=self.parts,
        )
        backend.resolve_returns_empty = True
        config = json.loads(json.dumps(self.config))
        config["bodies"] = [
            {"id": "base", "name": "base_link", "components": ["base-1"], "frame": {"coordinate_system": "base_datum"}}
        ]
        config["joints"] = []
        config.pop("frames", None)

        with self.assertRaises(BridgeError) as raised:
            freeze(config, self.destination, backend=backend)

        self.assertEqual(raised.exception.code, "dependency_resolution_unverified")

    def test_component_count_mismatch_blocks_freeze(self) -> None:
        backend = support.FixtureCadBackend(
            self.assembly,
            [
                {
                    "name": "base-1",
                    "path": str(self.parts[0]),
                    "transform": support.placement(),
                    "mass": support.mass_payload(1.0),
                }
            ],
            dependencies=self.parts,
        )
        backend.copy_extra_components = 1
        config = json.loads(json.dumps(self.config))
        config["bodies"] = [
            {"id": "base", "name": "base_link", "components": ["base-1"], "frame": {"coordinate_system": "base_datum"}}
        ]
        config["joints"] = []
        config.pop("frames", None)

        with self.assertRaises(BridgeError) as raised:
            freeze(config, self.destination, backend=backend)

        self.assertEqual(raised.exception.code, "dependency_collection_incomplete")

    def test_collector_without_a_top_level_blocks_freeze(self) -> None:
        backend = support.FixtureCadBackend(
            self.assembly,
            [
                {
                    "name": "base-1",
                    "path": str(self.parts[0]),
                    "transform": support.placement(),
                    "mass": support.mass_payload(1.0),
                }
            ],
            dependencies=self.parts,
        )

        def broken_pack(path, destination_dir):
            payload = support.FixtureCadBackend.collect_dependencies(backend, path, destination_dir)
            payload["top_level"] = str(self.tmp / "not-collected.SLDASM")
            return payload

        backend.collect_dependencies = broken_pack  # type: ignore[method-assign]
        config = json.loads(json.dumps(self.config))
        config["bodies"] = [
            {"id": "base", "name": "base_link", "components": ["base-1"], "frame": {"coordinate_system": "base_datum"}}
        ]
        config["joints"] = []
        config.pop("frames", None)

        with self.assertRaises(BridgeError) as raised:
            freeze(config, self.destination, backend=backend)

        self.assertEqual(raised.exception.code, "dependency_collection_incomplete")

    def test_save_flag_is_recorded_and_never_blocks_freeze(self) -> None:
        # `GetSaveFlag` answers "would SolidWorks prompt me to save this document?".
        # The API reference says many operations set it and that a document created by
        # an older release starts with it set, so it is evidence, never a refusal: the
        # capture copies the bytes on disk in a session of its own.
        backend, config = self._two_component_backend()
        # only the assembly carries SolidWorks' save flag
        backend.unsaved_paths = {str(self.assembly)}

        freeze(config, self.destination, backend=backend)

        state = json.loads((self.destination / "raw" / "document_state.json").read_text(encoding="utf-8"))
        self.assertFalse(state["saved"])
        closure = json.loads((self.destination / "raw" / "dependency_closure.json").read_text(encoding="utf-8"))
        self.assertEqual(closure["save_flag_documents"], [str(self.assembly)])
        evidence = json.loads((self.destination / "evidence" / "collection.json").read_text(encoding="utf-8"))
        self.assertEqual(evidence["capture"]["save_flag_documents"], [str(self.assembly)])

    def test_dependency_outside_allowed_roots_blocks_freeze(self) -> None:
        outside = self.tmp / "outside.SLDPRT"
        outside.write_bytes(b"outside")
        backend = support.FixtureCadBackend(
            self.assembly,
            [{"name": "base-1", "transform": support.placement(), "mass": support.mass_payload(1.0)}],
            dependencies=[outside],
        )

        with self.assertRaises(BridgeError) as raised:
            freeze(self.config, self.destination, backend=backend)

        self.assertEqual(raised.exception.code, "dependency_outside_allowed_roots")
        self.assertFalse(self.destination.exists())

    def test_missing_configuration_is_refused(self) -> None:
        config = dict(self.config, configuration="Nope")

        with self.assertRaises(BridgeError) as raised:
            freeze(config, self.destination, backend=self.backend)

        self.assertEqual(raised.exception.code, "cad_configuration_missing")

    def test_joint_with_authored_origin_is_refused(self) -> None:
        config = json.loads(json.dumps(self.config))
        config["joints"][0]["xyz"] = [0.0, 0.0, 0.2]

        with self.assertRaises(ConfigError) as raised:
            freeze(config, self.destination, backend=self.backend)

        self.assertEqual(raised.exception.code, "invalid_config")

    def test_tampered_snapshot_is_rejected(self) -> None:
        freeze(self.config, self.destination, backend=self.backend)
        target = self.destination / "scene.json"
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

        backend = support.FixtureCadBackend(
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
        )
        config = json.loads(json.dumps(self.config))
        config["bodies"] = [
            {"id": "base", "name": "base_link", "components": ["base-1"], "frame": {"coordinate_system": "base_datum"}},
            {"id": "arm", "name": "arm_link", "components": ["arm-1"], "frame": {"coordinate_system": "arm_datum"}},
        ]
        return backend, config

    def test_readings_come_from_the_collected_copy(self) -> None:
        backend, config = self._two_component_backend()
        freeze(config, self.destination, backend=backend)

        # the geometry/mass capture has to read the copy, not the working tree
        self.assertEqual(len(backend.collected_scene_calls), 1)
        collected = Path(backend.collected_scene_calls[0]).resolve()
        self.assertNotEqual(collected, self.assembly.resolve())
        self.assertEqual(collected.name, self.assembly.name)
        self.assertFalse(collected.is_relative_to((self.tmp / "cad").resolve()))
        # the copy is read from the staging tree and only then published to the
        # snapshot, so the published snapshot must carry the very same document
        published = (self.destination / "source" / self.assembly.name).resolve()
        self.assertTrue(published.is_file())
        self.assertEqual(support.file_digest(self.assembly), support.file_digest(published))

        raw = json.loads((self.destination / "raw" / "scene_raw.json").read_text(encoding="utf-8"))
        self.assertEqual(Path(raw["document"]).resolve(), collected)
        self.assertEqual(raw["source_document"], str(self.assembly))
        closure = json.loads((self.destination / "raw" / "dependency_closure.json").read_text(encoding="utf-8"))
        self.assertEqual(Path(closure["top_level"]).resolve(), collected)
        self.assertEqual(len(closure["mapping"]), 3)  # assembly + two parts
        self.assertEqual(closure["component_instances_compared"], 2)
        self.assertEqual(closure["configuration"], "Default")

    def test_backend_reporting_the_original_blocks_capture(self) -> None:
        backend, config = self._two_component_backend()
        backend.scene_document_override = str(self.assembly)

        with self.assertRaises(BridgeError) as raised:
            freeze(config, self.destination, backend=backend)

        self.assertEqual(raised.exception.code, "capture_source_mismatch")
        self.assertFalse(self.destination.exists())

    def test_native_session_locks_are_removed_only_from_the_staged_copy(self) -> None:
        backend, config = self._two_component_backend()
        original_lock = self.assembly.parent / "~$base.SLDPRT"
        original_lock.write_bytes(b"user session")
        collect = backend.collect_dependencies

        def collect_with_locks(path, destination):
            result = collect(path, destination)
            (Path(destination) / "~$base.SLDPRT").write_bytes(b"capture session")
            (Path(destination) / "~$notes.txt").write_bytes(b"not a native lock")
            return result

        backend.collect_dependencies = collect_with_locks  # type: ignore[method-assign]
        manifest = freeze(config, self.destination, backend=backend)
        self.assertNotIn("source/~$base.SLDPRT", manifest["files"])
        self.assertFalse((self.destination / "source/~$base.SLDPRT").exists())
        self.assertIn("source/~$notes.txt", manifest["files"])
        self.assertEqual(original_lock.read_bytes(), b"user session")

    def test_rewritten_references_are_expected_and_pass(self) -> None:
        backend, config = self._two_component_backend()
        # Native reference collection rewrites internal references and serialisation metadata, so
        # the copy is legitimately not byte-identical to the working tree
        backend.copy_rewrites_bytes = True

        freeze(config, self.destination, backend=backend)

        closure = json.loads((self.destination / "raw" / "dependency_closure.json").read_text(encoding="utf-8"))
        self.assertEqual(
            sorted(closure["copy_files"]), ["source/arm.SLDPRT", "source/base.SLDPRT", "source/robot.SLDASM"]
        )
        self.assertEqual(len(closure["original_files"]), 3)
        by_instance = {entry["instance"]: entry for entry in closure["mapping"]}
        self.assertEqual(sorted(by_instance), ["arm-1", "base-1", "top_level"])
        for entry in closure["mapping"]:
            self.assertTrue(entry["copy"].startswith("source/"))
            self.assertTrue(entry["source_sha256"])
            self.assertTrue(entry["copy_sha256"])
        # the rewritten assembly is accepted, and both sides still have their own
        # digest recorded
        self.assertNotEqual(by_instance["top_level"]["copy_sha256"], by_instance["top_level"]["source_sha256"])
        self.assertEqual(by_instance["base-1"]["copy_sha256"], by_instance["base-1"]["source_sha256"])
        self.assertEqual(by_instance["base-1"]["source"], str(self.parts[0]))

    def test_working_tree_changing_during_capture_blocks_freeze(self) -> None:
        backend, config = self._two_component_backend()
        backend.originals_change_during_capture = True

        with self.assertRaises(BridgeError) as raised:
            freeze(config, self.destination, backend=backend)

        self.assertEqual(raised.exception.code, "cad_source_changed")
        self.assertFalse(self.destination.exists())

    def test_save_flag_of_a_dependency_is_recorded_without_blocking(self) -> None:
        backend, config = self._two_component_backend()
        # only the *part* carries the flag: the assembly itself is clean, so the
        # closure scan is what sees it
        backend.unsaved_paths = {str(self.parts[0])}

        freeze(config, self.destination, backend=backend)

        closure = json.loads((self.destination / "raw" / "dependency_closure.json").read_text(encoding="utf-8"))
        self.assertEqual(closure["save_flag_documents"], [str(self.parts[0])])
        self.assertFalse(closure["original_states"][str(self.parts[0])]["saved"])

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

    def test_original_configuration_is_initial_evidence_not_current_state(self) -> None:
        backend, config = self._two_component_backend()
        # the bytes on disk never move: only the session's active configuration
        # does, after the readings have been taken
        backend.drift_configuration_after_reading = True

        freeze(config, self.destination, backend=backend)
        closure = json.loads((self.destination / "raw" / "dependency_closure.json").read_text(encoding="utf-8"))
        self.assertEqual(closure["original_states"][str(self.assembly)]["active_configuration"], "Default")
        collection = json.loads((self.destination / "evidence" / "collection.json").read_text(encoding="utf-8"))
        self.assertEqual(collection["capture"]["originals_unchanged"]["state_scope"], "initial_source_observation")
        self.assertEqual(collection["capture"]["originals_unchanged"]["files_checked"], 3)

    def test_unknown_dependency_state_cannot_prove_a_saved_source(self) -> None:
        for field in ("saved", "active_configuration"):
            with self.subTest(field=field):
                backend, config = self._two_component_backend()
                read = backend.document_state

                def unknown(path, read=read, field=field):
                    state = read(path)
                    if path == str(self.parts[0]):
                        state[field] = None
                    return state

                with (
                    patch.object(backend, "document_state", side_effect=unknown),
                    self.assertRaises(BridgeError) as raised,
                ):
                    freeze(config, self.destination, backend=backend)
                self.assertEqual(raised.exception.code, "cad_document_state_unreadable")
                self.assertEqual(backend.collected, [])

    def test_original_state_is_not_reread_after_copy_capture(self) -> None:
        backend, config = self._two_component_backend()
        collect = backend.collect_scene

        def captured(*args, **kwargs):
            scene = collect(*args, **kwargs)
            backend.document_state = None  # type: ignore[assignment,method-assign]
            return scene

        with patch.object(backend, "collect_scene", side_effect=captured):
            freeze(config, self.destination, backend=backend)
        self.assertTrue(self.destination.exists())
        closure = json.loads((self.destination / "raw" / "dependency_closure.json").read_text(encoding="utf-8"))
        self.assertEqual(len(closure["original_states"]), 3)

    def test_non_instance_dependency_is_checked_for_its_save_flag(self) -> None:
        backend, config = self._two_component_backend()
        skeleton = self.assembly.parent / "skeleton.SLDPRT"
        skeleton.write_bytes(b"external reference, not a component instance")
        backend._dependencies.append(skeleton)
        backend.unsaved_paths.add(str(skeleton))

        freeze(config, self.destination, backend=backend)

        closure = json.loads((self.destination / "raw" / "dependency_closure.json").read_text(encoding="utf-8"))
        self.assertIn(str(skeleton), closure["save_flag_documents"])

    def test_non_instance_dependency_bytes_cannot_change_during_capture(self) -> None:
        for side in ("original", "copy"):
            with self.subTest(side=side):
                backend, config = self._two_component_backend()
                reference = self.assembly.parent / "reference.dat"
                reference.write_bytes(b"external reference, not a component instance")
                backend._dependencies.append(reference)
                collect = backend.collect_scene

                def captured(*args, collect=collect, reference=reference, side=side, **kwargs):
                    scene = collect(*args, **kwargs)
                    target = reference if side == "original" else Path(args[0]).parent / reference.name
                    target.write_bytes(b"changed while reading")
                    return scene

                with (
                    patch.object(backend, "collect_scene", side_effect=captured),
                    self.assertRaises(BridgeError) as raised,
                ):
                    freeze(config, self.destination, backend=backend)
                self.assertEqual(
                    raised.exception.code, "cad_source_changed" if side == "original" else "cad_copy_changed"
                )
                self.assertFalse(self.destination.exists())

    def test_copy_configuration_mismatch_blocks_freeze(self) -> None:
        backend, config = self._two_component_backend()
        backend.copy_configuration = "Other"

        with self.assertRaises(BridgeError) as raised:
            freeze(config, self.destination, backend=backend)

        self.assertEqual(raised.exception.code, "dependency_configuration_mismatch")

    def test_requesting_a_configuration_that_is_not_active_blocks_freeze(self) -> None:
        backend = support.FixtureCadBackend(
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
            {"id": "base", "name": "base_link", "components": ["base-1"], "frame": {"coordinate_system": "base_datum"}},
            {"id": "arm", "name": "arm_link", "components": ["arm-1"], "frame": {"coordinate_system": "arm_datum"}},
        ]

        with self.assertRaises(BridgeError) as raised:
            freeze(config, self.destination, backend=backend)

        self.assertEqual(raised.exception.code, "cad_configuration_not_active")
        detail = raised.exception.detail
        self.assertIsInstance(detail, dict)
        self.assertEqual(detail["requested"], "Other")  # type: ignore[index]
        self.assertEqual(detail["active"], "Default")  # type: ignore[index]

    def test_referenced_configuration_change_blocks_freeze(self) -> None:
        backend, config = self._two_component_backend()
        backend.copy_referenced_configuration_change = True

        with self.assertRaises(BridgeError) as raised:
            freeze(config, self.destination, backend=backend)

        self.assertEqual(raised.exception.code, "dependency_instance_mismatch")

    def test_copy_with_missing_document_bytes_blocks_freeze(self) -> None:
        backend, config = self._two_component_backend()
        backend.copy_missing_files = True
        backend.resolve_only_existing = True

        with self.assertRaises(BridgeError) as raised:
            freeze(config, self.destination, backend=backend)

        self.assertEqual(raised.exception.code, "dependency_escape")

    def test_copy_loading_outside_the_snapshot_blocks_freeze(self) -> None:
        backend, config = self._two_component_backend()
        backend.copy_outside_documents = True

        with self.assertRaises(BridgeError) as raised:
            freeze(config, self.destination, backend=backend)

        # the same-named working-tree document is open in this session: never
        # closed behind the user's back, reported as a conflict instead
        self.assertEqual(raised.exception.code, "cad_same_name_conflict")
        detail = raised.exception.detail
        self.assertIsInstance(detail, dict)
        conflicts = detail["conflicts"]  # type: ignore[index]
        opened = {entry["open_document"] for entry in conflicts}
        self.assertEqual(opened, {str(self.parts[0]), str(self.parts[1])})
        self.assertTrue(all(entry["is_the_open_file"] for entry in conflicts))

    def test_copy_loading_an_absent_snapshot_document_blocks_freeze(self) -> None:
        backend, config = self._two_component_backend()
        # the copy resolves a document that is outside the snapshot but is *not*
        # open anywhere, so the failure stays a plain escape
        backend.copy_outside_documents = True
        backend.list_documents = lambda: [str(self.assembly)]  # type: ignore[method-assign]

        with self.assertRaises(BridgeError) as raised:
            freeze(config, self.destination, backend=backend)

        self.assertEqual(raised.exception.code, "dependency_escape")

    def test_ambiguous_instance_identity_blocks_freeze(self) -> None:
        backend, config = self._two_component_backend()
        backend.copy_duplicate_instances = True

        with self.assertRaises(BridgeError) as raised:
            freeze(config, self.destination, backend=backend)

        self.assertEqual(raised.exception.code, "dependency_instance_identity_ambiguous")

    def test_suppressed_instances_are_carried_not_treated_as_escapes(self) -> None:
        backend = support.FixtureCadBackend(
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
                    "suppressed": True,
                },
            ],
            dependencies=self.parts,
        )
        config = json.loads(json.dumps(self.config))
        config["bodies"] = [
            {"id": "base", "name": "base_link", "components": ["base-1"], "frame": {"coordinate_system": "base_datum"}}
        ]
        config["joints"] = []
        config.pop("frames", None)

        freeze(config, self.destination, backend=backend)

        closure = json.loads((self.destination / "raw" / "dependency_closure.json").read_text(encoding="utf-8"))
        self.assertEqual(closure["suppressed_instances"], 1)
        self.assertEqual(closure["component_instances_inside"], 1)
        self.assertEqual(closure["components_verified"], 2)

    def test_failed_capture_keeps_its_collected_copy_and_a_record(self) -> None:
        backend, config = self._two_component_backend()
        backend.scene_document_override = str(self.assembly)

        with self.assertRaises(BridgeError):
            freeze(config, self.destination, backend=backend)

        failure = self.destination.with_name(self.destination.name + ".failed-001")
        self.assertTrue(failure.is_dir())
        record = json.loads((failure / "failure.json").read_text(encoding="utf-8"))
        self.assertEqual(record["code"], "capture_source_mismatch")
        self.assertEqual(record["stage"], "readings")
        self.assertEqual(record["request"]["assembly"], str(self.assembly))
        self.assertEqual(record["request"]["configuration"], "Default")
        # the material that explains the failure survives: the collector's copy
        collected = failure / "partial" / "source" / self.assembly.name
        self.assertTrue(collected.is_file())
        self.assertTrue((failure / "partial" / "source" / self.parts[0].name).is_file())
        # ... and nothing was delivered
        self.assertFalse(self.destination.exists())

    def test_failed_readings_retain_environment_and_both_owned_session_ids(self) -> None:
        backend, config = self._two_component_backend()
        backend.scene_document_override = str(self.assembly)
        sessions = {
            "source": {"pid": 41, "executable": "sldworks.exe", "ownership": "windows_job"},
            "copy": {"pid": 42, "executable": "sldworks.exe", "ownership": "windows_job"},
        }
        backend.environment = lambda: {"revision": "34.0.0", "sessions": sessions}

        with self.assertRaises(BridgeError) as caught:
            freeze(config, self.destination, backend=backend, worker_version="test-worker")

        self.assertEqual(caught.exception.code, "capture_source_mismatch")
        failure = self.destination.with_name(self.destination.name + ".failed-001")
        environment = json.loads((failure / "partial/evidence/environment.json").read_text())
        self.assertEqual(environment["solidworks"]["sessions"], sessions)
        self.assertEqual(environment["solidworks"]["revision"], "34.0.0")
        self.assertEqual(environment["worker_version"], "test-worker")

    def test_geometry_failure_retains_environment_without_masking_original_error(self) -> None:
        from description_pipeline.sources.solidworks.errors import CadError

        backend, config = self._two_component_backend()
        sessions = {"copy": {"pid": 42, "executable": "sldworks.exe", "ownership": "windows_job"}}
        backend.environment = lambda: {"revision": "34.0.0", "sessions": sessions}

        def unavailable_mesh(*args, **kwargs):
            raise CadError("cad_body_faces_unreadable", "native faces unavailable", {"api": "IBody2.GetFaces"})

        backend.export_component_meshes = unavailable_mesh
        with self.assertRaises(CadError) as caught:
            freeze(config, self.destination, backend=backend)

        self.assertEqual(caught.exception.code, "cad_body_faces_unreadable")
        failure = self.destination.with_name(self.destination.name + ".failed-001")
        record = json.loads((failure / "failure.json").read_text())
        self.assertEqual(record["stage"], "geometry")
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
                original = backend.export_component_meshes

                def changed_result(destinations, original=original, change=change):
                    entries = original(destinations)
                    name = next(iter(entries))
                    if change == "omit":
                        entries.pop(name)
                    else:
                        entries[name]["component"] = "foreign-occurrence"
                    return entries

                backend.export_component_meshes = changed_result
                destination = self.tmp / change
                with self.assertRaises(BridgeError) as caught:
                    freeze(config, destination, backend=backend)
                self.assertEqual(caught.exception.code, "cad_mesh_export_failed")
                self.assertFalse(destination.exists())

    def test_retry_writes_a_second_diagnosis_and_keeps_the_first(self) -> None:
        backend, config = self._two_component_backend()
        backend.copy_configuration = "Other"
        with self.assertRaises(BridgeError):
            freeze(config, self.destination, backend=backend)

        backend.copy_configuration = None
        backend.resolve_returns_empty = True
        with self.assertRaises(BridgeError):
            freeze(config, self.destination, backend=backend)

        first = self.destination.with_name(self.destination.name + ".failed-001")
        second = self.destination.with_name(self.destination.name + ".failed-002")
        self.assertEqual(
            json.loads((first / "failure.json").read_text(encoding="utf-8"))["code"],
            "dependency_configuration_mismatch",
        )
        self.assertEqual(
            json.loads((second / "failure.json").read_text(encoding="utf-8"))["code"],
            "dependency_resolution_unverified",
        )
        self.assertEqual(
            json.loads((second / "failure.json").read_text(encoding="utf-8"))["stage"], "dependency_closure"
        )

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

    def test_failed_capture_does_not_leak_source_credentials_into_the_record(self) -> None:
        """The diagnostic record is committed evidence: it must not carry the source's secrets."""

        backend, config = self._two_component_backend()
        backend.scene_document_override = str(self.assembly)
        config = json.loads(json.dumps(config))
        config["worker_url"] = "http://user:secret@192.0.2.10:8765/jobs?token=abc"

        with self.assertRaises(BridgeError):
            freeze(config, self.destination, backend=backend)

        failure = self.destination.with_name(self.destination.name + ".failed-001")
        payload = (failure / "failure.json").read_text(encoding="utf-8")
        self.assertNotIn("secret", payload)
        self.assertNotIn("token=abc", payload)
        self.assertNotIn("user:", payload)
        # The record still has to explain the failure.
        record = json.loads(payload)
        self.assertEqual(record["code"], "capture_source_mismatch")
        self.assertEqual(record["request"]["assembly"], str(self.assembly))

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


if __name__ == "__main__":
    unittest.main()


class SharedModelContractTests(unittest.TestCase):
    """The snapshot scene must satisfy the pipeline-wide model contract."""

    def setUp(self) -> None:
        from description_pipeline.model import Robot

        self.robot_cls = Robot
        self.tmp = Path(tempfile.mkdtemp(prefix="sw-contract-"))
        self.assembly = support.make_cad_tree(self.tmp / "cad")
        self.backend = support.FixtureCadBackend(
            self.assembly,
            [
                {"name": "base-1", "transform": support.placement(), "mass": support.mass_payload(1.0)},
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
                    "frame": {"coordinate_system": "base_datum"},
                },
                {"id": "arm", "name": "arm_link", "components": ["arm-1"], "frame": {"coordinate_system": "arm_datum"}},
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
        scene = load_scene(destination)

        robot = self.robot_cls.from_dict(scene)

        payload = robot.to_dict()
        self.assertEqual(payload["schema_version"], "description.scene/v1")
        self.assertEqual(payload["units"], "SI")
        self.assertEqual({link["name"] for link in payload["links"]}, {"base_link", "arm_link"})
        self.assertEqual([joint["name"] for joint in payload["joints"]], ["hinge_joint"])

    def test_expected_entities_cover_the_assembly(self) -> None:
        destination = self.tmp / "snapshot"
        freeze(self.config, destination, backend=self.backend)
        scene = load_scene(destination)

        self.assertEqual(scene["provenance"]["expected_entities"], ["arm-1", "base-1"])
        assigned = [entity for link in scene["links"] for entity in link["provenance"]["source_entities"]]
        self.assertEqual(sorted(assigned), ["arm-1", "base-1"])


class DocumentedMassTests(unittest.TestCase):
    """A documented mass table is an explicit input, and the evidence says so."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="sw-mass-"))
        self.assembly = support.make_cad_tree(self.tmp / "cad")
        self.backend = support.FixtureCadBackend(
            self.assembly,
            [
                {
                    "name": "base-1",
                    "transform": support.placement(),
                    "mass": support.mass_payload(
                        1.0, (0.0, 0.0, 0.0), [[0.004, 0.0, 0.0], [0.0, 0.002, 0.0], [0.0, 0.0, 0.002]]
                    ),
                }
            ],
            dependencies=[self.tmp / "cad" / "base.SLDPRT"],
        )
        self.config = {
            "provider": "solidworks",
            "assembly": str(self.assembly),
            "configuration": "Default",
            "allowed_roots": [str(self.tmp / "cad")],
            "geometry": {"enabled": False},
            "material_source": "documented_table",
            "documented_masses": {"base-1": {"mass_kg": 2.5, "reason": "vendor drawing 12-3", "evidence": "base-1"}},
            "mass_evidence": {
                "reference": "vendor drawing 12-3 (fixture)",
                "file": "docs/provenance/drawing-12-3.json",
                "sha256": "0" * 64,
            },
            "bodies": [
                {
                    "id": "base",
                    "name": "base_link",
                    "components": ["base-1"],
                    "frame": {"coordinate_system": "base_datum"},
                }
            ],
            "joints": [],
        }

    def tearDown(self) -> None:
        support.cleanup(self.tmp)

    def test_documented_mass_is_applied_at_load_time_and_records_raw_used(self) -> None:
        freeze(self.config, self.tmp / "snapshot", backend=self.backend)
        raw_scene = json.loads((self.tmp / "snapshot" / "scene.json").read_text(encoding="utf-8"))
        # the snapshot keeps the CAD reading: nothing is replaced on disk
        self.assertAlmostEqual(raw_scene["links"][0]["inertial"]["mass"], 1.0, places=9)
        self.assertEqual(raw_scene["links"][0]["provenance"]["mass"], "raw/mass_properties.json")

        scene = load_scene(self.tmp / "snapshot")
        link = scene["links"][0]
        self.assertAlmostEqual(link["inertial"]["mass"], 2.5, places=9)
        self.assertAlmostEqual(link["inertial"]["inertia"][0], 0.01, places=9)  # scaled with the mass
        record = link["provenance"]["declared_masses"][0]
        self.assertEqual(record["raw_mass_kg"], 1.0)
        self.assertEqual(record["used_mass_kg"], 2.5)
        self.assertEqual(record["scale"], 2.5)
        self.assertTrue(record["reason"])
        self.assertEqual(link["provenance"]["mass"], "source.documented_masses")

    def test_documented_table_must_be_declared(self) -> None:
        config = dict(self.config)
        config["documented_masses"] = {}
        with self.assertRaises(ConfigError) as raised:
            freeze(config, self.tmp / "snapshot2", backend=self.backend)
        self.assertEqual(raised.exception.code, "invalid_config")
