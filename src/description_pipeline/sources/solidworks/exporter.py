"""Portable decoder for the saved SW2URDF 1.4 DataContract declaration.

This is an authoring declaration, not a source of mass properties or meshes.
The XML's inertial/visual/collision elements are deliberately not interpreted.
No CAD API, file access, external entities, legacy format or defaults are used.
"""

from __future__ import annotations

import base64
import binascii
import io
import math
import re
import xml.etree.ElementTree as ET
from typing import Any


NAMESPACE = "http://schemas.datacontract.org/2004/07/SW2URDF"
SERIALIZATION = "http://schemas.microsoft.com/2003/10/Serialization/"
ARRAYS = "http://schemas.microsoft.com/2003/10/Serialization/Arrays"
XSI = "http://www.w3.org/2001/XMLSchema-instance"
XSD = "http://www.w3.org/2001/XMLSchema"
MAX_XML_BYTES = 8 * 1024 * 1024
MAX_ELEMENTS = 150_000
MAX_DEPTH = 256
_SNAKE = re.compile(r"^[a-z][a-z0-9_]*$")


class ExporterDeclarationError(ValueError):
    """An invalid or unsupported saved authoring declaration."""

    def __init__(self, code: str, message: str, detail: Any = None):
        super().__init__(message)
        self.code = code
        self.detail = detail


def _fail(message: str, detail: Any = None) -> None:
    raise ExporterDeclarationError("discovery.exporter_invalid", message, detail)


class _Contract:
    def __init__(self, xml: str):
        if not isinstance(xml, str) or not xml:
            _fail("the saved declaration has no XML text")
        try:
            byte_count = len(xml.encode("utf-8"))
        except UnicodeError:
            _fail("the saved declaration is not valid Unicode XML")
        if byte_count > MAX_XML_BYTES:
            _fail("the saved declaration exceeds the XML size limit")
        if re.search(r"<!\s*(?:DOCTYPE|ENTITY)\b", xml, re.IGNORECASE):
            _fail("DTD and entity declarations are prohibited")
        self.namespaces: dict[ET.Element, dict[str, str]] = {}
        self.ids: dict[str, ET.Element] = {}
        scopes: list[dict[str, str]] = [{}]
        pending: dict[str, str] = {}
        count = 0
        try:
            parser = ET.iterparse(io.StringIO(xml), events=("start", "end", "start-ns"))
            for event, value in parser:
                if event == "start-ns":
                    pending[value[0]] = value[1]
                elif event == "start":
                    count += 1
                    if count > MAX_ELEMENTS or len(scopes) > MAX_DEPTH:
                        _fail("the saved declaration exceeds the XML complexity limit")
                    scope = scopes[-1] | pending
                    pending = {}
                    scopes.append(scope)
                    self.namespaces[value] = scope
                    identity = value.get(f"{{{SERIALIZATION}}}Id")
                    if identity is not None:
                        if not identity or identity in self.ids:
                            _fail("DataContract object ids must be nonempty and unique", identity)
                        self.ids[identity] = value
                else:
                    scopes.pop()
            self.root = parser.root
        except (ET.ParseError, UnicodeError, ValueError) as exc:
            if isinstance(exc, ExporterDeclarationError):
                raise
            _fail("the saved declaration is not valid XML", str(exc))
        if self.root.tag != f"{{{NAMESPACE}}}Link":
            _fail("the saved declaration must be a SW2URDF 1.4 Link DataContract", self.root.tag)
        for element in self.root.iter():
            self.resolve(element)

    def resolve(self, element: ET.Element | None) -> ET.Element | None:
        if element is None:
            return None
        seen: set[str] = set()
        while (reference := element.get(f"{{{SERIALIZATION}}}Ref")) is not None:
            if reference in seen or reference not in self.ids:
                _fail("DataContract reference is cyclic or unresolved", reference)
            if (
                len(element)
                or (element.text or "").strip()
                or element.get(f"{{{SERIALIZATION}}}Id")
                or element.get(f"{{{XSI}}}nil") in ("true", "1")
            ):
                _fail("a DataContract reference also defines a value", reference)
            seen.add(reference)
            element = self.ids[reference]
        if element.get(f"{{{XSI}}}nil") in ("true", "1"):
            if len(element) or (element.text or "").strip():
                _fail("a nil DataContract member also defines a value")
            return None
        return element

    def member(self, parent: ET.Element | None, name: str, *, optional: bool = False) -> ET.Element | None:
        if parent is None:
            return None
        values = parent.findall(f"{{{NAMESPACE}}}{name}")
        if len(values) > 1 or (not values and not optional):
            _fail("DataContract member must occur exactly once", name)
        return self.resolve(values[0]) if values else None

    def typed(self, element: ET.Element | None, name: str) -> ET.Element | None:
        if element is None:
            return None
        kind = self.qname(element, element.get(f"{{{XSI}}}type"))
        if kind is not None and kind != (NAMESPACE, name):
            _fail("DataContract object has an unexpected type", {"expected": name, "actual": kind})
        if element.tag == f"{{{NAMESPACE}}}URDFElement" and kind is None:
            _fail("a polymorphic URDFElement has no type", name)
        aliases = {"ParentLink": "Parent", "ChildLink": "Child", "SafetyController": "Safety"}
        accepted = {f"{{{NAMESPACE}}}{tag}" for tag in (name, aliases.get(name, name), "URDFElement")}
        if element.tag not in accepted:
            _fail("DataContract object uses an unexpected element", {"expected": name, "actual": element.tag})
        return element

    def qname(self, element: ET.Element, value: str | None) -> tuple[str, str] | None:
        if value is None:
            return None
        prefix, separator, local = value.partition(":")
        if not separator:
            prefix, local = "", prefix
        namespace = self.namespaces[element].get(prefix)
        if namespace is None or not local:
            _fail("DataContract type uses an unbound namespace", value)
        return namespace, local

    def text(self, element: ET.Element | None) -> str | None:
        if element is None:
            return None
        if len(element):
            _fail("a scalar DataContract member contains child elements", element.tag)
        return element.text or ""

    def boolean(self, element: ET.Element | None) -> bool | None:
        value = self.text(element)
        if value is None:
            return None
        if value not in ("true", "false", "1", "0"):
            _fail("a DataContract boolean is invalid", value)
        return value in ("true", "1")

    def attribute(self, parent: ET.Element | None, field: str, attribute_type: str) -> Any:
        attribute = self.member(parent, field)
        if attribute is None:
            return None
        # NameAttribute/XYZAttribute z:Ref points DIRECTLY at an Attribute object.
        if self.text(self.member(attribute, "AttributeType")) != attribute_type:
            _fail("a referenced URDF attribute has the wrong semantic type", field)
        self.boolean(self.member(attribute, "IsRequired"))
        value = self.member(attribute, "Value")
        if value is None:
            return None
        kind = self.qname(value, value.get(f"{{{XSI}}}type"))
        if kind == (XSD, "string"):
            return self.text(value)
        if kind == (XSD, "double"):
            return self.number(self.text(value))
        if kind == (ARRAYS, "ArrayOfdouble"):
            if any(child.tag != f"{{{ARRAYS}}}double" for child in value):
                _fail("a double array contains another wire type", field)
            return [self.number(self.text(child)) for child in value]
        _fail("an engineering declaration attribute has an unsupported wire type", {"field": field, "type": kind})

    @staticmethod
    def number(value: str | None) -> float:
        try:
            number = float(value)
        except (TypeError, ValueError):
            _fail("an engineering declaration number is invalid", value)
        if not math.isfinite(number):
            _fail("an engineering declaration number is not finite", value)
        return number

    def vector(self, parent: ET.Element | None, field: str, name: str) -> list[float] | None:
        value = self.attribute(parent, field, name)
        if value is not None and (not isinstance(value, list) or len(value) != 3):
            _fail("an engineering declaration vector must contain three doubles", field)
        return value

    def pid(self, element: ET.Element | None) -> str | None:
        value = self.text(element)
        if value is None:
            return None
        try:
            decoded = base64.b64decode("".join(value.split()), validate=True)
        except (ValueError, binascii.Error):
            _fail("a component persistent id is not base64")
        if not decoded:
            _fail("a non-nil component persistent id is empty")
        return decoded.hex()

    def joint(self, element: ET.Element | None) -> dict | None:
        element = self.typed(element, "Joint")
        if element is None:
            return None
        origin = self.typed(self.member(element, "Origin"), "Origin")
        axis = self.typed(self.member(element, "Axis"), "Axis")
        limit = self.typed(self.member(element, "Limit"), "Limit")
        optional = {}
        for field, kind, attributes in (
            ("Calibration", "Calibration", {"Falling": "falling", "Rising": "rising"}),
            ("Dynamics", "Dynamics", {"Damping": "damping", "Friction": "friction"}),
            (
                "Safety",
                "SafetyController",
                {
                    "KPosition": "k_position",
                    "KVelocity": "k_velocity",
                    "SoftLower": "soft_lower_limit",
                    "SoftUpper": "soft_upper_limit",
                },
            ),
            ("Mimic", "Mimic", {"JointName": "joint", "Multiplier": "multiplier", "Offset": "offset"}),
        ):
            child = self.typed(self.member(element, field, optional=True), kind)
            if child is not None:
                # Preserve optional declaration names as actually serialized.
                # They are audited, not translated into supported joint fields.
                optional[field.lower()] = {}
                for name in attributes:
                    attribute_field = f"{name}Attribute"
                    attribute = self.member(child, attribute_field)
                    key = self.text(self.member(attribute, "AttributeType"))
                    if not key or key in optional[field.lower()]:
                        _fail("an optional joint attribute has no unique semantic type", attribute_field)
                    optional[field.lower()][key] = self.attribute(child, attribute_field, key)
        return {
            "name": self.attribute(element, "NameAttribute", "name"),
            "type": self.attribute(element, "TypeAttribute", "type"),
            "parent": self.attribute(self.typed(self.member(element, "Parent"), "ParentLink"), "NameAttribute", "link"),
            "child": self.attribute(self.typed(self.member(element, "Child"), "ChildLink"), "NameAttribute", "link"),
            "coordinate_system": self.text(self.member(element, "CoordinateSystemName")),
            "axis_name": self.text(self.member(element, "AxisName")),
            "axis": self.vector(axis, "XYZAttribute", "xyz"),
            "origin": None
            if origin is None
            else {
                "xyz": self.vector(origin, "XYZAttribute", "xyz"),
                "rpy": self.vector(origin, "RPYAttribute", "rpy"),
                "customized": self.boolean(self.member(origin, "isCustomized")),
            },
            "limits": None
            if limit is None
            else {
                key: self.attribute(limit, f"{key.title()}Attribute", key)
                for key in ("lower", "upper", "effort", "velocity")
            },
            "optional": optional,
        }


def decode_exporter_v14(raw_xml: str, exporter_version: float) -> dict:
    """Decode the exact v1.4 Link tree, keeping missing values distinct from zero.

    Membership PIDs are canonical hex bytes, not XML container positions. The
    returned root link retains its Joint metadata without inventing a root joint.
    Namespace prefixes, element ordering and DataContract reference ids may vary.
    """

    if type(exporter_version) not in (int, float) or exporter_version != 1.4:
        raise ExporterDeclarationError(
            "discovery.exporter_version_unsupported", "only the SW2URDF 1.4 DataContract is supported", exporter_version
        )
    contract = _Contract(raw_xml)
    links: list[dict] = []
    names: set[str] = set()
    joints: set[str] = set()
    visited: set[ET.Element] = set()
    pending = [(contract.root, None, None)]
    while pending:
        element, parent_element, parent_name = pending.pop()
        if element in visited:
            _fail("the declared link graph is cyclic or has several parents")
        visited.add(element)
        contract.typed(element, "Link")
        name = contract.attribute(element, "NameAttribute", "name")
        if not isinstance(name, str) or _SNAKE.fullmatch(name) is None or name in names:
            _fail("declared link names must be unique exact snake_case", name)
        names.add(name)
        if contract.member(element, "Parent") is not parent_element:
            _fail("the Link Parent reference disagrees with the Children tree", name)
        container = contract.member(element, "SWComponentPIDs")
        pids = []
        if container is not None:
            for child in container:
                if child.tag != f"{{{ARRAYS}}}base64Binary":
                    _fail("SWComponentPIDs must contain individual base64Binary arrays", child.tag)
                pids.append(contract.pid(contract.resolve(child)))
        if any(pid is None for pid in pids) or len(pids) != len(set(pids)):
            _fail("a link contains nil or duplicate selected component PIDs", name)
        joint = contract.joint(contract.member(element, "Joint"))
        if parent_name is not None:
            if joint is None or joint["parent"] != parent_name or joint["child"] != name:
                _fail("the declared joint endpoints disagree with the Link tree", name)
            joint_name = joint["name"]
            if not isinstance(joint_name, str) or _SNAKE.fullmatch(joint_name) is None or joint_name in joints:
                _fail("declared joint names must be unique exact snake_case", joint_name)
            joints.add(joint_name)
        elif joint is not None and any(joint[key] not in (None, "") for key in ("name", "type", "parent", "child")):
            _fail("the root Link cannot declare an incoming joint", name)
        children = contract.member(element, "Children")
        child_elements = [] if children is None else [contract.resolve(child) for child in children]
        if any(child is None for child in child_elements):
            _fail("a declared child link is nil", name)
        links.append(
            {
                "name": name,
                "parent": parent_name,
                "component_pids": pids,
                "main_component_pid": contract.pid(contract.member(element, "SWMainComponentPID")),
                "fixed_frame": contract.boolean(contract.member(element, "isFixedFrame")),
                "incomplete": contract.boolean(contract.member(element, "isIncomplete")),
                "joint": joint,
            }
        )
        pending.extend((child, element, name) for child in reversed(child_elements))
    return {"root": links[0]["name"], "links": links}
