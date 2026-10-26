from __future__ import annotations

import hashlib


def valid_source(
    *,
    endpoint_root: str = "C:/description/state/lifecycle/gen-1/generation",
    install_id: str = "win-" + "b" * 32,
    config_digest: str = "a" * 64,
    release: dict | None = None,
) -> dict:
    release_record = {"role": "native", **(release or {})}
    arguments = (
        '-I -B -m description_pipeline serve --config "'
        f'{endpoint_root}/endpoint.json"'
    )
    listener = {
        "schema_version": "solidworks-to-urdf.windows-listener/v2",
        "relation": "direct_venv_child",
        "process_arguments_sha256": hashlib.sha256(arguments.encode()).hexdigest(),
        "scheduler_service_name": "Schedule",
        "scheduler_service_state": "Running",
        "scheduler_service_process_id": 2512,
        "scheduler_pid": 2512,
        "scheduler_image": "C:/Windows/System32/svchost.exe",
        "scheduler_start_utc": "2026-10-27T00:00:00Z",
        "scheduler_user_sid": "S-1-5-18",
        "scheduler_session_id": 0,
        "launcher_pid": 19344,
        "launcher_parent_pid": 2512,
        "launcher_image": f"{endpoint_root}/runtime/Scripts/python.exe",
        "launcher_image_sha256": "2" * 64,
        "launcher_start_utc": "2026-10-27T00:00:01Z",
        "launcher_user_sid": "S-1-5-21-1-2-3-500",
        "launcher_session_id": 1,
        "listener_pid": 15124,
        "listener_parent_pid": 19344,
        "listener_start_utc": "2026-10-27T00:00:02Z",
        "listener_user_sid": "S-1-5-21-1-2-3-500",
        "listener_session_id": 1,
        "venv_config_path": f"{endpoint_root}/runtime/pyvenv.cfg",
        "venv_config_sha256": "3" * 64,
        "venv_home": "C:/Python312",
        "venv_version": "3.12.10",
        "base_interpreter_path": "C:/Python312/python.exe",
        "base_interpreter_sha256": "4" * 64,
    }
    return {
        "schema_version": "solidworks-to-urdf.windows-source-state/v2",
        "task_path": "\\",
        "task_name": "Description",
        "task_xml_sha256": "5" * 64,
        "task_semantic_sha256": "6" * 64,
        "task_acl_sha256": "7" * 64,
        "principal_sid": "S-1-5-21-1-2-3-500",
        "principal": {"sid": "S-1-5-21-1-2-3-500"},
        "execute": f"{endpoint_root}/runtime/Scripts/python.exe",
        "arguments": arguments,
        "working_directory": "C:/description/state",
        "settings": {"multiple_instances": "IgnoreNew"},
        "triggers_sha256": "8" * 64,
        "endpoint_root": endpoint_root,
        "config_path": f"{endpoint_root}/endpoint.json",
        "config_sha256": "9" * 64,
        "config_digest": config_digest,
        "package_root": "C:/description/packages",
        "output_root": "C:/description/results",
        "state_root": "C:/description/state",
        "port": 8765,
        "install_id_path": "C:/description/state/install-id",
        "install_id": install_id,
        "release_identity_sha256": "c" * 64,
        "release": release_record
        if release is not None
        else {
            "version": "1.0.0",
            "source_commit": "d" * 40,
            "source_sha256": "e" * 64,
            "role": "native",
        },
        "listener": listener,
    }


def operator_status_sample(
    install_id: str,
    config_digest: str,
    release: dict,
    *,
    endpoint_root: str = "C:/description-e2e/state/lifecycle/gen-2/generation",
    port: int = 8765,
) -> dict:
    source = valid_source(
        endpoint_root=endpoint_root,
        install_id=install_id,
        config_digest=config_digest,
        release=release,
    )
    source.update(
        {
            "task_name": "description-e2e-endpoint",
            "task_xml_sha256": "a" * 64,
            "task_semantic_sha256": "c" * 64,
            "task_acl_sha256": "d" * 64,
            "principal_sid": "S-1-5-21-1",
            "principal": {"sid": "S-1-5-21-1"},
            "working_directory": "C:/description-e2e/state",
            "triggers_sha256": "5" * 64,
            "config_sha256": "6" * 64,
            "package_root": "C:/description-e2e/packages",
            "output_root": "C:/description-e2e/results",
            "state_root": "C:/description-e2e/state",
            "port": port,
            "install_id_path": "C:/description-e2e/state/install-id",
            "release_identity_sha256": "e" * 64,
        }
    )
    listener = source["listener"]
    listener["launcher_user_sid"] = "S-1-5-21-1"
    listener["listener_user_sid"] = "S-1-5-21-1"
    return {
        "task_name": source["task_name"],
        "task_path": source["task_path"],
        "task_state": "Running",
        "action_count": 1,
        "task_xml": "<Task/>",
        "task_xml_sha256": source["task_xml_sha256"],
        "task_semantic_sha256": source["task_semantic_sha256"],
        "task_acl_sha256": source["task_acl_sha256"],
        "endpoint_root": endpoint_root,
        "config_path": source["config_path"],
        "listener": listener,
        "maintenance_relation": "target",
        "install_id": install_id,
        "config_digest": config_digest,
        "release_identity_sha256": source["release_identity_sha256"],
        "source": source,
        "release": source["release"],
        "health": {
            "schema_version": "solidworks-to-urdf.health/v1",
            "pipeline_id": "solidworks-to-urdf",
            "ready": True,
            "native_platform": True,
            "release": source["release"],
            "endpoint": {"port": port},
        },
    }
