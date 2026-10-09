"""Analytic cuboids retain signed cross terms in generation and independent verification.

The explicit synthetic native protocol exercises arithmetic only; it cannot
qualify a SolidWorks API or a hardware model.
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

    def _benchmark_payload(self, component: str, *, convention: str | None = "solidworks_standard") -> dict:
        entry = BENCHMARK[component]
        payload = support.mass_payload(
            entry["mass"],
            (0.01, -0.02, 0.03) if component == "base-1" else (0.015, 0.005, 0.055),
            entry["tensor"].tolist(),
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

    def test_scene_preserves_the_standard_signed_tensor(self) -> None:
        snapshot = self._freeze()
        scene = load_scene(snapshot)
        link = scene["links"][0]
        inertia = np.array([link["inertial"]["inertia"][index] for index in (0, 1, 2, 3, 4, 5)], dtype=float)
        expected = BENCHMARK["base-1"]["tensor"]
        # 场景里是 6 分量（ixx, ixy, ixz, iyy, iyz, izz）
        six = np.array([expected[0, 0], expected[0, 1], expected[0, 2], expected[1, 1], expected[1, 2], expected[2, 2]])
        self.assertLess(float(np.abs(inertia - six).max()), 1e-15)
        self.assertEqual(link["provenance"]["parts"][0]["product_convention"], "solidworks_standard")

        raw = json.loads((snapshot / "raw" / "mass_properties.json").read_text(encoding="utf-8"))
        self.assertLess(raw["base-1"]["inertia"][0][1], 0.0)  # Signed raw cross terms remain unchanged

    def test_declared_mass_scales_the_tensor_once(self) -> None:
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

    def test_fixture_readings_also_require_an_explicit_convention(self) -> None:
        payload = support.mass_payload(1.2, inertia=BENCHMARK["base-1"]["tensor"].tolist())
        payload["reference"].pop("product_convention")
        self.backend.components[0]["mass"] = payload
        with self.assertRaises(ConfigError):
            self._freeze("no-convention")

    def test_positive_product_convention_is_rejected(self) -> None:
        self.backend.components[0]["mass"] = self._benchmark_payload("base-1", convention="solidworks_positive")
        with self.assertRaises(ConfigError):
            self._freeze("unsupported-convention")

    # --- 独立校验端 ---------------------------------------------------

    def test_verifier_agrees_with_the_signed_tensor(self) -> None:
        snapshot = self._freeze("oracle-ok")
        raw_scene = support.read_scene(snapshot)
        definition = self._definition()
        canonical = canonical_for(raw_scene, definition, snapshot)
        results = verify_normalization(raw_scene, definition, snapshot, canonical)
        link = next(entry for entry in results if entry["id"].endswith("base_link"))
        self.assertEqual(link["status"], "passed", link["details"])

    def test_verifier_rejects_a_model_with_flipped_cross_terms(self) -> None:
        snapshot = self._freeze("oracle-tamper")
        raw_scene = support.read_scene(snapshot)
        definition = self._definition()
        canonical = canonical_for(raw_scene, definition, snapshot)
        tampered = copy.deepcopy(canonical)
        inertia = tampered["links"][0]["inertial"]["inertia"]
        for index in (1, 2, 4):  # ixy / ixz / iyz
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
