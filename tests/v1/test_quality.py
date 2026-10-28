"""Independent analytic and adversarial checks; fixtures grant no native qualification."""

from __future__ import annotations

import copy
import hashlib
import struct
import json
import tempfile
import unittest
from unittest import mock
from pathlib import Path
from xml.etree import ElementTree as ET

import numpy as np

from description_pipeline.interface_records import CHECK_POLICY_V1, validate_qualification
from description_pipeline.io import PipelineError, canonical, write_json
from description_pipeline.verification import solidworks_urdf as quality
from tests.sources import support


class StructuralQualityTests(unittest.TestCase):
    def test_windows_pywin32_integer_version_is_a_valid_runtime_version(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_json(
                root / "tool.json",
                {
                    "schema_version": "solidworks-to-urdf.tool/v1",
                    "pipeline_id": "solidworks-to-urdf",
                    "source_sha256": "a" * 64,
                    "version": "1.0.0",
                    "runtime": {"python": "3.12.10", "packages": {"numpy": "2.5.3", "pywin32": "311"}},
                },
            )
            self.assertEqual("1.0.0", quality._tool(root)["version"])

    def test_structural_quality_uses_exact_python_runtime_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tool = {
                "schema_version": "solidworks-to-urdf.tool/v1",
                "pipeline_id": "solidworks-to-urdf",
                "source_sha256": "a" * 64,
                "version": "1.0.0",
                "runtime": {
                    "python": "3.12",
                    "packages": {"numpy": "2.5.3", "pywin32": "311"},
                },
            }
            write_json(root / "tool.json", tool)
            self.assertEqual("1.0.0", quality._tool(root)["version"])
            for value in ("3.120", "3.12garbage", "3.12.0rc1", 3.12):
                with self.subTest(value=repr(value)):
                    wrong = {**tool, "runtime": {**tool["runtime"], "python": value}}
                    write_json(root / "tool.json", wrong)
                    with self.assertRaisesRegex(PipelineError, "Missing pinned build runtime"):
                        quality._tool(root)

    def test_captured_matrices_are_rowmajor_and_reflections_fail(self):
        transform = np.array([[0, -1, 0, 0.1], [1, 0, 0, 0.2], [0, 0, 1, 0.3], [0, 0, 0, 1]], dtype=float)
        np.testing.assert_array_equal(transform, quality._cad_pose(transform.ravel()))
        transform[0, 1] = 1
        with self.assertRaises(PipelineError):
            quality._cad_pose(transform.ravel())

    def test_rotated_native_shaft_line_uses_component_frame(self):
        transform = quality._pose([0.2, -0.1, 0.3], [0.4, 0.5, 0.6])
        child = transform @ quality._pose([0.01, 0, 0.1], [0, 0, 0])
        reference = {"component": "shaft", "face_index": 3, "body_type": "solid"}
        authored = {"axis_reference": reference, "axis": [0, 0, 1]}
        record = {
            **reference,
            "surface": "cylinder",
            "radius_m": 0.01,
            "coordinate_frame": "component_local",
            "is_cylinder_raw": True,
            "cylinder_params_raw": [
                0.01,
                0,
                0.1,
                0,
                0,
                -1,
                0.01,
            ],
            "axis_point_m": [0.01, 0, 0.1],
            "axis_direction": [0, 0, -1],
        }
        self.assertAlmostEqual(0, quality._axis(authored, record, {"shaft": transform}, child)["offset_m"])
        record["axis_point_m"][0] += 0.01
        with self.assertRaises(PipelineError):
            quality._axis(authored, record, {"shaft": transform}, child)

    def test_signed_axis_and_continuous_limits_match_author(self):
        authored = {
            "name": "joint",
            "parent": "base_link",
            "child": "arm_link",
            "type": "continuous",
            "axis": [0, 0, 1],
            "limits": {"effort": 1.0, "velocity": 2.0},
            "limit_source": "native",
        }
        canonical_joint = {**authored, "xyz": [0, 0, 0], "rpy": [0, 0, 0]}
        node = ET.fromstring(
            '<joint type="continuous"><parent link="base_link"/><child link="arm_link"/>'
            '<origin xyz="0 0 0" rpy="0 0 0"/><axis xyz="0 0 1"/>'
            '<limit effort="1" velocity="2"/></joint>'
        )
        frames = {"base_link": np.eye(4), "arm_link": np.eye(4)}
        diagnostics: list[dict] = []
        quality._joint(node, authored, canonical_joint, {}, frames, diagnostics)
        self.assertEqual(diagnostics, [])
        node.find("axis").set("xyz", "0 0 -1")
        with self.assertRaises(PipelineError):
            quality._joint(node, authored, canonical_joint, {}, frames, [])

    def test_authored_zero_limits_load_with_a_motion_warning(self):
        authored = {
            "name": "joint",
            "parent": "base_link",
            "child": "arm_link",
            "type": "continuous",
            "axis": [0, 0, 1],
            "limits": {"effort": 0.0, "velocity": 0.0},
            "limit_source": "native",
        }
        canonical_joint = {**authored, "xyz": [0, 0, 0], "rpy": [0, 0, 0]}
        node = ET.fromstring(
            '<joint type="continuous"><parent link="base_link"/><child link="arm_link"/>'
            '<origin xyz="0 0 0" rpy="0 0 0"/><axis xyz="0 0 1"/>'
            '<limit effort="0" velocity="0"/></joint>'
        )
        frames = {"base_link": np.eye(4), "arm_link": np.eye(4)}
        diagnostics: list[dict] = []
        result = quality._joint(node, authored, canonical_joint, {}, frames, diagnostics)
        self.assertEqual(result["limits"], {"effort": 0.0, "velocity": 0.0})
        self.assertEqual([item["code"] for item in diagnostics], ["urdf.motion_unqualified"])
        self.assertFalse(diagnostics[0]["blocking"])

        negative = {**authored, "limits": {"effort": -1.0, "velocity": 0.0}}
        canonical_negative = {**negative, "xyz": [0, 0, 0], "rpy": [0, 0, 0]}
        node_negative = ET.fromstring(
            '<joint type="continuous"><parent link="base_link"/><child link="arm_link"/>'
            '<origin xyz="0 0 0" rpy="0 0 0"/><axis xyz="0 0 1"/>'
            '<limit effort="-1" velocity="0"/></joint>'
        )
        with self.assertRaises(PipelineError):
            quality._joint(node_negative, negative, canonical_negative, {}, frames, [])

    def test_incomplete_delivery_and_forged_green_report_fail_without_crashing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_json(root / "reports/quality.json", {"passed": True})
            report = quality.check_bundle(root)
            self.assertFalse(report["passed"])
            self.assertIn("report.binding", [check["id"] for check in report["checks"]])
            canonical(report)
            self.assertEqual(report["subject_status"], "unavailable")
            self.assertNotIn(str(root).encode(), canonical(report))

    def test_recomputed_tuple_values_bind_through_the_json_model(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report = {"schema_version": "probe", "checks": [], "passed": True, "value": ("a", 1)}
            write_json(root / "reports/quality.json", json.loads(json.dumps(report)))
            with mock.patch.object(quality, "evaluate_bundle", return_value=report):
                result = quality.check_bundle(root)
            self.assertNotIn("report.binding", [check["id"] for check in result["checks"]])

    def test_altered_saved_report_still_fails_with_the_same_message(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report = {"schema_version": "probe", "checks": [], "passed": True, "value": ("a", 1)}
            write_json(root / "reports/quality.json", {"schema_version": "probe", "checks": [], "passed": True,
                                                       "value": ["a", 2]})
            with mock.patch.object(quality, "evaluate_bundle", return_value=report):
                result = quality.check_bundle(root)
            binding = [check for check in result["checks"] if check["id"] == "report.binding"]
            self.assertEqual(len(binding), 1)
            self.assertIn("Saved quality report differs from recomputation", binding[0]["details"]["error"])
            self.assertFalse(result["passed"])


class DerivedSelectionAccountingTests(unittest.TestCase):
    """The independent recomputation binds each native instance and its own measurements."""

    def test_unassigned_rows_keep_each_instance_identity_and_never_redistribute(self):
        from description_pipeline.verification.solidworks_physics import _derive_closure

        placement = [1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0]
        masses = {
            "a": {
                "mass": 1.0,
                "com": [0.0, 0.0, 0.0],
                "inertia": [[1e-3, 0.0, 0.0], [0.0, 1e-3, 0.0], [0.0, 0.0, 1e-3]],
                "reference": {"product_convention": "solidworks_standard", "configuration": "Default"},
            },
            "b": {
                "mass": 2.0,
                "com": [0.0, 0.0, 0.0],
                "inertia": [[2e-3, 0.0, 0.0], [0.0, 2e-3, 0.0], [0.0, 0.0, 2e-3]],
                "reference": {"product_convention": "solidworks_standard", "configuration": "Other"},
            },
        }
        native = {
            "top_level": {
                "mass": 3.0,
                "com": [0.0, 0.0, 0.0],
                "inertia": [[3e-3, 0.0, 0.0], [0.0, 3e-3, 0.0], [0.0, 0.0, 3e-3]],
            },
            "component_context": {
                "status": "recorded",
                "assembly_mass_kg": 3.0,
                "instances": [
                    {
                        "name": "a",
                        "parent": None,
                        "depth": 0,
                        "document": "a.SLDPRT",
                        "document_type": "part",
                        "context_mass_kg": 1.0,
                        "context_volume_m3": None,
                        "overrides": {},
                        "effective": None,
                        "effective_error": None,
                    },
                    {
                        "name": "b",
                        "parent": "frame-9",
                        "depth": 1,
                        "document": "b.SLDPRT",
                        "document_type": "part",
                        "context_mass_kg": 2.0,
                        "context_volume_m3": None,
                        "overrides": {},
                        "effective": None,
                        "effective_error": None,
                    },
                ],
                "errors": [],
            },
        }
        scene = {
            "components": [{"name": "a", "transform": placement}, {"name": "b", "transform": placement}],
            "mass_properties": copy.deepcopy(masses),
            "notes": {},
        }
        source = {"bodies": [{"name": "base", "components": ["a"]}]}
        derived = _derive_closure(native, scene, scene["mass_properties"], source)
        selection = derived["selection"]
        self.assertAlmostEqual(selection["native_mass_kg"], 3.0, places=12)
        self.assertAlmostEqual(selection["unassigned_mass_kg"], 2.0, places=12)
        self.assertAlmostEqual(selection["selected_mass_kg"], 1.0, places=12)
        self.assertAlmostEqual(selection["accounting_delta_kg"], 0.0, places=12)
        self.assertFalse(selection["whole_mass_qualified"])
        self.assertEqual(len(selection["unassigned"]), 1)
        row = selection["unassigned"][0]
        self.assertEqual(row["component"], "b")
        self.assertEqual(row["document"], "b.SLDPRT")
        self.assertEqual(row["configuration"], "Other")
        self.assertEqual(row["parent"], "frame-9")
        self.assertAlmostEqual(row["mass_kg"], 2.0, places=12)


class MeshChunkContractTests(unittest.TestCase):
    """Delivered mesh parts must reproduce the captured source records exactly."""

    def _mesh(self, triangles):
        header = b"chunk contract test".ljust(80, b" ")
        records = b"".join(
            struct.pack(
                "<12fH",
                0.0,
                0.0,
                1.0,
                *a,
                *b,
                *c,
                1 if index < 2 else 2,
            )
            for index, (a, b, c) in enumerate(triangles)
        )
        return header + struct.pack("<I", len(triangles)) + records

    def _parts(self, payload, limit):
        total = struct.unpack("<I", payload[80:84])[0]
        count = (total + limit - 1) // limit
        parts = []
        for part in range(count):
            start = limit * part
            size = min(limit, total - start)
            parts.append(
                payload[:80] + struct.pack("<I", size) + payload[84 + 50 * start : 84 + 50 * (start + size)]
            )
        return parts

    def _fixture(self, root, *, limit, mutate, body_type=0):
        triangles = [
            ((0.0, 0.0, 0.0), (0.0, 1.0, 0.0), (1.0, 0.0, 0.0)),
            ((0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 0.0, 1.0)),
            ((0.0, 0.0, 0.0), (0.0, 0.0, 1.0), (0.0, 1.0, 0.0)),
            ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)),
        ]
        from description_pipeline.geometry.native_tessellation import (
            build_native_tessellation,
        )

        source_scope = support.fixture_source_scope("pcb-1")
        tessellation = build_native_tessellation(
            "pcb-1",
            [
                {
                    "body_type": body_type,
                    "body_index": 0,
                    "body_name": "fixture-body",
                    "face_count": 2,
                }
            ],
            [
                {
                    "body_type": body_type,
                    "body_index": 0,
                    "face_index": 0,
                    "triangle_start": 0,
                    "triangle_count": 2,
                },
                {
                    "body_type": body_type,
                    "body_index": 0,
                    "face_index": 1,
                    "triangle_start": 2,
                    "triangle_count": 2,
                },
            ],
            source_scope=source_scope,
        )
        payload = support.binary_stl_bytes(triangles)
        parts = self._parts(payload, limit)
        if mutate is not None:
            mutate(payload, parts)
        (root / "evidence/geometry").mkdir(parents=True, exist_ok=True)
        (root / "meshes/visual").mkdir(parents=True, exist_ok=True)
        (root / "evidence/geometry/0001_mesh.stl").write_bytes(payload)
        names = []
        for index, data in enumerate(parts):
            name = f"meshes/visual/board_0_part_{index}.stl"
            (root / name).write_bytes(data)
            names.append(name)
        visuals = "".join(
            '<visual><origin xyz="0 0 0" rpy="0 0 0"/><geometry>'
            f'<mesh filename="../{name}" scale="1 1 1"/></geometry></visual>'
            for name in names
        )
        node = ET.fromstring(f'<link name="board">{visuals}</link>')
        body = {"components": ["pcb-1"]}
        model_link = {
            "inertial": {
                "xyz": [0.0, 0.0, 0.0],
                "inertia": [1e-6, 0.0, 0.0, 1e-6, 0.0, 1e-6],
                "mass": 1.0,
            },
            "visuals": [
                {
                    "filename": "geometry/0001_mesh.stl",
                    "sha256": hashlib.sha256(payload).hexdigest(),
                    "mesh_name": "meshes/visual/board_0.stl",
                    "scale": [1, 1, 1],
                    "xyz": [0, 0, 0],
                    "rpy": [0, 0, 0],
                    "provenance": {"native_component": "pcb-1"},
                }
            ],
            "collisions": [],
        }
        geometry = {
            "pcb-1": {
                "path": "geometry/0001_mesh.stl",
                "sha": hashlib.sha256(payload).hexdigest(),
                "source_bytes": payload,
                "triangles": 4,
                "bodies": {
                    "solid": 1 if body_type == 0 else 0,
                    "sheet": 1 if body_type == 1 else 0,
                },
                "tessellation": tessellation,
            }
        }
        components = {"pcb-1": np.identity(4)}
        return node, body, model_link, geometry, components

    def _run(self, root, *, limit=2, mutate=None, body_type=0):
        node, body, model_link, geometry, components = self._fixture(
            root,
            limit=limit,
            mutate=mutate,
            body_type=body_type,
        )
        with mock.patch.object(quality, "CHUNK_TRIANGLES_MAX", limit):
            return quality._geometry(
                root, body, node, model_link, geometry, components, np.identity(4), np.identity(4), set()
            )

    def test_lossless_multipart_delivery_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            used, points = self._run(Path(tmp))
            self.assertEqual(
                used, ["meshes/visual/board_0_part_0.stl", "meshes/visual/board_0_part_1.stl"]
            )
            self.assertEqual(len(points), 12)

    def test_any_part_deviation_fails(self):
        mutations = {
            "swapped_parts": lambda _payload, parts: parts.__setitem__(slice(0, 2), [parts[1], parts[0]]),
            "truncated_part": lambda _payload, parts: parts.__setitem__(1, parts[1][:-50]),
            "corrupt_header": lambda _payload, parts: parts.__setitem__(1, b"X" + parts[1][1:]),
            "wrong_count": lambda _payload, parts: parts.__setitem__(
                0, parts[0][:80] + struct.pack("<I", 1) + parts[0][84:]
            ),
            "dropped_record": lambda _payload, parts: parts.__setitem__(1, parts[1][:84] + parts[1][84 + 50 :]),
        }
        for label, mutate in mutations.items():
            with self.subTest(case=label), tempfile.TemporaryDirectory() as tmp, self.assertRaises(PipelineError):
                self._run(Path(tmp), mutate=mutate)

    def test_chunk_count_boundaries_are_deterministic(self):
        self.assertEqual(quality._mesh_chunk_count(199_999), 1)
        self.assertEqual(quality._mesh_chunk_count(200_000), 2)
        self.assertEqual(quality._mesh_chunk_count(362_415), 2)
        self.assertEqual(quality._mesh_chunk_count(399_998), 2)
        self.assertEqual(quality._mesh_chunk_count(399_999), 3)

    def test_globally_flipped_closed_mesh_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            node, body, model_link, geometry, components = self._fixture(
                root,
                limit=2,
                mutate=None,
            )
            source = root / "evidence/geometry/0001_mesh.stl"
            flipped = bytearray(source.read_bytes())
            count = struct.unpack_from("<I", flipped, 80)[0]
            for index in range(count):
                offset = 84 + 50 * index
                normal = struct.unpack_from("<3f", flipped, offset)
                first = struct.unpack_from("<3f", flipped, offset + 12)
                second = struct.unpack_from("<3f", flipped, offset + 24)
                third = struct.unpack_from("<3f", flipped, offset + 36)
                struct.pack_into("<12f", flipped, offset, *[-value for value in normal], *first, *third, *second)
            source.write_bytes(flipped)
            geometry["pcb-1"]["sha"] = hashlib.sha256(flipped).hexdigest()
            geometry["pcb-1"]["source_bytes"] = bytes(flipped)
            model_link["visuals"][0]["sha256"] = geometry["pcb-1"]["sha"]
            for index, part in enumerate(self._parts(flipped, 2)):
                (root / f"meshes/visual/board_0_part_{index}.stl").write_bytes(part)
            with (
                self.assertRaisesRegex(PipelineError, "positive total material volume"),
                mock.patch.object(quality, "CHUNK_TRIANGLES_MAX", 2),
            ):
                quality._geometry(
                    root,
                    body,
                    node,
                    model_link,
                    geometry,
                    components,
                    np.identity(4),
                    np.identity(4),
                    set(),
                )

    def test_open_sheet_negative_signed_integral_is_not_a_solid_flip(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            node, body, model_link, geometry, components = self._fixture(
                root,
                limit=2,
                mutate=None,
                body_type=1,
            )
            source = root / "evidence/geometry/0001_mesh.stl"
            flipped = bytearray(source.read_bytes())
            count = struct.unpack_from("<I", flipped, 80)[0]
            for index in range(count):
                offset = 84 + 50 * index
                normal = struct.unpack_from("<3f", flipped, offset)
                first = struct.unpack_from("<3f", flipped, offset + 12)
                second = struct.unpack_from("<3f", flipped, offset + 24)
                third = struct.unpack_from("<3f", flipped, offset + 36)
                struct.pack_into(
                    "<12f",
                    flipped,
                    offset,
                    *[-value for value in normal],
                    *first,
                    *third,
                    *second,
                )
            source.write_bytes(flipped)
            geometry["pcb-1"]["sha"] = hashlib.sha256(flipped).hexdigest()
            geometry["pcb-1"]["source_bytes"] = bytes(flipped)
            model_link["visuals"][0]["sha256"] = geometry["pcb-1"]["sha"]
            for index, part in enumerate(self._parts(flipped, 2)):
                (root / f"meshes/visual/board_0_part_{index}.stl").write_bytes(part)
            with mock.patch.object(quality, "CHUNK_TRIANGLES_MAX", 2):
                _used, points = quality._geometry(
                    root,
                    body,
                    node,
                    model_link,
                    geometry,
                    components,
                    np.identity(4),
                    np.identity(4),
                    set(),
                )
            self.assertEqual(len(points), 12)

    def test_hollow_solid_accepts_inward_cavity_but_rejects_outward_cavity(self):
        def tetra(scale, center, inward):
            source = [
                ((0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)),
            ][0]
            transformed = [
                tuple(
                    float(center[axis] + scale * (point[axis] - 0.25))
                    for axis in range(3)
                )
                for point in source
            ]
            faces = (
                (0, 2, 1),
                (0, 1, 3),
                (0, 3, 2),
                (1, 2, 3),
            )
            if inward:
                faces = tuple((first, third, second) for first, second, third in faces)
            return [tuple(transformed[index] for index in face) for face in faces]

        def run_case(root, *, inward):
            node, body, model_link, geometry, components = self._fixture(
                root,
                limit=2,
                mutate=None,
            )
            triangles = tetra(2.0, (0.0, 0.0, 0.0), False) + tetra(
                0.25,
                (0.0, 0.0, 0.0),
                inward,
            )
            payload = self._mesh(triangles)
            source = root / "evidence/geometry/0001_mesh.stl"
            source.write_bytes(payload)
            geometry["pcb-1"]["sha"] = hashlib.sha256(payload).hexdigest()
            geometry["pcb-1"]["source_bytes"] = payload
            model_link["visuals"][0]["sha256"] = geometry["pcb-1"]["sha"]
            geometry["pcb-1"]["triangles"] = 8
            spans = geometry["pcb-1"]["tessellation"]["face_spans"]
            spans[0]["triangle_count"] = 4
            spans[1]["triangle_start"] = 4
            spans[1]["triangle_count"] = 4
            for index, part in enumerate(self._parts(payload, 4)):
                (root / f"meshes/visual/board_0_part_{index}.stl").write_bytes(part)
            with mock.patch.object(quality, "CHUNK_TRIANGLES_MAX", 4):
                return quality._geometry(
                    root,
                    body,
                    node,
                    model_link,
                    geometry,
                    components,
                    np.identity(4),
                    np.identity(4),
                    set(),
                )

        with tempfile.TemporaryDirectory() as tmp:
            run_case(Path(tmp), inward=True)
        with (
            tempfile.TemporaryDirectory() as tmp,
            self.assertRaisesRegex(PipelineError, "shell orientation"),
        ):
            run_case(Path(tmp), inward=False)


class EntityPartitionTests(unittest.TestCase):
    """Native, selected and unassigned entity partitions bind to the sealed scene."""

    def _bundle(self):
        source = {"bodies": [{"name": "base_link", "id": "base_link", "components": ["a", "b"]}]}
        raw = {"components": [{"name": "a"}, {"name": "b"}, {"name": "c"}]}
        model = {
            "links": [{"name": "base_link", "id": "base_link", "provenance": {"source_entities": ["a", "b"]}}],
            "provenance": {
                "expected_entities": ["a", "b"],
                "native_entities": ["a", "b", "c"],
                "unassigned_entities": ["c"],
            },
        }
        return source, raw, model

    def test_partitions_are_bound_to_the_sealed_scene(self):
        source, raw, model = self._bundle()
        self.assertEqual(
            quality._entities(source, raw, model),
            {"occurrences": 3, "bodies": 1, "selected": 2, "unassigned": 1},
        )
        for key in ("expected_entities", "native_entities", "unassigned_entities"):
            candidate = copy.deepcopy(model)
            candidate["provenance"][key].pop()
            with self.assertRaises(PipelineError):
                quality._entities(source, raw, candidate)
        missing = copy.deepcopy(model)
        missing["provenance"]["unassigned_entities"] = []
        with self.assertRaises(PipelineError):
            quality._entities(source, raw, missing)
        unknown = copy.deepcopy(source)
        unknown["bodies"][0]["components"] = ["a", "z"]
        with self.assertRaises(PipelineError):
            quality._entities(unknown, raw, model)
        duplicate = copy.deepcopy(raw)
        duplicate["components"].append({"name": "a"})
        with self.assertRaises(PipelineError):
            quality._entities(source, duplicate, model)


class QualifiedReportInventoryTests(unittest.TestCase):
    """R18: the report inventory binds the independently rebuilt object set."""

    @staticmethod
    def _qualifications(definition) -> list:
        return [
            {
                "schema_version": quality.QUALIFICATION_SCHEMA,
                "fact_id": fact["fact_id"],
                "scope": fact["scope"],
                "object_id": fact["object_id"],
                "status": "established",
                "evidence": [{"record": "input/handoff.json", "sha256": "0" * 64}],
            }
            for fact in quality.qualification_facts(definition)
        ]

    @staticmethod
    def _definition() -> dict:
        return {
            "checks": {},
            "source": {
                "bodies": [{"name": "base"}, {"name": "tool"}],
                "joints": [{"name": "hinge", "type": "revolute"}],
                "frames": [{"name": "tool_tcp"}],
            },
        }

    @staticmethod
    def _report(names, definition) -> dict:
        required = sorted(names)
        return {
            "passed": True,
            "required_checks": required,
            "checks": [{"id": name, "state": "passed", "passed": True} for name in required],
            "qualifications": QualifiedReportInventoryTests._qualifications(definition),
        }

    def test_complete_rebuilt_inventory_passes(self):
        definition = self._definition()
        quality.require_qualified_report(self._report(set(quality.required_checks(definition)), definition), definition)

    def test_fixed_only_report_is_refused(self):
        definition = self._definition()
        required = set(quality.required_checks(definition))
        fixed = set(CHECK_POLICY_V1["fixed_checks"])
        dynamic = required - fixed
        self.assertTrue(dynamic, "the rebuilt objects must expand dynamic gates")
        with self.assertRaises(PipelineError):
            quality.require_qualified_report(self._report(fixed & required, definition), definition)

    def test_unknown_object_gate_is_refused(self):
        definition = self._definition()
        report = self._report(set(quality.required_checks(definition)) | {"inertia.nonexistent_body"}, definition)
        with self.assertRaises(PipelineError):
            quality.require_qualified_report(report, definition)

    def test_inventory_mismatch_against_rebuilt_objects_is_refused(self):
        definition = self._definition()
        other = self._definition()
        other["source"]["frames"] = []
        with self.assertRaises(PipelineError):
            quality.require_qualified_report(self._report(set(quality.required_checks(other)), other), definition)


class QualificationItemTests(unittest.TestCase):
    """IF-14: the report's qualification items beside the checks."""

    def _root(self) -> Path:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        (root / "input").mkdir()
        (root / "model").mkdir()
        (root / "urdf").mkdir()
        write_json(root / "input/handoff.json", {"probe": "sealed input"})
        write_json(root / "model/robot.json", {"probe": "model"})
        (root / "urdf/robot.urdf").write_text("<robot/>", encoding="utf-8")
        from description_pipeline.request_evidence import build_request_evidence, write_request_evidence

        write_request_evidence(
            root / "input" / "request.json",
            build_request_evidence(
                purpose="engineering",
                request_id="fixture-quality",
                target_hardware="nd_fixture",
                change_summary="fixture",
                main_assembly="cad/robot.SLDASM",
                handoff_digest="a" * 64,
                platform_config_digest="c" * 64,
            ),
        )
        return root

    @staticmethod
    def _definition() -> dict:
        return {
            "checks": {},
            "source": {
                "bodies": [{"name": "base"}, {"name": "tool"}],
                "joints": [{"name": "hinge", "type": "revolute"}],
                "frames": [],
            },
        }

    @staticmethod
    def _checks(definition) -> list:
        return [
            {"id": name, "state": "passed", "passed": True} for name in quality.required_checks(definition)
        ]

    def test_items_carry_one_identity_per_object_and_out_of_scope_budgets(self):
        root = self._root()
        definition = self._definition()
        items = quality.qualifications(root, definition, self._checks(definition))
        keys = [(item["fact_id"], item["scope"], item["object_id"]) for item in items]
        self.assertEqual(len(keys), len(set(keys)), "the same fact never duplicates")
        self.assertIn(("inertia", "", "base"), keys)
        self.assertIn(("inertia", "", "tool"), keys)
        statuses = {(item["fact_id"], item["object_id"]): item["status"] for item in items}
        self.assertEqual(statuses[("physics.expected_mass", "")], "out_of_scope")
        self.assertEqual(statuses[("geometry.expected_extent", "")], "out_of_scope")
        self.assertEqual(statuses[("inertia", "base")], "established")
        self.assertEqual(items, quality.qualifications(root, definition, self._checks(definition)))

    def test_a_gate_that_did_not_establish_leaves_an_unqualified_fact(self):
        root = self._root()
        definition = self._definition()
        items = quality.qualifications(root, definition, [])
        statuses = {(item["fact_id"], item["object_id"]): item["status"] for item in items}
        self.assertEqual(statuses[("inertia", "base")], "unqualified")
        self.assertEqual(statuses[("physics.expected_mass", "")], "out_of_scope")

    def test_missing_records_refuse_the_ungrounded_item(self):
        root = self._root()
        definition = self._definition()
        (root / "model/robot.json").unlink()
        items = quality.qualifications(root, definition, self._checks(definition))
        inertia = next(item for item in items if item["fact_id"] == "inertia")
        self.assertEqual(inertia["evidence"], [], "a missing record leaves no evidence")
        with self.assertRaises(PipelineError):
            validate_qualification(inertia)
        report = {
            "passed": True,
            "required_checks": quality.required_checks(definition),
            "checks": self._checks(definition),
            "qualifications": items,
        }
        with self.assertRaises(PipelineError):
            quality.require_qualified_report(report, definition)

    def test_tampered_evidence_digest_is_refused_at_the_boundary(self):
        root = self._root()
        definition = self._definition()
        items = quality.qualifications(root, definition, self._checks(definition))
        report = {
            "passed": True,
            "required_checks": quality.required_checks(definition),
            "checks": self._checks(definition),
            "qualifications": items,
        }
        quality.require_qualified_report(report, definition, root=root)
        (root / "input/handoff.json").write_bytes(b"tampered input")
        with self.assertRaises(PipelineError):
            quality.require_qualified_report(report, definition, root=root)
        write_json(root / "input/handoff.json", {"probe": "sealed input"})
        items[0]["evidence"] = [{"record": "input/handoff.json", "sha256": "0" * 64}]
        with self.assertRaises(PipelineError):
            quality.require_qualified_report(report, definition, root=root)


class OptionalDesignExpectationTests(unittest.TestCase):
    def test_absent_expectations_do_not_execute_or_require_a_passing_placeholder(self):
        definition = {"checks": {}, "source": {"bodies": [], "joints": [], "frames": []}}
        gates = quality._Gates()
        quality._author_expectations(gates, definition, {}, [])
        self.assertEqual(gates.checks, [])
        required = quality.required_checks(definition)
        self.assertNotIn("physics.expected_mass", required)
        self.assertNotIn("geometry.expected_extent", required)
        for core in [
            "physics.mass_closure_equality",
            "physics.independent",
            "model.schema",
            "geometry.coverage",
            "geometry.assets",
            "source.integrity",
        ]:
            self.assertIn(core, required)
        report = {
            "passed": True,
            "required_checks": required,
            "checks": [{"id": key, "state": "passed", "passed": True} for key in required],
            "qualifications": QualifiedReportInventoryTests._qualifications(definition),
        }
        quality.require_qualified_report(report, definition)
        removed = copy.deepcopy(report)
        removed["checks"] = [row for row in removed["checks"] if row["id"] != "physics.mass_closure_equality"]
        removed["required_checks"].remove("physics.mass_closure_equality")
        with self.assertRaises(PipelineError):
            quality.require_qualified_report(removed, definition)

    def test_supplied_expectations_are_required_evaluated_and_not_defaulted(self):
        model = {"links": [{"inertial": {"mass": 2.0}}]}
        points = [np.array([[0, 0, 0], [0.5, 0.2, 0.1]])]
        definition = {"checks": {"expected_mass_kg": [1, 3], "expected_extent_m": [0.4, 0.6]}}
        gates = quality._Gates()
        quality._author_expectations(gates, definition, model, points)
        self.assertEqual({row["id"] for row in gates.checks}, {"physics.expected_mass", "geometry.expected_extent"})
        self.assertTrue(all(row["passed"] for row in gates.checks))
        required = quality.required_checks(definition)
        self.assertIn("physics.expected_mass", required)
        self.assertIn("geometry.expected_extent", required)
        for value in [None, [3, 4], [2, 2], [1, float("nan")], [True, 3], ["1", "3"], [1, 2, 3]]:
            candidate = {"checks": {"expected_mass_kg": value}}
            gates = quality._Gates()
            quality._author_expectations(gates, candidate, model, points)
            self.assertIn("physics.expected_mass", quality.required_checks(candidate))
            self.assertEqual(len(gates.checks), 1)
            self.assertFalse(gates.checks[0]["passed"], value)


class SurfaceOnlyGeometryExemptionTests(unittest.TestCase):
    """The zero-volume gate accepts only sealed, qualified surface-only CAD records."""

    QUALIFICATION = (
        "cad_authored_effective_instance; not geometry-derived, not independently physically qualified"
    )

    def _delivery(self, tmp, *, qualified=True):
        root = Path(tmp)
        occurrence = "pcb-1"
        record = {
            "schema_version": "solidworks-to-urdf.effective-instances/v1",
            "assembly": "robot.SLDASM",
            "configuration": "默认",
            "readings": {occurrence: {"role": "leaf", "document_type": "part"}} if qualified else {},
            "warnings": (
                [
                    {
                        "code": "cad_surface_only_effective_instance",
                        "blocking": False,
                        "component": occurrence,
                        "document": "board.SLDPRT",
                        "part_document_read": "unavailable",
                        "solid_bodies": 0,
                        "sheet_bodies": 2350,
                        "qualification": self.QUALIFICATION,
                    }
                ]
                if qualified
                else []
            ),
        }
        support.write_target_delivery(
            root,
            capture={
                "effective_instances": record,
                "components": [
                    {
                        "name": occurrence,
                        "mass_properties": {
                            "mode": "effective_instance",
                            "state": "effective_instance",
                            "reference": {"solid_bodies": 0, "sheet_bodies": 2350},
                        },
                    }
                ],
            },
        )
        return root

    def _stl_bytes(self, *, collinear):
        header = b"planar test mesh".ljust(80, b" ")
        if collinear:
            # One valid planar triangle plus one zero-area triangle: the mesh keeps
            # positive area and must be rejected by the degenerate-ratio gate.
            triangles = [
                ((0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0)),
                ((0.0, 0.0, 0.0), (0.0, 0.0, 0.0), (1.0, 0.0, 0.0)),
            ]
        else:
            triangles = [
                ((0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0)),
                ((0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (1.0, 1.0, 0.0)),
            ]
        payload = b"".join(struct.pack("<12fH", 0.0, 0.0, 1.0, *a, *b, *c, 0) for a, b, c in triangles)
        return header + struct.pack("<I", len(triangles)) + payload

    def _geometry_fixture(self, root, *, qualified, collinear, raw_payload=None):
        from description_pipeline.geometry.native_tessellation import (
            build_native_tessellation,
        )

        payload = raw_payload or self._stl_bytes(collinear=collinear)
        (root / "evidence/geometry").mkdir(parents=True, exist_ok=True)
        (root / "meshes/visual").mkdir(parents=True, exist_ok=True)
        (root / "evidence/geometry/0001_mesh.stl").write_bytes(payload)
        (root / "meshes/visual/board_0.stl").write_bytes(payload)
        digest = hashlib.sha256(payload).hexdigest()
        occurrence = "pcb-1"
        triangle_count = struct.unpack("<I", payload[80:84])[0]
        source_scope = support.fixture_source_scope(occurrence)
        tessellation = build_native_tessellation(
            occurrence,
            [
                {
                    "body_type": 1,
                    "body_index": 0,
                    "body_name": "sheet-fixture",
                    "face_count": 1,
                }
            ],
            [
                {
                    "body_type": 1,
                    "body_index": 0,
                    "face_index": 0,
                    "triangle_start": 0,
                    "triangle_count": triangle_count,
                }
            ],
            source_scope=source_scope,
        )
        node = ET.fromstring(
            '<link name="board"><visual><origin xyz="0 0 0" rpy="0 0 0"/>'
            '<geometry><mesh filename="../meshes/visual/board_0.stl" scale="1 1 1"/></geometry></visual></link>'
        )
        body = {"components": [occurrence]}
        model_link = {
            "inertial": {"xyz": [0.0, 0.0, 0.0], "inertia": [0.0, 0.0, 0.0, 0.0, 0.0, 0.0], "mass": 0.0},
            "visuals": [
                {
                    "filename": "geometry/0001_mesh.stl",
                    "sha256": digest,
                    "mesh_name": "meshes/visual/board_0.stl",
                    "scale": [1, 1, 1],
                    "xyz": [0, 0, 0],
                    "rpy": [0, 0, 0],
                    "provenance": {"native_component": occurrence},
                }
            ],
            "collisions": [],
        }
        geometry = {
            occurrence: {
                "path": "geometry/0001_mesh.stl",
                "sha": digest,
                "source_bytes": payload,
                "triangles": triangle_count,
                "bodies": {"solid": 0, "sheet": 1},
                "tessellation": tessellation,
            }
        }
        components = {occurrence: np.identity(4)}
        return node, body, model_link, geometry, components

    def _run_geometry(self, root, *, qualified, collinear, raw_payload=None):
        node, body, model_link, geometry, components = self._geometry_fixture(
            root, qualified=qualified, collinear=collinear, raw_payload=raw_payload
        )
        with mock.patch.object(
            quality,
            "_qualified_surface_only",
            return_value=qualified,
        ):
            return quality._geometry(
                root,
                body,
                node,
                model_link,
                geometry,
                components,
                np.identity(4),
                np.identity(4),
                set(),
            )

    def test_zero_volume_planar_mesh_is_accepted_when_qualified(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._run_geometry(Path(tmp), qualified=True, collinear=False)

    def test_zero_volume_planar_mesh_is_rejected_when_unqualified(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(PipelineError) as caught:
                self._run_geometry(Path(tmp), qualified=False, collinear=False)
            self.assertIn("Zero-volume", str(caught.exception))

    def _binary_mesh(self, triangles):
        header = b"gate mesh".ljust(80, b" ")
        payload = b"".join(struct.pack("<12fH", 0.0, 0.0, 1.0, *a, *b, *c, 0) for a, b, c in triangles)
        return header + struct.pack("<I", len(triangles)) + payload

    def _sliver(self, x, y):
        return ((x, y, 0.0), (x + 1e-7, y, 0.0), (x, y + 1e-7, 0.0))

    def _tetra(self, offset):
        v0 = (offset, 0.0, 0.0)
        v1 = (offset + 0.001, 0.0, 0.0)
        v2 = (offset, 0.001, 0.0)
        v3 = (offset, 0.0, 0.001)
        return [(v0, v2, v1), (v0, v1, v3), (v1, v2, v3), (v2, v0, v3)]

    def test_area_based_gate_fails_a_mostly_degenerate_mesh(self):
        triangles = self._tetra(0.0) + [self._sliver(0.01 + index * 1e-6, 0.02) for index in range(1200)]
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(PipelineError) as caught:
            self._run_geometry(
                Path(tmp), qualified=False, collinear=False, raw_payload=self._binary_mesh(triangles)
            )
        message = str(caught.exception)
        self.assertIn("area ratio", message)
        self.assertIn("count ratio", message)
        self.assertIn("1.0e-06", message)
        self.assertIn("1.0e-02", message)

    def test_area_based_gate_passes_many_tiny_slivers_on_a_large_mesh(self):
        triangles = [face for index in range(750) for face in self._tetra(index * 0.002)]
        triangles += [self._sliver(2.0 + index * 1e-6, 3.0) for index in range(30)]
        with tempfile.TemporaryDirectory() as tmp:
            self._run_geometry(
                Path(tmp), qualified=False, collinear=False, raw_payload=self._binary_mesh(triangles)
            )

    def test_stl_degenerate_area_is_accumulated_for_binary_and_ascii(self):
        from description_pipeline.geometry import stl

        triangles = [
            ((0.0, 0.0, 0.0), (0.001, 0.0, 0.0), (0.0, 0.001, 0.0)),
            self._sliver(0.01, 0.01),
            self._sliver(0.02, 0.02),
        ]
        sliver = struct.unpack("<f", struct.pack("<f", 1e-7))[0]
        valid = struct.unpack("<f", struct.pack("<f", 1e-3))[0]
        binary = stl.read_bytes(self._binary_mesh(triangles))
        text = "solid gate\n" + "".join(
            "facet normal 0 0 1\n" + "".join(f"vertex {v[0]} {v[1]} {v[2]}\n" for v in triangle) + "endfacet\n"
            for triangle in triangles
        ) + "endsolid gate\n"
        ascii_stats = stl.read_bytes(text.encode("ascii"))
        self.assertEqual(binary.degenerate, 2)
        self.assertAlmostEqual(binary.degenerate_area, sliver * sliver, places=12)
        self.assertAlmostEqual(binary.area, 0.5 * valid * valid + sliver * sliver, places=12)
        self.assertEqual(ascii_stats.degenerate, 2)
        self.assertAlmostEqual(ascii_stats.degenerate_area, 1e-14, places=12)
        self.assertAlmostEqual(ascii_stats.area, 5e-7 + 1e-14, places=12)

    def test_stl_rejects_non_ascii_bytes_without_repairing_geometry(self):
        from description_pipeline.geometry import stl

        valid = (
            b"solid mesh\nfacet normal 0 0 1\nouter loop\n"
            b"vertex 0 0 0\nvertex 1 0 0\nvertex 0 1 0\n"
            b"endloop\nendfacet\nendsolid mesh\n"
        )
        self.assertEqual(stl.read_bytes(valid).triangles, 1)
        np.testing.assert_array_equal(stl.vertices_bytes(valid), [[0, 0, 0], [1, 0, 0], [0, 1, 0]])
        for original, corrupted in (
            (b"vertex 1", b"v\xffertex 1"),
            (b"vertex 1", b"vertex \xff1"),
        ):
            damaged = valid.replace(original, corrupted)
            for reader in (stl.read_bytes, stl.vertices_bytes):
                with (
                    self.subTest(reader=reader.__name__, corrupted=corrupted),
                    self.assertRaisesRegex(stl.StlError, "non-ASCII"),
                ):
                    reader(damaged)

    def test_degenerate_plane_is_still_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(PipelineError) as caught:
                self._run_geometry(Path(tmp), qualified=True, collinear=True)
            self.assertIn("degenerate", str(caught.exception))

