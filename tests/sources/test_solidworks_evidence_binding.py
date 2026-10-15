"""归档证据文件的摘要绑定（配置形状）：文件绑定与定位锚点。

合同（见 docs/sources/solidworks.md）：
* ``source.mass_evidence`` 是 ``{reference, file, sha256}``，``file`` 是模型仓库内相对路径；
* 有声明质量时必须给出文件绑定，且每条 ``documented_masses[].evidence`` 是该文件内的
  定位锚点（非空字符串）；配置准入在触碰 SolidWorks 之前检查这些形状。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from . import _paths  # noqa: F401  (import side effect: sys.path)

from description_pipeline.io import file_digest  # noqa: E402
from description_pipeline.sources.solidworks.errors import ConfigError  # noqa: E402
from description_pipeline.sources.solidworks.freeze import validate_source_config  # noqa: E402

from . import support  # noqa: E402


def _detail(error: Exception) -> dict:
    detail = getattr(error, "detail", None)
    assert isinstance(detail, dict)
    return detail


class EvidenceBindingTests(unittest.TestCase):
    tmp: Path
    config: dict
    spec: Path

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="sw-evidence-"))
        self.spec = self.tmp / "docs" / "provenance" / "mass-spec.json"
        self.spec.parent.mkdir(parents=True)
        self.spec.write_text(
            json.dumps({"components": {"base-1": {"material": "PLA@15%"}, "arm-1": {"material": "PLA@15%"}}}),
            encoding="utf-8",
        )
        self.config = {
            "provider": "solidworks",
            "assembly": str(self.tmp / "cad"),
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
