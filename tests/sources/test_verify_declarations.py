"""Declaration verification: the v1.4 XML decoder and the native package gate.

One behavior regression suite for the declaration family: the decoder oracles
(the native DataContract object/reference shape) and the package-gate oracles
(native membership, rigidity, imported provenance and the discovery binding).
Generic two-link records are tests only, never CAD generation inputs.
"""

from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape

from description_pipeline.verification import v14_declarations as v14
from description_pipeline.io import PipelineError
from description_pipeline.verification import solidworks_urdf
from description_pipeline.verification.native_declarations import verify_declarations
from description_pipeline.verification.native_discovery import _Failure, rebuild_interface_frames, verify_discovery

from .selection_support import sealed_selection
from . import support


NS = 'xmlns="http://schemas.datacontract.org/2004/07/SW2URDF"'
XI = 'xmlns:i="http://www.w3.org/2001/XMLSchema-instance"'
ARR = 'xmlns:a="http://schemas.microsoft.com/2003/10/Serialization/Arrays"'
XS = 'xmlns:b="http://www.w3.org/2001/XMLSchema"'
Z = 'xmlns:z="http://schemas.microsoft.com/2003/10/Serialization/"'
HEADER = f"{NS} {XI} {ARR} {XS} {Z}"


def value_xml(value):
    if value is None:
        return '<Value i:nil="true"/>'
    if isinstance(value, list):
        values = "".join(f"<a:double>{v}</a:double>" for v in value)
        return f'<Value i:type="a:ArrayOfdouble">{values}</Value>'
    kind = "string" if isinstance(value, str) else "double"
    return f'<Value i:type="b:{kind}">{escape(str(value))}</Value>'


def attributes(identifier, fields):
    definitions = "".join(
        f'<Attribute z:Id="{identifier}_{member}"><AttributeType>{kind}</AttributeType>'
        f"<IsRequired>true</IsRequired>{value_xml(value)}</Attribute>"
        for member, kind, value in fields
    )
    refs = "".join(f'<{member} z:Ref="{identifier}_{member}"/>' for member, _kind, _value in fields)
    return f"<Attributes>{definitions}</Attributes>", refs


def element(kind, identifier, fields, extras=""):
    attrs, refs = attributes(identifier, fields)
    return f'<URDFElement z:Id="{identifier}" i:type="{kind}">{attrs}{extras}{refs}</URDFElement>'


def joint_xml(*, nil_velocity=True, root=False):
    prefix = "root_joint" if root else "joint"
    child_objects = "".join(
        [
            element(
                "Origin",
                prefix + "_origin",
                [("XYZAttribute", "xyz", [0, 0, 0.2]), ("RPYAttribute", "rpy", [0, 0, 1.5707963267948966])],
                "<isCustomized>false</isCustomized>",
            ),
            element("ParentLink", prefix + "_parent", [("NameAttribute", "link", "" if root else "base")]),
            element("ChildLink", prefix + "_child", [("NameAttribute", "link", "" if root else "tool")]),
            element("Axis", prefix + "_axis", [("XYZAttribute", "xyz", [0, 1, 0])]),
            element(
                "Limit",
                prefix + "_limit",
                [
                    ("LowerAttribute", "lower", -1.5),
                    ("UpperAttribute", "upper", 1.5),
                    ("EffortAttribute", "effort", 0.0),
                    ("VelocityAttribute", "velocity", None if nil_velocity else 3.0),
                ],
            ),
        ]
    )
    extras = (
        f"<ChildElements>{child_objects}</ChildElements>"
        + "".join(
            f'<{name} z:Ref="{prefix}_{name.lower()}"/>' for name in ["Origin", "Parent", "Child", "Axis", "Limit"]
        )
        + "<CoordinateSystemName>frame</CoordinateSystemName><AxisName>shaft</AxisName>"
    )
    return element(
        "Joint",
        prefix,
        [("NameAttribute", "name", "" if root else "hinge"), ("TypeAttribute", "type", "" if root else "revolute")],
        extras,
    )


def document_xml(*, extra_elements="", root_joint=False):
    child_attrs, child_refs = attributes("tool", [("NameAttribute", "name", "tool")])
    child = (
        f'<Link z:Id="tool">{child_attrs}<ChildElements>{joint_xml()}</ChildElements>'
        '<Joint z:Ref="joint"/><Parent z:Ref="base"/><Children/>'
        + child_refs
        + "<SWComponentPIDs><a:base64Binary>QUJD</a:base64Binary>"
        "<a:base64Binary>REVG</a:base64Binary></SWComponentPIDs>"
        "<isFixedFrame>false</isFixedFrame><isIncomplete>false</isIncomplete>"
        "<STLQualityFine>true</STLQualityFine></Link>"
    )
    attrs, refs = attributes("base", [("NameAttribute", "name", "base")])
    metadata = f'<ChildElements>{joint_xml(root=True)}</ChildElements><Joint z:Ref="root_joint"/>' if root_joint else ""
    return (
        f'<Link {HEADER} z:Id="base">{attrs}{metadata}<Parent i:nil="true"/>'
        f"<Children>{child}</Children>{refs}{extra_elements}"
        "<SWMainComponentPID>QUJDRA==</SWMainComponentPID><isFixedFrame>true</isFixedFrame></Link>"
    )


class V14DeclarationDecoderTests(unittest.TestCase):
    def assert_code(self, payload, expected):
        with self.assertRaises(v14.DeclarationError) as caught:
            v14.parse_v14_xml(payload)
        self.assertEqual(caught.exception.code, expected)

    def test_native_object_references_and_exact_missing_vs_zero(self):
        root = v14.parse_v14_xml(document_xml())
        self.assertEqual(root["name"], "base")
        self.assertTrue(root["is_fixed_frame"])
        self.assertIsNone(root["is_incomplete"])
        self.assertIsNone(root["stl_quality_fine"])
        self.assertEqual(root["main_component_pid"], "QUJDRA==")
        self.assertIsNone(root["component_pids"])
        self.assertIsNone(root["joint"])
        (child,) = root["children"]
        self.assertEqual(child["name"], "tool")
        self.assertFalse(child["is_fixed_frame"])
        self.assertFalse(child["is_incomplete"])
        self.assertTrue(child["stl_quality_fine"])
        self.assertEqual(child["children"], [])
        self.assertEqual(child["component_pids"], ["QUJD", "REVG"])
        self.assertEqual(
            child["joint"],
            {
                "name": "hinge",
                "type": "revolute",
                "parent_link": "base",
                "child_link": "tool",
                "origin": {"xyz": [0, 0, 0.2], "rpy": [0, 0, 1.5707963267948966], "customized": False},
                "axis": [0, 1, 0],
                "limit": {"lower": -1.5, "upper": 1.5, "effort": 0, "velocity": None},
                "coordinate_system_name": "frame",
                "axis_name": "shaft",
                "optional": {"calibration": None, "dynamics": None, "safety": None, "mimic": None},
            },
        )

    def test_root_metadata_holder_is_preserved_without_invented_joint(self):
        root = v14.parse_v14_xml(document_xml(root_joint=True))
        self.assertEqual(root["joint"]["name"], "")
        self.assertEqual(root["joint"]["type"], "")
        self.assertEqual(root["joint"]["coordinate_system_name"], "frame")

    def test_absent_nil_and_empty_arrays(self):
        for extra, expected in [
            ("", None),
            ('<SWComponentPIDs i:nil="true"/>', None),
            ("<SWComponentPIDs/>", []),
            ('<SWComponentPIDs><a:base64Binary i:nil="true"/></SWComponentPIDs>', [None]),
        ]:
            root = v14.parse_v14_xml(f"<Link {HEADER}>{extra}</Link>")
            self.assertEqual(root["component_pids"], expected)
            self.assertIsNone(root["joint"])
            self.assertIsNone(root["children"])

    def test_direct_serialized_members_are_attributes_without_nested_attribute(self):
        payload = (
            f"<Link {HEADER}><NameAttribute><AttributeType>name</AttributeType>"
            '<IsRequired>true</IsRequired><Value i:type="b:string">direct</Value>'
            "</NameAttribute></Link>"
        )
        self.assertEqual(v14.parse_v14_xml(payload)["name"], "direct")
        self.assert_code(payload.replace("<AttributeType>name</AttributeType>", ""), "v14.attribute_shape")
        self.assert_code(payload.replace('<Value i:type="b:string">direct</Value>', ""), "v14.attribute_shape")

    def test_shared_values_and_pid_slots(self):
        payload = document_xml().replace(
            "<a:base64Binary>QUJD</a:base64Binary>", '<a:base64Binary z:Id="pid">QUJD</a:base64Binary>'
        )
        payload = payload.replace(
            "<a:base64Binary>REVG</a:base64Binary>", '<a:base64Binary z:Ref="pid"/><a:base64Binary i:nil="true"/>'
        )
        root = v14.parse_v14_xml(payload)
        self.assertEqual(root["children"][0]["component_pids"], ["QUJD", "QUJD", None])
        payload = document_xml().replace(
            '<Value i:type="a:ArrayOfdouble">', '<Value z:Id="values" i:type="a:ArrayOfdouble">', 1
        )
        payload = payload.replace(
            '<Value i:type="a:ArrayOfdouble"><a:double>0</a:double><a:double>1</a:double>'
            "<a:double>0</a:double></Value>",
            '<Value z:Ref="values"/>',
        )
        self.assertEqual(v14.parse_v14_xml(payload)["children"][0]["joint"]["axis"], [0, 0, 0.2])

    def test_invalid_pid_and_old_repeated_container_shape_refused(self):
        for extra, code in [
            ("<SWComponentPIDs>QUJD</SWComponentPIDs>", "v14.pid_array"),
            ("<SWComponentPIDs/><SWComponentPIDs/>", "v14.duplicate_field"),
            ("<SWComponentPIDs><a:base64Binary>!</a:base64Binary></SWComponentPIDs>", "v14.invalid_pid"),
        ]:
            self.assert_code(f"<Link {HEADER}>{extra}</Link>", code)

    def test_reference_failures_are_explicit(self):
        for replacement, code in [
            ('<NameAttribute z:Ref="missing"/>', "v14.unresolved_reference"),
            ('<NameAttribute z:Id="both" z:Ref="base"/>', "v14.malformed_reference"),
            ('<NameAttribute z:Ref="base">text</NameAttribute>', "v14.malformed_reference"),
            ('<NameAttribute z:Ref="joint"/>', "v14.reference_type"),
        ]:
            self.assert_code(document_xml().replace('<NameAttribute z:Ref="base_NameAttribute"/>', replacement), code)
        self.assert_code(
            document_xml().replace("<Children/>", '<Children><Link z:Ref="base"/></Children>'), "v14.reference_cycle"
        )
        self.assert_code(document_xml().replace('z:Id="tool"', 'z:Id="base"'), "v14.reference_duplicate")
        self.assert_code(document_xml().replace('i:type="Limit"', 'i:type="Origin"'), "v14.reference_type")

    def test_bounded_reference_and_expanded_tree(self):
        with mock.patch.object(v14, "MAX_REF_HOPS", 0):
            self.assert_code(document_xml(), "v14.reference_too_deep")
        with mock.patch.object(v14, "MAX_LINKS", 1):
            self.assert_code(document_xml(), "v14.payload_too_large")

    def test_stale_serialized_physics_is_ignored(self):
        extra = "<Inertial><Mass>nonsense</Mass></Inertial><Visual><Mesh>missing.stl</Mesh></Visual>"
        self.assertEqual(v14.parse_v14_xml(document_xml()), v14.parse_v14_xml(document_xml(extra_elements=extra)))

    def test_optional_authored_semantics_are_preserved(self):
        dynamics = element("Dynamics", "dynamics", [("DampingAttribute", "damping", 0.5)])
        mimic = element(
            "Mimic", "mimic", [("JointNameAttribute", "joint", "driver"), ("MultiplierAttribute", "multiplier", 0)]
        )
        payload = document_xml().replace('<Joint z:Ref="joint"/>', '<Joint z:Ref="joint"/>' + dynamics + mimic)
        payload = payload.replace(
            "<AxisName>shaft</AxisName>", '<AxisName>shaft</AxisName><Dynamics z:Ref="dynamics"/><Mimic z:Ref="mimic"/>'
        )
        optional = v14.parse_v14_xml(payload)["children"][0]["joint"]["optional"]
        self.assertEqual(optional["dynamics"], {"Damping": 0.5, "Friction": None})
        self.assertEqual(optional["mimic"], {"JointName": "driver", "Multiplier": 0, "Offset": None})

    def test_xml_safety_guards(self):
        for xml, code in [
            ('<?xml version="1.0"?><!DOCTYPE Link><Link/>', "v14.unsafe_xml"),
            ('<!ENTITY x "y"><Link/>', "v14.unsafe_xml"),
            ("<Link><Children", "v14.malformed_xml"),
            ("<Robot/>", "v14.root_name"),
            ("<Link/>", "v14.root_name"),
        ]:
            self.assert_code(xml, code)
        with mock.patch.object(v14, "MAX_PAYLOAD_BYTES", 32):
            self.assert_code(document_xml(), "v14.payload_too_large")
        with mock.patch.object(v14, "MAX_DEPTH", 4):
            self.assert_code(document_xml(), "v14.payload_too_deep")
        with mock.patch.object(v14, "MAX_ELEMENTS", 3):
            self.assert_code(document_xml(), "v14.payload_too_large")

    def test_boundary_validation_and_hash(self):
        raw = document_xml()
        digest = hashlib.sha256(raw.encode()).hexdigest()
        bindings = [
            {
                "pid_hex": "414243",
                "name2": "part-1",
                "suppressed": False,
                "lightweight": False,
                "suppression_state": 2,
                "error_code": 0,
                "document": "part.SLDPRT",
                "configuration": "Default",
            }
        ]
        base = {
            "attribute_name": v14.ATTRIBUTE_NAME,
            "exporter_version": 1.4,
            "raw_xml": raw,
            "raw_xml_sha256": digest.upper(),
            "pid_bindings": bindings,
            "document": "robot.SLDASM",
            "configuration": "Default",
        }
        decoded = v14.validate_boundary(base)
        self.assertEqual(decoded["root"]["name"], "base")
        self.assertEqual(decoded["raw_xml_sha256"], digest)
        self.assertEqual(decoded["pid_bindings"], bindings)
        self.assertEqual(v14.pid_hex(" Q U J D \n"), "414243")
        for changes, code in [
            ({"attribute_name": v14.LEGACY_ATTRIBUTE_NAMES[0]}, "v14.legacy_attribute"),
            ({"attribute_name": "other"}, "v14.attribute_name"),
            ({"exporter_version": 1.3}, "v14.exporter_version"),
            ({"raw_xml_sha256": "0" * 64}, "v14.raw_hash_mismatch"),
            ({"raw_xml_sha256": None}, "v14.raw_hash_mismatch"),
            ({"configuration": None}, "v14.boundary_identity"),
            ({"pid_bindings": {}}, "v14.pid_bindings"),
            ({"pid_bindings": bindings * 2}, "v14.pid_bindings"),
            ({"raw_xml": ""}, "v14.empty_payload"),
        ]:
            with self.assertRaises(v14.DeclarationError) as caught:
                v14.validate_boundary({**base, **changes})
            self.assertEqual(caught.exception.code, code)


if __name__ == "__main__":
    unittest.main()


NS_TAG = "{http://schemas.datacontract.org/2004/07/SW2URDF}"
Z_TAG = "{http://schemas.microsoft.com/2003/10/Serialization/}"
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
        "document_state": support.saved_document_state(),
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
            if item.get(Z_TAG + "Id") == "joint_limit_EffortAttribute":
                item.find(NS_TAG + "Value").text = "5"
            if item.get(Z_TAG + "Id") == "joint_limit_VelocityAttribute":
                value = item.find(NS_TAG + "Value")
                value.attrib.clear()
                value.text = "2"
            if item.get(Z_TAG + "Id") in {"root_joint", "joint"}:
                item.find(NS_TAG + "CoordinateSystemName").text = (
                    "base_frame" if item.get(Z_TAG + "Id") == "root_joint" else "tool_frame"
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


class DeclarationConsumerTests(unittest.TestCase):
    """The URDF-stage binder must receive the declared carrier, never zero objects."""

    def _delivery(self, directory: str, mutate_raw=None) -> Path:
        package = Path(directory) / "delivery"
        native = b"synthetic native bytes for verification tests only"
        native_files = {"robot.SLDASM": hashlib.sha256(native).hexdigest()}
        raw = fixture()
        if mutate_raw is not None:
            mutate_raw(raw)
        raw["files"] = native_files
        raw["source_selection"] = sealed_selection(native_files, "robot.SLDASM", edges={"robot.SLDASM": []})
        raw["masses"] = [
            {"component": item["name2"], "mass_kg": 1.0}
            for item in raw["components"]
            if not item["document"].endswith(".SLDASM")
        ]
        robot = {"hardware_id": "fixture", "source": source_claims(), "checks": {}}
        robot["source"]["robot_name"] = "fixture"
        payload = support.frozen_discovery_payload(
            {
                "schema_version": "solidworks-to-urdf.native-discovery/v1",
                "run_id": "declaration-consumer",
                "contract": "native-discovery/v1",
                "raw": raw,
                "identity": copy.deepcopy(raw["identity"]),
            },
            files=native_files,
        )
        source = support.default_source_record(
            main_assembly="robot.SLDASM",
            configuration="Default",
            hardware_id="fixture",
            revision="r1",
        )
        support.write_target_delivery(
            package,
            capture={"declared_masses": {"items": {}, "evidence": None}},
            discovery=payload,
            source=source,
            originals={"robot.SLDASM": native.decode("utf-8")},
        )
        return package

    def test_declaration_consumer_binds_the_declared_carrier(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            returned = solidworks_urdf._input(self._delivery(directory), [])
        bodies = {body["name"]: body["components"] for body in returned["source"]["bodies"]}
        self.assertEqual(
            bodies,
            {"base": ["base_part"], "tool": ["module-1/bracket", "module-1/tool_part"]},
        )
        self.assertEqual(
            [
                (joint["name"], joint["type"], joint["parent"], joint["child"])
                for joint in returned["source"]["joints"]
            ],
            [("hinge", "revolute", "base", "tool")],
        )
        self.assertEqual(returned["source"]["frames"], [])

    def test_declaration_consumer_refuses_invalid_states_without_zero_objects(self) -> None:
        for label, mutate in (
            ("unreadable", lambda raw: raw["exporter"].update(state="unreadable")),
            ("unknown", lambda raw: raw["exporter"].update(state="unknown")),
            ("corrupt", lambda raw: raw["exporter"].update(raw_xml="<broken")),
        ):
            with (
                self.subTest(case=label),
                tempfile.TemporaryDirectory() as directory,
                self.assertRaises(PipelineError),
            ):
                solidworks_urdf._input(self._delivery(directory, mutate), [])

    def test_declaration_consumer_absent_state_raises_the_geometric_cause(self) -> None:
        with tempfile.TemporaryDirectory() as directory, self.assertRaises(Exception) as raised:  # noqa: B017
            solidworks_urdf._input(
                self._delivery(directory, lambda raw: raw["exporter"].update(state="absent")), []
            )
        self.assertIn("CS_<link>", str(raised.exception))

    @staticmethod
    def _interface(name: str, owner: str, configuration: str = "Default") -> dict:
        return {
            "name": name,
            "owner": owner,
            "configuration": configuration,
            "array": [1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0],
        }

    def test_declaration_frames_rebuild_native_interfaces(self) -> None:
        """A declaration without extension interfaces never proves their absence."""

        def inject(raw):
            raw["datums"].append(self._interface("TCP_tool_tcp", "module-1/tool_part"))
            raw["datums"].append(self._interface("SCS_sensor_mount", "module-1/bracket"))
            raw["datums"].append(self._interface("TCP_base_mount", "base_part"))

        with tempfile.TemporaryDirectory() as directory:
            returned = solidworks_urdf._input(self._delivery(directory, inject), [])
        frames = {
            frame["name"]: (frame["parent"], frame["coordinate_system"]) for frame in returned["source"]["frames"]
        }
        self.assertEqual(
            frames,
            {
                "tool_tcp": ("tool", "TCP_tool_tcp"),
                "sensor_mount": ("tool", "SCS_sensor_mount"),
                "base_mount": ("base", "TCP_base_mount"),
            },
        )
        # The frames become required gates: a model that drops or reparents an interface
        # fails the qualified report instead of passing with an unverified interface.
        identifiers = solidworks_urdf.required_checks(returned)
        self.assertIn("frames.tool_tcp", identifiers)
        self.assertIn("frames.base_mount", identifiers)

    def test_declaration_frame_gate_refuses_foreign_duplicate_and_wrong_configuration(self) -> None:
        cases = (
            (
                "foreign owner",
                lambda raw: raw["datums"].append(self._interface("TCP_orphan", "ghost-1")),
                "not owned by any rebuilt body",
            ),
            (
                "duplicate frame name",
                lambda raw: [
                    raw["datums"].append(self._interface("TCP_tool_tcp", "module-1/tool_part")) for _ in range(2)
                ],
                "derive the same frame name",
            ),
            (
                "wrong configuration",
                lambda raw: raw["datums"].append(self._interface("TCP_tool_tcp", "module-1/tool_part", "Other")),
                "another referenced configuration",
            ),
            (
                "body name collision",
                lambda raw: raw["datums"].append(self._interface("TCP_base", "module-1/tool_part")),
                "collides with a body name",
            ),
        )
        for label, mutate, needle in cases:
            with (
                self.subTest(case=label),
                tempfile.TemporaryDirectory() as directory,
                self.assertRaises(Exception) as raised,  # noqa: B017 - classification asserted below
            ):
                solidworks_urdf._input(self._delivery(directory, mutate), [])
            self.assertIn(needle, str(raised.exception))

    def test_declaration_frames_expand_subassembly_owners(self) -> None:
        """A sub-assembly owner attributes through its full proven leaf set, never by prefix."""

        def inject(raw):
            raw["datums"].append(self._interface("TCP_module_tcp", "module-1"))

        with tempfile.TemporaryDirectory() as directory:
            returned = solidworks_urdf._input(self._delivery(directory, inject), [])
        frames = {frame["name"]: frame["parent"] for frame in returned["source"]["frames"]}
        self.assertEqual(frames, {"module_tcp": "tool"})

    def test_interface_expansion_refuses_cross_body_and_empty(self) -> None:
        raw = {
            "components": [
                {"name2": "a-1", "configuration": "Default"},
                {"name2": "b-1", "configuration": "Default"},
                {"name2": "cont-1", "configuration": "Default"},
                {"name2": "cont-2", "configuration": "Default"},
            ],
            "datums": [self._interface("TCP_span", "cont-1")],
        }
        bodies = [
            {"id": "a", "name": "a", "components": ["a-1"], "frame": {"coordinate_system": "CS_a"}},
            {"id": "b", "name": "b", "components": ["b-1"], "frame": {"coordinate_system": "CS_b"}},
        ]
        with self.assertRaises(_Failure):
            rebuild_interface_frames(raw, bodies=bodies, base=None, expansion={"cont-1": ["a-1", "b-1"]})
        empty = copy.deepcopy(raw)
        empty["datums"][0]["owner"] = "cont-2"
        with self.assertRaises(_Failure):
            rebuild_interface_frames(empty, bodies=bodies, base=None, expansion={"cont-2": []})

    def test_declaration_frames_bind_each_owner_referenced_configuration(self) -> None:
        """Configuration binds the datum's native owner, not the shared main assembly."""

        def inject(raw):
            for collection in (raw["components"], raw["exporter"]["pid_bindings"]):
                for item in collection:
                    if item["name2"] == "module-1/tool_part":
                        item["configuration"] = "ToolR2"
            raw["datums"].append(self._interface("TCP_tool_tcp", "module-1/tool_part", "ToolR2"))
            raw["datums"].append(self._interface("TCP_base_mount", "base_part", "Default"))

        with tempfile.TemporaryDirectory() as directory:
            returned = solidworks_urdf._input(self._delivery(directory, inject), [])
        frames = {frame["name"]: frame["parent"] for frame in returned["source"]["frames"]}
        self.assertEqual(frames, {"tool_tcp": "tool", "base_mount": "base"})

    def test_declaration_frame_gate_refuses_wrong_owner_referenced_configuration(self) -> None:
        def mismatch(raw):
            raw["datums"].append(self._interface("TCP_tool_tcp", "module-1/tool_part", "ToolR9"))

        with (
            tempfile.TemporaryDirectory() as directory,
            self.assertRaises(Exception) as raised,  # noqa: B017 - classification asserted below
        ):
            solidworks_urdf._input(self._delivery(directory, mismatch), [])
        self.assertIn("another referenced configuration", str(raised.exception))

    def test_interface_expansion_refuses_a_missing_referenced_configuration(self) -> None:
        raw = {"components": [{"name2": "a-1"}], "datums": [self._interface("TCP_x", "a-1")]}
        bodies = [{"id": "a", "name": "a", "components": ["a-1"], "frame": {"coordinate_system": "CS_a"}}]
        with self.assertRaises(_Failure) as raised:
            rebuild_interface_frames(raw, bodies=bodies, base=None)
        self.assertIn("no recorded referenced configuration", str(raised.exception))

    def test_qualified_report_inventory_binds_the_declared_rebuild(self) -> None:
        from description_pipeline.interface_records import CHECK_POLICY_V1

        def inject(raw):
            raw["datums"].append(self._interface("TCP_tool_tcp", "module-1/tool_part"))

        with tempfile.TemporaryDirectory() as directory:
            root = self._delivery(directory, inject)
            definition = solidworks_urdf._input(root, [])
            required = set(solidworks_urdf.required_checks(definition))
            self.assertIn("frames.tool_tcp", required)
            checks = [{"id": name, "state": "passed", "passed": True} for name in sorted(required)]
            complete = {
                "passed": True,
                "required_checks": sorted(required),
                "checks": checks,
                "qualifications": solidworks_urdf.qualifications(root, definition, checks),
            }
        solidworks_urdf.require_qualified_report(complete, definition)
        fixed = required & set(CHECK_POLICY_V1["fixed_checks"])
        self.assertNotEqual(fixed, required)
        fixed_only = {
            "passed": True,
            "required_checks": sorted(fixed),
            "checks": [{"id": name, "state": "passed", "passed": True} for name in sorted(fixed)],
        }
        with self.assertRaises(PipelineError):
            solidworks_urdf.require_qualified_report(fixed_only, definition)
        with self.assertRaises(PipelineError):
            solidworks_urdf.require_qualified_report(
                {
                    "passed": True,
                    "required_checks": sorted(required | {"inertia.nonexistent_body"}),
                    "checks": [
                        {"id": name, "state": "passed", "passed": True}
                        for name in sorted(required | {"inertia.nonexistent_body"})
                    ],
                },
                definition,
            )


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
            report = verify_declarations(candidate)
            self.assertTrue(report["passed"], report["errors"])
            self.assertEqual(report["completion"], {})
            warnings = [item for item in report["diagnostics"] if item["code"] == "v14.unassigned_native"]
            self.assertEqual([item["detail"]["component"] for item in warnings], ["cover"])
            self.assertNotIn("cover", report["members"]["base"])

    def test_rigidity_to_two_declared_links_never_merges_or_selects_owner(self):
        raw = fixture()
        raw["components"].append(component("cover"))
        raw["mates"] = [lock("cover", "base_part"), lock("cover", "module-1/tool_part")]
        report = verify_declarations(raw)
        self.assertTrue(report["passed"], report["errors"])
        self.assertEqual(report["completion"], {})
        self.assertNotIn("cover", report["members"]["base"])
        self.assertNotIn("cover", report["members"]["tool"])
        warnings = [item for item in report["diagnostics"] if item["code"] == "v14.unassigned_native"]
        self.assertEqual([item["detail"]["component"] for item in warnings], ["cover"])
        self.assertFalse(warnings[0]["blocking"])

    def test_concentric_rotation_lock_rigidity_binds_true_and_the_documented_api(self):
        api = "IConcentricMateFeatureData.LockRotation"

        def coaxial(lock, witness):
            mate = {
                "name": "coaxial_washer",
                "type": "concentric",
                "suppressed": False,
                "error_code": 0,
                "scope": "",
                "limits": None,
                "entities": [
                    {
                        "component": "washer",
                        "feature": "Edge1",
                        "circle": {"center": [0, 0, 0], "normal": [0, 0, 1], "radius": 0.004},
                    },
                    {
                        "component": "base_part",
                        "feature": "Edge2",
                        "circle": {"center": [0, 0, 0], "normal": [0, 0, 1], "radius": 0.006},
                    },
                ],
            }
            if lock is not None:
                mate["lock_rotation"] = lock
            if witness is not None:
                mate["lock_rotation_api"] = witness
            return mate

        def seated():
            return {
                "name": "seated_washer",
                "type": "coincident",
                "suppressed": False,
                "error_code": 0,
                "scope": "",
                "limits": None,
                "entities": [
                    {"component": "washer", "feature": "Face1", "plane": {"point": [0, 0, 0], "normal": [0, 0, 1]}},
                    {
                        "component": "base_part",
                        "feature": "Face2",
                        "plane": {"point": [0, 0, 0], "normal": [0, 0, -1]},
                    },
                ],
            }

        def build(lock, witness):
            raw = fixture()
            raw["components"].append(component("washer"))
            raw["mates"] = [coaxial(lock, witness), seated()]
            return raw

        report = verify_declarations(build(True, api))
        self.assertTrue(report["passed"], report["errors"])
        self.assertEqual(report["completion"], {"washer": "base"})
        for label, raw in (
            ("free_spin", build(False, api)),
            ("unread_lock", build(None, api)),
            ("boolean_int", build(1, api)),
            ("string_true", build("true", api)),
            ("missing_witness", build(True, None)),
            ("guessed_witness", build(True, api + "-guess")),
        ):
            with self.subTest(case=label):
                report = verify_declarations(raw)
                self.assertTrue(report["passed"], report["errors"])
                self.assertEqual(report["completion"], {})
                warnings = [item for item in report["diagnostics"] if item["code"] == "v14.unassigned_native"]
                self.assertEqual([item["detail"]["component"] for item in warnings], ["washer"])

    def test_source_claims_cannot_absorb_or_omit_native_membership(self):
        raw = fixture()
        raw["components"].append(component("washer"))
        raw["mates"] = []
        absorbed = source_claims()
        absorbed["bodies"][0]["components"].append("washer")
        self.assertIn("v14.claims", self.errors(raw, absorbed))
        omitted = source_claims()
        omitted["bodies"][1]["components"] = ["module-1/tool_part"]
        self.assertIn("v14.claims", self.errors(fixture(), omitted))

    def test_zero_parameters_are_preserved_and_warn_as_motion_unqualified(self):
        def limit_items(root):
            return [
                item
                for item in root.iter()
                if (item.get(Z_TAG + "Id") or "").startswith("joint_limit_") and item.find(NS_TAG + "Value") is not None
            ]

        def write(values):
            def transform(root):
                for item in limit_items(root):
                    item.find(NS_TAG + "Value").text = values

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
                kind = item.find(NS_TAG + "AttributeType")
                if kind is not None and kind.text == "effort":
                    item.remove(item.find(NS_TAG + "Value"))
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
        rewrite(raw, lambda root: setattr(root.find(NS_TAG + "SWMainComponentPID"), "text", "REVG"))
        self.assertIn("v14.main_component", self.errors(raw))

    def test_cached_materialization_pid_binds_through_the_sealed_carrier(self):
        """A cached import PID is a complete identity: engine materialization + carrier."""

        materialization = r"C:\Users\operator\AppData\Local\Temp\swx16840\IC~~\imported_part.step.SLDPRT"
        session_root = r"C:\Users\operator\AppData\Local\Temp\swx16840"
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
            "author_imported_path": r"D:\author\imported_part.step",
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
            "author_imported_path": r"D:\author\imported_part.step",
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
            "outside_owned_temp": {"materialization_path": r"C:\elsewhere\IC~~\imported_part.step.SLDPRT"},
            "root_mismatch": {"session_temp_root": r"C:\Users\operator\AppData\Local\Temp\swx99999"},
            "not_cached_in_pid": {"state": "unread"},
            "external_document": {"materialization_path": r"D:\author\imported_part.step"},
            "author_metadata_divergence": {"author_imported_path": r"D:\elsewhere\imported_part.step"},
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
        rewrite(raw, lambda root: [setattr(item, "text", "true") for item in root.iter(NS_TAG + "isCustomized")])
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
            joint = next(item for item in root.iter() if item.get(Z_TAG + "Id") == "joint")
            d = ET.SubElement(joint, NS_TAG + "Dynamics")
            a = ET.SubElement(d, NS_TAG + "DampingAttribute")
            ET.SubElement(a, NS_TAG + "AttributeType").text = "damping"
            ET.SubElement(a, NS_TAG + "Value").text = "0.5"

        rewrite(raw, damping)
        self.assertIn("v14.unsupported_semantics", self.errors(raw))

    def test_package_gate_uses_bound_declaration_without_old_naming_or_graph_requirements(self):
        with tempfile.TemporaryDirectory() as directory:
            package = Path(directory) / "delivery"
            native = b"synthetic native bytes for verification tests only"
            native_files = {"robot.SLDASM": hashlib.sha256(native).hexdigest()}
            raw = fixture()
            raw["files"] = native_files
            raw["source_selection"] = sealed_selection(native_files, "robot.SLDASM", edges={"robot.SLDASM": []})
            raw["masses"] = [
                {"component": item["name2"], "mass_kg": 1.0}
                for item in raw["components"]
                if not item["document"].endswith(".SLDASM")
            ]
            robot = {
                "hardware_id": "fixture",
                "source": source_claims(),
                "checks": {},
            }
            robot["source"]["robot_name"] = "fixture"
            payload = support.frozen_discovery_payload(
                {
                    "schema_version": "solidworks-to-urdf.native-discovery/v1",
                    "run_id": "test",
                    "contract": "native-discovery/v1",
                    "raw": raw,
                    "identity": copy.deepcopy(raw["identity"]),
                },
                files=native_files,
            )
            source = support.default_source_record(
                main_assembly="robot.SLDASM",
                configuration="Default",
                hardware_id="fixture",
                revision="r1",
            )
            # The sealed record carries the authored CAD summary; keep the
            # fixture's source record coherent with the raw reading.
            source["metadata"]["change_summary"] = raw["identity"]["change_summary"]
            source["metadata"]["change_summary_source"] = "cad"
            support.write_target_delivery(
                package,
                capture={},
                discovery=payload,
                source=source,
                originals={"robot.SLDASM": native.decode("utf-8")},
            )

            def sync_robot() -> None:
                derived = payload.setdefault("derived", {})
                block = robot.get("source") or {}
                derived["bodies"] = copy.deepcopy(block.get("bodies") or [])
                derived["joints"] = copy.deepcopy(block.get("joints") or [])
                derived["frames"] = copy.deepcopy(block.get("frames") or [])
                derived["checks"] = copy.deepcopy(robot.get("checks") or {})
                payload["robot_name"] = block.get("robot_name")

            def reseal() -> None:
                sync_robot()
                payload["raw"] = copy.deepcopy(raw)
                from description_pipeline.io import write_json

                (package / "evidence/discovery.json").write_bytes(json.dumps(payload).encode())
                write_json(package / "input/source.json", source)
                support.seal_delivery(package)

            report = verify_discovery(package)
            self.assertTrue(report["passed"], report["errors"])
            self.assertEqual((report["bodies"], report["joints"]), (2, 1))
            self.assertIn("discovery.declarations", {check["id"] for check in report["checks"]})
            self.assertNotIn("discovery.budgets", {check["id"] for check in report["checks"]})

            authored = copy.deepcopy(raw["identity"])
            source["metadata"]["revision"] = None
            reseal()
            unsealed = verify_discovery(package)
            self.assertTrue(unsealed["passed"], unsealed["errors"])
            self.assertNotIn("discovery.revision", {item["id"] for item in unsealed["checks"]})
            self.assertEqual(
                [item["code"] for item in unsealed["diagnostics"]],
                ["discovery.revision_absent"],
            )
            self.assertTrue(all(item["blocking"] is False for item in unsealed["diagnostics"]))

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
                ["discovery.publication_metadata_incomplete", "discovery.revision_absent"],
            )
            self.assertTrue(all(item["blocking"] is False for item in unsealed["diagnostics"]))
            for mutate in (
                lambda: payload["identity"].update(hardware_id="fabricated"),
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
            raw["identity"] = authored
            payload["identity"] = copy.deepcopy(authored)
            robot["hardware_id"] = "fixture"
            robot["source"]["robot_name"] = "fixture"
            source["metadata"]["revision"] = "r9"
            reseal()
            self.assertIn("discovery.revision", {item["code"] for item in verify_discovery(package)["errors"]})
            source["metadata"]["revision"] = "r1"
            reseal()
            self.assertTrue(verify_discovery(package)["passed"])

            payload["budget"] = None
            reseal()
            malformed = verify_discovery(package)
            self.assertIn("discovery.budgets", {item["code"] for item in malformed["errors"]})
            budget = {"expected_mass_kg": [1, 5], "expected_extent_m": [0, 1]}
            budget_bytes = json.dumps(budget).encode()
            (package / "input/records/budget.json").write_bytes(budget_bytes)
            payload["budget"] = {
                "package_file": "budget.json",
                "key": "",
                "sha256": hashlib.sha256(budget_bytes).hexdigest(),
            }
            robot["checks"] = budget
            reseal()
            self.assertTrue(verify_discovery(package)["passed"])
            (package / "input/records/budget.json").write_bytes(budget_bytes + b" ")
            # Re-sealing the manifest is not enough: the record bytes must still
            # match the entry hash the discovery record carries.
            support.seal_delivery(package)
            changed_budget = verify_discovery(package)
            self.assertIn("discovery.record", {item["code"] for item in changed_budget["errors"]})
            (package / "input/cad/robot.SLDASM").write_bytes(native + b"tampered")
            support.seal_delivery(package)
            changed = verify_discovery(package)
            self.assertFalse(changed["passed"])
            self.assertIn("discovery.files", {item["code"] for item in changed["errors"]})


if __name__ == "__main__":
    unittest.main()
