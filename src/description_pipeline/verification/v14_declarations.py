"""Independent, bounded field reader for the saved SW2URDF v1.4 DataContract.

No producer decoder, .NET deserialization, CAD dependency or saved physics enters
this module. ``NameAttribute`` resolves directly to an Attribute containing Value;
Joint/Origin/Limit resolve to typed URDFElement objects. SWComponentPIDs is one
array container. Absent/null fields stay None, empty strings stay empty and zeros
stay zero. Stored meshes and inertials are deliberately not measurement inputs.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import math
import xml.etree.ElementTree as ET

ATTRIBUTE_NAME = "URDF Export Configuration (v1.4)"
LEGACY_ATTRIBUTE_NAMES = ("URDF Export Configuration (v1.3)", "URDF Export Configuration")
CONTRACT_NAMESPACE = "http://schemas.datacontract.org/2004/07/SW2URDF"
SUPPORTED_EXPORTER_VERSIONS = (1.4,)
MAX_PAYLOAD_BYTES = 16 * 1024 * 1024
MAX_DEPTH = 64
MAX_ELEMENTS = 200_000
MAX_REF_HOPS = 16
MAX_LINKS = 10_000

_XSI_NIL = "{http://www.w3.org/2001/XMLSchema-instance}nil"
_XSI_TYPE = "{http://www.w3.org/2001/XMLSchema-instance}type"
_DC_ID = "{http://schemas.microsoft.com/2003/10/Serialization/}Id"
_DC_REF = "{http://schemas.microsoft.com/2003/10/Serialization/}Ref"
_ARRAYS = "http://schemas.microsoft.com/2003/10/Serialization/Arrays"


class DeclarationError(Exception):
    """A refused declaration, never a silently partial decoded result."""

    def __init__(self, code: str, message: str, detail: dict | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.detail = detail or {}


def _refuse(code: str, message: str, detail: dict | None = None) -> None:
    raise DeclarationError(code, message, detail)


def _tag(name: str) -> str:
    return f"{{{CONTRACT_NAMESPACE}}}{name}"


class _Resolver:
    def __init__(self) -> None:
        self.table: dict[str, ET.Element] = {}
        self.links = 0

    def register(self, element: ET.Element) -> None:
        identifier = element.get(_DC_ID)
        if identifier is None:
            return
        if not identifier or identifier in self.table:
            _refuse("v14.reference_duplicate", "a z:Id is empty or appears twice", {"id": identifier})
        self.table[identifier] = element

    def deref(self, element: ET.Element | None) -> ET.Element | None:
        if element is None:
            return None
        seen: set[str] = set()
        for _ in range(MAX_REF_HOPS + 1):
            reference = element.get(_DC_REF)
            if reference is None:
                return element
            if element.get(_DC_ID) is not None or len(element) or (element.text or "").strip():
                _refuse("v14.malformed_reference", "a z:Ref also carries an Id or content")
            if reference in seen:
                _refuse("v14.reference_cycle", "references form a cycle", {"ref": reference})
            seen.add(reference)
            if len(seen) > MAX_REF_HOPS:
                _refuse("v14.reference_too_deep", "the reference chain is too deep")
            target = self.table.get(reference)
            if target is None:
                _refuse("v14.unresolved_reference", "a z:Ref has no matching z:Id", {"ref": reference})
            element = target
        _refuse("v14.reference_too_deep", "the reference chain is too deep")


def _first(element: ET.Element, name: str, resolver: _Resolver) -> ET.Element | None:
    matches = list(element.findall(_tag(name)))
    if len(matches) > 1:
        _refuse("v14.duplicate_field", "a DataMember appears twice", {"field": name})
    return resolver.deref(matches[0]) if matches else None


def _is_nil(element: ET.Element) -> bool:
    return element.get(_XSI_NIL) in {"true", "1"}


def _typed(element: ET.Element | None, kind: str, field: str | None = None) -> ET.Element | None:
    if element is None or _is_nil(element):
        return None
    actual = (element.get(_XSI_TYPE) or "").rsplit(":", 1)[-1]
    if element.tag != _tag(field or kind) and not (element.tag == _tag("URDFElement") and actual == kind):
        _refuse("v14.reference_type", "a declaration field resolves to another object type", {"expected": kind})
    if actual and actual != kind:
        _refuse("v14.reference_type", "a declaration object has a conflicting type", {"expected": kind})
    return element


def _string_value(holder: ET.Element | None) -> str | None:
    if holder is None or _is_nil(holder):
        return None
    if len(holder):
        _refuse("v14.invalid_scalar", "a string contains child elements")
    return holder.text if holder.text is not None else ""


def _number_value(holder: ET.Element | None, *, label: str) -> float | None:
    if holder is None or _is_nil(holder):
        return None
    if len(holder):
        _refuse("v14.invalid_scalar", f"{label} contains child elements")
    text = (holder.text or "").strip()
    try:
        value = float(text)
    except ValueError:
        _refuse("v14.invalid_scalar", f"{label} is not a number", {"text": text})
    if not math.isfinite(value):
        _refuse("v14.invalid_scalar", f"{label} is not finite", {"text": text})
    return value


def _attribute_value(owner: ET.Element, field: str, resolver: _Resolver) -> ET.Element | None:
    holder = _first(owner, field, resolver)
    if holder is None or _is_nil(holder):
        return None
    # URDFAttribute is serialized directly under the member name or an Attribute
    # in Attributes. A referenced unrelated object must never become missing data.
    if holder.tag not in {_tag(field), _tag("Attribute")}:
        _refuse("v14.reference_type", "an attribute resolves to another object type", {"field": field})
    attribute_type = _first(holder, "AttributeType", resolver)
    value = _first(holder, "Value", resolver)
    if attribute_type is None or value is None:
        _refuse("v14.attribute_shape", "an Attribute lacks AttributeType or Value", {"field": field})
    expected = {"name", "link"} if field == "NameAttribute" else {field.removesuffix("Attribute").lower()}
    if _string_value(attribute_type) not in expected:
        _refuse("v14.attribute_shape", "an Attribute carries another field's value", {"field": field})
    return value


def _attribute_string(owner: ET.Element, field: str, resolver: _Resolver) -> str | None:
    return _string_value(_attribute_value(owner, field, resolver))


def _attribute_numbers(owner: ET.Element, field: str, resolver: _Resolver) -> float | list[float] | None:
    value = _attribute_value(owner, field, resolver)
    if value is None or _is_nil(value):
        return None
    if (value.get(_XSI_TYPE) or "").rsplit(":", 1)[-1] == "ArrayOfdouble" or len(value):
        if any(item.tag != f"{{{_ARRAYS}}}double" for item in value):
            _refuse("v14.invalid_scalar", "a numeric array contains a non-double item", {"field": field})
        return [_number_value(resolver.deref(item), label=f"{field}[]") for item in value]
    return _number_value(value, label=field)


def _direct_string(owner: ET.Element, field: str, resolver: _Resolver) -> str | None:
    return _string_value(_first(owner, field, resolver))


def _direct_bool(owner: ET.Element, field: str, resolver: _Resolver) -> bool | None:
    text = _direct_string(owner, field, resolver)
    if text is None:
        return None
    if text.strip() in {"true", "1"}:
        return True
    if text.strip() in {"false", "0"}:
        return False
    _refuse("v14.invalid_scalar", f"{field} is not a boolean", {"text": text})


def pid_hex(value: str) -> str:
    """Canonical native byte identity; no occurrence-name fallback exists."""
    try:
        if not isinstance(value, str):
            raise ValueError
        decoded = base64.b64decode("".join(value.split()), validate=True)
        if not decoded:
            raise ValueError
        return decoded.hex()
    except (ValueError, binascii.Error):
        _refuse("v14.invalid_pid", "a persistent reference is not nonempty base64")


def _direct_base64(owner: ET.Element, field: str, resolver: _Resolver) -> str | None:
    value = _direct_string(owner, field, resolver)
    if value is not None:
        pid_hex(value)
    return value


def _direct_base64_list(owner: ET.Element, field: str, resolver: _Resolver) -> list[str | None] | None:
    holder = _first(owner, field, resolver)
    if holder is None or _is_nil(holder):
        return None
    if (holder.text or "").strip():
        _refuse("v14.pid_array", "SWComponentPIDs must contain base64Binary entries")
    values = []
    for item in holder:
        if item.tag != f"{{{_ARRAYS}}}base64Binary":
            _refuse("v14.pid_array", "SWComponentPIDs contains an unexpected item")
        value = _string_value(resolver.deref(item))
        if value is not None:
            pid_hex(value)
        values.append(value)
    return values


def _limit_record(element: ET.Element | None, resolver: _Resolver) -> dict | None:
    element = _typed(element, "Limit")
    if element is None:
        return None
    return {
        name: _attribute_numbers(element, name.title() + "Attribute", resolver)
        for name in ("lower", "upper", "effort", "velocity")
    }


def _origin_record(element: ET.Element | None, resolver: _Resolver) -> dict | None:
    element = _typed(element, "Origin")
    if element is None:
        return None
    return {
        "xyz": _attribute_numbers(element, "XYZAttribute", resolver),
        "rpy": _attribute_numbers(element, "RPYAttribute", resolver),
        "customized": _direct_bool(element, "isCustomized", resolver),
    }


def _joint_record(element: ET.Element | None, resolver: _Resolver) -> dict | None:
    element = _typed(element, "Joint")
    if element is None:
        return None
    parent = _typed(_first(element, "Parent", resolver), "ParentLink", "Parent")
    child = _typed(_first(element, "Child", resolver), "ChildLink", "Child")
    axis = _typed(_first(element, "Axis", resolver), "Axis")
    return {
        "name": _attribute_string(element, "NameAttribute", resolver),
        "type": _attribute_string(element, "TypeAttribute", resolver),
        "origin": _origin_record(_first(element, "Origin", resolver), resolver),
        "parent_link": _attribute_string(parent, "NameAttribute", resolver) if parent is not None else None,
        "child_link": _attribute_string(child, "NameAttribute", resolver) if child is not None else None,
        "axis": _attribute_numbers(axis, "XYZAttribute", resolver) if axis is not None else None,
        "limit": _limit_record(_first(element, "Limit", resolver), resolver),
        "coordinate_system_name": _direct_string(element, "CoordinateSystemName", resolver),
        "axis_name": _direct_string(element, "AxisName", resolver),
    }


def _link_record(element: ET.Element, resolver: _Resolver, ancestors: frozenset[int] = frozenset()) -> dict:
    _typed(element, "Link")
    if id(element) in ancestors:
        _refuse("v14.reference_cycle", "a Link cycles into its own subtree")
    if len(ancestors) >= MAX_DEPTH:
        _refuse("v14.payload_too_deep", "the expanded Link tree is nested too deeply")
    resolver.links += 1
    if resolver.links > MAX_LINKS:
        _refuse("v14.payload_too_large", "the expanded Link tree has too many links")
    ancestors = ancestors | {id(element)}
    children_holder = _first(element, "Children", resolver)
    children = None
    if children_holder is not None and not _is_nil(children_holder):
        children = []
        for item in children_holder:
            if item.tag != _tag("Link"):
                _refuse("v14.reference_type", "Children contains a non-Link object")
            child = resolver.deref(item)
            children.append(None if _is_nil(child) else _link_record(child, resolver, ancestors))
    return {
        "name": _attribute_string(element, "NameAttribute", resolver),
        "children": children,
        "joint": _joint_record(_first(element, "Joint", resolver), resolver),
        "main_component_pid": _direct_base64(element, "SWMainComponentPID", resolver),
        "component_pids": _direct_base64_list(element, "SWComponentPIDs", resolver),
        "is_fixed_frame": _direct_bool(element, "isFixedFrame", resolver),
        "is_incomplete": _direct_bool(element, "isIncomplete", resolver),
        "stl_quality_fine": _direct_bool(element, "STLQualityFine", resolver),
    }


def _guard_payload(xml_text: str) -> bytes:
    if not isinstance(xml_text, str) or not xml_text.strip():
        _refuse("v14.empty_payload", "the declaration payload is empty")
    if len(xml_text) > MAX_PAYLOAD_BYTES:
        _refuse("v14.payload_too_large", "the declaration payload exceeds the size cap")
    data = xml_text.encode("utf-8")
    if len(data) > MAX_PAYLOAD_BYTES:
        _refuse("v14.payload_too_large", "the declaration payload exceeds the size cap")
    lowered = xml_text.lower()
    if "<!doctype" in lowered or "<!entity" in lowered:
        _refuse("v14.unsafe_xml", "DOCTYPE and entity declarations are refused")
    return data


def parse_v14_xml(xml_text: str) -> dict:
    """Read the exact v1.4 DataContract shape using only the Python standard library."""
    data = _guard_payload(xml_text)
    try:
        root = ET.fromstring(data)
    except ET.ParseError as error:
        _refuse("v14.malformed_xml", f"the declaration payload is not well-formed XML: {error}")
    if root.tag != _tag("Link"):
        _refuse("v14.root_name", "the declaration root is not a v1.4 Link")
    resolver = _Resolver()
    count = 0
    stack = [(root, 1)]
    while stack:
        element, depth = stack.pop()
        count += 1
        if count > MAX_ELEMENTS:
            _refuse("v14.payload_too_large", "the payload has too many elements")
        if depth > MAX_DEPTH:
            _refuse("v14.payload_too_deep", "the payload is nested too deeply")
        resolver.register(element)
        stack.extend((child, depth + 1) for child in element)
    return _link_record(resolver.deref(root), resolver)


def validate_boundary(block: dict) -> dict:
    """Bind a raw v1.4 capture block to its payload, retaining the native PID rows."""
    if not isinstance(block, dict):
        _refuse("v14.boundary_missing", "no declaration boundary was provided")
    name = str(block.get("attribute_name") or "")
    if name in LEGACY_ATTRIBUTE_NAMES:
        _refuse("v14.legacy_attribute", "legacy URDF export attributes are unsupported", {"name": name})
    if name != ATTRIBUTE_NAME:
        _refuse("v14.attribute_name", "the declaration attribute name is not the v1.4 name", {"name": name})
    version = block.get("exporter_version")
    if version not in SUPPORTED_EXPORTER_VERSIONS:
        _refuse("v14.exporter_version", "the exporter version is not v1.4", {"exporter_version": version})
    raw = block.get("raw_xml")
    digest = hashlib.sha256(_guard_payload(raw)).hexdigest()
    stated = block.get("raw_xml_sha256")
    if stated is not None and (not isinstance(stated, str) or stated.lower() != digest):
        _refuse("v14.raw_hash_mismatch", "the raw payload hash differs from the boundary hash")
    return {
        "attribute_name": name,
        "exporter_version": version,
        "raw_xml_sha256": digest,
        "document": block.get("document"),
        "configuration": block.get("configuration"),
        "pid_bindings": block.get("pid_bindings"),
        "root": parse_v14_xml(raw),
    }
