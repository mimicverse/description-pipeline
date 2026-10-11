"""Generic declaration oracles do not use the source parser or real acceptance names."""

import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from xml.etree import ElementTree as ET

from description_pipeline.verification.native_declarations import verify_declarations
from description_pipeline.verification.native_discovery import verify_discovery
from .test_verify_v14_declarations import document_xml
from .selection_support import sealed_selection

NS = "{http://schemas.datacontract.org/2004/07/SW2URDF}"
Z = "{http://schemas.microsoft.com/2003/10/Serialization/}"
IDENTITY = [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1]


def rewrite(raw, transform):
    root = ET.fromstring(raw["exporter"]["raw_xml"])
    transform(root)
    xml = ET.tostring(root, encoding="unicode")
    raw["exporter"]["raw_xml"] = xml
    raw["exporter"]["raw_xml_sha256"] = hashlib.sha256(xml.encode()).hexdigest()


def component(name, document=None):
    return {
        "name2": name,
        "configuration": "Default",
        "suppressed": False,
        "lightweight": False,
        "suppression_state": 2,
        "document": document or name.replace("/", "_") + ".SLDPRT",
        "transform": list(IDENTITY),
    }


def fixture():
    xml = document_xml(
        root_joint=True, extra_elements=("<SWComponentPIDs><a:base64Binary>QUJDRA==</a:base64Binary></SWComponentPIDs>")
    )
    raw = {
        "exporter": {
            "state": "extracted",
            "attribute_name": "URDF Export Configuration (v1.4)",
            "exporter_version": 1.4,
            "raw_xml": xml,
            "raw_xml_sha256": hashlib.sha256(xml.encode()).hexdigest(),
            "document": "robot.SLDASM",
            "configuration": "Default",
            "pid_bindings": [],
        },
        "saved_configuration": "Default",
        "selection": {"main_assembly": "robot.SLDASM"},
        "components": [
            component("base_part"),
            component("module-1", "module.SLDASM"),
            component("module-1/tool_part"),
            component("module-1/bracket"),
        ],
        "datums": [
            {"name": name, "owner": "", "configuration": "Default", "array": list(IDENTITY)}
            for name in ["base_frame", "tool_frame"]
        ],
        "reference_axes": [
            {
                "kind": "reference_axis",
                "component": "",
                "selector": {"kind": "reference_axis", "component": "", "feature_name": "shaft"},
                "coordinate_frame": "assembly",
                "raw_endpoints_m": [0, -2, 0, 0, 3, 0],
                "axis_point_m": [0, -2, 0],
                "axis_direction": [0, 1, 0],
                "direction_semantics": "undirected_axis_line",
                "used_api": "IFeature.GetSpecificFeature2/IRefAxis.GetRefAxisParams",
            }
        ],
        "identity": {
            "hardware_id": "fixture",
            "revision": "r1",
            "parent_revision": None,
            "owner": "test-owner",
            "change_summary": "native verification fixture",
            "control": {"system": "handoff", "reference": "fixture"},
            "robot_name": "fixture",
        },
        "mates": [],
    }
    for key, name in [("41424344", "base_part"), ("414243", "module-1"), ("444546", "module-1/tool_part")]:
        selected = next(item for item in raw["components"] if item["name2"] == name)
        raw["exporter"]["pid_bindings"].append(
            {
                "pid_hex": key,
                "name2": name,
                "document": selected["document"],
                "configuration": "Default",
                "error_code": 0,
            }
        )

    def values(root):
        for item in root.iter():
            if item.get(Z + "Id") == "joint_limit_EffortAttribute":
                item.find(NS + "Value").text = "5"
            if item.get(Z + "Id") == "joint_limit_VelocityAttribute":
                value = item.find(NS + "Value")
                value.attrib.clear()
                value.text = "2"
            if item.get(Z + "Id") in {"root_joint", "joint"}:
                item.find(NS + "CoordinateSystemName").text = (
                    "base_frame" if item.get(Z + "Id") == "root_joint" else "tool_frame"
                )

    rewrite(raw, values)
    return raw


def source_claims():
    return {
        "bodies": [
            {"name": "base", "components": ["base_part"], "frame": {"coordinate_system": "base_frame"}},
            {
                "name": "tool",
                "components": ["module-1/bracket", "module-1/tool_part"],
                "frame": {"coordinate_system": "tool_frame"},
            },
        ],
        "joints": [
            {
                "name": "hinge",
                "type": "revolute",
                "parent": "base",
                "child": "tool",
                "axis": [0, 1, 0],
                "limits": {"lower": -1.5, "upper": 1.5, "effort": 5, "velocity": 2},
                "axis_reference": {"kind": "reference_axis", "component": "", "feature_name": "shaft"},
            }
        ],
        "frames": [],
    }


def lock(left, right, *, error=0, kind="lock"):
    return {
        "name": "native_lock",
        "type": kind,
        "suppressed": False,
        "error_code": error,
        "entities": [{"component": left, "point": [0, 0, 0]}, {"component": right, "point": [0, 0, 0]}],
    }


class NativeDeclarationsTests(unittest.TestCase):
    def errors(self, raw, source=None):
        return {item["code"] for item in verify_declarations(raw, source)["errors"]}

    def test_valid_native_tree_container_membership_and_claims(self):
        report = verify_declarations(fixture(), source_claims())
        self.assertTrue(report["passed"], report["errors"])
        self.assertEqual(report["members"]["tool"], ["module-1/bracket", "module-1/tool_part"])

    def test_unresolved_internal_mates_do_not_refute_explicit_membership(self):
        raw = fixture()
        raw["mates"] = [lock("module-1/tool_part", "module-1/bracket", kind="unread")]
        self.assertTrue(verify_declarations(raw)["passed"])

    def test_unique_complete_rigidity_completes_only_unclaimed_leaf(self):
        raw = fixture()
        raw["components"].append(component("cover"))
        raw["mates"] = [lock("cover", "base_part")]
        report = verify_declarations(raw)
        self.assertTrue(report["passed"], report["errors"])
        self.assertEqual(report["completion"], {"cover": "base"})
        for patch in [{"error_code": 1}, {"type": "unread"}, {"type": "coincident"}]:
            candidate = copy.deepcopy(raw)
            candidate["mates"][0].update(patch)
            self.assertIn("v14.coverage", self.errors(candidate))

    def test_rigidity_to_two_declared_links_never_merges_or_selects_owner(self):
        raw = fixture()
        raw["components"].append(component("cover"))
        raw["mates"] = [lock("cover", "base_part"), lock("cover", "module-1/tool_part")]
        report = verify_declarations(raw)
        self.assertIn("v14.coverage", {item["code"] for item in report["errors"]})
        self.assertEqual(report["completion"], {})
        self.assertNotIn("cover", report["members"]["base"])
        self.assertNotIn("cover", report["members"]["tool"])

    def test_zero_parameters_are_preserved_and_warn_as_motion_unqualified(self):
        def limit_items(root):
            return [
                item
                for item in root.iter()
                if (item.get(Z + "Id") or "").startswith("joint_limit_") and item.find(NS + "Value") is not None
            ]

        def write(values):
            def transform(root):
                for item in limit_items(root):
                    item.find(NS + "Value").text = values

            return transform

        raw = fixture()
        rewrite(raw, write("0"))
        report = verify_declarations(raw)
        self.assertNotIn("v14.operating_parameters", {item["code"] for item in report["errors"]})
        warnings = [item for item in report["diagnostics"] if item["code"] == "v14.motion_unqualified"]
        self.assertTrue(warnings)
        self.assertFalse(warnings[0]["blocking"])
        self.assertIn("unlimited", warnings[0]["message"])
        self.assertEqual(
            warnings[0]["detail"]["limits"],
            dict.fromkeys(["lower", "upper", "effort", "velocity"], 0),
        )

        for label, values, expected in (
            ("negative", "-1.0", {"v14.operating_parameters"}),
            ("nonfinite", "inf", {"v14.invalid_scalar", "v14.operating_parameters"}),
        ):
            with self.subTest(case=label):
                candidate = fixture()
                rewrite(candidate, write(values))
                self.assertTrue(expected & set(self.errors(candidate)), label)

        candidate = fixture()

        def drop_effort(root):
            for item in limit_items(root):
                kind = item.find(NS + "AttributeType")
                if kind is not None and kind.text == "effort":
                    item.remove(item.find(NS + "Value"))
                    return

        rewrite(candidate, drop_effort)
        self.assertIn("v14.attribute_shape", self.errors(candidate))

    def test_boundary_tristate_exact_hash_document_configuration_and_pid(self):
        for change, code in [
            ({"state": "unread", "error": "native failure"}, "v14.unread"),
            ({"state": "absent"}, "v14.absent"),
            ({"raw_xml_sha256": None}, "v14.raw_hash_mismatch"),
            ({"document": "other.SLDASM"}, "v14.capture_binding"),
            ({"configuration": "other"}, "v14.capture_binding"),
        ]:
            raw = fixture()
            raw["exporter"].update(change)
            self.assertIn(code, self.errors(raw))
        raw = fixture()
        raw["exporter"]["pid_bindings"][0]["error_code"] = 2
        self.assertIn("v14.pid_unread", self.errors(raw))

    def test_imported_source_requires_native_path_confinement_and_bound_bytes(self):
        raw = fixture()
        proof = {
            "state": "resolved",
            "kind": "native_imported_document",
            "used_api": "IComponent2.GetPathName/IComponent2.GetImportedPath/IComponent2.IsVirtual",
            "source_root": "C:\\locked-copy",
            "imported_path": "C:\\locked-copy\\source.step",
            "native_document_path": "C:\\temp\\generated.sldprt",
            "document": "source.step",
            "sha256": "a" * 64,
            "is_virtual": False,
        }
        row = raw["exporter"]["pid_bindings"][2]
        item = next(item for item in raw["components"] if item["name2"] == row["name2"])
        row.update(document="source.step", source_provenance=copy.deepcopy(proof))
        item.update(document="source.step", source_provenance=copy.deepcopy(proof))
        raw["files"] = {"source.step": "a" * 64}
        self.assertTrue(verify_declarations(raw)["passed"])
        for patch in [
            {"imported_path": "C:\\outside\\source.step"},
            {"sha256": "b" * 64},
            {"source_root": None},
            {"state": "unread"},
            {"used_api": "basename_guess"},
        ]:
            candidate = copy.deepcopy(raw)
            candidate["exporter"]["pid_bindings"][2]["source_provenance"].update(patch)
            native = next(item for item in candidate["components"] if item["name2"] == row["name2"])
            native["source_provenance"].update(patch)
            self.assertIn("v14.imported_binding", self.errors(candidate), patch)
        candidate = fixture()
        candidate["exporter"]["pid_bindings"][2]["document"] = None
        native = next(item for item in candidate["components"] if item["name2"] == row["name2"])
        native.update(document=None, virtual=True)
        self.assertIn("v14.pid_document_unread", self.errors(candidate))
        raw = fixture()
        raw["exporter"]["pid_bindings"][0]["name2"] = "foreign"
        self.assertIn("v14.pid_unread", self.errors(raw))

    def test_overlap_and_foreign_main_pid_fail(self):
        raw = fixture()
        raw["exporter"]["pid_bindings"][0].update(
            {key: raw["exporter"]["pid_bindings"][2][key] for key in ["name2", "document", "configuration"]}
        )
        self.assertIn("v14.membership_overlap", self.errors(raw))
        raw = fixture()
        rewrite(raw, lambda root: setattr(root.find(NS + "SWMainComponentPID"), "text", "REVG"))
        self.assertIn("v14.main_component", self.errors(raw))

    def test_cached_materialization_pid_binds_through_the_sealed_carrier(self):
        """A cached import PID is a complete identity: engine materialization + carrier."""

        materialization = r"C:\Users\33985\AppData\Local\Temp\swx16840\IC~~\NP-F550.step.SLDPRT"
        session_root = r"C:\Users\33985\AppData\Local\Temp\swx16840"
        document = materialization.replace("/", "\\").strip().lower()
        owner, occurrence = "module.SLDASM", "module-1/tool_part"
        owner_sha = "a" * 64
        provenance = {
            "state": "cached_materialization",
            "kind": "native_import_materialization",
            "used_api": "IComponent2.GetPathName/IComponent2.GetImportedPath/IComponent2.IsVirtual",
            "source_root": "C:\\locked-copy",
            "native_document_path": materialization,
            "materialization_path": materialization,
            "session_temp_root": session_root,
            "author_imported_path": r"D:\author\NP-F550.step",
            "author_imported_in_source_root": False,
            "author_imported_relative": None,
            "is_virtual": False,
            "owner": owner,
            "owner_sha256": owner_sha,
            "occurrence": occurrence,
            "defining_anchor": None,
            "document": None,
        }
        slot = {
            "document": owner,
            "instance": occurrence,
            "owner_anchor": None,
            "state": "cached_materialization",
            "binding": "author_metadata_only",
            "materialization": materialization,
            "session_temp_root": session_root,
            "author_imported_path": r"D:\author\NP-F550.step",
            "owner_sha256": owner_sha,
            "configuration": "Default",
            "suppression_state": 2,
            "is_virtual": False,
        }
        raw = fixture()
        raw["files"] = {owner: owner_sha}
        raw["source_selection"] = sealed_selection({owner: owner_sha}, owner, edges={owner: []}, imports=[slot])
        row = raw["exporter"]["pid_bindings"][2]
        item = next(entry for entry in raw["components"] if entry["name2"] == row["name2"])
        self.assertEqual(item["name2"], occurrence)
        row.update(document=document, source_provenance=copy.deepcopy(provenance))
        item.update(document=document, source_provenance=copy.deepcopy(provenance))
        errors = self.errors(raw)
        self.assertNotIn("v14.imported_binding", errors)
        self.assertNotIn("v14.pid_document_unread", errors)
        self.assertNotIn("v14.pid_binding", errors)

        cases = {
            "outside_owned_temp": {"materialization_path": r"C:\elsewhere\IC~~\NP-F550.step.SLDPRT"},
            "root_mismatch": {"session_temp_root": r"C:\Users\33985\AppData\Local\Temp\swx99999"},
            "not_cached_in_pid": {"state": "unread"},
            "external_document": {"materialization_path": r"D:\author\NP-F550.step"},
            "author_metadata_divergence": {"author_imported_path": r"D:\elsewhere\NP-F550.step"},
        }
        for label, patch in cases.items():
            with self.subTest(case=label):
                candidate = copy.deepcopy(raw)
                candidate["exporter"]["pid_bindings"][2]["source_provenance"].update(patch)
                self.assertIn("v14.imported_binding", self.errors(candidate), patch)
        candidate = copy.deepcopy(raw)
        candidate["source_selection"]["closure"]["imports"] = []
        self.assertIn("v14.imported_binding", self.errors(candidate))

    def test_unproven_absolute_document_stays_refused_without_provenance(self):
        """A provenance-less PID document must still be a confined package path."""

        raw = fixture()
        row = raw["exporter"]["pid_bindings"][2]
        item = next(entry for entry in raw["components"] if entry["name2"] == row["name2"])
        row.pop("source_provenance", None)
        item.pop("source_provenance", None)
        row.update(document=r"C:\external\tool_part.SLDPRT")
        item.update(document=r"C:\external\tool_part.SLDPRT")
        self.assertIn("v14.pid_binding", self.errors(raw))

    def test_serialized_origin_is_claim_only_when_customized(self):
        raw = fixture()  # stored origin differs from native datums, but isCustomized=false
        self.assertTrue(verify_declarations(raw)["passed"])
        rewrite(raw, lambda root: [setattr(item, "text", "true") for item in root.iter(NS + "isCustomized")])
        self.assertIn("v14.evidence_invalid", self.errors(raw))

    def test_saved_pose_restrictions_are_residual_diagnostics_not_qualification_gates(self):
        raw = fixture()
        raw["mates"] = [lock("base_part", "module-1/tool_part")]
        report = verify_declarations(raw)
        self.assertTrue(report["passed"], report["errors"])
        (warning,) = report["diagnostics"]
        self.assertEqual(warning["code"], "v14.saved_pose_restriction")
        self.assertFalse(warning["blocking"])
        self.assertEqual(warning["detail"]["joint"], "hinge")
        self.assertEqual(warning["detail"]["raw_residuals"], [0, 0, 0, 0, 1, 0])
        self.assertEqual(warning["detail"]["max_abs_residual"], 1)
        self.assertNotIn("declarations.motion", {row["id"] for row in report["checks"]})
        raw["mates"][0]["type"] = "unknown"
        report = verify_declarations(raw)
        self.assertTrue(report["passed"])
        self.assertNotIn("v14.saved_pose_restriction", {row["code"] for row in report["diagnostics"]})
        self.assertIn("v14.mate_inference_unavailable", {row["code"] for row in report["diagnostics"]})

    def test_frame_axis_and_generated_claim_tampering_fail(self):
        raw = fixture()
        raw["reference_axes"][0]["axis_direction"] = [1, 0, 0]
        self.assertIn("v14.evidence_invalid", self.errors(raw))
        raw = fixture()
        raw["datums"][1]["owner"] = "foreign"
        self.assertIn("v14.datum_unread", self.errors(raw))
        claims = source_claims()
        claims["joints"][0]["limits"]["effort"] = 10
        self.assertIn("v14.claims", self.errors(fixture(), claims))
        claims = source_claims()
        claims["bodies"][1]["components"].pop()
        self.assertIn("v14.claims", self.errors(fixture(), claims))

    def test_optional_saved_semantics_block_silent_discard(self):
        raw = fixture()

        def damping(root):
            joint = next(item for item in root.iter() if item.get(Z + "Id") == "joint")
            d = ET.SubElement(joint, NS + "Dynamics")
            a = ET.SubElement(d, NS + "DampingAttribute")
            ET.SubElement(a, NS + "AttributeType").text = "damping"
            ET.SubElement(a, NS + "Value").text = "0.5"

        rewrite(raw, damping)
        self.assertIn("v14.unsupported_semantics", self.errors(raw))

    def test_package_gate_uses_bound_declaration_without_old_naming_or_graph_requirements(self):
        with tempfile.TemporaryDirectory() as directory:
            package = Path(directory)
            native = b"synthetic native bytes for verification tests only"
            (package / "robot.SLDASM").write_bytes(native)
            native_files = {"robot.SLDASM": hashlib.sha256(native).hexdigest()}
            raw = fixture()
            raw["files"] = native_files
            raw["source_selection"] = sealed_selection(native_files, "robot.SLDASM", edges={"robot.SLDASM": []})
            raw["masses"] = [
                {"component": item["name2"], "mass_kg": 1.0}
                for item in raw["components"]
                if not item["document"].endswith(".SLDASM")
            ]
            payload = {
                "schema_version": "solidworks-to-urdf.native-discovery/v1",
                "run_id": "test",
                "contract": "native-discovery/v1",
                "raw": raw,
                "native_files": native_files,
                "identity": copy.deepcopy(raw["identity"]),
            }
            data = json.dumps(payload).encode()
            (package / "discovery").mkdir()
            (package / "discovery/native-discovery.json").write_bytes(data)
            inventory = (json.dumps(native_files, sort_keys=True, ensure_ascii=False, indent=2) + "\n").encode()
            robot = {
                "schema_version": "solidworks-to-urdf.input/v1",
                "hardware_id": "fixture",
                "source": source_claims(),
                "provenance": {
                    "generator": "native-discovery",
                    "generator_version": "test",
                    "contract": "native-discovery/v1",
                    "run_id": "test",
                    "discovery_sha256": hashlib.sha256(data).hexdigest(),
                    "native_inventory_sha256": hashlib.sha256(inventory).hexdigest(),
                },
            }
            robot["source"]["robot_name"] = "fixture"
            (package / "robot.yaml").write_text(json.dumps(robot))
            (package / "cad-revision.json").write_text(
                json.dumps(
                    {
                        "schema_version": "solidworks-to-urdf.cad-revision/v1",
                        **{key: value for key, value in raw["identity"].items() if key != "robot_name"},
                        "cad_files": native_files,
                    }
                )
            )
            report = verify_discovery(package)
            self.assertTrue(report["passed"], report["errors"])
            self.assertEqual((report["bodies"], report["joints"]), (2, 1))
            self.assertIn("discovery.declarations", {check["id"] for check in report["checks"]})
            self.assertNotIn("discovery.budgets", {check["id"] for check in report["checks"]})

            def reseal():
                data = json.dumps(payload).encode()
                (package / "discovery/native-discovery.json").write_bytes(data)
                robot["provenance"]["discovery_sha256"] = hashlib.sha256(data).hexdigest()
                (package / "robot.yaml").write_text(json.dumps(robot))

            authored = copy.deepcopy(raw["identity"])
            manifest = (package / "cad-revision.json").read_bytes()
            (package / "cad-revision.json").unlink()
            self.assertIn("discovery.revision", {item["code"] for item in verify_discovery(package)["errors"]})
            raw["identity"] = dict.fromkeys(authored)
            payload["identity"] = copy.deepcopy(raw["identity"])
            robot["hardware_id"] = None
            robot["source"]["robot_name"] = "robot"
            reseal()
            unsealed = verify_discovery(package)
            self.assertTrue(unsealed["passed"], unsealed["errors"])
            self.assertNotIn("discovery.revision", {item["id"] for item in unsealed["checks"]})
            self.assertEqual(
                [item["code"] for item in unsealed["diagnostics"]],
                ["discovery.publication_metadata_incomplete", "discovery.revision_not_sealed"],
            )
            self.assertTrue(all(item["blocking"] is False for item in unsealed["diagnostics"]))
            for mutate in (
                lambda: robot.update(hardware_id="fabricated"),
                lambda: raw["identity"].update(owner=42),
                lambda: raw["identity"].update(control={"system": "unknown", "reference": None}),
                lambda: raw["identity"].update(control={"system": "git", "reference": "short-hash"}),
                lambda: robot["source"].update(robot_name="fabricated"),
                lambda: payload["identity"].update(revision="fabricated"),
            ):
                mutate()
                reseal()
                self.assertIn("discovery.identity", {item["code"] for item in verify_discovery(package)["errors"]})
                raw["identity"] = dict.fromkeys(authored)
                payload["identity"] = copy.deepcopy(raw["identity"])
                robot["hardware_id"] = None
                robot["source"]["robot_name"] = "robot"
            (package / "cad-revision.json").write_bytes(manifest)
            reseal()
            self.assertIn("discovery.revision", {item["code"] for item in verify_discovery(package)["errors"]})
            raw["identity"] = authored
            payload["identity"] = copy.deepcopy(authored)
            robot["hardware_id"] = "fixture"
            robot["source"]["robot_name"] = "fixture"
            reseal()
            self.assertTrue(verify_discovery(package)["passed"])

            payload["budget"] = None
            reseal()
            malformed = verify_discovery(package)
            self.assertIn("discovery.budgets", {item["code"] for item in malformed["errors"]})
            budget = {"expected_mass_kg": [1, 5], "expected_extent_m": [0, 1]}
            budget_bytes = json.dumps(budget).encode()
            (package / "budget.json").write_bytes(budget_bytes)
            payload["budget"] = {
                "package_file": "budget.json",
                "key": "",
                "sha256": hashlib.sha256(budget_bytes).hexdigest(),
            }
            robot["checks"] = budget
            reseal()
            self.assertTrue(verify_discovery(package)["passed"])
            (package / "budget.json").write_bytes(budget_bytes + b" ")
            changed_budget = verify_discovery(package)
            self.assertIn("discovery.record", {item["code"] for item in changed_budget["errors"]})
            (package / "robot.SLDASM").write_bytes(native + b"tampered")
            changed = verify_discovery(package)
            self.assertFalse(changed["passed"])
            self.assertIn("discovery.files", {item["code"] for item in changed["errors"]})


if __name__ == "__main__":
    unittest.main()
