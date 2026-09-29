"""onshape_export 工具链测试：URL、缓存、装配体诊断、几何拆分、布局归一、核验。

不联网：真实文档用例只读 tests/fixtures/onshape 下的 Onshape 响应快照。
"""

import json
import os
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from typing import ClassVar
from unittest import mock

TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS))

try:  # MuJoCo 是可选依赖：装了才跑真加载相关用例
    import mujoco  # noqa: F401

    MUJOCO_AVAILABLE = True
except ModuleNotFoundError:  # pragma: no cover
    MUJOCO_AVAILABLE = False

from onshape_export import (  # noqa: E402
    assembly as assembly_module,
    checks,
    contacts,
    densities,
    geometry,
    layout,
    stl,
    url as url_module,
    verify,
)
from onshape_export.api import OnshapeClient, OnshapeError, load_credentials  # noqa: E402
from onshape_export.cache import ResponseCache  # noqa: E402
from onshape_export.cli import _joint_properties_for  # noqa: E402
from tests import onshape_fixture  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "onshape"
CACHE_FIXTURE = FIXTURES / "cache"
#: The captured identity stays in the fixture; see tests/onshape_fixture.py.
MICROBAN_SOURCE = (
    json.loads((CACHE_FIXTURE / "source.json").read_text(encoding="utf-8")) if onshape_fixture.AVAILABLE else {}
)
MICROBAN_ELEMENT = MICROBAN_SOURCE.get("element_id", "")
MICROBAN_STUDIO = onshape_fixture.mass_properties_ids("KFzB")[0] if onshape_fixture.AVAILABLE else ""


def reference(**overrides):
    base = {
        "document_id": "doc-1",
        "element_id": "asm-1",
        "workspace_id": "ws-1",
    }
    base.update(overrides)
    return url_module.DocumentRef(**base)


def assembly_payload(mates, instances=("root", "child")):
    features = []
    for name, occurrences, mate_type in mates:
        entities = [
            {
                "matedOccurrence": path,
                "matedCS": {"origin": [0, 0, 0], "xAxis": [1, 0, 0], "yAxis": [0, 1, 0], "zAxis": [0, 0, 1]},
            }
            for path in occurrences
        ]
        features.append(
            {
                "id": f"fid-{name}",
                "featureType": "mate",
                "featureData": {"name": name, "mateType": mate_type, "matedEntities": entities},
            }
        )
    return {
        "rootAssembly": {
            "instances": [{"id": item, "name": item, "type": "Assembly"} for item in instances],
            "occurrences": [
                {"path": [item], "transform": [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1]} for item in instances
            ],
            "features": features,
        }
    }


def features_payload(limits):
    return {
        "features": [
            {
                "typeName": "BTMMate",
                "message": {
                    "name": name,
                    "parameters": [
                        {"typeName": "BTMParameterBoolean", "message": {"parameterId": "limitsEnabled", "value": True}},
                        {
                            "typeName": "BTMParameterNullableQuantity",
                            "message": {"parameterId": "limitAxialZMin", "expression": f"{low} rad"},
                        },
                        {
                            "typeName": "BTMParameterNullableQuantity",
                            "message": {"parameterId": "limitAxialZMax", "expression": f"{high} rad"},
                        },
                    ],
                },
            }
            for name, (low, high) in limits.items()
        ]
    }


class UrlTests(unittest.TestCase):
    def test_full_url_workspace(self):
        ref = url_module.parse_reference("https://cad.onshape.com/documents/abc/w/def/e/ghi")
        self.assertEqual((ref.document_id, ref.workspace_id, ref.element_id), ("abc", "def", "ghi"))
        self.assertEqual(ref.wvm, "w")
        self.assertTrue(ref.url().endswith("/documents/abc/w/def/e/ghi"))

    def test_version_url(self):
        ref = url_module.parse_reference("https://cad.onshape.com/documents/abc/v/ver/e/ghi")
        self.assertEqual(ref.version_id, "ver")
        self.assertIsNone(ref.workspace_id)
        self.assertEqual(ref.wvm, "v")

    def test_explicit_ids_override_url(self):
        ref = url_module.parse_reference("https://cad.onshape.com/documents/abc/w/def/e/ghi", element_id="other")
        self.assertEqual(ref.element_id, "other")

    def test_missing_element_is_rejected(self):
        with self.assertRaises(url_module.ReferenceError):
            url_module.parse_reference("https://cad.onshape.com/documents/abc/w/def")

    def test_workspace_and_version_conflict(self):
        with self.assertRaises(url_module.ReferenceError):
            url_module.parse_reference(document_id="a", element_id="b", workspace_id="w", version_id="v")


class CacheAndClientTests(unittest.TestCase):
    def test_cache_roundtrip_and_miss(self):
        with tempfile.TemporaryDirectory() as folder:
            cache = ResponseCache(Path(folder))
            cache.save_json("sample", {"value": 1})
            cache.save_bytes("blob.bin", b"abc")
            self.assertEqual(cache.load_json("sample")["value"], 1)
            self.assertEqual(cache.load_bytes("blob.bin"), b"abc")
            with self.assertRaises(FileNotFoundError):
                cache.require_json("missing")

    def test_offline_client_refuses_network(self):
        with tempfile.TemporaryDirectory() as folder:
            client = OnshapeClient.from_env(cache_dir=Path(folder), offline=True)
            with self.assertRaises(OnshapeError):
                client.get_assembly(reference())

    def test_credentials_from_environment(self):
        with mock.patch.dict(
            os.environ,
            {"ONSHAPE_ACCESS_KEY": "ak", "ONSHAPE_SECRET_KEY": "sk", "ONSHAPE_API": "https://x"},
            clear=False,
        ):
            stack, access, secret, bearer = load_credentials()
        self.assertEqual((stack, access, secret, bearer), ("https://x", "ak", "sk", ""))

    def test_hmac_header_shape(self):
        client = OnshapeClient(access_key="ak", secret_key="sk")
        headers = client._headers("GET", "/api/x", {"a": "1"}, "application/json")
        self.assertTrue(headers["Authorization"].startswith("On ak:HmacSHA256:"))
        self.assertIn("On-Nonce", headers)


class AssemblyAndChecksTests(unittest.TestCase):
    def test_tree_and_limits(self):
        payload = assembly_payload(
            [
                ("dof_hinge", (("root",), ("child",)), "REVOLUTE"),
            ]
        )
        features = features_payload({"dof_hinge": (-0.5, 0.5)})
        parsed = assembly_module.Assembly.from_responses(reference(), payload, features, {})
        self.assertEqual(parsed.dof_mates[0].joint_name, "hinge")
        self.assertEqual(parsed.dof_mates[0].limits, (-0.5, 0.5))
        self.assertTrue(parsed.tree_report()["is_tree"])
        findings = checks.run_checks(parsed)
        self.assertEqual(checks.summarize(findings)["errors"], 0)

    def test_unresolved_mate_is_error(self):
        payload = assembly_payload([("dof_hinge", (("root",),), "REVOLUTE")])
        parsed = assembly_module.Assembly.from_responses(reference(), payload, {}, {})
        findings = checks.run_checks(parsed)
        codes = {finding.code for finding in findings}
        self.assertIn("OSX002", codes)

    def test_duplicate_mate_names_are_error(self):
        payload = assembly_payload(
            [
                ("dof_hinge", (("root",), ("child",)), "REVOLUTE"),
                ("dof_hinge", (("root",), ("child",)), "REVOLUTE"),
            ]
        )
        parsed = assembly_module.Assembly.from_responses(reference(), payload, {}, {})
        findings = checks.run_checks(parsed)
        self.assertIn("OSX003", {finding.code for finding in findings})

    def test_cycle_is_reported(self):
        payload = assembly_payload(
            [
                ("dof_a", (("a",), ("b",)), "REVOLUTE"),
                ("dof_b", (("b",), ("c",)), "REVOLUTE"),
                ("dof_c", (("c",), ("a",)), "REVOLUTE"),
            ],
            instances=("a", "b", "c"),
        )
        parsed = assembly_module.Assembly.from_responses(reference(), payload, {}, {})
        self.assertTrue(parsed.tree_report()["has_cycle"])
        self.assertIn("OSX004", {finding.code for finding in checks.run_checks(parsed)})

    def test_shared_frame_orphan_is_error(self):
        payload = assembly_payload(
            [
                ("dof_hinge", (("root",), ("child",)), "REVOLUTE"),
                ("frame_body", (("root",), ("orphan",)), "FASTENED"),
                ("frame_imu", (("child",), ("orphan",)), "FASTENED"),
            ],
            instances=("root", "child", "orphan"),
        )
        parsed = assembly_module.Assembly.from_responses(reference(), payload, {}, {})
        self.assertIn("OSX006", {finding.code for finding in checks.run_checks(parsed)})

    def test_no_dof_mates_is_error(self):
        payload = assembly_payload([("fix_part", (("root",), ("child",)), "FASTENED")])
        parsed = assembly_module.Assembly.from_responses(reference(), payload, {}, {})
        findings = checks.run_checks(parsed)
        self.assertIn("OSX001", {finding.code for finding in findings})

    def test_mass_check_only_covers_parts_used_in_assembly(self):
        payload = assembly_payload([("dof_hinge", (("root",), ("child",)), "REVOLUTE")])
        payload["rootAssembly"]["instances"][1].update({"type": "Part", "partId": "USED"})
        parsed = assembly_module.Assembly.from_responses(reference(), payload, {}, {})
        unused = {"STUDIO": {"UNUSED": {"hasMass": False}}}
        findings = checks.run_checks(parsed, mass_properties=unused)
        self.assertNotIn("OSX009", {finding.code for finding in findings})
        used = {"STUDIO": {"USED": {"hasMass": False}}}
        findings = checks.run_checks(parsed, mass_properties=used)
        self.assertIn("OSX009", {finding.code for finding in findings})

    def test_unclassified_dof_mate_is_error_without_auto(self):
        payload = assembly_payload([("hinge", (("root",), ("child",)), "REVOLUTE")])
        parsed = assembly_module.Assembly.from_responses(reference(), payload, {}, {})
        findings = checks.run_checks(parsed)
        self.assertIn("OSX010", {finding.code for finding in findings})
        self.assertEqual([finding.severity for finding in findings if finding.code == "OSX010"], [checks.ERROR])
        relaxed = checks.run_checks(parsed, auto_dof=True)
        self.assertEqual([finding.severity for finding in relaxed if finding.code == "OSX010"], [checks.INFO])

    def test_all_fastened_unclassified_is_only_info(self):
        payload = assembly_payload(
            [
                ("dof_hinge", (("root",), ("child",)), "REVOLUTE"),
                ("glue", (("root",), ("child",)), "FASTENED"),
            ]
        )
        parsed = assembly_module.Assembly.from_responses(reference(), payload, {}, {})
        codes = {finding.code: finding.severity for finding in checks.run_checks(parsed)}
        self.assertEqual(codes.get("OSX010b"), checks.INFO)
        self.assertNotIn("OSX010", codes)

    def test_apply_mate_roles_renames_by_type_and_avoids_collisions(self):
        payload = assembly_payload(
            [
                ("hinge", (("root",), ("child",)), "REVOLUTE"),
                ("glue", (("root",), ("child",)), "FASTENED"),
                ("dof_hinge", (("root",), ("child",)), "REVOLUTE"),
            ]
        )
        plan = assembly_module.mate_role_plan(payload)
        self.assertEqual(len(plan["unclassified"]), 2)
        self.assertTrue(plan["collisions"])  # dof_hinge 已存在 → 顺延命名
        rewritten = assembly_module.apply_mate_roles(payload)
        names = [feature["featureData"]["name"] for feature in rewritten["rootAssembly"]["features"]]
        self.assertIn("dof_hinge_2", names)
        self.assertIn("fix_glue", names)
        self.assertEqual(len(names), len(set(names)))  # 无重名
        # 原始数据不被修改
        self.assertEqual(
            [feature["featureData"]["name"] for feature in payload["rootAssembly"]["features"]],
            ["hinge", "glue", "dof_hinge"],
        )


@onshape_fixture.requires_fixture
class RealDocumentTests(unittest.TestCase):
    """用真实 Microban 文档快照做回归：这正是本工具要拦住的场景与最终产物。"""

    ref: ClassVar[object]
    assembly_json: ClassVar[dict]
    features_json: ClassVar[dict]
    mate_values: ClassVar[dict]
    assembly: ClassVar[assembly_module.Assembly]

    @classmethod
    def setUpClass(cls):
        folder = CACHE_FIXTURE / "json"
        cls.assembly_json = json.loads((folder / f"assembly_{MICROBAN_ELEMENT}.json").read_text(encoding="utf-8"))
        cls.features_json = json.loads(
            (folder / f"assembly_features_{MICROBAN_ELEMENT}.json").read_text(encoding="utf-8")
        )
        cls.mate_values = json.loads((folder / f"mate_values_{MICROBAN_ELEMENT}.json").read_text(encoding="utf-8"))
        cls.ref = url_module.parse_reference(MICROBAN_SOURCE["url"])
        cls.assembly = assembly_module.Assembly.from_responses(
            cls.ref, cls.assembly_json, cls.features_json, cls.mate_values
        )

    def test_real_assembly_has_tree_of_19_dof(self):
        self.assertEqual(len(self.assembly.dof_mates), 19)
        self.assertEqual(len(self.assembly.frame_mates), 4)
        report = self.assembly.tree_report()
        self.assertTrue(report["is_tree"])
        self.assertEqual(report["edges"], report["nodes"] - 1)

    def test_real_assembly_passes_preflight(self):
        findings = checks.run_checks(self.assembly)
        errors = [finding for finding in findings if finding.severity == checks.ERROR]
        self.assertEqual(errors, [])

    def test_real_mate_limits_present(self):
        limits = {mate.joint_name: mate.limits for mate in self.assembly.dof_mates}
        self.assertEqual(limits["right_knee"], (-0.785398, 2.35619))
        self.assertEqual(limits["head"], (-1.5708, 1.5708))
        self.assertTrue(all(value is not None for value in limits.values()))

    def test_world_frames_are_computed(self):
        mate = next(item for item in self.assembly.dof_mates if item.joint_name == "head")
        origin, axis = self.assembly.mate_world_frame(mate)
        self.assertAlmostEqual(origin[2], 0.126, places=4)
        self.assertAlmostEqual(abs(axis[2]), 1.0, places=6)

    def test_cache_fixture_is_usable_offline(self):
        """CI 冒烟：缓存布局能被离线客户端读取（真正的 CLI 冒烟在 workflow 里跑）。"""
        cache = ResponseCache(CACHE_FIXTURE)
        self.assertEqual(
            cache.require_json(f"assembly_{MICROBAN_ELEMENT}")["rootAssembly"]["instances"].__len__(),
            24,
        )
        self.assertTrue(cache.json_path(f"mass_properties_{MICROBAN_STUDIO}").is_file())


class GeometryTests(unittest.TestCase):
    def _cube_gltf(self, size=0.02):
        vertices = [
            (0, 0, 0),
            (size, 0, 0),
            (size, size, 0),
            (0, size, 0),
            (0, 0, size),
            (size, 0, size),
            (size, size, size),
            (0, size, size),
        ]
        faces = [
            (0, 2, 1),
            (0, 3, 2),
            (4, 5, 6),
            (4, 6, 7),
            (0, 1, 5),
            (0, 5, 4),
            (1, 2, 6),
            (1, 6, 5),
            (2, 3, 7),
            (2, 7, 6),
            (3, 0, 4),
            (3, 4, 7),
        ]
        positions = b"".join(struct.pack("<3f", *vertex) for vertex in vertices)
        indices = b"".join(struct.pack("<H", value) for face in faces for value in face)
        payload = positions + indices
        import base64

        uri = "data:application/octet-stream;base64," + base64.b64encode(payload).decode()
        return {
            "buffers": [{"uri": uri}],
            "bufferViews": [
                {"byteOffset": 0, "byteLength": len(positions)},
                {"byteOffset": len(positions), "byteLength": len(indices)},
            ],
            "accessors": [
                {"bufferView": 0, "byteOffset": 0, "componentType": 5126, "type": "VEC3", "count": len(vertices)},
                {"bufferView": 1, "byteOffset": 0, "componentType": 5123, "type": "SCALAR", "count": len(faces) * 3},
            ],
            "meshes": [{"primitives": [{"attributes": {"POSITION": 0}, "indices": 1}]}],
            "nodes": [{"name": "cube", "mesh": 0}],
        }

    def test_split_gltf_matches_part(self):
        gltf = self._cube_gltf(0.02)
        bodies = {"P1": {"volume": [0.02**3], "centroid": [0.01, 0.01, 0.01], "hasMass": True}}
        files, unresolved = geometry.split_gltf(gltf, bodies)
        self.assertEqual(unresolved, [])
        box = stl.bounds_bytes(files["P1"])
        self.assertEqual(box["triangles"], 12)
        self.assertAlmostEqual(box["max"][0] - box["min"][0], 0.02, places=6)

    def test_invalid_bytes_are_rejected_with_a_clear_error(self):
        """内存里的坏字节也要报可读错误，而不是泄出 NameError（历史缺陷）。"""

        with self.assertRaises(ValueError) as caught:
            stl.bounds_bytes(b"definitely not an STL")
        self.assertIn("STL", str(caught.exception))

    def test_split_gltf_reports_mismatch(self):
        gltf = self._cube_gltf(0.02)
        bodies = {"P1": {"volume": [0.5], "centroid": [9.0, 9.0, 9.0], "hasMass": True}}
        files, unresolved = geometry.split_gltf(gltf, bodies)
        self.assertEqual(files, {})
        self.assertTrue(unresolved)


class DensityOverrideTests(unittest.TestCase):
    def _body(self, mass=1.24, volume=1.0e-6):
        return {
            "volume": [volume],
            "mass": [mass],
            "centroid": [0.0, 0.0, 0.0] * 3,
            "inertia": [1e-6] * 27,
            "hasMass": True,
        }

    def test_rescale_scales_mass_and_inertia(self):
        volume = 1.0e-6
        body = self._body(mass=1240.0 * volume, volume=volume)  # 密度 1240 kg/m³
        scaled = densities.rescale_body(body, 957.0)
        self.assertAlmostEqual(scaled["mass"][0], 957.0 * volume, places=12)
        self.assertAlmostEqual(scaled["inertia"][0], 1e-6 * (957.0 / 1240.0), places=12)
        self.assertEqual(body["mass"][0], 1240.0 * volume)  # 原数据不变

    def test_apply_by_part_and_name(self):
        volume = 1.0e-6
        bodies = {"studio": {key: self._body(mass=1240.0 * volume, volume=volume) for key in ("P1", "P2", "P3")}}
        names = {"P1": "steel_shim__steel_shim", "P2": "shoulder__shoulder", "P3": "other__other"}
        result, applied = densities.apply_overrides(bodies, {"P1": 7850.0}, {"shoulder": 957.0}, names)
        self.assertAlmostEqual(result["studio"]["P1"]["mass"][0], 7850.0 * volume, places=12)
        self.assertAlmostEqual(result["studio"]["P2"]["mass"][0], 957.0 * volume, places=12)
        self.assertEqual(len(applied), 2)
        self.assertEqual(result["studio"]["P3"]["mass"][0], 1240.0 * volume)  # 未覆盖

    def test_unknown_name_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "map.json"
            path.write_text(
                json.dumps(
                    {
                        "names": {"not_a_part": 1000.0},
                        "PLA 15% infill (equivalent)": {"density_kg_m3": 957},
                    }
                ),
                encoding="utf-8",
            )
            by_part, by_name, _ = densities.load_overrides(path)
            self.assertEqual(by_name, {"not_a_part": 1000.0})
            with self.assertRaises(densities.DensityError):
                densities.apply_overrides({"studio": {"P1": self._body()}}, by_part, by_name, {"P1": "x"})

    def test_density_out_of_range_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "map.json"
            path.write_text(json.dumps({"parts": {"P1": 99999.0}}), encoding="utf-8")
            with self.assertRaises(densities.DensityError):
                densities.load_overrides(path)

    def test_part_names_from_assembly(self):
        payload = assembly_payload([("dof_hinge", (("root",), ("child",)), "REVOLUTE")])
        payload["rootAssembly"]["instances"][0].update(
            {"type": "Part", "partId": "P1", "name": "steel_shim__steel_shim <1>"}
        )
        names = densities.part_names_from_assembly(payload)
        self.assertEqual(names["P1"], "steel_shim__steel_shim")


class LayoutAndVerifyTests(unittest.TestCase):
    def _write_engine_output(self, folder: Path) -> None:
        assets = folder / "assets"
        assets.mkdir(parents=True)
        (assets / "part.stl").write_bytes(_binary_stl([(0, 0, 0), (1, 0, 0), (0, 1, 0)]))
        (folder / "robot.urdf").write_text(
            """<?xml version="1.0" ?><robot name="engine-output">
  <link name="base"><inertial><mass value="0.5"/><origin xyz="0 0 0"/>
    <inertia ixx="0.001" ixy="0" ixz="0" iyy="0.001" iyz="0" izz="0.001"/></inertial>
    <visual><geometry><mesh filename="assets/part.stl"/></geometry></visual></link>
  <link name="tip"><inertial><mass value="0.2"/><origin xyz="0 0 0"/>
    <inertia ixx="0.0004" ixy="0" ixz="0" iyy="0.0004" iyz="0" izz="0.0004"/></inertial></link>
  <joint name="hinge" type="revolute">
    <parent link="base"/><child link="tip"/>
    <origin xyz="0 0 1" rpy="0 0 0"/><axis xyz="0 0 1"/>
    <limit lower="-1" upper="1"/>
  </joint>
</robot>
""",
            encoding="utf-8",
        )
        (folder / "robot.xml").write_text(
            """<mujoco model="engine-output"><compiler meshdir="assets"/>
  <asset><mesh name="part.stl" file="part.stl"/></asset>
  <worldbody><body name="base"><inertial mass="0.5" pos="0 0 0"/></body></worldbody>
</mujoco>
""",
            encoding="utf-8",
        )

    def test_normalize_rewrites_layout(self):
        with tempfile.TemporaryDirectory() as folder:
            engine_dir = Path(folder) / "engine"
            root = Path(folder) / "out"
            self._write_engine_output(engine_dir)
            manifest = layout.normalize(engine_dir, root)
            urdf = (root / "urdf" / "robot.urdf").read_text(encoding="utf-8")
            self.assertIn('filename="../meshes/part.stl"', urdf)
            self.assertIn('<robot name="robot"', urdf)
            self.assertTrue((root / "meshes" / "part.stl").is_file())
            mjcf = (root / "mjcf" / "robot.xml").read_text(encoding="utf-8")
            self.assertIn('meshdir="../meshes"', mjcf)
            self.assertEqual(manifest["urdf"], "urdf/robot.urdf")

    def test_normalize_refuses_conflicting_mesh(self):
        with tempfile.TemporaryDirectory() as folder:
            engine_dir = Path(folder) / "engine"
            root = Path(folder) / "out"
            self._write_engine_output(engine_dir)
            meshes = root / "meshes"
            meshes.mkdir(parents=True)
            (meshes / "part.stl").write_bytes(b"different")
            with self.assertRaises(layout.LayoutError):
                layout.normalize(engine_dir, root)

    def test_normalize_stabilises_engine_temp_class_name(self):
        """引擎把临时目录名写进 MJCF 默认 class；入库必须是稳定值。"""

        with tempfile.TemporaryDirectory() as folder:
            engine_dir = Path(folder) / "engine"
            root = Path(folder) / "out"
            self._write_engine_output(engine_dir)
            text = (
                (engine_dir / "robot.xml")
                .read_text(encoding="utf-8")
                .replace(
                    '<mujoco model="engine-output">',
                    '<mujoco model="engine-output"><default class="onshape-export-ab12cd34">'
                    '<joint armature="0.005"/></default>',
                )
                .replace('<body name="base">', '<body name="base" childclass="onshape-export-ab12cd34">')
            )
            (engine_dir / "robot.xml").write_text(text, encoding="utf-8")
            layout.normalize(engine_dir, root)
            mjcf = (root / "mjcf" / "robot.xml").read_text(encoding="utf-8")
            self.assertNotIn("onshape-export-", mjcf)
            self.assertIn('class="robot"', mjcf)
            self.assertIn('childclass="robot"', mjcf)

    def test_verify_accepts_consistent_export(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self._write_engine_output(root / "engine")
            layout.normalize(root / "engine", root)
            (root / "mjcf" / "scene.xml").write_text(
                '<mujoco model="scene"><include file="robot.xml"/></mujoco>', encoding="utf-8"
            )
            report = verify.verify(root)
            self.assertTrue(report["summary"]["passed"], report["findings"])
            self.assertEqual(report["structure"]["links"], 2)

    def test_verify_detects_missing_mesh_and_zero_mass(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self._write_engine_output(root / "engine")
            layout.normalize(root / "engine", root)
            (root / "meshes" / "part.stl").unlink()
            text = (
                (root / "urdf" / "robot.urdf")
                .read_text(encoding="utf-8")
                .replace('<mass value="0.2"/>', '<mass value="0"/>')
            )
            (root / "urdf" / "robot.urdf").write_text(text, encoding="utf-8")
            report = verify.verify(root)
            codes = {finding["code"] for finding in report["findings"]}
            self.assertIn("OSV003b", codes)
            self.assertIn("OSV005", codes)

    def test_verify_flags_mirrored_asymmetric_limit(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self._write_engine_output(root / "engine")
            layout.normalize(root / "engine", root)
            payload = assembly_payload([("dof_hinge", (("base",), ("tip",)), "REVOLUTE")], instances=("base", "tip"))
            payload["rootAssembly"]["occurrences"][1]["transform"] = [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 1, 0, 0, 0, 1]
            parsed = assembly_module.Assembly.from_responses(reference(), payload, {}, {})
            # 源 mate 轴为 +Z，导出轴在 root 系为 +Z：方向一致 → 只有非对称限位的告警才会出现
            text = (
                (root / "urdf" / "robot.urdf")
                .read_text(encoding="utf-8")
                .replace('<axis xyz="0 0 1"/>', '<axis xyz="0 0 -1"/>')
                .replace('<origin xyz="0 0 1" rpy="0 0 0"/>', '<origin xyz="0 0 0" rpy="0 0 0"/>')
                .replace('<limit lower="-1" upper="1"/>', '<limit lower="-2" upper="1"/>')
            )
            (root / "urdf" / "robot.urdf").write_text(text, encoding="utf-8")
            report = verify.verify(root, assembly=parsed)
            codes = {finding["code"] for finding in report["findings"]}
            self.assertIn("OSV012", codes)

    def test_reference_comparison_detects_mirrored_range(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self._write_engine_output(root / "engine")
            layout.normalize(root / "engine", root)
            reference = root / "reference.urdf"
            reference.write_text(
                (root / "urdf" / "robot.urdf")
                .read_text(encoding="utf-8")
                .replace('<limit lower="-1" upper="1"/>', '<limit lower="-2" upper="1"/>'),
                encoding="utf-8",
            )
            links, joints = verify.parse_urdf(root / "urdf" / "robot.urdf")
            findings, details = verify.compare_with_reference(links, joints, reference)
            self.assertIn("OSV023", {finding.code for finding in findings})
            self.assertEqual(details["summary"]["range_mismatch"], 1)
            self.assertEqual(details["joints"][0]["verdict"], "range_mismatch")

    def test_reference_comparison_passes_for_identical_model(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self._write_engine_output(root / "engine")
            layout.normalize(root / "engine", root)
            links, joints = verify.parse_urdf(root / "urdf" / "robot.urdf")
            findings, details = verify.compare_with_reference(links, joints, root / "urdf" / "robot.urdf")
            self.assertEqual([item for item in findings if item.severity == verify.ERROR], [])
            self.assertIn("OSV025", {finding.code for finding in findings})
            self.assertEqual(details["summary"]["verdicts"], ["ok"])
            self.assertEqual(details["summary"]["joints_compared"], 1)
            self.assertEqual(details["joints"][0]["position_error_m"], 0.0)

    def test_reference_comparison_matches_every_joint(self):
        """回归：连杆映射必须按 ref→导出 方向使用，否则会漏配关节（曾只比中 1 个）。"""

        def model(prefix: str) -> str:
            return f"""<?xml version="1.0" ?><robot name="robot">
  <link name="{prefix}base"><inertial><mass value="1"/><origin xyz="0 0 0"/></inertial></link>
  <link name="{prefix}mid"><inertial><mass value="1"/><origin xyz="0 0 0.1"/></inertial></link>
  <link name="{prefix}tip"><inertial><mass value="1"/><origin xyz="0 0 0.2"/></inertial></link>
  <joint name="j1" type="revolute"><parent link="{prefix}base"/><child link="{prefix}mid"/>
    <origin xyz="0 0 0.1" rpy="0 0 0"/><axis xyz="0 1 0"/><limit lower="-1" upper="1"/></joint>
  <joint name="j2" type="revolute"><parent link="{prefix}mid"/><child link="{prefix}tip"/>
    <origin xyz="0 0 0.1" rpy="0 0 0"/><axis xyz="0 0 1"/><limit lower="-0.5" upper="2"/></joint>
</robot>
"""

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "ours.urdf").write_text(model("a_"), encoding="utf-8")
            (root / "ref.urdf").write_text(model("b_"), encoding="utf-8")
            links, joints = verify.parse_urdf(root / "ours.urdf")
            findings, details = verify.compare_with_reference(links, joints, root / "ref.urdf")
            codes = {finding.code for finding in findings}
            self.assertEqual(details["summary"]["joints_compared"], 2)
            self.assertNotIn("OSV020d", codes)
            self.assertEqual(details["summary"]["verdicts"], ["ok"])

    def test_reference_comparison_handles_swapped_roles_and_opposite_axis(self):
        """父子互换 + 轴反向（= 关节角语义相同）：非对称限位必须判定为等价。"""

        ours = """<?xml version="1.0" ?><robot name="robot">
  <link name="b_hip"><inertial><mass value="1"/><origin xyz="0 0 0"/></inertial></link>
  <link name="b_trunk"><inertial><mass value="1"/><origin xyz="0 0 -0.1"/></inertial></link>
  <joint name="yaw" type="revolute"><parent link="b_hip"/><child link="b_trunk"/>
    <origin xyz="0 0 0" rpy="0 0 0"/><axis xyz="0 0 -1"/>
    <limit lower="-4.18879" upper="1.0472"/></joint>
</robot>
"""
        ref = """<?xml version="1.0" ?><robot name="robot">
  <link name="a_trunk"><inertial><mass value="1"/><origin xyz="0 0 -0.1"/></inertial></link>
  <link name="a_hip"><inertial><mass value="1"/><origin xyz="0 0 0"/></inertial></link>
  <joint name="yaw" type="revolute"><parent link="a_trunk"/><child link="a_hip"/>
    <origin xyz="0 0 0" rpy="0 0 0"/><axis xyz="0 0 1"/>
    <limit lower="-4.18879" upper="1.0472"/></joint>
</xml>
""".replace("</xml>", "</robot>")

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "ours.urdf").write_text(ours, encoding="utf-8")
            (root / "ref.urdf").write_text(ref, encoding="utf-8")
            links, joints = verify.parse_urdf(root / "ours.urdf")
            findings, details = verify.compare_with_reference(links, joints, root / "ref.urdf")
            self.assertEqual([f for f in findings if f.severity == verify.ERROR], [])
            row = details["joints"][0]
            self.assertEqual(row["roles"], "swapped")
            self.assertEqual(row["axis_direction"], "opposite")
            self.assertEqual(row["range_flip"], 1)
            self.assertEqual(row["verdict"], "ok")

    def test_inertia_validity_and_normalised_comparison(self):
        urdf = """<?xml version="1.0" ?><robot name="robot">
  <link name="base"><inertial><mass value="1"/><origin xyz="0 0 0"/>
    <inertia ixx="0.01" ixy="0" ixz="0" iyy="0.01" iyz="0" izz="0.01"/></inertial></link>
  <link name="bad"><inertial><mass value="1"/><origin xyz="0 0 0"/>
    <inertia ixx="0.9" ixy="0" ixz="0" iyy="0.01" iyz="0" izz="0.01"/></inertial></link>
  <joint name="j" type="revolute"><parent link="base"/><child link="bad"/>
    <origin xyz="0 0 0.1" rpy="0 0 0"/><axis xyz="0 0 1"/>
    <limit effort="1" velocity="1" lower="-1" upper="1"/></joint>
</robot>
"""
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "model.urdf").write_text(urdf, encoding="utf-8")
            links, _ = verify.parse_urdf(root / "model.urdf")
            findings = verify._check_inertia(links)
            self.assertIn("OSV030c", {finding.code for finding in findings})  # 0.9 > 0.01+0.01

            # 同一几何、密度 ×4：质量与惯量同时 ×4，I/m 应保持不变
            heavier = (
                urdf.replace('<mass value="1"/>', '<mass value="4"/>')
                .replace('ixx="0.01"', 'ixx="0.04"')
                .replace('iyy="0.01"', 'iyy="0.04"')
                .replace('izz="0.01"', 'izz="0.04"')
                .replace('ixx="0.9"', 'ixx="3.6"')
            )
            (root / "ref.urdf").write_text(heavier, encoding="utf-8")
            verify.parse_urdf(root / "model.urdf")
            verify.parse_urdf(root / "ref.urdf")
            details: dict = {"summary": {}}
            ref_links, _ = verify.parse_urdf(root / "ref.urdf")
            mapping = {"base": "base", "bad": "bad"}
            findings = verify._compare_inertias(links, ref_links, mapping, details)
            codes = {finding.code for finding in findings}
            self.assertIn("OSV033", codes)  # I/m 与质量无关，应判定为一致
            self.assertNotIn("OSV032", codes)

    def test_effort_velocity_compared_with_reference(self):
        our = {"j": {"effort": 10.0, "velocity": 10.0}}
        same = {"j": {"effort": 10.0, "velocity": 10.0}}
        other = {"j": {"effort": 0.64, "velocity": 5.0}}
        codes = {f.code for f in verify._check_effort_velocity(our, same)}
        self.assertIn("OSV034b", codes)
        self.assertTrue(all(f.severity == verify.WARNING for f in verify._check_effort_velocity(our, same)))
        self.assertIn("OSV034", {f.code for f in verify._check_effort_velocity(our, other)})

    def test_massless_frame_link_with_zero_inertia_is_allowed(self):
        urdf = """<?xml version="1.0" ?><robot name="robot">
  <link name="body_frame"><inertial><mass value="1e-9"/><origin xyz="0 0 0"/>
    <inertia ixx="0" ixy="0" ixz="0" iyy="0" iyz="0" izz="0"/></inertial></link>
  <link name="part"><inertial><mass value="0.5"/><origin xyz="0 0 0"/>
    <inertia ixx="0.001" ixy="0" ixz="0" iyy="0.001" iyz="0" izz="0.001"/></inertial></link>
  <joint name="j" type="fixed"><parent link="part"/><child link="body_frame"/>
    <origin xyz="0 0 0" rpy="0 0 0"/></joint>
</robot>
"""
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "model.urdf"
            path.write_text(urdf, encoding="utf-8")
            links, _ = verify.parse_urdf(path)
            findings = verify._check_inertia(links)
            self.assertEqual([f for f in findings if f.severity == verify.ERROR], [])

    def test_reports_avoid_machine_specific_paths(self):
        """入库的报告不能带本机绝对路径：参照模型只记文件名 + SHA-256。"""

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self._write_engine_output(root / "engine")
            layout.normalize(root / "engine", root)
            reference = root / "reference.urdf"
            reference.write_text((root / "urdf" / "robot.urdf").read_text(encoding="utf-8"), encoding="utf-8")
            report = verify.verify(root, reference=reference)
            identity = report["reference_comparison"]["reference"]
            self.assertEqual(identity["file"], "reference.urdf")
            self.assertRegex(identity["sha256"] or "", r"^[0-9a-f]{64}$")
            self.assertNotIn("root", report)  # 报告不记录导出目录，避免机器相关字段
            self.assertNotIn(str(root.parent), json.dumps(report))

    def test_joint_properties_are_filtered_per_output_format(self):
        """MJCF 的 type=motor 不能写进 URDF 的关节类型，反之 URDF 专用键不进 MJCF。"""

        properties = {
            "default": {"type": "motor", "damping": 0.041, "armature": 0.0018, "max_effort": 0.64, "max_velocity": 5.0}
        }
        urdf = _joint_properties_for(properties, "urdf")
        mjcf = _joint_properties_for(properties, "mjcf")
        self.assertEqual(urdf, {"default": {"max_effort": 0.64, "max_velocity": 5.0}})
        self.assertEqual(mjcf, {"default": {"type": "motor", "damping": 0.041, "armature": 0.0018}})

    def test_invalid_joint_type_is_reported(self):
        urdf = """<?xml version="1.0" ?><robot name="robot">
  <link name="base"><inertial><mass value="1"/><origin xyz="0 0 0"/>
    <inertia ixx="0.001" ixy="0" ixz="0" iyy="0.001" iyz="0" izz="0.001"/></inertial></link>
  <link name="tip"><inertial><mass value="1"/><origin xyz="0 0 0.1"/>
    <inertia ixx="0.001" ixy="0" ixz="0" iyy="0.001" iyz="0" izz="0.001"/></inertial></link>
  <joint name="j" type="motor"><parent link="base"/><child link="tip"/>
    <origin xyz="0 0 0.1" rpy="0 0 0"/><axis xyz="0 0 1"/></joint>
</robot>
"""
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "model.urdf"
            path.write_text(urdf, encoding="utf-8")
            links, joints = verify.parse_urdf(path)
            codes = {finding.code for finding in verify._check_tree(links, joints)}
            self.assertIn("OSV002e", codes)

    @unittest.skipUnless(MUJOCO_AVAILABLE, "需要 mujoco")
    def test_mjcf_dynamics_comparison_detects_parameter_drift(self):
        template = """<mujoco model="robot"><compiler angle="radian"/>
  <default><default class="robot">
    <joint damping="{damping}" frictionloss="{friction}" armature="{armature}"/>
  </default></default>
  <worldbody><body name="base"><freejoint/>
    <inertial mass="1" pos="0 0 0" diaginertia="1e-3 1e-3 1e-3"/>
    <body name="tip"><joint name="j" type="hinge" class="robot" axis="0 0 1"/>
      <inertial mass="1" pos="0 0 0.01" diaginertia="1e-4 1e-4 1e-4"/></body>
  </body></worldbody>
  <actuator><{actuator} name="j" joint="j"/></actuator>
</mujoco>
"""

        def write(path: Path, **kwargs):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(template.format(**kwargs), encoding="utf-8")

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            write(root / "mjcf" / "robot.xml", damping=0.041, friction=0.013, armature=0.0018, actuator="motor")
            reference = root / "reference.xml"
            write(reference, damping=0.041, friction=0.013, armature=0.0018, actuator="motor")
            matches = verify._compare_mjcf_dynamics(root, reference)
            self.assertTrue(all(finding.severity != verify.ERROR for finding in matches))

            write(root / "mjcf" / "robot.xml", damping=0.1, friction=0.1, armature=0.005, actuator="position")
            drifted = verify._compare_mjcf_dynamics(root, reference)
            codes = {finding.code for finding in drifted if finding.severity == verify.ERROR}
            self.assertIn("OSV040", codes)
            self.assertIn("OSV041", codes)


class ContactExcludeTests(unittest.TestCase):
    """接触排除：输入校验、写进 MJCF、以及网格顺序可复现。"""

    MJCF = """<mujoco model="engine-output"><compiler meshdir="assets"/>
  <asset><mesh name="b" file="b.stl"/><mesh name="a" file="a.stl"/></asset>
  <worldbody>
    <body name="base"><inertial mass="0.5" pos="0 0 0"/></body>
    <body name="tip"><inertial mass="0.2" pos="0 0 1"/></body>
  </worldbody>
</mujoco>
"""

    def _write_engine_output(self, folder: Path, mjcf: str) -> None:
        (folder / "assets").mkdir(parents=True)
        (folder / "robot.xml").write_text(mjcf, encoding="utf-8")
        (folder / "robot.urdf").write_text(
            '<?xml version="1.0" ?><robot name="engine-output">\n'
            '  <link name="base"><inertial><mass value="0.5"/><origin xyz="0 0 0"/>'
            '<inertia ixx="0.001" ixy="0" ixz="0" iyy="0.001" iyz="0" izz="0.001"/></inertial>'
            "</link>\n</robot>\n",
            encoding="utf-8",
        )

    def test_load_accepts_bare_list_and_object(self):
        with tempfile.TemporaryDirectory() as folder:
            bare = Path(folder) / "bare.json"
            bare.write_text(json.dumps([["a", "b"]]), encoding="utf-8")
            self.assertEqual(contacts.load(bare), [("a", "b")])
            wrapped = Path(folder) / "wrapped.json"
            wrapped.write_text(
                json.dumps({"source": {"reason": "设计贴合"}, "excludes": [["b", "c"]]}),
                encoding="utf-8",
            )
            self.assertEqual(contacts.load(wrapped), [("b", "c")])

    def test_load_rejects_malformed_entries(self):
        cases = {
            "not-a-list.json": {"excludes": "base"},
            "self-pair.json": {"excludes": [["base", "base"]]},
            "duplicate.json": {"excludes": [["a", "b"], ["b", "a"]]},
            "empty.json": {"excludes": []},
            "triple.json": {"excludes": [["a", "b", "c"]]},
            "blank.json": {"excludes": [["a", " "]]},
        }
        with tempfile.TemporaryDirectory() as folder:
            for name, payload in cases.items():
                with self.subTest(name=name):
                    path = Path(folder) / name
                    path.write_text(json.dumps(payload), encoding="utf-8")
                    with self.assertRaises(contacts.ContactError):
                        contacts.load(path)
            broken = Path(folder) / "broken.json"
            broken.write_text("{not json", encoding="utf-8")
            with self.assertRaises(contacts.ContactError):
                contacts.load(broken)

    def test_normalize_writes_contact_excludes_and_records_them(self):
        with tempfile.TemporaryDirectory() as folder:
            engine_dir = Path(folder) / "engine"
            root = Path(folder) / "out"
            self._write_engine_output(engine_dir, self.MJCF)
            manifest = layout.normalize(engine_dir, root, contact_excludes=[("base", "tip")])
            mjcf = (root / "mjcf" / "robot.xml").read_text(encoding="utf-8")
            self.assertIn('<exclude body1="base" body2="tip"/>', mjcf)
            self.assertLess(mjcf.index("</worldbody>"), mjcf.index("<contact>"))
            self.assertEqual(contacts.declared(mjcf), [("base", "tip")])
            self.assertEqual(manifest["contact_excludes"], [["base", "tip"]])
            # URDF 没有接触排除机制：不应被改写
            self.assertNotIn("contact", (root / "urdf" / "robot.urdf").read_text(encoding="utf-8"))

    def test_normalize_rejects_unknown_contact_body(self):
        with tempfile.TemporaryDirectory() as folder:
            engine_dir = Path(folder) / "engine"
            root = Path(folder) / "out"
            self._write_engine_output(engine_dir, self.MJCF)
            with self.assertRaises(contacts.ContactError) as caught:
                layout.normalize(engine_dir, root, contact_excludes=[("base", "typo")])
            self.assertIn("typo", str(caught.exception))
            self.assertIn("base", str(caught.exception))

    def test_normalize_sorts_asset_meshes_for_reproducible_output(self):
        """引擎按 set 遍历网格，顺序随 PYTHONHASHSEED 变化；导出必须字节可复现。"""

        reversed_mjcf = self.MJCF.replace(
            '<mesh name="b" file="b.stl"/><mesh name="a" file="a.stl"/>',
            '<mesh name="a" file="a.stl"/><mesh name="b" file="b.stl"/>',
        )
        with tempfile.TemporaryDirectory() as folder:
            first, second = Path(folder) / "one", Path(folder) / "two"
            self._write_engine_output(first, self.MJCF)
            self._write_engine_output(second, reversed_mjcf)
            layout.normalize(first, Path(folder) / "out-one")
            layout.normalize(second, Path(folder) / "out-two")
            one = (Path(folder) / "out-one" / "mjcf" / "robot.xml").read_text(encoding="utf-8")
            two = (Path(folder) / "out-two" / "mjcf" / "robot.xml").read_text(encoding="utf-8")
            self.assertEqual(one, two)
            self.assertLess(one.index('file="a.stl"'), one.index('file="b.stl"'))

    def test_verify_reports_declared_contact_excludes(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "mjcf").mkdir(parents=True)
            (root / "mjcf" / "robot.xml").write_text(
                self.MJCF.replace(
                    "</worldbody>",
                    '</worldbody>\n  <contact>\n    <exclude body1="base" body2="tip"/>\n  </contact>',
                ),
                encoding="utf-8",
            )
            found = verify._check_contact_excludes(root / "mjcf" / "robot.xml")
            self.assertEqual([finding.code for finding in found], ["OSV036"])
            self.assertEqual(found[0].severity, verify.INFO)
            self.assertEqual(found[0].context["pairs"], [["base", "tip"]])
            (root / "mjcf" / "robot.xml").write_text(self.MJCF, encoding="utf-8")
            self.assertEqual(verify._check_contact_excludes(root / "mjcf" / "robot.xml"), [])


def _binary_stl(vertices):
    out = bytearray(b"\0" * 80)
    out += struct.pack("<I", 1)
    out += struct.pack("<3f", 0.0, 0.0, 1.0)
    for vertex in vertices:
        out += struct.pack("<3f", *vertex)
    out += b"\0\0"
    return bytes(out)


if __name__ == "__main__":
    unittest.main()
