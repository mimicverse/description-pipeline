"""Focused tests for the minimal independent v1.4 declaration decoder.

The XML fixtures follow the authoritative SW2URDF DataContract shapes
(``DataContractSerializer(typeof(Link))``): wrapped ``URDFAttribute`` values carry
``AttributeType``/``IsRequired``/``Value`` with ``i:type`` annotations, arrays use the
Serialization ``ArrayOfdouble`` shape, references carry ``z:Id``/``z:Ref``, and strings
use ``i:nil`` for null.  Fixtures are synthetic and generic; no M3 names, counts or
hashes appear anywhere here (synthetic tests are never generation input).
"""

from __future__ import annotations

import hashlib
import unittest
from unittest import mock

from description_pipeline.verification import v14_declarations as v14

NS = 'xmlns="http://schemas.datacontract.org/2004/07/SW2URDF"'
XI = 'xmlns:i="http://www.w3.org/2001/XMLSchema-instance"'
ARR = 'xmlns:a="http://schemas.microsoft.com/2003/10/Serialization/Arrays"'
XS = 'xmlns:b="http://www.w3.org/2001/XMLSchema"'
Z = 'xmlns:z="http://schemas.microsoft.com/2003/10/Serialization/"'


def attr_string(field: str, atype: str, value: str | None) -> str:
    value_xml = '<Value i:nil="true"/>' if value is None else f'<Value i:type="b:string">{value}</Value>'
    return f"<{field}><Attribute {NS}><AttributeType>{atype}</AttributeType><IsRequired>true</IsRequired>{value_xml}</Attribute></{field}>"


def attr_numbers(field: str, atype: str, values: list[float] | None) -> str:
    if values is None:
        value_xml = '<Value i:nil="true"/>'
    else:
        items = "".join(f"<a:double>{value!r}</a:double>" for value in values)
        value_xml = f'<Value i:type="a:ArrayOfdouble">{items}</Value>'
    return f"<{field}><Attribute {NS}><AttributeType>{atype}</AttributeType><IsRequired>true</IsRequired>{value_xml}</Attribute></{field}>"


def attr_scalar(field: str, atype: str, value: float | None, *, nil: bool = False) -> str:
    if nil or value is None:
        value_xml = '<Value i:nil="true"/>'
    else:
        value_xml = f'<Value i:type="b:double">{value!r}</Value>'
    return f"<{field}><Attribute {NS}><AttributeType>{atype}</AttributeType><IsRequired>true</IsRequired>{value_xml}</Attribute></{field}>"


def joint_xml(*, nil_velocity: bool = True) -> str:
    limit = (
        "<Limit>"
        + attr_scalar("LowerAttribute", "lower", -1.5)
        + attr_scalar("UpperAttribute", "upper", 1.5)
        + attr_scalar("EffortAttribute", "effort", 0.0)
        + attr_scalar("VelocityAttribute", "velocity", 3.0, nil=nil_velocity)
        + "</Limit>"
    )
    return (
        "<Joint>"
        + attr_string("NameAttribute", "name", "joint-1")
        + attr_string("TypeAttribute", "type", "revolute")
        + "<Origin>"
        + attr_numbers("XYZAttribute", "xyz", [0.0, 0.0, 0.2])
        + attr_numbers("RPYAttribute", "rpy", [0.0, 0.0, 1.5707963267948966])
        + "</Origin>"
        + "<Parent>" + attr_string("NameAttribute", "link", "base-1") + "</Parent>"
        + "<Child>" + attr_string("NameAttribute", "link", "tool-1") + "</Child>"
        + "<Axis>" + attr_numbers("XYZAttribute", "xyz", [0.0, 1.0, 0.0]) + "</Axis>"
        + limit
        + "<CoordinateSystemName>axis-frame</CoordinateSystemName>"
        + "<AxisName>RefAxis-A</AxisName>"
        + "</Joint>"
    )


def document_xml(*, with_namespace: bool = True, extra_elements: str = "") -> str:
    prefixes = f'{NS} {XI} {ARR} {XS} {Z}' if with_namespace else f'{XI} {ARR} {XS} {Z}'
    header = f'{prefixes} z:Id="i1"'
    child = (
        '<Link z:Id="i2">'
        '<Parent z:Ref="i1"/>'
        "<Children />"
        + attr_string("NameAttribute", "name", "tool-1")
        + "<SWComponentPIDs>QUJD</SWComponentPIDs>"
        + "<SWComponentPIDs>REVG</SWComponentPIDs>"
        + joint_xml()
        + "<isFixedFrame>false</isFixedFrame>"
        + "<isIncomplete>false</isIncomplete>"
        + "<STLQualityFine>true</STLQualityFine>"
        + "</Link>"
    )
    return (
        f"<Link {header}>"
        '<Parent i:nil="true"/>'
        f"<Children>{child}</Children>"
        + extra_elements
        + attr_string("NameAttribute", "name", "base-1")
        + "<SWMainComponentPID>QUJDRA==</SWMainComponentPID>"
        + "<isFixedFrame>true</isFixedFrame>"
        + "</Link>"
    )


class V14DeclarationDecoderTests(unittest.TestCase):
    def test_extracts_declared_tree_with_exact_missing_vs_zero(self):
        root = v14.parse_v14_xml(document_xml())
        self.assertEqual(root["name"], "base-1")
        self.assertTrue(root["is_fixed_frame"])
        self.assertIsNone(root["is_incomplete"])
        self.assertIsNone(root["stl_quality_fine"])
        self.assertEqual(root["main_component_pid"], "QUJDRA==")
        self.assertIsNone(root["component_pids"])
        self.assertIsNone(root["joint"])
        self.assertEqual(len(root["children"]), 1)
        child = root["children"][0]
        self.assertEqual(child["name"], "tool-1")
        self.assertFalse(child["is_fixed_frame"])
        self.assertFalse(child["is_incomplete"])
        self.assertTrue(child["stl_quality_fine"])
        self.assertEqual(child["children"], [])
        self.assertEqual(child["component_pids"], ["QUJD", "REVG"])
        joint = child["joint"]
        self.assertEqual(joint["name"], "joint-1")
        self.assertEqual(joint["type"], "revolute")
        self.assertEqual(joint["parent_link"], "base-1")
        self.assertEqual(joint["child_link"], "tool-1")
        self.assertEqual(joint["origin"]["xyz"], [0.0, 0.0, 0.2])
        self.assertAlmostEqual(joint["origin"]["rpy"][2], 1.5707963267948966, places=12)
        self.assertEqual(joint["axis"], [0.0, 1.0, 0.0])
        self.assertEqual(joint["limit"]["lower"], -1.5)
        self.assertEqual(joint["limit"]["effort"], 0.0)  # a present zero stays zero
        self.assertIsNone(joint["limit"]["velocity"])  # i:nil is missing, not zero
        self.assertEqual(joint["coordinate_system_name"], "axis-frame")
        self.assertEqual(joint["axis_name"], "RefAxis-A")

    def test_missing_fields_are_none_and_never_defaulted(self):
        minimal = (
            f"<Link {NS} {XI}>{attr_string('NameAttribute', 'name', 'only-1')}</Link>"
        )
        root = v14.parse_v14_xml(minimal)
        self.assertEqual(
            sorted(root),
            [
                "children",
                "component_pids",
                "is_fixed_frame",
                "is_incomplete",
                "joint",
                "main_component_pid",
                "name",
                "stl_quality_fine",
            ],
        )
        self.assertIsNone(root["children"])
        self.assertIsNone(root["joint"])
        self.assertIsNone(root["is_fixed_frame"])
        joint = (
            "<Joint>"
            + attr_string("NameAttribute", "name", "j2")
            + attr_string("TypeAttribute", "type", "fixed")
            + "</Joint>"
        )
        with_joint = f"<Link {NS} {XI}>{attr_string('NameAttribute', 'name', 'l2')}{joint}</Link>"
        parsed_joint = v14.parse_v14_xml(with_joint)["joint"]
        self.assertIsNone(parsed_joint["origin"])
        self.assertIsNone(parsed_joint["axis"])
        self.assertIsNone(parsed_joint["limit"])
        self.assertIsNone(parsed_joint["coordinate_system_name"])
        self.assertIsNone(parsed_joint["axis_name"])
        self.assertIsNone(parsed_joint["parent_link"])
        self.assertIsNone(parsed_joint["child_link"])

    def test_boundary_validation_and_hash(self):
        raw = document_xml()
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        result = v14.validate_boundary(
            {
                "attribute_name": v14.ATTRIBUTE_NAME,
                "exporter_version": 1.4,
                "raw_xml": raw,
                "raw_xml_sha256": digest.upper(),
                "document": "robot.SLDASM",
                "configuration": "Default",
                "pid_bindings": {"QUJD": "part-1"},
            }
        )
        self.assertEqual(result["raw_xml_sha256"], digest)
        self.assertEqual(result["root"]["name"], "base-1")
        self.assertEqual(result["document"], "robot.SLDASM")
        self.assertEqual(result["pid_bindings"], {"QUJD": "part-1"})

        def code(block) -> str:
            with self.assertRaises(v14.DeclarationError) as caught:
                v14.validate_boundary(block)
            return caught.exception.code

        base = {"attribute_name": v14.ATTRIBUTE_NAME, "exporter_version": 1.4, "raw_xml": raw}
        self.assertEqual(code({**base, "attribute_name": "URDF Export Configuration (v1.3)"}), "v14.legacy_attribute")
        self.assertEqual(code({**base, "attribute_name": "URDF Export Configuration"}), "v14.legacy_attribute")
        self.assertEqual(code({**base, "attribute_name": "Something Else"}), "v14.attribute_name")
        self.assertEqual(code({**base, "exporter_version": 1.3}), "v14.exporter_version")
        self.assertEqual(code({**base, "exporter_version": 1.5}), "v14.exporter_version")
        self.assertEqual(code({**base, "raw_xml_sha256": "0" * 64}), "v14.raw_hash_mismatch")
        self.assertEqual(code({**base, "raw_xml": "   "}), "v14.empty_payload")
        self.assertEqual(code(None), "v14.boundary_missing")

    def test_xml_safety_guards(self):
        def code(xml_text: str) -> str:
            with self.assertRaises(v14.DeclarationError) as caught:
                v14.parse_v14_xml(xml_text)
            return caught.exception.code

        self.assertEqual(code('<?xml version="1.0"?><!DOCTYPE Link><Link></Link>'), "v14.unsafe_xml")
        self.assertEqual(code("<!ENTITY x \"y\"><Link/>"), "v14.unsafe_xml")
        self.assertEqual(code("<Link><Children"), "v14.malformed_xml")
        self.assertEqual(code("<Robot/>"), "v14.root_name")
        with mock.patch.object(v14, "MAX_PAYLOAD_BYTES", 32):
            self.assertEqual(code(document_xml()), "v14.payload_too_large")
        deep = "<Link>" + "<Extra>" * 8 + "</Extra>" * 8 + "</Link>"
        with mock.patch.object(v14, "MAX_DEPTH", 5):
            self.assertEqual(code(deep), "v14.payload_too_deep")
        with mock.patch.object(v14, "MAX_ELEMENTS", 3):
            self.assertEqual(code(deep), "v14.payload_too_large")

    def test_namespace_tolerance_and_unknown_elements_ignored(self):
        plain = document_xml(with_namespace=False)
        self.assertEqual(v14.parse_v14_xml(plain)["children"][0]["name"], "tool-1")
        extras = (
            "<Inertial><Mass><Value>12.5</Value></Mass></Inertial>"
            "<Visual><Geometry><Mesh filename=\"old.stl\"/></Geometry></Visual>"
            "<Whatever attr=\"1\"/>"
            "<SWComponentPIDs i:nil=\"true\"/>"
        )
        with_extras = v14.parse_v14_xml(document_xml(extra_elements=extras))
        self.assertNotIn("inertial", with_extras)
        self.assertNotIn("visual", with_extras)
        # The link-level nil is the child's own SWComponentPIDs, which keeps its values.
        self.assertEqual(with_extras["children"][0]["component_pids"], ["QUJD", "REVG"])

    def test_reference_annotations_are_ignored(self):
        doc = document_xml()
        self.assertIn('z:Ref="i1"', doc)
        root = v14.parse_v14_xml(doc)
        self.assertEqual(root["children"][0]["name"], "tool-1")  # z:Ref/z:Id never followed
        nil_children = (
            f"<Link {NS} {XI}><Children i:nil=\"true\"/>{attr_string('NameAttribute', 'name', 'l3')}</Link>"
        )
        self.assertIsNone(v14.parse_v14_xml(nil_children)["children"])


if __name__ == "__main__":
    unittest.main()
