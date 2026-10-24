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
REWRITE_ENVIRONMENT_EVIDENCE = "evidence/evidence/rewrite-environment.json"

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
        "owned native process and retirement observation",
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
    "metadata.closure_binding": (
        "validated dependency closure binding",
        "the value is the closure top-level projection, not a fresh CAD path read",
    ),
    "metadata.native_projection": (
        "transparent projection of a native observation",
        "the delivered value is copied from capture_state.active_configuration",
    ),
    "metadata.save_flag_inversion": (
        "GetSaveFlag boolean inversion",
        "saved is derived from the same validated capture_state.save_flag observation",
    ),
    "metadata.operation_claim": (
        "native operation claim",
        "open_document declares read-only mode; this is not the IsOpenedReadOnly observation",
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
    rewrite_environment: dict[str, Any],
    *,
    expected_tool: dict[str, Any],
    runtime_sha256: str,
    rewrite_sha256: str,
    expected_configuration: str,
    closure_top_level: str,
) -> list[dict[str, Any]]:
    """Project non-native environment metadata into the existing value-warning shape."""

    from ...io import PipelineError

    def fail(message: str, detail: object | None = None) -> None:
        error = PipelineError(message)
        if detail is not None:
            error.details = detail
        raise error

    def require_record(value: Any, label: str, *, required: set[str], allowed: set[str]) -> dict[str, Any]:
        if not isinstance(value, dict):
            fail(f"{label} metadata must be an object")
        missing = sorted(required - set(value))
        unknown = sorted(set(value) - allowed)
        if missing or unknown:
            fail(f"{label} metadata keys are incomplete or unknown", {"missing": missing, "unknown": unknown})
        return value

    def require_text(value: Any, label: str) -> str:
        if not isinstance(value, str) or not value:
            fail(f"{label} metadata must be a nonempty string")
        return value

    def require_digest(value: Any, label: str) -> str:
        text = require_text(value, label)
        if len(text) != 64 or any(char not in "0123456789abcdef" for char in text):
            fail(f"{label} metadata digest must be lowercase SHA-256")
        return text

    def same_document(left: Any, right: Any) -> bool:
        return bool(
            isinstance(left, str)
            and isinstance(right, str)
            and ntpath.normcase(ntpath.normpath(left)) == ntpath.normcase(ntpath.normpath(right))
        )

    fields_by_file_rule: dict[tuple[str, str], list[str]] = {}

    def add(file: str, rule_id: str, *pointers: str) -> None:
        if rule_id not in _RULE_TEXT:
            raise AssertionError(f"unknown metadata warning rule: {rule_id}")
        fields_by_file_rule.setdefault((file, rule_id), []).extend(pointers)

    def classify_toolchain(record: dict[str, Any], file: str) -> None:
        from ...delivery import PIPELINE_ID
        from ...schemas import TOOL_SCHEMA

        toolchain = require_record(
            record["toolchain"],
            f"{file} toolchain",
            required={
                "schema_version",
                "pipeline_id",
                "version",
                "source_sha256",
                "release",
                "runtime",
            },
            allowed={
                "schema_version",
                "pipeline_id",
                "version",
                "source_sha256",
                "release",
                "runtime",
            },
        )
        if toolchain["schema_version"] != TOOL_SCHEMA:
            fail(f"{file} toolchain uses an unknown schema")
        if toolchain["pipeline_id"] != PIPELINE_ID:
            fail(f"{file} toolchain belongs to another pipeline")
        if toolchain["version"] != require_text(expected_tool.get("version"), "expected tool version"):
            fail(f"{file} toolchain version differs from the delivery tool")
        if require_digest(toolchain["source_sha256"], f"{file} toolchain.source_sha256") != require_digest(
            expected_tool.get("source_sha256"),
            "expected tool source_sha256",
        ):
            fail(f"{file} toolchain source digest differs from the delivery tool")
        if toolchain["release"] != expected_tool.get("release"):
            fail(f"{file} toolchain release differs from the delivery tool")
        add(
            file,
            "metadata.tool_identity",
            *(
                f"/toolchain/{key}"
                for key in ("schema_version", "pipeline_id", "version", "source_sha256", "release")
            ),
        )

        runtime = require_record(
            toolchain["runtime"],
            f"{file} toolchain.runtime",
            required={"role", "python", "system", "machine", "packages"},
            allowed={"role", "python", "system", "machine", "packages"},
        )
        for key in ("role", "python", "system", "machine"):
            require_text(runtime[key], f"{file} toolchain.runtime.{key}")
        expected_runtime = require_record(
            expected_tool.get("runtime"),
            "expected tool runtime",
            required={"role", "python", "system", "machine", "packages"},
            allowed={"role", "python", "system", "machine", "packages"},
        )
        if runtime["role"] != "native" or runtime["system"] != "Windows":
            fail(f"{file} toolchain is not the native Windows runtime")
        if runtime != expected_runtime:
            fail(f"{file} toolchain runtime differs from the delivery native runtime")
        packages = runtime["packages"]
        if not isinstance(packages, dict) or not all(
            isinstance(name, str) and name and isinstance(version, str) and version
            for name, version in packages.items()
        ):
            fail(f"{file} toolchain.runtime.packages metadata must map names to versions")
        add(
            file,
            "metadata.platform_execution",
            *(f"/toolchain/runtime/{key}" for key in ("role", "python", "system", "machine", "packages")),
        )

    def session_identity(value: Any, label: str, *, required_state: str) -> dict[str, Any]:
        identity = require_record(
            value,
            label,
            required={"pid", "state"},
            allowed={"pid", "state", "executable", "ownership", "lease"},
        )
        if type(identity["pid"]) is not int or identity["pid"] <= 0:
            fail(f"{label}.pid must be a positive integer")
        if identity["state"] != required_state:
            fail(f"{label}.state must be {required_state!r}")
        if "executable" in identity:
            require_text(identity["executable"], f"{label}.executable")
        if "ownership" in identity:
            require_text(identity["ownership"], f"{label}.ownership")
        lease = identity.get("lease")
        if lease is not None:
            lease = require_record(
                lease,
                f"{label}.lease",
                required={"held", "holder_pid", "path"},
                allowed={"held", "holder_pid", "path"},
            )
            if type(lease["held"]) is not bool:
                fail(f"{label}.lease.held must be a boolean")
            if lease["holder_pid"] is not None and (
                type(lease["holder_pid"]) is not int or lease["holder_pid"] <= 0
            ):
                fail(f"{label}.lease.holder_pid must be null or a positive integer")
            require_text(lease["path"], f"{label}.lease.path")
            if lease["held"] != (required_state == "active"):
                fail(f"{label}.lease state contradicts the process state")
        return identity

    def classify_solidworks(record: dict[str, Any], file: str) -> dict[str, dict[str, Any]]:
        facts = require_record(
            record["solidworks"],
            f"{file} solidworks",
            required={"revision", "input_mode", "sessions", "build", "license", "visible"},
            allowed={
                "revision",
                "input_mode",
                "sessions",
                "build",
                "license",
                "visible",
                "unavailable",
            },
        )
        require_text(facts["revision"], f"{file} solidworks.revision")
        if facts["input_mode"] != "saved_documents":
            fail(f"{file} solidworks.input_mode is not the supported platform declaration")
        add(file, "metadata.platform_constant", "/solidworks/input_mode")

        sessions = facts["sessions"]
        if not isinstance(sessions, dict) or set(sessions) != {"source", "copy"}:
            fail(f"{file} solidworks.sessions must name the source and copy sessions exactly")
        source_session = session_identity(
            sessions["source"],
            f"{file} solidworks.sessions.source",
            required_state="closed",
        )
        copy_session = session_identity(
            sessions["copy"],
            f"{file} solidworks.sessions.copy",
            required_state="active",
        )
        if source_session["pid"] == copy_session["pid"]:
            fail(f"{file} source and copy sessions reuse one pid")
        for role in ("source", "copy"):
            add(file, "metadata.process_observation", "/solidworks/sessions/" + _pointer_token(role))

        unavailable = facts.get("unavailable", {})
        if not isinstance(unavailable, dict):
            fail(f"{file} solidworks.unavailable metadata must be an object")
        unknown_unavailable = sorted(set(unavailable) - {"GetBuildNumbers", "GetCurrentLicenseType"})
        if unknown_unavailable:
            fail(f"{file} solidworks.unavailable metadata contains unknown members", unknown_unavailable)
        for member, reason in unavailable.items():
            require_text(reason, f"{file} solidworks.unavailable.{member}")
            add(file, "metadata.platform_diagnostic", "/solidworks/unavailable/" + _pointer_token(member))

        for key, member, expected_type, allow_bool in (
            ("build", "GetBuildNumbers", str, False),
            ("license", "GetCurrentLicenseType", int, False),
            ("visible", "Visible", bool, True),
        ):
            value = facts[key]
            if value is None:
                if key != "visible" and member not in unavailable:
                    fail(f"{file} solidworks.{key} failure has no bound unavailable diagnostic")
                add(file, "metadata.platform_unavailable", f"/solidworks/{key}")
                continue
            valid = type(value) is expected_type if allow_bool else (
                type(value) is expected_type and not isinstance(value, bool)
            )
            if key == "build" and isinstance(value, str) and not value:
                valid = False
            if not valid:
                fail(f"{file} solidworks.{key} does not have its documented native type")
            if member in unavailable:
                fail(f"{file} solidworks.{key} has both a value and an unavailable diagnostic")
        return {"source": source_session, "copy": copy_session}

    def classify_session_boundary(
        record: dict[str, Any],
        file: str,
        *,
        capture_sessions: dict[str, dict[str, Any]],
        rewrite_sessions: dict[str, dict[str, Any]],
    ) -> None:
        boundary = require_record(
            record["session_boundary"],
            f"{file} session_boundary",
            required={
                "closure_top_level",
                "configuration",
                "observed_configuration",
                "capture_state",
                "retirement",
                "opened",
            },
            allowed={
                "closure_top_level",
                "configuration",
                "observed_configuration",
                "capture_state",
                "retirement",
                "opened",
            },
        )
        configuration = require_text(boundary["configuration"], f"{file} session_boundary.configuration")
        closure = require_text(boundary["closure_top_level"], f"{file} session_boundary.closure_top_level")
        observed = require_text(
            boundary["observed_configuration"],
            f"{file} session_boundary.observed_configuration",
        )
        if configuration != expected_configuration:
            fail(f"{file} session_boundary.configuration differs from the frozen source configuration")
        if not same_document(closure, closure_top_level):
            fail(f"{file} session_boundary.closure_top_level differs from the dependency closure")

        capture_state = require_record(
            boundary["capture_state"],
            f"{file} session_boundary.capture_state",
            required={
                "path",
                "title",
                "saved",
                "save_flag",
                "active_configuration",
                "configurations",
                "read_only",
                "lightweight",
                "unsupported",
            },
            allowed={
                "path",
                "title",
                "saved",
                "save_flag",
                "active_configuration",
                "configurations",
                "read_only",
                "lightweight",
                "unsupported",
            },
        )
        require_text(capture_state["path"], f"{file} capture_state.path")
        require_text(capture_state["title"], f"{file} capture_state.title")
        active_configuration = require_text(
            capture_state["active_configuration"],
            f"{file} capture_state.active_configuration",
        )
        configurations = capture_state["configurations"]
        if not isinstance(configurations, list) or not all(
            isinstance(item, str) and item for item in configurations
        ):
            fail(f"{file} capture_state.configurations metadata must be nonempty strings")
        save_flag = capture_state["save_flag"]
        saved = capture_state["saved"]
        if type(save_flag) is not bool or type(saved) is not bool or saved is save_flag:
            fail(f"{file} capture_state save flag and saved value are not a native boolean inversion")
        if capture_state["read_only"] is not True:
            fail(f"{file} capture_state.read_only is not an observed true boolean")
        if observed != active_configuration:
            fail(f"{file} session_boundary observed_configuration differs from the native active configuration")
        if active_configuration != expected_configuration:
            fail(f"{file} capture_state.active_configuration differs from the frozen source configuration")
        if expected_configuration not in configurations:
            fail(f"{file} capture_state.configurations omits the frozen source configuration")
        if capture_state["lightweight"] is not None or capture_state["unsupported"] != ["lightweight"]:
            fail(f"{file} capture_state lightweight capability diagnostic is inconsistent")
        capture_path = capture_state["path"]

        add(file, "metadata.source_configuration_echo", "/session_boundary/configuration")
        add(file, "metadata.closure_binding", "/session_boundary/closure_top_level")
        add(file, "metadata.native_projection", "/session_boundary/observed_configuration")
        add(file, "metadata.save_flag_inversion", "/session_boundary/capture_state/saved")
        add(
            file,
            "metadata.platform_diagnostic",
            "/session_boundary/capture_state/lightweight",
            "/session_boundary/capture_state/unsupported",
        )

        opened = require_record(
            boundary["opened"],
            f"{file} session_boundary.opened",
            required={"opened", "path", "read_only", "errors", "warnings"},
            allowed={"opened", "path", "read_only", "errors", "warnings"},
        )
        opened_path = require_text(opened["path"], f"{file} opened.path")
        require_text(opened["opened"], f"{file} opened.opened")
        if opened["read_only"] is not True:
            fail(f"{file} opened.read_only is not exactly true")
        if type(opened["errors"]) is not int or opened["errors"] != 0:
            fail(f"{file} OpenDoc6 reported a failed or invalid open")
        if type(opened["warnings"]) is not int or opened["warnings"] < 0:
            fail(f"{file} OpenDoc6 warning count is not a nonnegative integer")
        if not same_document(capture_path, closure) or not same_document(opened_path, closure):
            fail(f"{file} opened and captured paths do not match the dependency closure")
        add(file, "metadata.operation_claim", "/session_boundary/opened/read_only")

        retirement = require_record(
            boundary["retirement"],
            f"{file} session_boundary.retirement",
            required={"role", "retired", "before", "after"},
            allowed={"role", "retired", "before", "after"},
        )
        if retirement["role"] != "copy":
            fail(f"{file} retirement.role must be 'copy'")
        if retirement["retired"] is not True:
            fail(f"{file} retirement.retired is not exactly true")
        retired_before = session_identity(
            retirement["before"],
            f"{file} retirement.before",
            required_state="active",
        )
        retired_after = session_identity(
            retirement["after"],
            f"{file} retirement.after",
            required_state="closed",
        )
        if retired_before["pid"] != retired_after["pid"]:
            fail(f"{file} retirement changed the retired process pid")
        if retired_before["pid"] != rewrite_sessions["copy"]["pid"]:
            fail(f"{file} retirement.before differs from the rewrite copy session")
        for key in ("executable", "ownership"):
            if (
                key in retired_before or key in rewrite_sessions["copy"]
            ) and retired_before.get(key) != rewrite_sessions["copy"].get(key):
                fail(f"{file} retirement.before {key} differs from the rewrite copy session")
        if capture_sessions["source"]["pid"] != rewrite_sessions["source"]["pid"]:
            fail(f"{file} capture and rewrite source pids differ")
        if capture_sessions["source"]["state"] != "closed":
            fail(f"{file} source session remained active during capture")
        if capture_sessions["copy"]["state"] != "active":
            fail(f"{file} capture copy session is not active")
        if capture_sessions["copy"]["pid"] == retired_before["pid"]:
            fail(f"{file} capture reused the retired rewrite process")
        add(
            file,
            "metadata.process_observation",
            "/session_boundary/retirement/role",
            "/session_boundary/retirement/retired",
            "/session_boundary/retirement/before",
            "/session_boundary/retirement/after",
        )

    runtime = require_record(
        runtime,
        RUNTIME_EVIDENCE,
        required={
            "captured_at",
            "platform",
            "python",
            "worker_version",
            "toolchain",
            "solidworks",
            "session_boundary",
        },
        allowed={
            "captured_at",
            "platform",
            "python",
            "worker_version",
            "toolchain",
            "solidworks",
            "session_boundary",
        },
    )
    rewrite_environment = require_record(
        rewrite_environment,
        REWRITE_ENVIRONMENT_EVIDENCE,
        required={
            "captured_at",
            "platform",
            "python",
            "worker_version",
            "toolchain",
            "solidworks",
        },
        allowed={
            "captured_at",
            "platform",
            "python",
            "worker_version",
            "toolchain",
            "solidworks",
        },
    )
    runtime_sha256 = require_digest(runtime_sha256, RUNTIME_EVIDENCE)
    rewrite_sha256 = require_digest(rewrite_sha256, REWRITE_ENVIRONMENT_EVIDENCE)
    expected_configuration = require_text(expected_configuration, "expected configuration")
    closure_top_level = require_text(closure_top_level, "closure top-level")
    expected_tool = require_record(
        expected_tool,
        "expected native tool",
        required={"schema_version", "pipeline_id", "version", "source_sha256", "runtime"},
        allowed={"schema_version", "pipeline_id", "version", "source_sha256", "release", "runtime"},
    )

    sessions_by_file = {}
    for record, file in (
        (runtime, RUNTIME_EVIDENCE),
        (rewrite_environment, REWRITE_ENVIRONMENT_EVIDENCE),
    ):
        for key in ("captured_at", "platform", "python", "worker_version"):
            require_text(record[key], f"{file} {key}")
        if record["worker_version"] != expected_tool["version"]:
            fail(f"{file} worker_version differs from the delivery native tool")
        add(file, "metadata.platform_execution", "/captured_at", "/platform", "/worker_version")
        add(file, "metadata.python_version_split", "/python")
        classify_toolchain(record, file)
        sessions_by_file[file] = classify_solidworks(record, file)

    classify_session_boundary(
        runtime,
        RUNTIME_EVIDENCE,
        capture_sessions=sessions_by_file[RUNTIME_EVIDENCE],
        rewrite_sessions=sessions_by_file[REWRITE_ENVIRONMENT_EVIDENCE],
    )

    objects: list[dict[str, Any]] = []
    for file, digest in (
        (RUNTIME_EVIDENCE, runtime_sha256),
        (REWRITE_ENVIRONMENT_EVIDENCE, rewrite_sha256),
    ):
        for rule_id in _RULE_ORDER:
            fields = sorted(set(fields_by_file_rule.get((file, rule_id), [])))
            if not fields:
                continue
            source, reason = _RULE_TEXT[rule_id]
            objects.append(
                {
                    "object_type": "metadata",
                    "object": file,
                    "fields": fields,
                    "source": source,
                    "rule_id": rule_id,
                    "rule_version": "v1",
                    "reason": reason,
                    "file": file,
                    "sha256": digest,
                    "reference": file,
                }
            )
    return [
        {
            "code": METADATA_WARNING_CODE,
            "message": METADATA_WARNING_MESSAGE,
            "objects": objects,
        }
    ]


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
