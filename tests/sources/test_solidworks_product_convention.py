"""惯性积符号约定：``solidworks_positive`` 原始读数必须先转标准张量再旋转/缩放。

基准来自 `solidworks-bridge/docs/native-acceptance-20260917.md` 的解析长方体：
base 0.08×0.06×0.04 m / 1.4976 kg / RPY (0.2,-0.3,0.4)，
arm 0.025×0.04×0.10 m / 0.78 kg / RPY (-0.25,0.15,-0.35)。
SolidWorks 的正惯性积记法下，交叉项是 ``∫xy dm`` 等，标准张量要求取相反数；
生成端与独立校验端各自转换（互不引用），raw 原始读数保持不变。
"""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

import numpy as np

from . import _paths  # noqa: F401  (import side effect: sys.path)

from description_pipeline.io import file_digest  # noqa: E402
from description_pipeline.sources.solidworks.errors import ConfigError  # noqa: E402
from description_pipeline.sources.solidworks.freeze import freeze  # noqa: E402
from description_pipeline.sources.solidworks.scene import load_scene  # noqa: E402
from description_pipeline.sources.solidworks.scene import normalize_scene as canonical_for  # noqa: E402
from description_pipeline.sources.solidworks.verify import verify_normalization  # noqa: E402

from . import support  # noqa: E402

#: 审阅记录里公布的解析张量（零件轴向、质心处，kg·m²）
BENCHMARK: dict[str, dict[str, Any]] = {
    "base-1": {
        "dims": (0.08, 0.06, 0.04),
        "mass": 1.4976,
        "rpy": (0.2, -0.3, 0.4),
        "tensor": np.array(
            [
                [0.000736794738909252, -0.000100408007527001, -0.000135129433213249],
                [-0.000100408007527001, 0.000971871964885852, -0.000107539749708579],
                [-0.000135129433213249, -0.000107539749708579, 0.001186693296204896],
            ]
        ),
    },
    "arm-1": {
        "dims": (0.025, 0.04, 0.10),
        "mass": 0.78,
        "rpy": (-0.25, 0.15, -0.35),
        "tensor": np.array(
            [
                [0.000718668964410054, -0.000041995020835945, -0.000124319205163004],
                [-0.000041995020835945, 0.000679673987903405, -0.000092385813467567],
                [-0.000124319205163004, -0.000092385813467567, 0.000190907047686543],
            ]
        ),
    },
}


def _rpy_matrix(roll: float, pitch: float, yaw: float) -> np.ndarray:
    cr, sr = np.cos(roll), np.sin(roll)
    cp, sp = np.cos(pitch), np.sin(pitch)
    cy, sy = np.cos(yaw), np.sin(yaw)
    return np.array(
        [
            [cy * cp, cy * sp * sr - sy * cr, cy * sp * cr + sy * sr],
            [sy * cp, sy * sp * sr + cy * cr, sy * sp * cr - cy * sr],
            [-sp, cp * sr, cp * cr],
        ]
    )


def _cuboid_tensor(mass: float, dims: tuple[float, float, float], rpy: tuple[float, float, float]) -> np.ndarray:
    a, b, c = dims
    principal = np.diag([mass / 12 * (b * b + c * c), mass / 12 * (a * a + c * c), mass / 12 * (a * a + b * b)])
    rotation = _rpy_matrix(*rpy)
    return rotation @ principal @ rotation.T


def _products_matrix(tensor: np.ndarray) -> list[list[float]]:
    """标准张量 → SolidWorks 正惯性积记法的原始 9 个数（交叉项取反）。"""

    raw = tensor.copy()
    off_diagonal = ~np.eye(3, dtype=bool)
    raw[off_diagonal] = -tensor[off_diagonal]
    return [[float(value) for value in row] for row in raw]


class ProductConventionTests(unittest.TestCase):
    tmp: Path
    backend: support.FixtureCadBackend
    config: dict
    snapshot: Path
    raw_scene: dict

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="sw-convention-"))
        self.assembly = support.make_cad_tree(self.tmp / "cad")
        self.payload = self._benchmark_payload("base-1")
        self.backend = support.FixtureCadBackend(
            self.assembly,
            [{"name": "base-1", "transform": support.placement(), "mass": self.payload}],
            dependencies=[self.tmp / "cad" / "base.SLDPRT"],
        )
        self.config = {
            "provider": "solidworks",
            "assembly": str(self.assembly),
            "configuration": "Default",
            "allowed_roots": [str(self.tmp / "cad")],
            "geometry": {"enabled": False},
            "bodies": [{"id": "base", "name": "base_link", "components": ["base-1"]}],
            "joints": [],
        }

    def tearDown(self) -> None:
        support.cleanup(self.tmp)

    def _benchmark_payload(self, component: str, *, convention: str | None = "solidworks_positive") -> dict:
        entry = BENCHMARK[component]
        payload = support.mass_payload(
            entry["mass"],
            (0.01, -0.02, 0.03) if component == "base-1" else (0.015, 0.005, 0.055),
            _products_matrix(entry["tensor"]),
        )
        reference: dict = {"used_api": "IMassProperty2.GetMomentOfInertia(0)"}
        if convention is not None:
            reference["product_convention"] = convention
        payload["reference"] = reference
        return payload

    def _freeze(self, name: str = "snapshot", **overrides) -> Path:
        snapshot = self.tmp / name
        freeze(dict(self.config, **overrides), snapshot, backend=self.backend)
        return snapshot

    def _definition(self, **source_overrides) -> dict:
        source = dict(self.config)
        source.update(source_overrides)
        return {
            "schema_version": "description.definition/v1",
            "hardware_id": "fixture",
            "source": source,
            "overrides": [],
        }

    # --- 解析基准 -----------------------------------------------------

    def test_analytic_cuboid_matches_the_audited_benchmark(self) -> None:
        for component, entry in BENCHMARK.items():
            with self.subTest(component=component):
                analytic = _cuboid_tensor(entry["mass"], entry["dims"], entry["rpy"])
                self.assertLess(float(np.abs(analytic - entry["tensor"]).max()), 1e-15)

    # --- 生成端 -------------------------------------------------------

    def test_scene_converts_positive_products_to_the_standard_tensor(self) -> None:
        snapshot = self._freeze()
        scene = load_scene(snapshot)
        link = scene["links"][0]
        inertia = np.array([link["inertial"]["inertia"][index] for index in (0, 1, 2, 3, 4, 5)], dtype=float)
        expected = BENCHMARK["base-1"]["tensor"]
        # 场景里是 6 分量（ixx, ixy, ixz, iyy, iyz, izz）
        six = np.array([expected[0, 0], expected[0, 1], expected[0, 2], expected[1, 1], expected[1, 2], expected[2, 2]])
        self.assertLess(float(np.abs(inertia - six).max()), 1e-15)
        self.assertEqual(link["provenance"]["parts"][0]["product_convention"], "solidworks_positive")

        raw = json.loads((snapshot / "raw" / "mass_properties.json").read_text(encoding="utf-8"))
        self.assertGreater(raw["base-1"]["inertia"][0][1], 0.0)  # 原始正惯性积没有被改写

    def test_declared_mass_scales_the_converted_tensor_once(self) -> None:
        evidence = self.tmp / "spec.json"
        evidence.write_text(json.dumps({"components": {"base-1": {"material": "steel"}}}), encoding="utf-8")
        config = dict(
            self.config,
            material_source="documented_table",
            mass_evidence={
                "reference": "vendor spec",
                "file": "docs/provenance/spec.json",
                "sha256": file_digest(evidence),
            },
            documented_masses={"base-1": {"mass_kg": 2.9952, "reason": "2x benchmark mass", "evidence": "components"}},
        )
        snapshot = self.tmp / "scaled"
        freeze(config, snapshot, backend=self.backend)
        scene = load_scene(snapshot)
        inertia = scene["links"][0]["inertial"]["inertia"]
        expected = BENCHMARK["base-1"]["tensor"] * 2.0
        six = np.array([expected[0, 0], expected[0, 1], expected[0, 2], expected[1, 1], expected[1, 2], expected[2, 2]])
        self.assertLess(float(np.abs(np.array(inertia) - six).max()), 1e-14)
        # 再规范化一次不得重复缩放或重复转换
        scene_again = load_scene(snapshot)
        self.assertEqual(scene_again["links"][0]["inertial"]["inertia"], inertia)

    def test_native_reading_without_a_convention_is_refused(self) -> None:
        self.backend.components[0]["mass"] = self._benchmark_payload("base-1", convention=None)
        with self.assertRaises(ConfigError) as raised:
            self._freeze("no-convention")
        self.assertIn("product_convention", str(raised.exception))

    def test_fixture_reading_without_a_convention_is_treated_as_a_tensor(self) -> None:
        tensor = BENCHMARK["base-1"]["tensor"]
        payload = support.mass_payload(1.2, (0.0, 0.0, 0.0), [[float(v) for v in row] for row in tensor])
        self.backend.components[0]["mass"] = payload
        snapshot = self._freeze("fixture-no-convention")
        scene = load_scene(snapshot)
        six = np.array([tensor[0, 0], tensor[0, 1], tensor[0, 2], tensor[1, 1], tensor[1, 2], tensor[2, 2]])
        self.assertLess(float(np.abs(np.array(scene["links"][0]["inertial"]["inertia"]) - six).max()), 1e-18)

    # --- 独立校验端 ---------------------------------------------------

    def test_verifier_agrees_with_the_converted_tensor(self) -> None:
        snapshot = self._freeze("oracle-ok")
        raw_scene = support.read_scene(snapshot)
        definition = self._definition()
        canonical = canonical_for(raw_scene, definition, snapshot)
        results = verify_normalization(raw_scene, definition, snapshot, canonical)
        link = next(entry for entry in results if entry["id"].endswith("base_link"))
        self.assertEqual(link["status"], "passed", link["details"])

    def test_verifier_rejects_a_model_with_the_unconverted_sign(self) -> None:
        snapshot = self._freeze("oracle-tamper")
        raw_scene = support.read_scene(snapshot)
        definition = self._definition()
        canonical = canonical_for(raw_scene, definition, snapshot)
        tampered = copy.deepcopy(canonical)
        inertia = tampered["links"][0]["inertial"]["inertia"]
        for index in (1, 2, 4):  # ixy / ixz / iyz：旧实现漏取反时的符号
            inertia[index] = -inertia[index]
        results = verify_normalization(raw_scene, definition, snapshot, tampered)
        link = next(entry for entry in results if entry["id"].endswith("base_link"))
        self.assertEqual(link["status"], "failed")
        self.assertFalse(link["details"]["inertia_ok"])

    def test_unsupported_convention_is_refused(self) -> None:
        self.backend.components[0]["mass"] = self._benchmark_payload("base-1", convention="ansys_tensor")
        with self.assertRaises(ConfigError) as raised:
            self._freeze("unsupported")
        self.assertIn("product_convention", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
