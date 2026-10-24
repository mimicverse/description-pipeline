"""Contract-convergence guards: one owner per shared list or constant (VER-02/03/OC-02; L0-1/02/04)."""

from __future__ import annotations

import ast
import importlib
import re
import unittest
from pathlib import Path

from description_pipeline import schemas
from description_pipeline.orchestration import report_view
from description_pipeline.orchestration import uploads
from description_pipeline.schemas import exporter_declaration_present
from description_pipeline.sources.solidworks import discovery
from description_pipeline.stages import (
    DELIVERY_STAGES,
    FIXED_REQUIRED_CHECKS,
    HOST_BY_STAGE,
    PORTABLE_STAGES,
    STAGE_IDS,
    required_rows_complete,
)
from description_pipeline.verification import native_discovery, solidworks_urdf

REPO = Path(__file__).resolve().parents[2]

#: Test fixtures that still quote an owned boundary schema id as data.  Shrink-only:
#: each domain removes its own file here when its consumers switch to the constants.
SCHEMA_LITERAL_FIXTURES = {
    "tests/sources/support.py",
    "tests/sources/test_solidworks_closure.py",
    "tests/sources/test_solidworks_effective_instances.py",
    "tests/sources/test_solidworks_freeze.py",
    "tests/sources/test_solidworks_link_group_selections.py",
    "tests/sources/test_verify_declarations.py",
    "tests/v1/test_diagnostic_preview.py",
    "tests/v1/test_discovery_transfer.py",
    "tests/v1/test_effective_instance_integration.py",
    "tests/v1/test_quality.py",
    "tests/v1/test_stage_transfer.py",
    "tests/v1/test_structural_report.py",
}


class ContractConvergenceTests(unittest.TestCase):
    def test_runtime_sources_use_one_english_display_vocabulary(self) -> None:
        """Runtime text and code stay English; Chinese belongs to project documents only."""
        offenders = []
        old_keys = re.compile(
            r"\b(?:name|label|state|title|meaning|note|scope|reason|"
            r"resume_from_name|stage_name|headline|manual_scope_note)_zh\b|_ZH\b"
        )
        # Scan the product namespace, not generated editable-install metadata
        # (for example src/mimicverse_description.egg-info/PKG-INFO).
        for base in ("src/description_pipeline", "deploy", "tools"):
            for path in (REPO / base).rglob("*"):
                if not path.is_file() or "__pycache__" in path.parts or path.suffix in {".pyc", ".pyo"}:
                    continue
                try:
                    text = path.read_text(encoding="utf-8")
                except UnicodeDecodeError:
                    continue
                relative = path.relative_to(REPO).as_posix()
                if any("\u4e00" <= character <= "\u9fff" for character in text):
                    offenders.append((relative, "Han character"))
                if old_keys.search(text):
                    offenders.append((relative, "legacy language suffix"))
        self.assertEqual(offenders, [])

    def test_portal_labels_cover_the_fixed_required_checks(self) -> None:
        self.assertEqual(report_view._UNLABELLED_REQUIRED_CHECKS, ())
        self.assertLessEqual(FIXED_REQUIRED_CHECKS, set(report_view._INDEPENDENT_LABELS))

    def test_required_check_inventory_is_single_sourced(self) -> None:
        from description_pipeline.interface_records import CHECK_POLICY_V1

        self.assertEqual(set(solidworks_urdf.required_checks()), set(FIXED_REQUIRED_CHECKS))
        self.assertEqual(CHECK_POLICY_V1["fixed_checks"], sorted(FIXED_REQUIRED_CHECKS))
        definition = {
            "checks": {"expected_mass_kg": [0.5, 1.0], "expected_extent_m": [0.1, 1.0]},
            "source": {"bodies": [{"name": "base_link"}], "joints": [{"name": "waist_pitch", "type": "revolute"}]},
        }
        identifiers = set(solidworks_urdf.required_checks(definition))
        self.assertLessEqual(FIXED_REQUIRED_CHECKS, identifiers)
        self.assertIn("physics.expected_mass", identifiers)
        self.assertIn("inertia.base_link", identifiers)
        self.assertIn("shafts.waist_pitch", identifiers)

    def test_portable_stages_derive_from_the_contract_hosts(self) -> None:
        self.assertEqual(PORTABLE_STAGES, ("freeze", "generate", "verify", "publish"))
        self.assertEqual(tuple(stage for stage, hosts in HOST_BY_STAGE.items() if hosts == ("linux",)), PORTABLE_STAGES)

    def test_delivery_stages_split_the_native_boundary_from_report_rendering(self) -> None:
        """Report rendering keys on delivery stages, not on every portable stage."""
        self.assertEqual(DELIVERY_STAGES, ("generate", "verify", "publish"))
        self.assertLessEqual(set(DELIVERY_STAGES), set(PORTABLE_STAGES))
        self.assertEqual(tuple(stage for stage in STAGE_IDS if stage in DELIVERY_STAGES), DELIVERY_STAGES)

    def test_owned_schema_ids_are_never_retyped_as_literals(self) -> None:
        owned = {
            name: value for name, value in vars(schemas).items() if name.endswith("_SCHEMA") and isinstance(value, str)
        }
        self.assertLessEqual(
            {
                "INPUT_SCHEMA",
                "TOOL_SCHEMA",
                "EFFECTIVE_INSTANCES_SCHEMA",
                "LINK_GROUP_MASS_SCHEMA",
                "HEALTH_SCHEMA",
                "RESOURCE_POLICY_SCHEMA",
                "CACHE_RELEASE_SCHEMA",
                "CACHE_RELEASE_RECEIPT_SCHEMA",
                "CACHE_IMPORT_CONFIRMATION_SCHEMA",
                "CONFIG_SNAPSHOT_SCHEMA",
                "INSTALL_RECORD_SCHEMA",
                "DELIVERY_MANIFEST_SCHEMA",
                "SOURCE_RECORD_SCHEMA",
                "PLATFORM_CONFIG_SCHEMA",
                "CHECK_POLICY_SCHEMA",
            },
            set(owned),
        )
        offenders = []
        for base in ("src", "tests"):
            for path in (REPO / base).rglob("*.py"):
                relative = path.relative_to(REPO).as_posix()
                if path.name == "schemas.py" or relative in SCHEMA_LITERAL_FIXTURES:
                    continue
                text = path.read_text(encoding="utf-8")
                for value in owned.values():
                    if f'"{value}"' in text or f"'{value}'" in text:
                        offenders.append((relative, value))
        self.assertEqual(offenders, [])

    def test_named_criteria_match_the_quality_contract(self) -> None:
        expected = {
            "description_pipeline.verification.solidworks_physics": {
                "MASS_RTOL": 1e-6,
                "MASS_CLOSURE_ATOL": 1e-12,
                "COM_ATOL_M": 5e-5,
                "CLOSURE_COM_ATOL_M": 1e-3,
                "CLOSURE_TENSOR_RTOL": 1e-2,
                "TENSOR_RTOL": 1e-4,
                "LINK_DIRECT_MASS_RTOL": 1e-12,
                "LINK_DIRECT_COM_ATOL_M": 1e-9,
                "LINK_DIRECT_TENSOR_RTOL": 1e-9,
                "LINK_REPEAT_MASS_ATOL_KG": 1e-9,
                "LINK_REPEAT_COM_ATOL_M": 5e-7,
                "LINK_REPEAT_TENSOR_RTOL": 1e-6,
            },
            "description_pipeline.verification.solidworks_urdf": {"POSITION_TOL_M": 5e-5},
            "description_pipeline.geometry.stl": {"DEGENERATE_AREA": 1e-14, "MESH_PART_TRIANGLES_MAX": 199_999},
        }
        for module_name, names in expected.items():
            module = importlib.import_module(module_name)
            for name, value in names.items():
                self.assertEqual(getattr(module, name), value, f"{module_name}.{name}")
        self.assertEqual(discovery.AXIS_OFFSET_TOL_M, native_discovery.AXIS_OFFSET_TOL_M)
        self.assertEqual(native_discovery.AXIS_OFFSET_TOL_M, native_discovery._CONFLICT_POS_TOL_M)
        self.assertEqual(solidworks_urdf.POSITION_TOL_M, native_discovery.AXIS_OFFSET_TOL_M)

    def test_named_criteria_definition_sites_are_tracked(self) -> None:
        known = {
            "MASS_RTOL": [
                "src/description_pipeline/sources/solidworks/verify.py",
                "src/description_pipeline/verification/solidworks_physics.py",
            ],
            "MASS_CLOSURE_ATOL": ["src/description_pipeline/verification/solidworks_physics.py"],
            "COM_ATOL_M": ["src/description_pipeline/verification/solidworks_physics.py"],
            "POSITION_TOL_M": ["src/description_pipeline/verification/solidworks_urdf.py"],
            "AXIS_OFFSET_TOL_M": [
                "src/description_pipeline/sources/solidworks/discovery.py",
                "src/description_pipeline/verification/native_discovery.py",
            ],
            "DEGENERATE_AREA": ["src/description_pipeline/geometry/stl.py"],
            "MESH_PART_TRIANGLES_MAX": ["src/description_pipeline/geometry/stl.py"],
        }
        found = {name: [] for name in known}
        pattern = re.compile(rf"^({'|'.join(known)})\s*=", re.M)
        for path in (REPO / "src").rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            for match in pattern.finditer(text):
                found[match.group(1)].append(path.relative_to(REPO).as_posix())
        for name, paths in found.items():
            self.assertEqual(sorted(paths), sorted(known[name]), name)

    def test_upload_limits_have_one_definition_site(self) -> None:
        self.assertEqual(uploads.MAX_FILES, 4096)
        self.assertEqual(uploads.MAX_TOTAL_BYTES, 2 * 1024**3)
        self.assertEqual(uploads.MAX_FILE_BYTES, 512 * 1024**2)
        self.assertGreaterEqual(uploads.MAX_BODY_BYTES, uploads.MAX_TOTAL_BYTES)
        from description_pipeline.orchestration import portal

        self.assertEqual(portal.MAX_FILES, uploads.MAX_FILES)
        self.assertEqual(portal.MAX_TOTAL_BYTES, uploads.MAX_TOTAL_BYTES)
        self.assertEqual(portal.MAX_FILE_BYTES, uploads.MAX_FILE_BYTES)
        pattern = re.compile(r"^MAX_(FILES|TOTAL_BYTES|FILE_BYTES)\s*=", re.M)
        sites = [
            path.relative_to(REPO).as_posix()
            for path in (REPO / "src").rglob("*.py")
            if pattern.search(path.read_text(encoding="utf-8"))
        ]
        self.assertEqual(sites, ["src/description_pipeline/orchestration/uploads.py"])

    def test_verification_never_imports_orchestration_or_the_store(self) -> None:
        """Independence guard: receipts arrive as entry inputs, never via the store."""

        offenders = []
        for path in (REPO / "src/description_pipeline/verification").rglob("*.py"):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Import):
                    modules = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    modules = [node.module or ""]
                else:
                    continue
                for module in modules:
                    if "orchestration" in module.split(".") or module.endswith("linux_store"):
                        offenders.append((path.relative_to(REPO).as_posix(), module))
        self.assertEqual(offenders, [])

    def test_completeness_helper_matches_the_nested_gate_contract(self) -> None:
        passed = {"id": "a", "state": "passed", "passed": True}
        self.assertTrue(required_rows_complete(["a"], [dict(passed)]))
        for required, rows in (
            ([], []),
            (["a"], []),
            (["a"], [{"id": "a", "state": "failed", "passed": False}]),
            (["a"], [dict(passed, id="b")]),
            (["a"], [dict(passed), dict(passed)]),
            ([1], [dict(passed, id=1)]),
        ):
            with self.subTest(required=required, rows=rows):
                self.assertFalse(required_rows_complete(required, rows))


class ExporterPredicateTests(unittest.TestCase):
    """The producer mode and the verifier regime share one predicate (VER-12 residual)."""

    def test_only_a_proven_absent_block_selects_mate_inference(self) -> None:
        self.assertFalse(exporter_declaration_present(None))
        self.assertFalse(exporter_declaration_present({}))
        self.assertFalse(exporter_declaration_present({"exporter": {"state": "absent"}}))
        self.assertTrue(exporter_declaration_present({"exporter": {"state": "unread"}}))
        self.assertTrue(exporter_declaration_present({"exporter": {"state": "extracted"}}))
        self.assertTrue(exporter_declaration_present({"exporter": None}))
        self.assertTrue(exporter_declaration_present({"exporter": "unexpected"}))

    def test_both_halves_use_the_single_owner(self) -> None:
        self.assertIs(discovery.exporter_declaration_present, exporter_declaration_present)
        self.assertIs(native_discovery.exporter_declaration_present, exporter_declaration_present)
