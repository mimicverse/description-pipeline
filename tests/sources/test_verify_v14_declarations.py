"""Independent oracle fixtures use the native DataContract object/reference shape.

The generic two-link records are tests only, never CAD generation inputs.
"""

from __future__ import annotations

import hashlib
import unittest
from unittest import mock
from xml.sax.saxutils import escape

from description_pipeline.verification import v14_declarations as v14

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
