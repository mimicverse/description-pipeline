"""Minimal, safe, independent extraction of the SW2URDF v1.4 declaration payload.

The v1.4 payload is the DataContract XML of the SW2URDF ``Link`` tree
(``DataContractSerializer(typeof(Link))``, namespace
``http://schemas.datacontract.org/2004/07/SW2URDF``) stored in the SolidWorks attribute
``URDF Export Configuration (v1.4)``.  Upstream shapes are authoritative in the SW2URDF
source: ``Link``/``Joint``/``Axis``/``Origin``/``Limit``/``URDFAttribute`` DataMember
names, with ``URDFAttribute`` serialized as ``Attribute{AttributeType, IsRequired,
Value}`` and wrapped values carrying ``i:type`` annotations.

This module extracts only the declared identity, membership, topology and kinematics
facts the verifier needs.  Missing-vs-zero is preserved exactly as serialized: an absent
element is ``None``, ``i:nil`` is an explicit null (a null list item is preserved as
``None`` in place), and a present zero is ``0``.  Nothing is defaulted or invented (no
metadata, limits, drives, physics) and no type is deserialized.

DataContract references: the serializer emits ``z:Id`` on the first occurrence of a
shared object and ``z:Ref`` on later ones.  Reference-only elements are resolved through
a bounded, cycle-checked in-payload table; a reference that cannot be resolved, forms a
cycle, carries both ``z:Id`` and ``z:Ref``, or exceeds the hop bound is an explicit
refusal (``v14.unresolved_reference`` / ``v14.reference_cycle`` /
``v14.malformed_reference`` / ``v14.reference_too_deep``) -- a reference-only
declaration is never silently treated as an absent field or a default value.

Platform: Linux-safe -- the Python standard library only, no COM, no pywin32, and no new
Windows dependencies; the payload is plain text captured on the Windows side.

XML safety: the payload is size-capped, DOCTYPE/entity declarations are refused before
parsing, and the stdlib parser is used (no external entity resolution).
"""

from __future__ import annotations

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

_XSI_NIL = "{http://www.w3.org/2001/XMLSchema-instance}nil"
_DC_ID = "{http://schemas.microsoft.com/2003/10/Serialization/}Id"
_DC_REF = "{http://schemas.microsoft.com/2003/10/Serialization/}Ref"


class DeclarationError(Exception):
    """A refused v1.4 payload with the specific reason; never a partially parsed result."""

    def __init__(self, code: str, message: str, detail: dict | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.detail = detail or {}


def _refuse(code: str, message: str, detail: dict | None = None) -> None:
    raise DeclarationError(code, message, detail)


def _localname(tag: object) -> str:
    return str(tag).rsplit("}", 1)[-1]


class _Resolver:
    """The payload's own bounded z:Id -> element table (no external resolution)."""

    def __init__(self) -> None:
        self.table: dict[str, ET.Element] = {}

    def register(self, element: ET.Element) -> None:
        identifier = element.get(_DC_ID)
        if identifier is None:
            return
        if identifier in self.table:
            _refuse("v14.reference_duplicate", "a z:Id appears twice in the payload", {"id": identifier})
        self.table[identifier] = element

    def deref(self, element: ET.Element | None) -> ET.Element | None:
        if element is None:
            return None
        seen: set[str] = set()
        hops = 0
        while True:
            reference = element.get(_DC_REF)
            if reference is None:
                return element
            if element.get(_DC_ID) is not None:
                _refuse(
                    "v14.malformed_reference",
                    "an element carries both z:Id and z:Ref",
                    {"id": element.get(_DC_ID), "ref": reference},
                )
            if reference in seen:
                _refuse("v14.reference_cycle", "the payload's references form a cycle", {"id": reference})
            seen.add(reference)
            hops += 1
            if hops > MAX_REF_HOPS:
                _refuse("v14.reference_too_deep", "the payload's reference chain is too deep")
            target = self.table.get(reference)
            if target is None:
                _refuse("v14.unresolved_reference", "a z:Ref has no matching z:Id in the payload", {"ref": reference})
            element = target


def _first(element: ET.Element, name: str, resolver: _Resolver) -> ET.Element | None:
    for child in element:
        if _localname(child.tag) == name:
            return resolver.deref(child)
    return None


def _all(element: ET.Element, name: str, resolver: _Resolver) -> list[ET.Element | None]:
    out: list[ET.Element | None] = []
    for child in element:
        if _localname(child.tag) == name:
            out.append(resolver.deref(child))
    return out


def _is_nil(element: ET.Element) -> bool:
    return element.get(_XSI_NIL) in {"true", "1"}


def _string_value(holder: ET.Element | None) -> str | None:
    if holder is None or _is_nil(holder):
        return None
    return holder.text if holder.text is not None else ""


def _number_value(holder: ET.Element | None, *, label: str) -> float | None:
    if holder is None or _is_nil(holder):
        return None
    text = (holder.text or "").strip()
    if not text:
        _refuse("v14.invalid_scalar", f"{label} carries no numeric text")
    try:
        value = float(text)
    except ValueError:
        _refuse("v14.invalid_scalar", f"{label} is not a number", {"text": text})
    if not math.isfinite(value):
        _refuse("v14.invalid_scalar", f"{label} is not finite", {"text": text})
    return value


def _attribute_value(owner: ET.Element, field: str, resolver: _Resolver) -> ET.Element | None:
    """The ``Value`` element of a wrapped ``URDFAttribute`` field, or None when absent."""

    holder = _first(owner, field, resolver)
    if holder is None or _is_nil(holder):
        return None
    attribute = _first(holder, "Attribute", resolver)
    if attribute is None or _is_nil(attribute):
        return None
    return _first(attribute, "Value", resolver)


def _attribute_string(owner: ET.Element, field: str, resolver: _Resolver) -> str | None:
    return _string_value(_attribute_value(owner, field, resolver))


def _attribute_numbers(owner: ET.Element, field: str, resolver: _Resolver) -> float | list[float] | None:
    """A wrapped numeric value: arrays carry element children, scalars carry text."""

    value = _attribute_value(owner, field, resolver)
    if value is None or _is_nil(value):
        return None
    items = list(value)
    if items:
        numbers = []
        for item in items:
            numbers.append(_number_value(item, label=f"{field}[]"))
        return numbers
    return _number_value(value, label=field)


def _direct_string(owner: ET.Element, field: str, resolver: _Resolver) -> str | None:
    return _string_value(_first(owner, field, resolver))


def _direct_bool(owner: ET.Element, field: str, resolver: _Resolver) -> bool | None:
    holder = _first(owner, field, resolver)
    if holder is None or _is_nil(holder):
        return None
    text = (holder.text or "").strip().lower()
    if text == "true":
        return True
    if text == "false":
        return False
    _refuse("v14.invalid_scalar", f"{field} is not a boolean", {"text": holder.text})
    return None


def _direct_base64(owner: ET.Element, field: str, resolver: _Resolver) -> str | None:
    return _string_value(_first(owner, field, resolver))


def _direct_base64_list(owner: ET.Element, field: str, resolver: _Resolver) -> list[str | None] | None:
    """Individual base64 PID entries are preserved positionally; a null entry stays None."""

    holders = _all(owner, field, resolver)
    if not holders:
        return None
    return [_string_value(holder) for holder in holders]


def _limit_record(element: ET.Element | None, resolver: _Resolver) -> dict | None:
    if element is None or _is_nil(element):
        return None
    return {
        "lower": _attribute_numbers(element, "LowerAttribute", resolver),
        "upper": _attribute_numbers(element, "UpperAttribute", resolver),
        "effort": _attribute_numbers(element, "EffortAttribute", resolver),
        "velocity": _attribute_numbers(element, "VelocityAttribute", resolver),
    }


def _origin_record(element: ET.Element | None, resolver: _Resolver) -> dict | None:
    if element is None or _is_nil(element):
        return None
    return {
        "xyz": _attribute_numbers(element, "XYZAttribute", resolver),
        "rpy": _attribute_numbers(element, "RPYAttribute", resolver),
    }


def _joint_record(element: ET.Element | None, resolver: _Resolver) -> dict | None:
    if element is None or _is_nil(element):
        return None
    parent = _first(element, "Parent", resolver)
    child = _first(element, "Child", resolver)
    axis = _first(element, "Axis", resolver)
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


def _link_record(
    element: ET.Element, resolver: _Resolver, ancestors: frozenset[str] = frozenset()
) -> dict:
    identifier = element.get(_DC_ID)
    if identifier is not None:
        if identifier in ancestors:
            _refuse("v14.reference_cycle", "a link reference cycles into its own subtree", {"id": identifier})
        ancestors = ancestors | {identifier}
    children_holder = _first(element, "Children", resolver)
    if children_holder is None or _is_nil(children_holder):
        children = None
    else:
        children = []
        for item in _all(children_holder, "Link", resolver):
            if item is None:
                children.append(None)
                continue
            children.append(_link_record(item, resolver, ancestors))
    joint_holder = _first(element, "Joint", resolver)
    return {
        "name": _attribute_string(element, "NameAttribute", resolver),
        "children": children,
        "joint": _joint_record(joint_holder, resolver) if joint_holder is not None else None,
        "main_component_pid": _direct_base64(element, "SWMainComponentPID", resolver),
        "component_pids": _direct_base64_list(element, "SWComponentPIDs", resolver),
        "is_fixed_frame": _direct_bool(element, "isFixedFrame", resolver),
        "is_incomplete": _direct_bool(element, "isIncomplete", resolver),
        "stl_quality_fine": _direct_bool(element, "STLQualityFine", resolver),
    }


def _guard_payload(xml_text: str) -> bytes:
    if not isinstance(xml_text, str) or not xml_text.strip():
        _refuse("v14.empty_payload", "the declaration payload is empty")
    lowered = xml_text.lower()
    if "<!doctype" in lowered:
        _refuse("v14.unsafe_xml", "DOCTYPE declarations are refused")
    if "<!entity" in lowered:
        _refuse("v14.unsafe_xml", "entity declarations are refused")
    data = xml_text.encode("utf-8")
    if len(data) > MAX_PAYLOAD_BYTES:
        _refuse("v14.payload_too_large", "the declaration payload exceeds the size cap", {"bytes": len(data)})
    return data


def _guard_tree(root: ET.Element, resolver: _Resolver) -> None:
    count = 0
    stack = [(root, 1)]
    while stack:
        element, depth = stack.pop()
        count += 1
        if count > MAX_ELEMENTS:
            _refuse("v14.payload_too_large", "the declaration payload has too many elements")
        if depth > MAX_DEPTH:
            _refuse("v14.payload_too_deep", "the declaration payload is nested too deeply")
        resolver.register(element)
        for child in element:
            stack.append((child, depth + 1))


def parse_v14_xml(xml_text: str) -> dict:
    """Parse the declared Link tree from the raw v1.4 DataContract XML string."""

    data = _guard_payload(xml_text)
    try:
        root = ET.fromstring(data)
    except ET.ParseError as error:
        _refuse("v14.malformed_xml", f"the declaration payload is not well-formed XML: {error}")
    if _localname(root.tag) != "Link":
        _refuse("v14.root_name", "the declaration root is not a Link", {"root": _localname(root.tag)})
    resolver = _Resolver()
    _guard_tree(root, resolver)
    root = resolver.deref(root)
    if root is None:
        _refuse("v14.unresolved_reference", "the declaration root is an unresolvable reference")
    return _link_record(root, resolver)


def validate_boundary(block: dict) -> dict:
    """Validate the raw record boundary and extract the declaration.

    Expected boundary fields (B's side): ``attribute_name``, ``exporter_version``,
    ``raw_xml`` (string), optional ``raw_xml_sha256``, plus the observed ``document``
    and ``configuration`` and the per-PID ``pid_bindings`` carried through verbatim.
    Legacy attribute names are refused as unsupported (v1.4 only).
    """

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
    digest = hashlib.sha256(
        (raw.encode("utf-8") if isinstance(raw, str) else b"")
    ).hexdigest()
    stated = block.get("raw_xml_sha256")
    if isinstance(stated, str) and stated and stated.lower() != digest:
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
