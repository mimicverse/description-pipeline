"""质量来源合同：documented_table 必须覆盖全部纳入组件，独立校验要能自己发现缺口。

覆盖三类错误注入（生成侧 + 独立校验侧）：
* 表格漏条目 → documented_table 直接拒绝，校验侧 `source.normalization.mass_provenance` 也要报出来；
* 表格拼错/多余键 → 配置、加载、校验三处都拒绝；
* 严格 CAD 模式遇到"材料未验证"的读数 → 读数原样保留并记录非阻塞警告（默认密度不是证据）。
"""

from __future__ import annotations

import json
import copy
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

    def test_freeze_records_a_warning_for_cad_mass_from_an_unverified_material(self) -> None:
        # 真实读数会记录 material_assignment.unverified_reason；这里模拟那份派生 CAD
        # （SolidWorks 默认密度 1000 kg/m³ 给出"看起来正常"的质量）。默认密度不是证据，
        # 但属于可警告缺陷：冻结成功，测量值原样保留，警告逐字携带原始读数。
        self.backend.components[1]["mass"]["reference"].update(
            part_document="C:/cad/arm.SLDPRT", scope="part_document", volume_m3=6.25e-5, density_kg_m3=7900.0
        )
        self.backend.components[1]["mass"]["reference"]["material_assignment"] = {
            "schema_version": "swbridge.material-assignment/v1",
            "configuration": "默认",
            "unverified_reason": "cad_material_provenance_missing",
            "message": "Every solid requires an explicit physical material; default density is not evidence",
            "missing_body_indices": [0],
        }
        snapshot = self._freeze("unverified", material_source="cad")
        record = json.loads((snapshot / "raw" / "material_provenance.json").read_text(encoding="utf-8"))
        self.assertEqual(record["schema_version"], "solidworks-to-urdf.material-provenance/v1")
        self.assertEqual(len(record["warnings"]), 1)
        warning = record["warnings"][0]
        self.assertEqual(warning["code"], "cad_material_provenance_missing")
        self.assertFalse(warning["blocking"])
        self.assertEqual(warning["component"], "arm-1")
        self.assertEqual(warning["configuration"], "默认")
        self.assertEqual(warning["missing_body_indices"], [0])
        self.assertEqual(warning["material_assignment"]["unverified_reason"], "cad_material_provenance_missing")
        self.assertAlmostEqual(warning["measured"]["mass_kg"], 0.5, places=12)
        # 逐字对齐校验侧合同：记录条目与 raw/mass_properties.json 的原始行完全一致。
        raw_rows = json.loads((snapshot / "raw" / "mass_properties.json").read_text(encoding="utf-8"))
        raw_row = raw_rows["arm-1"]
        self.assertEqual(warning["document"], raw_row["reference"]["part_document"])
        self.assertEqual(warning["material_assignment"], raw_row["reference"]["material_assignment"])
        self.assertEqual(warning["measured"]["mass_kg"], raw_row["mass"])
        self.assertEqual(warning["measured"]["center_of_mass_m"], raw_row["com"])
        self.assertEqual(warning["measured"]["inertia_kg_m2"], raw_row["inertia"])
        self.assertEqual(warning["measured"]["volume_m3"], raw_row["reference"]["volume_m3"])
        self.assertEqual(warning["measured"]["density_kg_m3"], raw_row["reference"]["density_kg_m3"])
        self.assertEqual(warning["measured"]["scope"], "part_document")
        # 读数不被猜测值替换：link 质量仍是 CAD 实测值，警告同时写进 scene 的 provenance。
        scene = load_scene(snapshot)
        arm = next(link for link in scene["links"] if link["name"] == "arm_link")
        self.assertAlmostEqual(arm["inertial"]["mass"], 0.5, places=12)
        self.assertEqual(
            [entry["code"] for entry in scene["provenance"]["warnings"]],
            ["cad_material_provenance_missing"],
        )
        self.assertEqual(scene["provenance"]["warnings"][0]["components"], ["arm-1"])

    def test_freeze_records_an_empty_warning_list_for_verified_materials(self) -> None:
        snapshot = self._freeze("verified", material_source="cad")
        record = json.loads((snapshot / "raw" / "material_provenance.json").read_text(encoding="utf-8"))
        self.assertEqual(record["warnings"], [])
        scene = load_scene(snapshot)
        self.assertNotIn("warnings", scene["provenance"])

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

    def _warned_oracle_input(self) -> tuple[Path, dict, dict, dict]:
        self.backend.components[1]["mass"]["reference"]["material_assignment"] = {
            "schema_version": "swbridge.material-assignment/v1",
            "unverified_reason": "cad_material_provenance_missing",
            "missing_body_indices": [0],
        }
        snapshot = self._freeze("oracle-warned", material_source="cad")
        raw_scene = support.read_scene(snapshot)
        definition = self._definition(material_source="cad")
        return snapshot, raw_scene, definition, canonical_for(raw_scene, definition, snapshot)

    def _mass_check(self, snapshot: Path, raw_scene: dict, definition: dict, canonical: dict) -> dict:
        return next(
            entry
            for entry in verify_normalization(raw_scene, definition, snapshot, canonical)
            if entry["id"] == "source.normalization.mass_provenance"
        )

    def test_oracle_accepts_native_numbers_with_exact_material_warning_coverage(self) -> None:
        snapshot, raw_scene, definition, canonical = self._warned_oracle_input()
        check = self._mass_check(snapshot, raw_scene, definition, canonical)
        self.assertEqual(check["status"], "passed", check)
        self.assertEqual(check["checked"], ["arm-1", "base-1"])
        self.assertFalse(check["details"]["material_assignment_verified"])
        self.assertEqual(check["details"]["warnings"], canonical["provenance"]["warnings"])
        self.assertEqual(check["details"]["cad_mass_without_verified_material"], ["arm-1"])

    def test_oracle_requires_sorted_coverage_of_all_unverified_occurrences(self) -> None:
        assignment = {
            "schema_version": "swbridge.material-assignment/v1",
            "unverified_reason": "cad_material_provenance_missing",
            "missing_body_indices": [0],
        }
        for component in self.backend.components:
            component["mass"]["reference"]["material_assignment"] = copy.deepcopy(assignment)
        snapshot = self._freeze("oracle-warned-all", material_source="cad")
        raw_scene = support.read_scene(snapshot)
        definition = self._definition(material_source="cad")
        canonical = canonical_for(raw_scene, definition, snapshot)
        self.assertEqual(self._mass_check(snapshot, raw_scene, definition, canonical)["status"], "passed")
        for components in (["arm-1"], ["base-1", "arm-1"]):
            with self.subTest(components=components):
                altered = copy.deepcopy(canonical)
                altered["provenance"]["warnings"][0]["components"] = components
                check = self._mass_check(snapshot, raw_scene, definition, altered)
                self.assertEqual(check["status"], "failed", check)

    def test_oracle_rejects_removed_duplicate_and_false_material_warning_coverage(self) -> None:
        snapshot, raw_scene, definition, canonical = self._warned_oracle_input()
        original = canonical["provenance"]["warnings"][0]
        mutations = {
            "removed": [],
            "duplicated": [original, original],
            "missing_occurrence": [dict(original, components=[])],
            "invented_occurrence": [dict(original, components=["arm-1", "ghost-1"])],
            "duplicate_occurrence": [dict(original, components=["arm-1", "arm-1"])],
            "wrong_reason": [dict(original, unverified_reasons=["cad_material_read_failed"])],
            "omitted_reason": [dict(original, unverified_reasons=[])],
            "wrong_code": [dict(original, code="cad_material_read_failed")],
            "blocking_true": [dict(original, blocking=True)],
            "blocking_zero": [dict(original, blocking=0)],
            "blocking_null": [dict(original, blocking=None)],
            "missing_flag": [{key: value for key, value in original.items() if key != "blocking"}],
            "empty_message": [dict(original, message="")],
            "malformed_list": original,
            "malformed_entry": [None],
        }
        for case, warnings in mutations.items():
            with self.subTest(case=case):
                altered = copy.deepcopy(canonical)
                altered["provenance"]["warnings"] = warnings
                check = self._mass_check(snapshot, raw_scene, definition, altered)
                self.assertEqual(check["status"], "failed", check)
                self.assertTrue(check["details"]["warning_coverage_errors"], check)

    def test_oracle_rejects_unknown_and_falsy_raw_material_reasons(self) -> None:
        snapshot, raw_scene, definition, canonical = self._warned_oracle_input()
        path = snapshot / "raw" / "mass_properties.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        for reason in ("cad_material_read_failed", "unknown", "", None, False, 0):
            with self.subTest(reason=reason):
                payload["arm-1"]["reference"]["material_assignment"]["unverified_reason"] = reason
                path.write_text(json.dumps(payload), encoding="utf-8")
                check = self._mass_check(snapshot, raw_scene, definition, canonical)
                self.assertEqual(check["status"], "failed", check)
                self.assertEqual(check["details"]["unsupported_material_reasons"], {"arm-1": reason})

    def test_oracle_rejects_fabricated_material_warning_for_verified_readings(self) -> None:
        snapshot = self._freeze("oracle-clean", material_source="cad")
        raw_scene = support.read_scene(snapshot)
        definition = self._definition(material_source="cad")
        canonical = canonical_for(raw_scene, definition, snapshot)
        canonical["provenance"]["warnings"] = [{"code": "cad_material_provenance_missing", "components": ["arm-1"]}]
        check = self._mass_check(snapshot, raw_scene, definition, canonical)
        self.assertEqual(check["status"], "failed", check)
        self.assertTrue(check["details"]["warning_coverage_errors"])

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
