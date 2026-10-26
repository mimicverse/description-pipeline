"""Strict identity checks for the Windows endpoint lifecycle evidence."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime

HEX64 = re.compile(r"[0-9a-f]{64}")
LISTENER_SCHEMA = "solidworks-to-urdf.windows-listener/v2"
SOURCE_SCHEMA = "solidworks-to-urdf.windows-source-state/v2"

_SOURCE_FIELDS = frozenset(
    {
        "schema_version",
        "task_path",
        "task_name",
        "task_xml_sha256",
        "task_semantic_sha256",
        "task_acl_sha256",
        "principal_sid",
        "principal",
        "execute",
        "arguments",
        "working_directory",
        "settings",
        "triggers_sha256",
        "endpoint_root",
        "config_path",
        "config_sha256",
        "config_digest",
        "package_root",
        "output_root",
        "state_root",
        "port",
        "install_id_path",
        "install_id",
        "release_identity_sha256",
        "release",
        "listener",
    }
)

_LISTENER_FIELDS = frozenset(
    {
        "schema_version",
        "relation",
        "process_arguments_sha256",
        "scheduler_service_name",
        "scheduler_service_state",
        "scheduler_service_process_id",
        "scheduler_pid",
        "scheduler_image",
        "scheduler_start_utc",
        "scheduler_user_sid",
        "scheduler_session_id",
        "launcher_pid",
        "launcher_parent_pid",
        "launcher_image",
        "launcher_image_sha256",
        "launcher_start_utc",
        "launcher_user_sid",
        "launcher_session_id",
        "listener_pid",
        "listener_parent_pid",
        "listener_start_utc",
        "listener_user_sid",
        "listener_session_id",
        "venv_config_path",
        "venv_config_sha256",
        "venv_home",
        "venv_version",
        "base_interpreter_path",
        "base_interpreter_sha256",
    }
)

_LISTENER_STRINGS = (
    "schema_version",
    "relation",
    "process_arguments_sha256",
    "scheduler_service_name",
    "scheduler_service_state",
    "scheduler_image",
    "scheduler_start_utc",
    "scheduler_user_sid",
    "launcher_image",
    "launcher_image_sha256",
    "launcher_start_utc",
    "launcher_user_sid",
    "listener_start_utc",
    "listener_user_sid",
    "venv_config_path",
    "venv_config_sha256",
    "venv_home",
    "venv_version",
    "base_interpreter_path",
    "base_interpreter_sha256",
)

_LISTENER_DIGESTS = (
    "process_arguments_sha256",
    "launcher_image_sha256",
    "venv_config_sha256",
    "base_interpreter_sha256",
)

_LISTENER_PIDS = (
    "scheduler_pid",
    "scheduler_service_process_id",
    "launcher_pid",
    "launcher_parent_pid",
    "listener_pid",
    "listener_parent_pid",
)

_LISTENER_SESSIONS = (
    "scheduler_session_id",
    "launcher_session_id",
    "listener_session_id",
)

_HEALTH_FIELDS = frozenset(
    {
        "schema_version",
        "pipeline_id",
        "ready",
        "native_platform",
        "release",
        "endpoint",
    }
)

_HEALTH_RELEASE_FIELDS = frozenset(
    {"version", "source_commit", "source_sha256", "role"}
)


def _normalize_path(value: object) -> str:
    return str(value or "").replace("\\", "/").rstrip("/").casefold()


def _is_integer(value: object) -> bool:
    return type(value) is int


def _is_utc(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed


def _listener_document_matches(value: object) -> bool:
    """Validate the frozen process-lineage evidence independent of any source object."""

    if not isinstance(value, dict) or set(value) != _LISTENER_FIELDS:
        return False
    if any(not isinstance(value.get(key), str) or not value[key] for key in _LISTENER_STRINGS):
        return False
    if any(
        not isinstance(value.get(key), str) or HEX64.fullmatch(value[key]) is None
        for key in _LISTENER_DIGESTS
    ):
        return False
    if any(not _is_integer(value.get(key)) or value[key] <= 0 for key in _LISTENER_PIDS):
        return False
    if any(
        not _is_integer(value.get(key)) or value[key] < 0
        for key in _LISTENER_SESSIONS
    ):
        return False
    if (
        value["schema_version"] != LISTENER_SCHEMA
        or value["relation"] != "direct_venv_child"
        or value["scheduler_service_name"] != "Schedule"
        or value["scheduler_service_state"] != "Running"
        or value["scheduler_service_process_id"] != value["scheduler_pid"]
        or value["scheduler_user_sid"] != "S-1-5-18"
        or value["scheduler_session_id"] != 0
        or value["launcher_parent_pid"] != value["scheduler_pid"]
        or value["listener_parent_pid"] != value["launcher_pid"]
        or value["launcher_pid"] == value["listener_pid"]
        or value["launcher_session_id"] != value["listener_session_id"]
        or not re.search(r"/system32/svchost\.exe$", _normalize_path(value["scheduler_image"]))
        or not _normalize_path(value["base_interpreter_path"]).endswith("/python.exe")
        or not _normalize_path(value["base_interpreter_path"]).startswith(
            _normalize_path(value["venv_home"]) + "/"
        )
    ):
        return False
    scheduler_started = _is_utc(value["scheduler_start_utc"])
    launcher_started = _is_utc(value["launcher_start_utc"])
    listener_started = _is_utc(value["listener_start_utc"])
    return (
        scheduler_started is not None
        and launcher_started is not None
        and listener_started is not None
        and scheduler_started <= launcher_started <= listener_started
    )


def sealed_source_listener_matches(source_value: object) -> bool:
    """Validate listener evidence only as part of the complete sealed source context."""

    if (
        not isinstance(source_value, dict)
        or set(source_value) != _SOURCE_FIELDS
        or source_value.get("schema_version") != SOURCE_SCHEMA
    ):
        return False
    listener = source_value.get("listener")
    if not _listener_document_matches(listener):
        return False
    principal = source_value.get("principal")
    arguments = source_value.get("arguments")
    if (
        not isinstance(principal, dict)
        or principal.get("sid") != source_value.get("principal_sid")
        or not isinstance(arguments, str)
        or not arguments
        or type(source_value.get("port")) is not int
        or source_value["port"] < 1
        or source_value["port"] > 65535
    ):
        return False
    endpoint_root = _normalize_path(source_value.get("endpoint_root"))
    if (
        not endpoint_root
        or _normalize_path(source_value.get("execute"))
        != endpoint_root + "/runtime/scripts/python.exe"
        or _normalize_path(listener["launcher_image"])
        != _normalize_path(source_value.get("execute"))
        or listener["process_arguments_sha256"]
        != hashlib.sha256(arguments.encode("utf-8")).hexdigest()
        or _normalize_path(source_value.get("config_path"))
        != endpoint_root + "/endpoint.json"
        or listener["launcher_user_sid"] != source_value.get("principal_sid")
        or listener["listener_user_sid"] != source_value.get("principal_sid")
        or _normalize_path(listener["venv_config_path"])
        != endpoint_root + "/runtime/pyvenv.cfg"
    ):
        return False
    for key in (
        "task_xml_sha256",
        "task_semantic_sha256",
        "task_acl_sha256",
        "config_sha256",
        "triggers_sha256",
        "release_identity_sha256",
    ):
        if not isinstance(source_value.get(key), str) or HEX64.fullmatch(source_value[key]) is None:
            return False
    return True


def endpoint_health_matches_source(health: object, source_value: object) -> bool:
    """Validate only the maintenance health facts that the sealed source can bind."""

    if (
        not isinstance(health, dict)
        or set(health) != _HEALTH_FIELDS
        or not isinstance(source_value, dict)
        or health.get("schema_version") != "solidworks-to-urdf.health/v1"
        or health.get("pipeline_id") != "solidworks-to-urdf"
        or health.get("ready") is not True
        or health.get("native_platform") is not True
        or not isinstance(health.get("release"), dict)
        or set(health["release"]) != _HEALTH_RELEASE_FIELDS
        or not isinstance(health.get("endpoint"), dict)
        or set(health["endpoint"]) != {"port"}
    ):
        return False
    release = health["release"]
    if any(not isinstance(release.get(key), str) or not release[key] for key in release):
        return False
    port = health["endpoint"].get("port")
    return (
        release.get("role") == "native"
        and release == source_value.get("release")
        and type(port) is int
        and port == source_value.get("port")
    )
