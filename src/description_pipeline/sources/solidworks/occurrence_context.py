"""Compare the sealed native instance domain before engineering measurements."""

from collections.abc import Mapping

from .errors import CadError


def _index(rows):
    if not isinstance(rows, (list, tuple)):
        raise CadError("cad_occurrence_context_invalid", "native occurrence inventory is unreadable")
    result = {}
    for row in rows:
        name = row.get("name2") if isinstance(row, Mapping) else None
        if not isinstance(name, str) or not name or name in result or row.get("instance_id") != name:
            raise CadError("cad_occurrence_context_invalid", "native occurrence identities are incomplete or repeated")
        state = row.get("suppression_state")
        if type(state) is not int or state not in {0, 1, 2, 3, 4} or row.get("suppressed") is not (state == 0):
            raise CadError(
                "cad_occurrence_context_invalid", "native occurrence suppression is unbound", {"component": name}
            )
        parent = row.get("parent_occurrence")
        if not isinstance(parent, str):
            raise CadError("cad_occurrence_context_invalid", "native occurrence parent is unbound", {"component": name})
        if state != 0 and (
            row.get("document_type") not in {"part", "assembly"}
            or not isinstance(row.get("configuration"), str)
            or not row["configuration"]
        ):
            raise CadError(
                "cad_occurrence_context_invalid",
                "native occurrence type or configuration is unbound",
                {"component": name},
            )
        result[name] = row
    return result


def _source(row, native_files):
    provenance = row.get("source_provenance")
    if isinstance(provenance, Mapping):
        if provenance.get("state") == "cached_materialization":
            from .session_artifacts import session_source_identity

            try:
                return session_source_identity(row, native_files=native_files)
            except ValueError as error:
                raise CadError("cad_occurrence_context_invalid", "a cached source has no canonical identity") from error
        if provenance.get("state") == "embedded_virtual_component":
            from .discovery import sealed_embedded_virtual_document

            if not sealed_embedded_virtual_document(row, row["name2"], native_files):
                raise CadError("cad_occurrence_context_invalid", "a virtual source has no sealed owner identity")
        elif not (provenance.get("state") == "unread" and row.get("suppression_state") == 0):
            raise CadError("cad_occurrence_context_invalid", "a native source classification is unreadable")
        return {
            key: provenance.get(key)
            for key in (
                "state",
                "kind",
                "owner",
                "owner_sha256",
                "defining_anchor",
                "is_virtual",
            )
        }
    reference = row.get("native_reference") or {}
    return {
        "document": row.get("document"),
        **{key: reference.get(key) for key in ("is_virtual", "defining", "native_owner", "defining_anchor")},
    }


def compare_occurrence_context(expected, observed, *, native_files):
    """Reject the complete contextual delta without clipping either inventory."""
    sealed, current = _index(expected), _index(observed)
    changed = {}
    for name in sorted(sealed.keys() & current.keys()):
        fields = {}
        for field in ("parent_occurrence", "document_type", "suppression_state", "configuration"):
            if sealed[name].get(field) != current[name].get(field):
                fields[field] = {"expected": sealed[name].get(field), "observed": current[name].get(field)}
        old_source, new_source = _source(sealed[name], native_files), _source(current[name], native_files)
        if old_source != new_source:
            fields["source"] = {"expected": old_source, "observed": new_source}
        if fields:
            changed[name] = fields
    missing, extra = sorted(sealed.keys() - current.keys()), sorted(current.keys() - sealed.keys())
    if missing or extra or changed:
        raise CadError(
            "cad_occurrence_context_changed",
            "the native instance domain differs from sealed discovery",
            {"missing": missing, "extra": extra, "changed": changed},
        )


def validate_occurrence_context(rows):
    """Require current native identity, parent, state, configuration and type facts."""
    _index(rows)
