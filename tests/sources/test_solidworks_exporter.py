"""Exact DataContract wire tests and portable declaration evidence binding."""

from __future__ import annotations

import hashlib
import json
import math
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from . import _paths  # noqa: F401
from . import support  # noqa: E402
from .selection_support import sealed_selection  # noqa: E402
from .test_solidworks_discovery import IDENTITY, FakeBackend
from description_pipeline.sources.solidworks.discovery import (
    DISCOVERY_SCHEMA,
    _exporter_constraint_checks,
    _exporter_declaration,
    _exporter_joint_facts,
    _exporter_memberships,
    _exporter_native_pairs,
    prepare_native_package,
    source_definition,
)
from description_pipeline.sources.solidworks.exporter import (
    ARRAYS,
    NAMESPACE,
    SERIALIZATION,
    XSD,
    XSI,
    ExporterDeclarationError,
    decode_exporter_v14,
)
from description_pipeline.interface_records import PURPOSE_ENGINEERING, PURPOSE_INSPECTION_PREVIEW
from description_pipeline.request_evidence import build_request_evidence, request_evidence_bytes


def wire_xml(limit_values=None) -> str:
    """Two links with the saved 1.4 shape, including direct Attribute z:Ref."""

    def attribute(identity, name, value, kind="s:string"):
        wire_value = '<Value i:nil="true" />' if value is None else f'<Value i:type="{kind}">{value}</Value>'
        return (
            f'<Attribute z:Id="{identity}"><AttributeType>{name}</AttributeType>'
            f"<IsRequired>true</IsRequired>{wire_value}</Attribute>"
        )

    def vector(identity, name, values):
        return attribute(
            identity, name, "".join(f"<a:double>{value}</a:double>" for value in values), "a:ArrayOfdouble"
        )

    def joint(prefix, root=False):
        name, kind, parent, child = ("", "", "", "") if root else ("hinge", "revolute", "base_link", "arm")
        attrs = attribute(prefix + "name", "name", name) + attribute(prefix + "type", "type", kind)
        origin = (
            f'<URDFElement z:Id="{prefix}origin" i:type="Origin"><Attributes>'
            + vector(prefix + "xyz", "xyz", [0, 0, 0])
            + vector(prefix + "rpy", "rpy", [0, 0, 0])
            + f'</Attributes><RPYAttribute z:Ref="{prefix}rpy"/><XYZAttribute z:Ref="{prefix}xyz"/>'
            + "<isCustomized>false</isCustomized></URDFElement>"
        )
        endpoints = ""
        for element_type, endpoint in (("Parent", parent), ("Child", child)):
            endpoints += (
                f'<URDFElement z:Id="{prefix}{element_type}" i:type="{element_type}Link"><Attributes>'
                + attribute(prefix + element_type + "name", "link", endpoint)
                + f'</Attributes><NameAttribute z:Ref="{prefix}{element_type}name"/></URDFElement>'
            )
        axis = (
            f'<URDFElement z:Id="{prefix}axis" i:type="Axis"><Attributes>'
            + vector(prefix + "axisxyz", "xyz", [0, 0, 0 if root else 1])
            + f'</Attributes><XYZAttribute z:Ref="{prefix}axisxyz"/></URDFElement>'
        )
        limits = f'<URDFElement z:Id="{prefix}limit" i:type="Limit"><Attributes>'
        for key in ("lower", "upper", "effort", "velocity"):
            limits += attribute(prefix + key, key, None if root else str((limit_values or {}).get(key, 0)), "s:double")
        limits += "</Attributes>"
        for key in ("lower", "upper", "effort", "velocity"):
            limits += f'<{key.title()}Attribute z:Ref="{prefix}{key}"/>'
        limits += "</URDFElement>"
        return (
            f'<URDFElement z:Id="{prefix}joint" i:type="Joint"><Attributes>{attrs}</Attributes>'
            f"<ChildElements>{origin}{endpoints}{axis}{limits}</ChildElements>"
            f'<NameAttribute z:Ref="{prefix}name"/><TypeAttribute z:Ref="{prefix}type"/>'
            f'<Parent z:Ref="{prefix}Parent"/><Child z:Ref="{prefix}Child"/>'
            f'<Origin z:Ref="{prefix}origin"/><Axis z:Ref="{prefix}axis"/><Limit z:Ref="{prefix}limit"/>'
            f"<CoordinateSystemName>{'c_base' if root else 'c_arm'}</CoordinateSystemName>"
            f"<AxisName>{'' if root else 'a_hinge'}</AxisName></URDFElement>"
        )

    def link(prefix, name, parent, children, pid):
        parent_reference = 'i:nil="true"' if parent is None else f'z:Ref="{parent}"'
        return (
            f'<Link z:Id="{prefix}"><Attributes>{attribute(prefix + "linkname", "name", name)}</Attributes>'
            f'<ChildElements>{joint(prefix, parent is None)}</ChildElements><NameAttribute z:Ref="{prefix}linkname"/>'
            f"<Children>{children}</Children><Parent {parent_reference}/>"
            f'<Joint z:Ref="{prefix}joint"/><SWComponentPIDs><a:base64Binary>{pid}</a:base64Binary></SWComponentPIDs>'
            '<SWMainComponentPID i:nil="true"/><isFixedFrame>false</isFixedFrame><isIncomplete>false</isIncomplete>'
            # These old derived caches are deliberately unusable as engineering facts.
            "<Inertial><mass>NaN</mass></Inertial><Visual><filename>old/manual.stl</filename></Visual></Link>"
        )

    child = link("moving", "arm", "root", "", "YXJt")
    root = link("root", "base_link", None, child, "YmFzZQ==")
    return root.replace(
        '<Link z:Id="root">',
        f'<Link xmlns="{NAMESPACE}" xmlns:z="{SERIALIZATION}" xmlns:i="{XSI}" '
        f'xmlns:s="{XSD}" xmlns:a="{ARRAYS}" z:Id="root">',
        1,
    )


def raw_record() -> dict:
    xml = wire_xml({"lower": -1.0, "upper": 1.0, "effort": 1.0, "velocity": 1.0})
    files = {
        name: hashlib.sha256(b"native-file-test").hexdigest()
        for name in ("robot.SLDASM", "base.SLDPRT", "arm.SLDPRT")
    }
    return {
        "schema_version": DISCOVERY_SCHEMA,
        "identity": {
            "hardware_id": "wire_test",
            "revision": "r1",
            "owner": "test",
            "change_summary": "wire decoder",
            "control": {"system": "handoff", "reference": "test"},
            "main_assembly": "robot.SLDASM",
            "robot_name": "wire_test",
        },
        "saved_configuration": "Default",
        "document_state": support.saved_document_state(),
        # The exporter fixture's frozen bytes are the deterministic placeholder
        # written by the preparation test; the derive contract binds them.
        "files": files,
        "source_selection": sealed_selection(
            files,
            "robot.SLDASM",
            edges={
                "robot.SLDASM": ["base.SLDPRT", "arm.SLDPRT"],
                "base.SLDPRT": [],
                "arm.SLDPRT": [],
            },
        ),
        "components": [
            {
                "name2": name,
                "document": document,
                "configuration": "Default",
                "transform": IDENTITY.copy(),
                "suppressed": False,
                "lightweight": False,
                "suppression_state": 2,
            }
            for name, document in (("base-1", "base.SLDPRT"), ("arm-1", "arm.SLDPRT"))
        ],
        "datums": [
            {"name": name, "owner": "", "configuration": "Default", "array": IDENTITY.copy()}
            for name in ("c_base", "c_arm")
        ],
        "mates": [],
        "exporter": {
            "state": "extracted",
            "attribute_name": "URDF Export Configuration (v1.4)",
            "exporter_version": 1.4,
            "raw_xml": xml,
            "raw_xml_sha256": hashlib.sha256(xml.encode()).hexdigest(),
            "document": "robot.SLDASM",
            "configuration": "Default",
            "pid_bindings": [
                {"pid_hex": pid, "name2": name, "document": doc, "configuration": "Default", "error_code": 0}
                for pid, name, doc in ((b"base".hex(), "base-1", "base.SLDPRT"), (b"arm".hex(), "arm-1", "arm.SLDPRT"))
            ],
        },
        "reference_axes": [
            {
                "kind": "reference_axis",
                "component": "",
                "selector": {"kind": "reference_axis", "component": "", "feature_name": "a_hinge"},
                "coordinate_frame": "assembly",
                "raw_endpoints_m": [0, 0, 0, 0, 0, 1],
                "axis_point_m": [0, 0, 0],
                "axis_direction": [0, 0, 1],
                "direction_semantics": "undirected_axis_line",
                "used_api": "IRefAxis.GetRefAxisParams",
            }
        ],
    }


def native_limit_mate(lower: float, upper: float) -> dict:
    return {
        "name": "hinge__limit",
        "scope": "",
        "type": "limitangle",
        "suppressed": False,
        "is_advanced_mate": True,
        "is_advanced_mate_error": None,
        "is_advanced_mate_api": "IAngleMateFeatureData.IsAdvancedMate",
        "native_limit": support.native_limit_observation(6, nominal=0.0, lower=lower, upper=upper),
        "error_code": 0,
        "entities": [
            {
                "entity_index": 0,
                "component": "base-1",
                "reference_configuration": "Default",
                "feature": "LimitPlane1",
                "mate_entity_reference": {"error": None, "geometry": None},
            },
            {
                "entity_index": 1,
                "component": "arm-1",
                "reference_configuration": "Default",
                "feature": "LimitPlane2",
                "mate_entity_reference": {"error": None, "geometry": None},
            },
        ],
        "limits": {"unit": "rad", "lower": lower, "upper": upper},
    }


class ExporterWireTests(unittest.TestCase):
    def test_exact_references_and_individual_byte_arrays_preserve_root_and_zero_values(self):
        parsed = decode_exporter_v14(wire_xml(), 1.4)
        self.assertEqual(parsed["root"], "base_link")
        self.assertEqual(len(parsed["links"]), 2)
        root, child = parsed["links"]
        self.assertEqual(root["component_pids"], [b"base".hex()])
        self.assertEqual(child["component_pids"], [b"arm".hex()])
        self.assertEqual(root["joint"]["name"], "")
        self.assertEqual(root["joint"]["coordinate_system"], "c_base")
        self.assertEqual(root["joint"]["limits"], dict.fromkeys(("lower", "upper", "effort", "velocity")))
        self.assertEqual(child["joint"]["limits"], dict.fromkeys(("lower", "upper", "effort", "velocity"), 0.0))
        self.assertNotIn("inertial", child)
        self.assertNotIn("visual", child)

    def test_namespace_prefix_and_reference_identity_are_not_semantics(self):
        xml = wire_xml().replace("z:", "reference:").replace("xmlns:z=", "xmlns:reference=")
        xml = xml.replace('"moving', '"another_object_')
        self.assertEqual(decode_exporter_v14(xml, 1.4), decode_exporter_v14(wire_xml(), 1.4))

    def test_forward_attribute_reference_and_multiple_pid_items(self):
        xml = wire_xml().replace('<NameAttribute z:Ref="rootlinkname"/>', "")
        xml = xml.replace('z:Id="root"><Attributes>', 'z:Id="root"><NameAttribute z:Ref="rootlinkname"/><Attributes>')
        xml = xml.replace(
            "<a:base64Binary>YmFzZQ==</a:base64Binary>",
            "<a:base64Binary>YmFzZQ==</a:base64Binary><a:base64Binary>AQID</a:base64Binary>",
        )
        root = decode_exporter_v14(xml, 1.4)["links"][0]
        self.assertEqual(root["name"], "base_link")
        self.assertEqual(root["component_pids"], [b"base".hex(), "010203"])

    def test_ref_target_is_attribute_not_a_nested_attribute_wrapper(self):
        xml = wire_xml().replace('<Attribute z:Id="rootlinkname">', '<Attribute z:Id="rootlinkname"><Attribute>')
        with self.assertRaises(ExporterDeclarationError):
            decode_exporter_v14(xml, 1.4)

    def test_invalid_versions_namespaces_reference_graphs_and_tree_endpoints_fail(self):
        for version in (1.3, 1.5, "1.4", None, True):
            with self.subTest(version=version), self.assertRaises(ExporterDeclarationError):
                decode_exporter_v14(wire_xml(), version)
        for xml in (
            wire_xml().replace(NAMESPACE, NAMESPACE + "wrong"),
            wire_xml().replace('z:Ref="rootlinkname"', 'z:Ref="missing"'),
            wire_xml().replace('z:Id="movingname"', 'z:Id="rootname"'),
            wire_xml().replace('<Parent z:Ref="root"/>', '<Parent z:Ref="moving"/>'),
            wire_xml().replace("<a:base64Binary>YXJt", "<a:base64Binary>***"),
        ):
            with self.subTest(xml=xml[:100]), self.assertRaises(ExporterDeclarationError):
                decode_exporter_v14(xml, 1.4)

    def test_non_snake_raw_names_are_preserved_for_the_shared_normalizer(self):
        xml = wire_xml().replace(">base_link</Value>", ">Base Link</Value>").replace(">arm</Value>", ">A Arm</Value>")
        parsed = decode_exporter_v14(xml, 1.4)
        self.assertEqual(parsed["root"], "Base Link")
        self.assertEqual([link["name"] for link in parsed["links"]], ["Base Link", "A Arm"])
        self.assertEqual(parsed["links"][1]["joint"]["name"], "hinge")

    def test_dtd_and_xml_resource_limits_fail_before_decoding(self):
        with self.assertRaises(ExporterDeclarationError):
            decode_exporter_v14('<!DOCTYPE Link [<!ENTITY name "x">]>' + wire_xml(), 1.4)
        with (
            mock.patch("description_pipeline.sources.solidworks.exporter.MAX_XML_BYTES", 100),
            self.assertRaises(ExporterDeclarationError),
        ):
            decode_exporter_v14(wire_xml(), 1.4)
        with (
            mock.patch("description_pipeline.sources.solidworks.exporter.MAX_DEPTH", 3),
            self.assertRaises(ExporterDeclarationError),
        ):
            decode_exporter_v14(wire_xml(), 1.4)


class ExporterDiscoveryTests(unittest.TestCase):
    def derive(self, record):
        declaration = decode_exporter_v14(record["exporter"]["raw_xml"], 1.4)
        findings = []
        bodies, clusters = _exporter_memberships(record, declaration, findings)
        joints = _exporter_joint_facts(record, declaration, bodies, findings)
        _exporter_constraint_checks(declaration, clusters, joints, findings)
        return bodies, joints, findings

    def test_native_names_memberships_axes_do_not_require_a_rediscovered_mate_graph(self):
        bodies, joints, findings = self.derive(raw_record())
        self.assertEqual(
            [(body["name"], body["components"]) for body in bodies], [("base_link", ["base-1"]), ("arm", ["arm-1"])]
        )
        self.assertEqual(
            joints[0]["axis_reference"], {"kind": "reference_axis", "component": "", "feature_name": "a_hinge"}
        )
        self.assertEqual(joints[0]["limits"], {"lower": -1.0, "upper": 1.0, "effort": 1.0, "velocity": 1.0})
        self.assertEqual([item["code"] for item in findings], ["direction.semantics_unresolved"])
        self.assertFalse(findings[0]["blocking"])

    def test_rotation_window_policy_uses_purpose_specific_rules(self):
        record = raw_record()
        record["exporter"]["raw_xml"] = wire_xml(
            {"lower": 0.0, "upper": 0.0, "effort": 0.0, "velocity": 0.0}
        )
        record["exporter"]["raw_xml_sha256"] = hashlib.sha256(
            record["exporter"]["raw_xml"].encode()
        ).hexdigest()
        declaration = decode_exporter_v14(record["exporter"]["raw_xml"], 1.4)
        bodies, _clusters = _exporter_memberships(record, declaration, [])
        findings = []
        joints = _exporter_joint_facts(
            record,
            declaration,
            bodies,
            findings,
            purpose=PURPOSE_INSPECTION_PREVIEW,
        )
        self.assertEqual(joints[0]["limit_source"], "preview_policy")
        self.assertEqual(
            joints[0]["limits"],
            {"lower": -math.pi, "upper": math.pi, "effort": 0.0, "velocity": 0.0},
        )
        self.assertEqual(
            joints[0]["preview_policy"]["rule_id"], "inspection_rotation_full_turn/v1"
        )
        engineering_findings = []
        engineering = _exporter_joint_facts(
            record,
            declaration,
            bodies,
            engineering_findings,
            purpose=PURPOSE_ENGINEERING,
        )
        self.assertEqual(engineering[0]["limit_source"], "default_policy")
        self.assertEqual(
            engineering[0]["limits"],
            {"lower": -math.pi, "upper": math.pi, "effort": 0.0, "velocity": 0.0},
        )
        self.assertEqual(
            engineering[0]["default_policy"]["rule_id"],
            "engineering_rotation_default_window/v1",
        )
        self.assertIs(engineering[0]["default_policy"]["engineering_position_range_qualified"], False)
        self.assertEqual(
            [item["code"] for item in engineering_findings if item["code"] == "discovery.joint_default_policy"],
            ["discovery.joint_default_policy"],
        )
        self.assertFalse(
            next(item for item in engineering_findings if item["code"] == "discovery.joint_default_policy")["blocking"]
        )

    def test_engineering_prefers_native_then_nonzero_author_range(self) -> None:
        native = raw_record()
        native["mates"] = [native_limit_mate(-1.0, 1.0)]
        _, joints, _findings = self.derive(native)
        self.assertEqual(joints[0]["limit_source"], "native")
        self.assertEqual(joints[0]["limits"], {"lower": -1.0, "upper": 1.0, "effort": 1.0, "velocity": 1.0})

        declared = raw_record()
        declared["mates"] = []
        _, joints, _findings = self.derive(declared)
        self.assertEqual(joints[0]["limit_source"], "declared_author")
        self.assertEqual(joints[0]["limits"], {"lower": -1.0, "upper": 1.0, "effort": 1.0, "velocity": 1.0})

    def test_engineering_unreadable_native_limit_uses_default_and_retains_errors(self) -> None:
        record = raw_record()
        record["exporter"]["raw_xml"] = wire_xml({"lower": 0.0, "upper": 0.0, "effort": 0.0, "velocity": 0.0})
        record["exporter"]["raw_xml_sha256"] = hashlib.sha256(record["exporter"]["raw_xml"].encode()).hexdigest()
        record["mates"] = [
            {
                "name": "hinge__limit",
                "type": "limitangle",
                "suppressed": False,
                "error_code": 0,
                "is_advanced_mate": None,
                "is_advanced_mate_error": None,
                "is_advanced_mate_api": None,
                "native_limit": None,
                "limits": None,
                "entities": [],
            }
        ]
        _bodies, joints, findings = self.derive(record)
        self.assertEqual(joints[0]["limit_source"], "default_policy")
        policy = joints[0]["default_policy"]
        self.assertEqual(policy["native_absence"], "native_limit_observation_unreadable")
        self.assertEqual(
            policy["native_observation_errors"],
            [
                {
                    "mate": "hinge__limit",
                    "reason": "native limit entities do not span the declared parent/child subtrees",
                }
            ],
        )
        warning = next(item for item in findings if item["code"] == "discovery.joint_default_policy")
        self.assertFalse(warning["blocking"])
        self.assertEqual(warning["detail"]["native_observation_errors"], policy["native_observation_errors"])

    def test_equal_bounds_and_zero_drives_are_preserved_as_stage_warnings(self):
        for values, expected in (
            ({"lower": -1, "upper": -1, "effort": 2, "velocity": 3}, "discovery.joint_limits_degenerate"),
            ({"lower": -1, "upper": 1, "effort": 0, "velocity": 3}, "discovery.joint_drive_unspecified"),
            ({"lower": -1, "upper": 1, "effort": 2, "velocity": 0}, "discovery.joint_drive_unspecified"),
        ):
            raw = raw_record()
            raw["exporter"]["raw_xml"] = wire_xml(values)
            _, joints, findings = self.derive(raw)
            self.assertEqual(joints[0]["limits"], values)
            self.assertEqual(
                [item["code"] for item in findings],
                ["direction.semantics_unresolved", expected],
            )
            self.assertFalse(findings[0]["blocking"])
            self.assertFalse(findings[1]["blocking"])
            self.assertIn("qualification_blocker", findings[1]["detail"])

    def test_exporter_zero_placeholder_without_native_range_uses_engineering_default(self):
        raw = raw_record()
        raw["exporter"]["raw_xml"] = wire_xml()
        raw["exporter"]["raw_xml_sha256"] = hashlib.sha256(raw["exporter"]["raw_xml"].encode()).hexdigest()
        _bodies, joints, findings = self.derive(raw)
        self.assertEqual(joints[0]["limit_source"], "default_policy")
        self.assertEqual(joints[0]["default_policy"]["rule_id"], "engineering_rotation_default_window/v1")
        item = next(item for item in findings if item["code"] == "discovery.joint_default_policy")
        self.assertFalse(item["blocking"])

    def test_missing_bounds_default_while_invalid_values_still_block(self):
        raw = raw_record()
        declaration = decode_exporter_v14(raw["exporter"]["raw_xml"], 1.4)
        bodies, _ = _exporter_memberships(raw, declaration, [])
        declaration["links"][1]["joint"]["limits"] = {
            "lower": None,
            "upper": 1,
            "effort": 2,
            "velocity": 3,
        }
        missing_findings = []
        missing = _exporter_joint_facts(raw, declaration, bodies, missing_findings)
        self.assertEqual(missing[0]["limit_source"], "default_policy")
        self.assertNotIn("discovery.joint_limits_invalid", {item["code"] for item in missing_findings})

        for changes, code in (
            ({"upper": float("nan")}, "discovery.joint_limits_invalid"),
            ({"lower": 2, "upper": 1}, "discovery.joint_limits_invalid"),
            ({"effort": None}, "discovery.joint_drive_invalid"),
            ({"velocity": float("inf")}, "discovery.joint_drive_invalid"),
            ({"effort": -1}, "discovery.joint_drive_invalid"),
            ({"velocity": -1}, "discovery.joint_drive_invalid"),
        ):
            raw = raw_record()
            declaration = decode_exporter_v14(raw["exporter"]["raw_xml"], 1.4)
            bodies, _ = _exporter_memberships(raw, declaration, [])
            declaration["links"][1]["joint"]["limits"] = {
                "lower": -1,
                "upper": 1,
                "effort": 2,
                "velocity": 3,
                **changes,
            }
            findings = []
            _exporter_joint_facts(raw, declaration, bodies, findings)
            self.assertIn(code, {item["code"] for item in findings if item["blocking"]})
        with self.assertRaises(ExporterDeclarationError):
            decode_exporter_v14(wire_xml({"velocity": float("nan")}), 1.4)

    def test_uncovered_material_warning_preserves_exact_occurrence_without_assignment(self):
        raw = raw_record()
        raw["components"].append({**raw["components"][0], "name2": "unselected-1"})
        bodies, _, findings = self.derive(raw)
        coverage = next(item for item in findings if item["code"] == "discovery.exporter_membership_uncovered")
        self.assertFalse(coverage["blocking"])
        self.assertEqual(
            coverage["detail"],
            {
                "components": ["unselected-1"],
                "qualification_blocker": "complete_material_coverage",
            },
        )
        self.assertFalse(any("unselected-1" in body["components"] for body in bodies))
        self.assertIn("unselected-1", {item["name2"] for item in raw["components"]})

    def test_moving_selected_subtree_is_not_absorbed_by_parent_selection(self):
        raw = raw_record()
        raw["components"][0].update(name2="assembly-1", document="assembly.SLDASM")
        raw["components"][1]["name2"] = "assembly-1/arm-1"
        raw["components"].append({**raw["components"][1], "name2": "assembly-1/base-1", "document": "base.SLDPRT"})
        for binding, component in zip(raw["exporter"]["pid_bindings"], raw["components"][:2], strict=True):
            binding.update({key: component[key] for key in ("name2", "document")})
        bodies, _, _ = self.derive(raw)
        self.assertEqual(bodies[0]["components"], ["assembly-1/base-1"])
        self.assertEqual(bodies[1]["components"], ["assembly-1/arm-1"])

    def test_unknown_pid_overlap_uncovered_and_binding_identity_fail_closed(self):
        for mutation, code in (
            (lambda raw: raw["exporter"]["pid_bindings"].pop(), "discovery.exporter_pid_unresolved"),
            (
                lambda raw: raw["exporter"]["pid_bindings"][1].update(name2="base-1", document="base.SLDPRT"),
                "discovery.exporter_membership_overlap",
            ),
            (
                lambda raw: raw["components"].append({**raw["components"][0], "name2": "unselected-1"}),
                "discovery.exporter_membership_uncovered",
            ),
            (
                lambda raw: raw["exporter"]["pid_bindings"][0].update(configuration="Other"),
                "discovery.exporter_pid_unresolved",
            ),
        ):
            raw = raw_record()
            mutation(raw)
            self.assertIn(code, {item["code"] for item in self.derive(raw)[2]})

    def test_only_complete_rigidity_proves_an_unselected_attachment(self):
        raw = raw_record()
        raw["components"].append({**raw["components"][0], "name2": "cover-1"})
        raw["mates"] = [
            {
                "name": "cover_lock",
                "type": "lock",
                "limits": None,
                "error_code": 0,
                "suppressed": False,
                "entities": [{"component": "base-1"}, {"component": "cover-1"}],
            }
        ]
        bodies, _, findings = self.derive(raw)
        self.assertEqual(bodies[0]["components"], ["base-1", "cover-1"])
        self.assertIn("cover-1", bodies[0]["rigid_attachments"])
        self.assertNotIn("discovery.exporter_membership_uncovered", {item["code"] for item in findings})
        raw["mates"][0]["error_code"] = 1
        self.assertIn("discovery.exporter_membership_uncovered", {item["code"] for item in self.derive(raw)[2]})

    def test_rank_five_and_ambiguous_attachments_never_choose_an_owner(self):
        raw = raw_record()
        raw["components"].append({**raw["components"][0], "name2": "washer-1"})
        cylinder = {"point": [0, 0, 0], "direction": [0, 0, 1], "radius": 0.01}
        plane = {"point": [0, 0, 0], "normal": [0, 0, 1]}
        raw["mates"] = [
            {
                "name": kind,
                "type": kind,
                "limits": None,
                "error_code": 0,
                "entities": [
                    {"component": "base-1", shape: geometry},
                    {"component": "washer-1", shape: geometry},
                ],
            }
            for kind, shape, geometry in (("concentric", "cylinder", cylinder), ("coincident", "plane", plane))
        ]
        bodies, _, findings = self.derive(raw)
        self.assertEqual(bodies[0]["components"], ["base-1"])
        self.assertIn("discovery.exporter_membership_uncovered", {item["code"] for item in findings})
        raw["mates"] = [
            {
                "name": name,
                "type": "lock",
                "limits": None,
                "error_code": 0,
                "entities": [{"component": name}, {"component": "washer-1"}],
            }
            for name in ("base-1", "arm-1")
        ]
        bodies, _, findings = self.derive(raw)
        self.assertFalse(any("washer-1" in body["components"] for body in bodies))
        self.assertIn("discovery.exporter_rigidity_conflict", {item["code"] for item in findings})

    def test_main_component_pid_must_belong_to_its_declared_link(self):
        raw = raw_record()
        xml = raw["exporter"]["raw_xml"]
        raw["exporter"]["raw_xml"] = xml.replace(
            '<SWMainComponentPID i:nil="true"/>', "<SWMainComponentPID>YmFzZQ==</SWMainComponentPID>"
        )
        self.assertIn("discovery.exporter_main_component_mismatch", {item["code"] for item in self.derive(raw)[2]})

    def test_axis_requires_endpoint_provenance_and_child_frame_on_shaft(self):
        raw = raw_record()
        raw["reference_axes"][0]["raw_endpoints_m"] = None
        self.assertIn("discovery.exporter_axis_invalid", {item["code"] for item in self.derive(raw)[2]})
        raw = raw_record()
        raw["datums"][1]["array"][3] = 0.1
        self.assertIn("discovery.joint_frame_off_axis", {item["code"] for item in self.derive(raw)[2]})

    def test_duplicate_frame_names_across_owners_cannot_drop_scope(self):
        raw = raw_record()
        raw["datums"].append({**raw["datums"][1], "owner": "arm-1"})
        self.assertIn("discovery.exporter_frame_ambiguous", {item["code"] for item in self.derive(raw)[2]})

    def test_saved_pose_rigidity_cannot_merge_or_veto_separately_declared_links(self):
        raw = raw_record()
        raw["mates"] = [
            {
                "name": "saved_pose_lock",
                "type": "lock",
                "limits": None,
                "error_code": 0,
                "entities": [{"component": "base-1"}, {"component": "arm-1"}],
            }
        ]
        bodies, _, findings = self.derive(raw)
        self.assertEqual([body["components"] for body in bodies], [["base-1"], ["arm-1"]])
        codes = {item["code"] for item in findings}
        self.assertIn("discovery.exporter_rigidity_conflict", codes)
        self.assertIn("discovery.exporter_saved_pose_restriction", codes)
        self.assertFalse(any(item["blocking"] for item in findings))

    def test_parallel_saved_pose_restrictions_keep_per_mate_evidence(self):
        raw = raw_record()
        raw["mates"] = [
            {
                "name": name,
                "scope": "",
                "type": "parallel",
                "limits": None,
                "error_code": 0,
                "entities": [
                    {"component": component, "plane": {"point": [0, 0, 0], "normal": normal}}
                    for component in ("base-1", "arm-1")
                ],
            }
            for name, normal in (("pose_hold_one", [1, 0, 0]), ("pose_hold_two", [0, 1, 0]), ("shaft", [0, 0, 1]))
        ]
        bodies, joints, findings = self.derive(raw)
        self.assertFalse(any(item["blocking"] for item in findings))
        self.assertEqual([body["components"] for body in bodies], [["base-1"], ["arm-1"]])
        self.assertEqual(joints[0]["axis"]["direction"], [0, 0, 1])
        observations = [item for item in findings if item["code"] == "discovery.exporter_saved_pose_restriction"]
        self.assertEqual([item["detail"]["mate"] for item in observations], ["pose_hold_one", "pose_hold_two"])
        for item in observations:
            detail = item["detail"]
            self.assertFalse(item["blocking"])
            self.assertEqual(detail["joint"], "hinge")
            self.assertEqual(detail["scope"], "")
            self.assertEqual(detail["mate_type"], "parallel")
            self.assertEqual(detail["components"], ["arm-1", "base-1"])
            self.assertEqual(detail["links"], ["arm", "base_link"])
            self.assertEqual(detail["declared_twist"], [0, 0, 0, 0, 0, 1])
            self.assertEqual(detail["raw_residuals"], [row[5] for row in detail["rows"]])
            self.assertEqual(detail["max_abs_residual"], 1.0)
            self.assertEqual(detail["max_normalized_residual"], 1.0)
            self.assertEqual(detail["threshold"], 1e-6)

    def test_saved_pose_observation_keeps_existing_scaled_tolerance(self):
        for deviation, expected_count in ((5e-7, 0), (2e-6, 1)):
            with self.subTest(deviation=deviation):
                raw = raw_record()
                raw["mates"] = [
                    {
                        "name": "nearly_axial",
                        "type": "parallel",
                        "limits": None,
                        "error_code": 0,
                        "entities": [
                            {"component": component, "plane": {"point": [0, 0, 0], "normal": [deviation, 0, 1]}}
                            for component in ("base-1", "arm-1")
                        ],
                    }
                ]
                findings = self.derive(raw)[2]
                observations = [
                    item for item in findings if item["code"] == "discovery.exporter_saved_pose_restriction"
                ]
                self.assertEqual(len(observations), expected_count)
                if observations:
                    self.assertAlmostEqual(observations[0]["detail"]["max_normalized_residual"], deviation, places=12)
                    self.assertFalse(observations[0]["blocking"])

    def test_incomplete_mates_are_diagnostic_and_cannot_complete_rank_six_membership(self):
        for kind, error, normals, expected in (
            ("parallel", 0, ([1, 0, 0], [0, 1, 0]), "discovery.mate_geometry_mismatch"),
            ("parallel", 0, (None, None), "discovery.mate_entities_unsupported"),
            ("lock", 1, (None, None), "discovery.mate_error_state"),
            ("gear", 0, (None, None), "discovery.mate_unsupported"),
        ):
            with self.subTest(expected=expected):
                raw = raw_record()
                raw["components"].append({**raw["components"][0], "name2": "cover-1"})
                raw["mates"] = [
                    {
                        "name": "recorded_lock",
                        "type": "lock",
                        "limits": None,
                        "error_code": 0,
                        "entities": [{"component": "base-1"}, {"component": "cover-1"}],
                    },
                    {
                        "name": "incomplete_constraint",
                        "type": kind,
                        "limits": None,
                        "error_code": error,
                        "entities": [
                            {
                                "component": component,
                                **({"plane": {"point": [0, 0, 0], "normal": normal}} if normal else {}),
                            }
                            for component, normal in zip(("base-1", "cover-1"), normals, strict=True)
                        ],
                    },
                ]
                pair = _exporter_native_pairs(raw, [])["base-1", "cover-1"]
                self.assertEqual(pair["rank"], 6)
                self.assertTrue(pair["unresolved"])
                self.assertEqual([item["mate"] for item in pair["constraints"]], ["recorded_lock"])
                bodies, _, findings = self.derive(raw)
                self.assertEqual(bodies[0]["components"], ["base-1"])
                self.assertFalse(any(item["blocking"] for item in findings))
                diagnostic = next(item for item in findings if item["code"] == expected)
                self.assertEqual(diagnostic["detail"]["mate"], "incomplete_constraint")
                self.assertIn("discovery.exporter_membership_uncovered", {item["code"] for item in findings})

    def test_rejected_mate_does_not_hide_other_valid_saved_pose_restrictions(self):
        raw = raw_record()
        raw["mates"] = [
            {
                "name": "valid_pose_hold",
                "scope": "",
                "type": "parallel",
                "limits": None,
                "error_code": 0,
                "entities": [
                    {"component": component, "plane": {"point": [0, 0, 0], "normal": [1, 0, 0]}}
                    for component in ("base-1", "arm-1")
                ],
            },
            {
                "name": "unread_pose_hold",
                "type": "lock",
                "limits": None,
                "error_code": 1,
                "entities": [{"component": "base-1"}, {"component": "arm-1"}],
            },
        ]
        findings = self.derive(raw)[2]
        observations = [item for item in findings if item["code"] == "discovery.exporter_saved_pose_restriction"]
        self.assertEqual([item["detail"]["mate"] for item in observations], ["valid_pose_hold"])
        self.assertFalse(any(item["blocking"] for item in findings))

    def test_one_sided_parallel_geometry_cannot_contribute_rigidity_or_pose_rows(self):
        raw = raw_record()
        raw["mates"] = [
            {
                "name": "one_sided_parallel",
                "type": "parallel",
                "limits": None,
                "error_code": 0,
                "entities": [
                    {"component": "base-1", "cylinder": {"point": [0, 0, 0], "direction": [1, 0, 0], "radius": 1}},
                    {"component": "arm-1"},
                ],
            }
        ]
        findings = []
        pair = _exporter_native_pairs(raw, findings)["arm-1", "base-1"]
        self.assertEqual(pair["rows"], [])
        self.assertEqual(pair["constraints"], [])
        self.assertEqual(pair["rank"], 0)
        self.assertTrue(pair["unresolved"])
        self.assertEqual([item["code"] for item in findings], ["discovery.mate_entities_unsupported"])
        self.assertFalse(findings[0]["blocking"])

    def test_conflicting_native_limits_block_without_replacing_the_declared_range(self):
        raw = raw_record()
        raw["mates"] = [native_limit_mate(-2.0, 2.0)]
        _, joints, findings = self.derive(raw)
        observed = next(item for item in findings if item["code"] == "discovery.joint_limits_conflict")
        self.assertEqual(observed["detail"]["native"], [-2.0, 2.0])
        self.assertEqual(observed["detail"]["declared"], (-1.0, 1.0))
        self.assertEqual(joints, [])
        self.assertTrue(observed["blocking"])

    def test_structural_declaration_errors_remain_blocking(self):
        for mutate, code in (
            (lambda raw: raw["exporter"]["pid_bindings"].pop(), "discovery.exporter_pid_unresolved"),
            (
                lambda raw: raw["exporter"]["pid_bindings"][1].update(name2="base-1", document="base.SLDPRT"),
                "discovery.exporter_membership_overlap",
            ),
            (lambda raw: raw["datums"].pop(), "discovery.exporter_frame_missing"),
            (lambda raw: raw["reference_axes"].clear(), "discovery.exporter_axis_missing"),
        ):
            with self.subTest(code=code):
                raw = raw_record()
                mutate(raw)
                self.assertIn(code, {item["code"] for item in self.derive(raw)[2] if item["blocking"]})

    def test_signed_axis_is_independent_of_native_undirected_axis_orientation(self):
        raw = raw_record()
        raw["reference_axes"][0]["axis_direction"] = [0, 0, -1]
        self.assertEqual(self.derive(raw)[1][0]["axis"]["direction"], [0, 0, 1])
        raw["reference_axes"][0].update(axis_direction=[1, 0, 0], raw_endpoints_m=[0, 0, 0, 1, 0, 0])
        self.assertIn("discovery.exporter_axis_mismatch", {item["code"] for item in self.derive(raw)[2]})

    def test_only_customized_origin_is_a_second_authored_frame_claim(self):
        raw = raw_record()
        raw["exporter"]["raw_xml"] = raw["exporter"]["raw_xml"].replace(
            "<a:double>0</a:double>", "<a:double>0.01</a:double>"
        )
        self.assertNotIn("discovery.exporter_origin_mismatch", {item["code"] for item in self.derive(raw)[2]})
        raw["exporter"]["raw_xml"] = raw["exporter"]["raw_xml"].replace(
            "<isCustomized>false</isCustomized>", "<isCustomized>true</isCustomized>"
        )
        self.assertIn("discovery.exporter_origin_mismatch", {item["code"] for item in self.derive(raw)[2]})

    def test_absent_unread_and_wrong_document_configuration_hash_are_distinct(self):
        identity = {"main_assembly": "robot.SLDASM", "delivery_configuration": "Default"}
        for change, expected in (
            ({"state": "absent"}, None),
            ({"state": "unread"}, "discovery.exporter_unread"),
            ({"document": "other.SLDASM"}, "discovery.exporter_binding_mismatch"),
            ({"configuration": "Other"}, "discovery.exporter_binding_mismatch"),
            ({"raw_xml_sha256": "0" * 64}, "discovery.exporter_digest_mismatch"),
        ):
            raw = raw_record()
            raw["exporter"].update(change)
            findings = []
            self.assertIsNone(_exporter_declaration(raw, identity, findings))
            self.assertEqual([item["code"] for item in findings], [] if expected is None else [expected])

    def test_preparation_preserves_declared_zero_warnings_when_publication_metadata_is_deferred(self):
        raw = raw_record()
        raw["identity"]["owner"] = None
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "input"
            source.mkdir()
            for name in ("robot.SLDASM", "base.SLDPRT", "arm.SLDPRT"):
                (source / name).write_bytes(b"native-file-test")
            prepared = Path(directory) / "prepared"
            result = prepare_native_package(source, prepared, "wire-run", backend=FakeBackend(raw))
            self.assertTrue(result.passed, result.findings)
            self.assertFalse((prepared / "cad-revision.json").exists())
            self.assertFalse((prepared / "robot.yaml").exists())
            payload = json.loads(result.discovery_path.read_text())
            record = json.loads((prepared / "source.json").read_text(encoding="utf-8"))
            self.assertEqual(record["identity"]["hardware_id"], "wire_test")
        self.assertEqual(payload["identity"]["delivery_configuration"], "Default")
        self.assertEqual(payload["raw"]["identity"].get("delivery_configuration"), None)
        self.assertEqual(payload["raw"]["exporter"]["raw_xml"], raw["exporter"]["raw_xml"])
        self.assertNotIn("discovery.joint_name_missing", {item["code"] for item in result.findings})
        budget = next(item for item in result.findings if item["code"] == "discovery.design_budget_unspecified")
        self.assertFalse(budget["blocking"])
        self.assertNotIn("budget", payload)
        owner = next(item for item in result.findings if item["code"] == "discovery.owner_missing")
        self.assertFalse(owner["blocking"])
        self.assertEqual(owner["detail"]["publication_blocker"], "owner")
        self.assertFalse([item for item in result.findings if item["blocking"]])

    def test_prepared_engineering_source_carries_the_default_policy(self):
        raw = raw_record()
        xml = wire_xml({"lower": 0.0, "upper": 0.0, "effort": 0.0, "velocity": 0.0})
        raw["exporter"].update(raw_xml=xml, raw_xml_sha256=hashlib.sha256(xml.encode()).hexdigest())
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "input"
            source.mkdir()
            for name in ("robot.SLDASM", "base.SLDPRT", "arm.SLDPRT"):
                (source / name).write_bytes(b"native-file-test")
            prepared = Path(directory) / "prepared"
            result = prepare_native_package(source, prepared, "default-policy", backend=FakeBackend(raw))
            self.assertTrue(result.passed, result.findings)
            request = build_request_evidence(
                purpose=PURPOSE_ENGINEERING,
                request_id="default-policy",
                target_hardware="wire_test",
                change_summary="default policy",
                main_assembly="robot.SLDASM",
                handoff_digest="a" * 64,
                platform_config_digest="b" * 64,
            )
            request_path = prepared / "input/request.json"
            request_path.parent.mkdir(parents=True)
            request_path.write_bytes(request_evidence_bytes(request))
            definition = source_definition(prepared)
        joint = definition["joints"][0]
        self.assertEqual(joint["limit_source"], "default_policy")
        self.assertEqual(joint["limits"], {"lower": -math.pi, "upper": math.pi, "effort": 0.0, "velocity": 0.0})
        self.assertEqual(joint["default_policy"]["rule_id"], "engineering_rotation_default_window/v1")
        self.assertEqual(joint["default_policy"]["native_observation_errors"], [])

    def test_non_snake_native_interfaces_are_normalized_without_blocking(self):
        raw = raw_record()
        xml = raw["exporter"]["raw_xml"].replace(">base_link</Value>", ">Base Link</Value>").replace(
            ">arm</Value>", ">A Arm</Value>"
        )
        raw["exporter"].update(raw_xml=xml, raw_xml_sha256=hashlib.sha256(xml.encode()).hexdigest())
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "input"
            source.mkdir()
            for name in ("robot.SLDASM", "base.SLDPRT", "arm.SLDPRT"):
                (source / name).write_bytes(b"native-file-test")
            prepared = Path(directory) / "prepared"
            result = prepare_native_package(source, prepared, "raw-names", backend=FakeBackend(raw))
            self.assertTrue(result.passed, result.findings)
            payload = json.loads(result.discovery_path.read_text())
        self.assertEqual(
            payload["derived_interface_names"],
            {"A Arm": "arm_link", "Base Link": "base_link", "hinge": "hinge_joint"},
        )
        self.assertFalse([item for item in result.findings if item["blocking"]])

    def test_valid_declarations_generate_named_axis_input_without_stale_derived_values(self):
        raw = raw_record()
        limits = {"lower": -1.0, "upper": 1.0, "effort": 2.0, "velocity": 3.0}
        xml = wire_xml(limits)
        raw["exporter"].update(raw_xml=xml, raw_xml_sha256=hashlib.sha256(xml.encode()).hexdigest())
        bodies, joints, findings = self.derive(raw)
        self.assertEqual([item["code"] for item in findings], ["direction.semantics_unresolved"])
        self.assertEqual(joints[0]["limits"], limits)
        self.assertEqual(joints[0]["axis"]["direction"], [0.0, 0.0, 1.0])
        # One clear schema shape: a delivery without interface datums has no interface frames.
        self.assertEqual(
            [datum["name"] for datum in raw["datums"] if str(datum.get("name", "")).startswith(("TCP_", "SCS_"))],
            [],
        )
        self.assertEqual(joints[0]["axis_reference"]["kind"], "reference_axis")
        self.assertNotIn("old/manual.stl", str((bodies, joints)))

    def test_authored_axis_is_emitted_exactly_instead_of_the_reprojected_native_line(self):
        raw = raw_record()
        raw["reference_axes"][0]["axis_direction"] = [1e-9, 0.0, 1.0]
        raw["reference_axes"][0]["raw_endpoints_m"] = [0, 0, 0, 1e-9, 0, 1]
        _bodies, joints, findings = self.derive(raw)
        self.assertFalse([item for item in findings if item["blocking"]])
        self.assertEqual(joints[0]["declared_axis"], [0.0, 0.0, 1.0])
        # The independent declaration check compares exactly, so the emitted
        # direction is the authored vector, not the reprojected native line.
        self.assertEqual(joints[0]["axis"]["direction"], [0.0, 0.0, 1.0])


if __name__ == "__main__":
    unittest.main()
