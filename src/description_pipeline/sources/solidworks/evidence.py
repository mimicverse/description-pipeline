"""Environment and capture conditions recorded next to every snapshot.

A model built from a snapshot is only reproducible together with the conditions
it was captured under: which SolidWorks build, which worker, which configuration
and which capture options.  They are written as evidence, not inferred later.
"""

from __future__ import annotations

import ntpath
import platform
import sys
import time
from pathlib import Path
from typing import Any

EVIDENCE_SCHEMA = "solidworks-to-urdf.evidence/v1"
METADATA_WARNING_CODE = "metadata.non_native"
METADATA_WARNING_MESSAGE = (
    "Platform metadata includes values that are not direct CAD engineering readings; "
    "each field is bound to recorded evidence."
)
RUNTIME_EVIDENCE = "evidence/native-runtime.json"

_RULE_TEXT = {
    "metadata.platform_execution": (
        "native process and runtime execution observation",
        "recorded from host, Python, or runtime APIs rather than a CAD engineering getter",
    ),
    "metadata.python_version_split": (
        "Python version string normalization",
        "the delivered value is the first whitespace-delimited token of sys.version",
    ),
    "metadata.tool_identity": (
        "installed tool and release identity",
        "computed from installed package files, release constants, or their recorded digests",
    ),
    "metadata.process_observation": (
        "owned native process and session observation",
        "PID, process, lease, role, and lifecycle fields are controlled platform metadata",
    ),
    "metadata.platform_constant": (
        "adapter platform constant",
        "the adapter declares this value instead of reading a CAD engineering property",
    ),
    "metadata.platform_diagnostic": (
        "adapter capability or failure diagnostic",
        "the value reports platform behavior or unavailable capability and is not a CAD value",
    ),
    "metadata.platform_unavailable": (
        "optional native getter failure placeholder",
        "the recorded null is a platform failure placeholder and must not be read as a native value",
    ),
    "metadata.source_configuration_echo": (
        "frozen source configuration echo",
        "the value echoes the selected source configuration even when it matches the native read",
    ),
    "metadata.save_flag_inversion": (
        "GetSaveFlag boolean inversion",
        "saved is derived from the same validated capture_state.save_flag observation",
    ),
}
_RULE_ORDER = tuple(_RULE_TEXT)


def capture_environment(backend: Any = None, worker_version: str = "unknown") -> dict[str, Any]:
    """Everything that is cheap to read and needed to explain a snapshot later."""
    from ...build import tool_identity

    environment: dict[str, Any] = {
        "captured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "platform": platform.platform(),
        "python": sys.version.split()[0],
        "worker_version": worker_version,
        "toolchain": tool_identity(),
    }
    if backend is not None:
        reader = getattr(backend, "environment", None)
        if callable(reader):
            environment["solidworks"] = reader()
    return environment


def metadata_value_warnings(
    runtime: dict[str, Any],
    *,
    expected_tool: dict[str, Any],
    runtime_sha256: str,
    expected_configuration: str,
) -> list[dict[str, Any]]:
    """Project one prepared runtime's metadata while preserving native diagnostics."""

    from ...delivery import PIPELINE_ID
    from ...io import PipelineError
    from ...schemas import TOOL_SCHEMA

    def fail(message: str, detail: object | None = None) -> None:
        error = PipelineError(message)
        if detail is not None:
            error.details = detail
        raise error

    def text(value: Any, label: str) -> str:
        if not isinstance(value, str) or not value:
            fail(f"{label} metadata must be a nonempty string")
        return value

    def digest(value: Any, label: str) -> str:
        value = text(value, label)
        if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
            fail(f"{label} metadata digest must be lowercase SHA-256")
        return value

    def same_path(left: Any, right: Any) -> bool:
        return bool(
            isinstance(left, str)
            and isinstance(right, str)
            and ntpath.normcase(ntpath.normpath(left)) == ntpath.normcase(ntpath.normpath(right))
        )

    if not isinstance(runtime, dict):
        fail("native runtime metadata must be an object")
    fields_by_rule: dict[str, list[str]] = {}

    def add(rule: str, *fields: str) -> None:
        if rule not in _RULE_TEXT:
            raise AssertionError(f"unknown metadata warning rule: {rule}")
        fields_by_rule.setdefault(rule, []).extend(fields)

    required_runtime = {
        "captured_at",
        "platform",
        "python",
        "worker_version",
        "toolchain",
        "solidworks",
        "prepared_capture",
    }
    missing = sorted(required_runtime - set(runtime))
    if missing:
        fail("native runtime metadata keys are incomplete", {"missing": missing})
    for key in ("captured_at", "platform", "python", "worker_version"):
        text(runtime[key], f"{RUNTIME_EVIDENCE} {key}")
    add("metadata.platform_execution", "/captured_at", "/platform", "/worker_version")
    add("metadata.python_version_split", "/python")
    if runtime["worker_version"] != text(expected_tool.get("version"), "expected tool version"):
        fail("native runtime worker_version differs from the delivery native tool")

    toolchain = runtime["toolchain"]
    if not isinstance(toolchain, dict):
        fail("native runtime toolchain metadata must be an object")
    required_toolchain = {"schema_version", "pipeline_id", "version", "source_sha256", "release", "runtime"}
    if set(toolchain) != required_toolchain:
        fail("native runtime toolchain metadata keys are incomplete or unknown")
    if toolchain["schema_version"] != TOOL_SCHEMA or toolchain["pipeline_id"] != PIPELINE_ID:
        fail("native runtime toolchain identity is unsupported")
    if toolchain["version"] != text(expected_tool.get("version"), "expected tool version"):
        fail("native runtime toolchain version differs from the delivery tool")
    if digest(toolchain["source_sha256"], "native runtime source_sha256") != digest(
        expected_tool.get("source_sha256"), "expected tool source_sha256"
    ):
        fail("native runtime toolchain digest differs from the delivery tool")
    if toolchain["release"] != expected_tool.get("release"):
        fail("native runtime toolchain release differs from the delivery tool")
    add(
        "metadata.tool_identity",
        *(
            f"/toolchain/{_pointer_token(key)}"
            for key in ("schema_version", "pipeline_id", "version", "source_sha256", "release")
        ),
    )
    runtime_identity = toolchain["runtime"]
    expected_runtime = expected_tool.get("runtime")
    if not isinstance(runtime_identity, dict) or runtime_identity != expected_runtime:
        fail("native runtime toolchain runtime differs from the delivery native runtime")
    for key in ("role", "python", "system", "machine", "packages"):
        if key not in runtime_identity:
            fail(f"native runtime toolchain.runtime.{key} is missing")
    add(
        "metadata.platform_execution",
        *(f"/toolchain/runtime/{_pointer_token(key)}" for key in ("role", "python", "system", "machine", "packages")),
    )

    solidworks = runtime["solidworks"]
    if not isinstance(solidworks, dict):
        fail("native runtime solidworks metadata must be an object")
    for key in ("revision", "input_mode", "sessions"):
        if key not in solidworks:
            fail(f"native runtime solidworks.{key} is missing")
    text(solidworks["revision"], "native runtime solidworks.revision")
    sessions = solidworks["sessions"]
    if not isinstance(sessions, dict) or set(sessions) != {"copy"}:
        fail("native runtime solidworks.sessions must contain only the prepared copy session")
    copy_session = sessions["copy"]
    if (
        not isinstance(copy_session, dict)
        or type(copy_session.get("pid")) is not int
        or copy_session["pid"] <= 0
        or copy_session.get("state") != "active"
    ):
        fail("native runtime copy session is not one active owned process")
    add("metadata.process_observation", "/solidworks/sessions/copy")
    for key in set(solidworks) - {"revision", "input_mode", "sessions"}:
        add("metadata.platform_diagnostic", f"/solidworks/{_pointer_token(key)}")

    prepared = runtime["prepared_capture"]
    if not isinstance(prepared, dict):
        fail("native runtime prepared_capture metadata must be an object")
    policy = prepared.get("policy")
    opened = prepared.get("opened")
    state = prepared.get("state_before")
    if not isinstance(policy, dict) or not isinstance(opened, dict) or not isinstance(state, dict):
        fail("prepared_capture policy/opened/state_before metadata is missing")
    for key in ("source_root", "assembly", "requested_configuration"):
        text(policy.get(key), f"prepared_capture.policy.{key}")
    assembly = policy["assembly"]
    if policy["requested_configuration"] != expected_configuration:
        fail("prepared_capture.policy requested configuration differs from source")
    if "search_root" in policy and not same_path(policy["search_root"], policy["source_root"]):
        fail("prepared_capture.policy search root differs from its source root")
    add(
        "metadata.source_configuration_echo",
        "/prepared_capture/policy/requested_configuration",
    )
    for key in set(policy) - {"source_root", "assembly", "requested_configuration", "search_root"}:
        add("metadata.platform_diagnostic", f"/prepared_capture/policy/{_pointer_token(key)}")

    for key in ("path", "read_only"):
        if key not in opened:
            fail(f"prepared_capture.opened.{key} is missing")
    text(opened["path"], "prepared_capture.opened.path")
    if opened["read_only"] is not True:
        fail("prepared_capture.opened is not read-only")
    if not same_path(opened["path"], assembly):
        fail("prepared_capture.opened path differs from its policy assembly")
    for key in set(opened) - {"path", "read_only"}:
        add("metadata.platform_diagnostic", f"/prepared_capture/opened/{_pointer_token(key)}")

    for key in ("path", "read_only", "active_configuration"):
        if key not in state:
            fail(f"prepared_capture.state_before.{key} is missing")
    text(state["path"], "prepared_capture.state_before.path")
    if state["read_only"] is not True:
        fail("prepared_capture.state_before is not read-only")
    if not same_path(state["path"], assembly):
        fail("prepared_capture.state_before path differs from its policy assembly")
    if state["active_configuration"] != expected_configuration:
        fail("prepared_capture.state_before configuration differs from source")
    if "configuration" in state and state["configuration"] != expected_configuration:
        fail("prepared_capture.state_before native configuration differs from source")
    save_flag = state.get("save_flag")
    saved = state.get("saved")
    if type(save_flag) is bool:
        if type(saved) is not bool or saved is save_flag:
            fail("prepared_capture.state_before saved differs from the native save-flag inversion")
        add("metadata.save_flag_inversion", "/prepared_capture/state_before/saved")
    else:
        if saved is not None:
            fail("prepared_capture.state_before invents saved state from an unknown save flag")
        add(
            "metadata.platform_diagnostic",
            "/prepared_capture/state_before/save_flag",
            "/prepared_capture/state_before/saved",
        )
    for key in set(state) - {"path", "read_only", "active_configuration", "configuration", "save_flag", "saved"}:
        add("metadata.platform_diagnostic", f"/prepared_capture/state_before/{_pointer_token(key)}")
    add("metadata.source_configuration_echo", "/prepared_capture/state_before/active_configuration")

    digest(runtime_sha256, RUNTIME_EVIDENCE)
    text(expected_configuration, "expected configuration")
    if not isinstance(expected_tool, dict):
        fail("expected native tool metadata must be an object")
    objects = []
    for rule in _RULE_ORDER:
        fields = sorted(set(fields_by_rule.get(rule, [])))
        if not fields:
            continue
        source, reason = _RULE_TEXT[rule]
        objects.append(
            {
                "object_type": "metadata",
                "object": RUNTIME_EVIDENCE,
                "fields": fields,
                "source": source,
                "rule_id": rule,
                "rule_version": "v1",
                "reason": reason,
                "file": RUNTIME_EVIDENCE,
                "sha256": runtime_sha256,
                "reference": RUNTIME_EVIDENCE,
            }
        )
    return [{"code": METADATA_WARNING_CODE, "message": METADATA_WARNING_MESSAGE, "objects": objects}]


def _pointer_token(value: str) -> str:
    return value.replace("~", "~0").replace("/", "~1")


def build_evidence(
    *,
    identity: dict[str, Any],
    environment: dict[str, Any],
    dependency_closure: dict[str, Any],
    capture: dict[str, Any],
    limits: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The evidence file that travels with a snapshot."""

    return {
        "schema_version": EVIDENCE_SCHEMA,
        "identity": identity,
        "environment": environment,
        "dependency_closure": dependency_closure,
        "capture": capture,
        "limits": limits or {},
    }


def write_json(path: Path, payload: object) -> None:
    from .jsonio import write_json as write

    write(path, payload)
