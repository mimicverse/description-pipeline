"""质量来源合同：documented_table 必须覆盖全部纳入组件，独立校验要能自己发现缺口。

覆盖三类错误注入（生成侧 + 独立校验侧）：
* 表格漏条目 → documented_table 直接拒绝，校验侧 `source.normalization.mass_provenance` 也要报出来；
* 表格拼错/多余键 → 配置、加载、校验三处都拒绝；
* 严格 CAD 模式遇到"材料未验证"的读数 → 不允许把默认密度占位质量当读数（缺声明即失败）。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from . import _paths  # noqa: F401  (import side effect: sys.path)

from description_pipeline.sources.solidworks.errors import ConfigError  # noqa: E402
from description_pipeline.sources.solidworks.freeze import freeze, validate_source_config  # noqa: E402
from description_pipeline.sources.solidworks.scene import load_scene  # noqa: E402
from description_pipeline.sources.solidworks.scene import normalize_scene as canonical_for  # noqa: E402
from description_pipeline.sources.solidworks.verify import verify_normalization  # noqa: E402

from . import support  # noqa: E402


def _detail(error: Exception) -> dict:
    """BridgeError.detail 是 object：测试里显式窄化，避免 mypy 报索引错误。"""

    detail = getattr(error, "detail", None)
    assert isinstance(detail, dict)
    return detail


def _documented(mass_kg: float) -> dict:
    return {"mass_kg": mass_kg, "reason": "vendor drawing", "evidence": "COMPONENT"}


def _binding() -> dict:
    """形状合法的证据文件绑定；文件本身由独立校验在模型仓库里核对。"""

    return {
        "reference": "vendor drawing bundle (fixture)",
        "file": "docs/provenance/spec.json",
        "sha256": "0" * 64,
    }


class MassEvidenceContractTests(unittest.TestCase):
    tmp: Path
    snapshot: Path
    config: dict
    raw_scene: dict

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="sw-mass-evidence-"))
        self.assembly = support.make_cad_tree(self.tmp / "cad")
        self.backend = support.FixtureCadBackend(
            self.assembly,
            [
                {"name": "base-1", "transform": support.placement(), "mass": support.mass_payload(1.0)},
                {"name": "arm-1", "transform": support.placement((0.0, 0.0, 0.2)), "mass": support.mass_payload(0.5)},
            ],
            dependencies=[self.tmp / "cad" / "base.SLDPRT", self.tmp / "cad" / "arm.SLDPRT"],
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
                },
                {"id": "arm", "name": "arm_link", "components": ["arm-1"], "frame": {"coordinate_system": "arm_datum"}},
            ],
            "joints": [],
        }

    def tearDown(self) -> None:
        support.cleanup(self.tmp)

    def _definition(self, **source_overrides) -> dict:
        source = dict(self.config)
        source.update(source_overrides)
        if source.get("documented_masses") and "mass_evidence" not in source:
            # 新合同：声明质量必须绑定归档证据文件；这里只给形状，文件由独立校验核对
            source["mass_evidence"] = _binding()
        return {
            "schema_version": "description.definition/v1",
            "hardware_id": "fixture",
            "source": source,
            "overrides": [],
        }

    def _freeze(self, name: str = "snapshot", **source_overrides) -> Path:
        if source_overrides.get("documented_masses") and "mass_evidence" not in source_overrides:
            source_overrides["mass_evidence"] = _binding()
        snapshot = self.tmp / name
        freeze(dict(self.config, **source_overrides), snapshot, backend=self.backend)
        return snapshot

    # --- 配置层 -------------------------------------------------------

    def test_documented_table_must_cover_every_included_component(self) -> None:
        with self.assertRaises(ConfigError) as raised:
            validate_source_config(
                dict(
                    self.config,
                    material_source="documented_table",
                    documented_masses={"base-1": _documented(2.5)},
                    mass_evidence=_binding(),
                )
            )
        self.assertEqual(raised.exception.code, "invalid_config")
        self.assertEqual(_detail(raised.exception)["missing"], ["arm-1"])

    def test_declared_entry_that_matches_no_component_is_rejected(self) -> None:
        for masses, field, expected in (
            ({"base-1": _documented(2.5), "arm-11": _documented(0.5)}, "unknown", ["arm-11"]),
            (
                {"base-1": _documented(2.5), "arm-1": _documented(0.5), "nope-1": _documented(1.0)},
                "unknown",
                ["nope-1"],
            ),
        ):
            with self.subTest(expected=expected):
                with self.assertRaises(ConfigError) as raised:
                    validate_source_config(
                        dict(
                            self.config,
                            material_source="documented_table",
                            documented_masses=masses,
                            mass_evidence=_binding(),
                        )
                    )
                self.assertEqual(_detail(raised.exception)[field], expected)

    # --- 生成侧（基于实际读数）----------------------------------------

    def test_freeze_refuses_a_partial_table(self) -> None:
        with self.assertRaises(ConfigError) as raised:
            self._freeze(
                "partial",
                material_source="documented_table",
                documented_masses={"base-1": _documented(2.5)},
            )
        self.assertEqual(_detail(raised.exception)["missing"], ["arm-1"])

    def test_freeze_refuses_cad_mass_from_an_unverified_material(self) -> None:
        # 真实读数会记录 material_assignment.unverified_reason；这里模拟那份派生 CAD
        # （SolidWorks 默认密度 1000 kg/m³ 给出"看起来正常"的质量）。
        self.backend.components[1]["mass"]["reference"]["material_assignment"] = {
            "schema_version": "swbridge.material-assignment/v1",
            "unverified_reason": "cad_material_provenance_missing",
        }
        with self.assertRaises(ConfigError) as raised:
            self._freeze("unverified", material_source="cad")
        self.assertEqual(_detail(raised.exception)["unverified_material"], ["arm-1"])

    def test_documented_masses_that_cover_everything_freeze_normally(self) -> None:
        snapshot = self._freeze(
            "complete",
            material_source="documented_table",
            documented_masses={"base-1": _documented(2.5), "arm-1": _documented(0.5)},
        )
        scene = load_scene(snapshot)
        base = next(link for link in scene["links"] if link["name"] == "base_link")
        self.assertAlmostEqual(base["inertial"]["mass"], 2.5, places=9)
        self.assertEqual(base["provenance"]["mass"], "source.documented_masses")
        self.assertEqual(base["provenance"]["mass_sources"], ["documented"])
        self.assertEqual(base["provenance"]["inertia_model"], "scaled_cad_uniform_density")
        self.assertIn("不等于实测惯量", base["provenance"]["inertia_model_scope"])
        record = base["provenance"]["declared_masses"][0]
        self.assertEqual(record["scale"], 2.5)
        arm = next(link for link in scene["links"] if link["name"] == "arm_link")
        self.assertEqual(arm["provenance"]["mass_sources"], ["documented"])

    def test_author_table_with_an_unknown_key_is_refused_at_load_time(self) -> None:
        snapshot = self._freeze(
            "typo",
            material_source="documented_table",
            documented_masses={"base-1": _documented(2.5), "arm-1": _documented(0.5)},
        )
        raw_scene = support.read_scene(snapshot)
        definition = self._definition(
            material_source="documented_table",
            documented_masses={"base-1": _documented(2.5), "arm-1": _documented(0.5), "arm-11": _documented(0.5)},
        )
        with self.assertRaises(ConfigError) as raised:
            canonical_for(raw_scene, definition, snapshot)
        self.assertEqual(_detail(raised.exception)["unknown"], ["arm-11"])

    def test_tampered_snapshot_table_is_caught_by_the_manifest(self) -> None:
        snapshot = self._freeze(
            "tampered",
            material_source="documented_table",
            documented_masses={"base-1": _documented(2.5), "arm-1": _documented(0.5)},
        )
        path = snapshot / "raw" / "declared_masses.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["items"]["arm-11"] = _documented(0.5)
        path.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaises(Exception) as raised:
            load_scene(snapshot)
        self.assertIn("incomplete or changed", str(raised.exception))

    # --- 独立校验 -----------------------------------------------------

    def test_oracle_rediscovers_a_missing_declared_mass(self) -> None:
        snapshot = self._freeze(
            "oracle-missing",
            material_source="documented_table",
            documented_masses={"base-1": _documented(2.5), "arm-1": _documented(0.5)},
        )
        raw_scene = support.read_scene(snapshot)
        complete = self._definition(
            material_source="documented_table",
            documented_masses={"base-1": _documented(2.5), "arm-1": _documented(0.5)},
        )
        canonical = canonical_for(raw_scene, complete, snapshot)
        # 定义在构建后才被改成"漏了 arm-1"：模型侧一切自洽，只有独立校验能从原始覆盖里发现
        definition = self._definition(
            material_source="documented_table", documented_masses={"base-1": _documented(2.5)}
        )
        results = verify_normalization(raw_scene, definition, snapshot, canonical)
        check = next(entry for entry in results if entry["id"] == "source.normalization.mass_provenance")
        self.assertEqual(check["status"], "failed")
        self.assertEqual(check["details"]["missing_declared"], ["arm-1"])

    def test_oracle_rejects_unknown_and_tampered_declared_entries(self) -> None:
        snapshot = self._freeze(
            "oracle-unknown",
            material_source="documented_table",
            documented_masses={"base-1": _documented(2.5), "arm-1": _documented(0.5)},
        )
        raw_scene = support.read_scene(snapshot)
        complete = self._definition(
            material_source="documented_table",
            documented_masses={"base-1": _documented(2.5), "arm-1": _documented(0.5)},
        )
        canonical = canonical_for(raw_scene, complete, snapshot)
        definition = self._definition(
            material_source="documented_table",
            documented_masses={
                "base-1": _documented(2.5),
                "arm-1": _documented(0.5),
                "arm-11": _documented(0.5),  # 拼错：不会匹配任何组件
            },
        )
        results = verify_normalization(raw_scene, definition, snapshot, canonical)
        check = next(entry for entry in results if entry["id"] == "source.normalization.mass_provenance")
        self.assertEqual(check["status"], "failed")
        self.assertEqual(check["details"]["unknown_declared"], ["arm-11"])

    def test_oracle_flags_cad_mass_without_a_verified_material(self) -> None:
        snapshot = self._freeze("oracle-cad", material_source="cad")
        raw_scene = support.read_scene(snapshot)
        definition = self._definition(material_source="cad", documented_masses={})
        canonical = canonical_for(raw_scene, definition, snapshot)
        path = snapshot / "raw" / "mass_properties.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["arm-1"]["reference"]["material_assignment"] = {
            "schema_version": "swbridge.material-assignment/v1",
            "unverified_reason": "cad_material_provenance_missing",
        }
        path.write_text(json.dumps(payload), encoding="utf-8")
        # 构建之后原始读数被替换成"材料未验证"（例如把派生 CAD 的读数混进了原生快照）
        results = verify_normalization(raw_scene, definition, snapshot, canonical)
        check = next(entry for entry in results if entry["id"] == "source.normalization.mass_provenance")
        self.assertEqual(check["status"], "failed")
        self.assertEqual(check["details"]["cad_mass_without_verified_material"], ["arm-1"])

    def test_oracle_records_the_inertia_model_when_masses_are_declared(self) -> None:
        snapshot = self._freeze(
            "oracle-model",
            material_source="documented_table",
            documented_masses={"base-1": _documented(2.5), "arm-1": _documented(0.5)},
        )
        raw_scene = support.read_scene(snapshot)
        definition = self._definition(
            material_source="documented_table",
            documented_masses={"base-1": _documented(2.5), "arm-1": _documented(0.5)},
        )
        canonical = canonical_for(raw_scene, definition, snapshot)
        results = verify_normalization(raw_scene, definition, snapshot, canonical)
        check = next(entry for entry in results if entry["id"] == "source.normalization.mass_provenance")
        self.assertEqual(check["status"], "passed", check)
        self.assertEqual(check["details"]["inertia_model"], "scaled_cad_uniform_density")
        self.assertEqual(check["details"]["scaled_components"], ["arm-1", "base-1"])


if __name__ == "__main__":
    unittest.main()
