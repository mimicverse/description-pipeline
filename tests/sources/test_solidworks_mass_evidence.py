"""质量来源合同：documented_table 必须覆盖全部纳入组件，独立校验要能自己发现缺口。

覆盖三类错误注入（生成侧 + 独立校验侧）：
* 表格漏条目 → documented_table 直接拒绝，校验侧 `source.normalization.mass_provenance` 也要报出来；
* 表格拼错/多余键 → 配置、加载、校验三处都拒绝；
* 严格 CAD 模式遇到"材料未验证"的读数 → 读数原样保留并记录非阻塞警告（默认密度不是证据）。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from . import _paths  # noqa: F401  (import side effect: sys.path)

from description_pipeline.sources.solidworks.discovery import CONTRACT, DISCOVERY_SCHEMA  # noqa: E402
from description_pipeline.sources.solidworks.errors import ConfigError  # noqa: E402
from description_pipeline.sources.solidworks.freeze import freeze as _freeze  # noqa: E402
from description_pipeline.sources.solidworks.freeze import validate_source_config as _validate_source_config  # noqa: E402
from description_pipeline.sources.solidworks.scene import load_scene  # noqa: E402

from . import support  # noqa: E402
from .selection_support import sealed_selection  # noqa: E402


def _Backend(document, components, **kwargs):  # fixture plumbing: supported native datum names
    """The fixture backend carrying this module's supported native datum names."""

    kwargs.setdefault(
        "coordinate_systems",
        {
            "CS_base_link": support.placement(),
            "CS_arm_link": support.placement((0.0, 0.0, 0.2)),
        },
    )
    return support.FixtureCadBackend(document, components, **kwargs)


def freeze(config, *args, **kwargs):  # noqa: F811 - fixture adapter for the request sets
    return _freeze(support.capture_requests(config), *args, **kwargs)


def validate_source_config(config):  # noqa: F811 - fixture adapter for the request sets
    return _validate_source_config(support.capture_requests(config))


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


def _native_record(*, files: dict[str, str]) -> dict:
    """The synthetic discovery record matching this module's freeze fixture.

    Carriers use the supported native semantics: a body owns its ``CS_<link>``
    datum and the link name comes from that datum name alone.  The two links
    are joined by the solved concentric+coincident mate pair the carrier model
    requires for a connected joint tree (the fixture previously held two
    unconnected bodies; a disconnected island is no longer loadable input).
    """

    def translated(z: float) -> list[float]:
        return [1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0, z, 0.0, 0.0, 0.0, 1.0]

    components = [
        {
            "name2": "base-1",
            "lightweight": False,
            "suppression_state": 2,
            "instance_id": "base-1",
            "document": "base.SLDPRT",
            "configuration": "Default",
            "fixed": True,
            "suppressed": False,
            "transform": translated(0.0),
        },
        {
            "name2": "arm-1",
            "lightweight": False,
            "suppression_state": 2,
            "instance_id": "arm-1",
            "document": "arm.SLDPRT",
            "configuration": "Default",
            "fixed": False,
            "suppressed": False,
            "transform": translated(0.2),
        },
    ]
    datums = [
        {"name": "CS_base_link", "owner": "base-1", "array": translated(0.0)},
        {"name": "CS_arm_link", "owner": "arm-1", "array": translated(0.2)},
    ]
    masses = [
        {"component": "base-1", "mass_kg": 1.0, "material": "Alloy Steel"},
        {"component": "arm-1", "mass_kg": 0.5, "material": "Alloy Steel"},
    ]
    mates = [
        {
            "name": "hinge_joint__coaxial",
            "type": "concentric",
            "suppressed": False,
            "error_code": 0,
            "scope": "",
            "limits": None,
            "entities": [
                {
                    "component": "base-1",
                    "feature": "Cyl1",
                    "face_index": 3,
                    "cylinder": {"point": [0.0, 0.0, 0.2], "direction": [0.0, 0.0, 1.0], "radius": 0.006},
                },
                {
                    "component": "arm-1",
                    "feature": "Cyl2",
                    "face_index": 7,
                    "cylinder": {"point": [0.0, 0.0, 0.0], "direction": [0.0, 0.0, 1.0], "radius": 0.006},
                },
            ],
        },
        {
            "name": "hinge_joint__locate",
            "type": "coincident",
            "suppressed": False,
            "error_code": 0,
            "scope": "",
            "limits": None,
            "entities": [
                {
                    "component": "base-1",
                    "feature": "Plane1",
                    "plane": {"point": [0.0, 0.0, 0.2], "normal": [0.0, 0.0, 1.0]},
                },
                {
                    "component": "arm-1",
                    "feature": "Plane2",
                    "plane": {"point": [0.0, 0.0, 0.0], "normal": [0.0, 0.0, -1.0]},
                },
            ],
        },
    ]
    assembly_mass = sum(row["mass_kg"] for row in masses)
    return {
        "schema_version": DISCOVERY_SCHEMA,
        "contract": CONTRACT,
        "namespace": "dp",
        "identity": {
            "hardware_id": "mass_evidence_fixture",
            "revision": "r1",
            "owner": "mechanical-team",
            "change_summary": "mass evidence module fixture",
            "control": {"system": "handoff", "reference": "mass-evidence-run"},
            "delivery_configuration": "Default",
            "main_assembly": "robot.SLDASM",
            "robot_name": "robot",
        },
        "saved_configuration": "Default",
        "document_state": support.saved_document_state(),
        "components": components,
        "mates": mates,
        "datums": datums,
        "masses": masses,
        "assembly_mass_kg": assembly_mass,
        "assembly_extent_m": 0.5,
        "properties": {
            "document": {"dp.design_budget_record": "budget.json#robot"},
            "components": {},
            "mates": {
                "hinge_joint__coaxial": {
                    "dp.joint.axis_sign": "+1",
                    "dp.joint.limits_record": "joints/hinge.json#limits",
                    "dp.joint.drive_record": "joints/hinge.json#drive",
                }
            },
        },
        "files": dict(files),
        "source_selection": sealed_selection(
            files,
            "robot.SLDASM",
            edges={
                "robot.SLDASM": ["base.SLDPRT", "arm.SLDPRT"],
                "base.SLDPRT": [],
                "arm.SLDPRT": [],
            },
        ),
    }


def _compose_scene_inputs(destination: Path, cad_root: Path) -> None:
    """Fixture plumbing: seal the consumer inputs the Linux scene rebuild reads.

    Synthetic test-only composition (the canonical/E2E producers build their
    own evidence and never use this helper): CAD bytes, the discovery payload
    with its raw native record and controlled records, and a re-sealed
    snapshot manifest so the added members stay bound by the verified
    inventory.
    """

    import hashlib

    names = ["robot.SLDASM", "base.SLDPRT", "arm.SLDPRT"]
    cad_files = {name: cad_root / name for name in names}
    files = {name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in cad_files.items()}
    record = _native_record(files=files)
    controlled = {
        "budget.json": json.dumps(
            {
                "robot": {
                    "expected_mass_kg": [record["assembly_mass_kg"] - 0.1, record["assembly_mass_kg"] + 0.1],
                    "expected_extent_m": [0.2, 0.8],
                }
            }
        ),
        "joints/hinge.json": json.dumps(
            {"limits": {"lower": -1.0, "upper": 1.0}, "drive": {"effort": 2.0, "velocity": 3.0}}
        ),
    }
    support.compose_delivery_inputs(
        destination,
        main_assembly="robot.SLDASM",
        configuration="Default",
        record=record,
        cad_files=cad_files,
        records=controlled,
        record_keys={"joints/hinge.json": "limits"},
    )
    from description_pipeline.sources.snapshot import write_manifest

    manifest = json.loads((destination / "manifest.json").read_text(encoding="utf-8"))
    write_manifest(
        destination,
        kind=manifest["kind"],
        identity=manifest["identity"],
        evidence_class=manifest["evidence_class"],
    )


class MassEvidenceContractTests(unittest.TestCase):
    tmp: Path
    snapshot: Path
    config: dict
    raw_scene: dict

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="sw-mass-evidence-"))
        self.assembly = support.make_cad_tree(self.tmp / "cad")
        self.backend = _Backend(
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
        self.config = {
            "provider": "solidworks",
            "assembly": str(self.assembly),
            "configuration": "Default",
            "allowed_roots": [str(self.tmp / "cad")],
            "geometry": {"enabled": False},
            "coordinate_systems": ["CS_base_link", "CS_arm_link"],
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
        _compose_scene_inputs(snapshot, self.tmp / "cad")
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
        _compose_scene_inputs(snapshot, self.tmp / "cad")
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
        _compose_scene_inputs(snapshot, self.tmp / "cad")
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

