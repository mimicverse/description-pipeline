"""归档证据文件的摘要绑定：独立校验从原件复核文件、摘要与锚点。

合同（见 docs/sources/solidworks.md）：
* ``source.mass_evidence`` 是 ``{reference, file, sha256}``，``file`` 是模型仓库内相对路径；
* 每条 ``documented_masses[].evidence`` 是**该文件内的定位锚点**；
* 独立校验从模型仓库原文重算摘要，拒绝文件缺失、路径逃逸、摘要变化、锚点不匹配与未覆盖组件。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from . import _paths  # noqa: F401  (import side effect: sys.path)

from description_pipeline.io import file_digest  # noqa: E402
from description_pipeline.sources.solidworks.errors import ConfigError  # noqa: E402
from description_pipeline.sources.solidworks.freeze import freeze, validate_source_config  # noqa: E402

from . import support  # noqa: E402


def _detail(error: Exception) -> dict:
    detail = getattr(error, "detail", None)
    assert isinstance(detail, dict)
    return detail


class EvidenceBindingTests(unittest.TestCase):
    tmp: Path
    root: Path
    snapshot: Path
    config: dict
    raw_scene: dict
    spec: Path

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="sw-evidence-"))
        self.assembly = support.make_cad_tree(self.tmp / "cad")
        self.backend = support.FixtureCadBackend(
            self.assembly,
            [
                # Off-axis COM: the product signal proves the convention at capture time.
                {
                    "name": "base-1",
                    "transform": support.placement(),
                    "mass": support.mass_payload(1.0, (0.01, 0.02, 0.0)),
                },
                {"name": "arm-1", "transform": support.placement((0.0, 0.0, 0.2)), "mass": support.mass_payload(0.5)},
            ],
            dependencies=[self.tmp / "cad" / "base.SLDPRT", self.tmp / "cad" / "arm.SLDPRT"],
        )
        # 模型仓库布局：config/robot.yaml 是根标记，快照在 sources/<id>/ 下（与 build.freeze 一致）
        self.root = self.tmp / "model"
        (self.root / "config").mkdir(parents=True)
        (self.root / "config" / "robot.yaml").write_text(
            "schema_version: description.definition/v1\n", encoding="utf-8"
        )
        self.spec = self.root / "docs" / "provenance" / "mass-spec.json"
        self.spec.parent.mkdir(parents=True)
        self.spec.write_text(
            json.dumps({"components": {"base-1": {"material": "PLA@15%"}, "arm-1": {"material": "PLA@15%"}}}),
            encoding="utf-8",
        )
        self.config = {
            "provider": "solidworks",
            "assembly": str(self.assembly),
            "configuration": "Default",
            "allowed_roots": [str(self.tmp / "cad")],
            "geometry": {"enabled": False},
            "material_source": "documented_table",
            "mass_evidence": self._binding(),
            "documented_masses": {
                "base-1": {"mass_kg": 2.5, "reason": "printed part", "evidence": "base-1"},
                "arm-1": {"mass_kg": 0.5, "reason": "printed part", "evidence": "arm-1"},
            },
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
        self.snapshot = self.root / "sources" / "case" / "snapshot"
        freeze(dict(self.config), self.snapshot, backend=self.backend)
        self.raw_scene = support.read_scene(self.snapshot)

    def tearDown(self) -> None:
        support.cleanup(self.tmp)

    def _binding(self, **overrides) -> dict:
        binding = {
            "reference": "Neutral component material spec",
            "file": "docs/provenance/mass-spec.json",
            "sha256": file_digest(self.spec),
        }
        binding.update(overrides)
        return binding

    def _definition(self, **source_overrides) -> dict:
        source = dict(self.config)
        source.update(source_overrides)
        return {
            "schema_version": "description.definition/v1",
            "hardware_id": "fixture",
            "source": source,
            "overrides": [],
        }

    # --- 配置形状 -----------------------------------------------------

    def test_config_requires_a_file_binding_for_documented_masses(self) -> None:
        without = dict(self.config)
        without.pop("mass_evidence")
        with self.assertRaises(ConfigError) as raised:
            validate_source_config(without)
        self.assertIn("mass_evidence", str(raised.exception))

        as_string = dict(self.config, mass_evidence="vendor drawing 12-3")
        with self.assertRaises(ConfigError) as raised:
            validate_source_config(as_string)
        self.assertEqual(raised.exception.code, "invalid_config")

    def test_config_rejects_malformed_bindings(self) -> None:
        for overrides in (
            {"sha256": "ABC"},
            {"sha256": "0" * 63},
            {"file": ""},
            {"reference": ""},
            {"file": "docs/provenance/mass-spec.json", "sha256": "0" * 64, "extra": 1},
        ):
            with self.subTest(overrides=overrides):
                config = dict(self.config, mass_evidence=self._binding(**overrides))
                with self.assertRaises(ConfigError):
                    validate_source_config(config)

    def test_config_requires_an_anchor_for_every_documented_mass(self) -> None:
        config = dict(self.config)
        config["documented_masses"] = dict(config["documented_masses"])
        config["documented_masses"]["arm-1"] = {"mass_kg": 0.5, "reason": "printed part"}
        with self.assertRaises(ConfigError) as raised:
            validate_source_config(config)
        self.assertEqual(_detail(raised.exception)["component"], "arm-1")

    # --- 独立校验 -----------------------------------------------------

    def test_string_binding_without_documented_masses_still_validates(self) -> None:
        # cad 模式：材料来自 CAD，说明字符串仍然合法（没有声明质量需要绑定文件）
        config = dict(self.config)
        config.pop("mass_evidence")
        config.pop("documented_masses")
        config["material_source"] = "cad"
        config["mass_evidence"] = "materials come from the CAD material library"
        validated = validate_source_config(config)
        self.assertEqual(validated["material_source"], "cad")


if __name__ == "__main__":
    unittest.main()
