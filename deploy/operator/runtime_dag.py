#!/usr/bin/env python3
"""Verify the DAG that Airflow actually parsed matches the active generation record."""

from __future__ import annotations

import argparse
import configparser
import hashlib
import json
import os
import stat
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from install_record import DAG_ID, verify_running_generation  # noqa: E402

MANAGED_MARKER = "# managed-by: description-airflow"
DAG_BUNDLE_NAME = "dags-folder"
DAG_BUNDLE_VERSION = None
FORBIDDEN_CONFIG_ENV = ("AIRFLOW_CONFIG", "AIRFLOW__CORE__DAGS_FOLDER")


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def regular_file(path: Path, label: str, *, mode: int | None = None) -> Path:
    require(not path.is_symlink() and path.is_file(), f"{label} must be a regular non-link file: {path}")
    if mode is not None:
        require(stat.S_IMODE(path.stat().st_mode) == mode, f"{label} must have mode {mode:04o}: {path}")
    return path


def active_dag(record: dict[str, Any]) -> tuple[Path, str]:
    binding = record.get("dag")
    require(isinstance(binding, dict), "install record has no DAG binding")
    require(binding.get("id") == DAG_ID, "install record DAG id differs")
    configured = Path(str(binding.get("path") or ""))
    require(configured.is_absolute(), "install record DAG path is not absolute")
    require(not configured.is_symlink(), "install record DAG path is a link")
    resolved = Path(os.path.realpath(configured))
    regular_file(resolved, "active DAG")
    digest = hashlib.sha256(resolved.read_bytes()).hexdigest()
    require(digest == binding.get("digest"), "active DAG digest differs from the install record")
    return resolved, digest


def configured_dags_folder(airflow_home: Path) -> tuple[Path, str]:
    airflow_home = Path(airflow_home)
    require(airflow_home.is_absolute(), "AIRFLOW_HOME must be absolute")
    cfg = regular_file(airflow_home / "airflow.cfg", "airflow.cfg", mode=0o600)
    payload = cfg.read_bytes()
    text = payload.decode("utf-8")
    require(text.startswith(MANAGED_MARKER), "airflow.cfg is not managed by the deployment")
    parser = configparser.ConfigParser(interpolation=None)
    parser.read_string(text)
    configured = str(parser.get("core", "dags_folder", fallback="")).strip()
    require(bool(configured), "airflow.cfg has no core.dags_folder")
    folder = Path(configured)
    require(folder.is_absolute(), "airflow.cfg core.dags_folder is not absolute")
    require(not folder.is_symlink() and folder.is_dir(), f"airflow.cfg DAG folder is not a real directory: {folder}")
    return folder.resolve(strict=True), hashlib.sha256(payload).hexdigest()


def effective_dags_folder(airflow_home: Path) -> Path:
    for key in FORBIDDEN_CONFIG_ENV:
        require(key not in os.environ, f"{key} must not override the managed Airflow configuration")
    os.environ["AIRFLOW_HOME"] = str(Path(airflow_home))
    from airflow.configuration import conf

    configured = str(conf.get("core", "dags_folder")).strip()
    require(bool(configured), "Airflow resolved no effective core.dags_folder")
    folder = Path(configured)
    require(folder.is_absolute(), "Airflow effective core.dags_folder is not absolute")
    return folder.resolve(strict=True)


def require_local_bundle(bundle_name: Any, bundle_version: Any) -> None:
    require(bundle_name == DAG_BUNDLE_NAME, "Airflow DAG bundle name differs")
    require(bundle_version == DAG_BUNDLE_VERSION, "Airflow local DAG bundle must have a null version")


def serialized_identity(
    dag_id: str,
    version: Any,
    rows: list[Any],
    *,
    dag_path: Path,
    dags_folder: Path,
) -> dict[str, Any]:
    """Bind one DagVersion and its serialized row to the configured file and folder."""

    require(version is not None, "Airflow has no DAG version for the configured DAG")
    require(version.dag_id == dag_id, "Airflow DAG version belongs to another DAG")
    require_local_bundle(version.bundle_name, version.bundle_version)
    require(len(rows) == 1, "Airflow does not have exactly one serialized DAG for the current version")
    row = rows[0]
    require(row.dag_id == dag_id, "serialized DAG row belongs to another DAG")
    require(
        row.dag_version_id == version.id,
        "serialized DAG version differs from the current DagVersion",
    )
    document = row.data
    require(isinstance(document, dict) and isinstance(document.get("dag"), dict), "serialized DAG is malformed")
    dag = document["dag"]
    fileloc = Path(str(dag.get("fileloc") or ""))
    require(fileloc.is_absolute(), "serialized DAG has no absolute fileloc")
    require(Path(os.path.realpath(fileloc)) == dag_path, "serialized DAG fileloc differs from the active DAG")
    processor_folder = Path(str(dag.get("_processor_dags_folder") or ""))
    require(processor_folder.is_absolute(), "serialized DAG has no processor DAG folder")
    require(
        processor_folder.resolve(strict=True) == dags_folder,
        "serialized DAG processor folder differs from airflow.cfg",
    )
    require(dag.get("dag_id") == dag_id, "serialized DAG id differs")
    require(isinstance(row.dag_hash, str) and bool(row.dag_hash), "serialized DAG hash is missing")
    return {
        "bundle_name": version.bundle_name,
        "bundle_version": version.bundle_version,
        "dag_hash": row.dag_hash,
        "dag_version_id": str(version.id),
        "dag_version_number": version.version_number,
        "fileloc": str(dag_path),
        "processor_dags_folder": str(dags_folder),
    }


def serialized_dag(dag_id: str, *, dag_path: Path, dags_folder: Path) -> dict[str, Any]:
    from airflow.models.dag_version import DagVersion
    from airflow.models.serialized_dag import SerializedDagModel
    from airflow.settings import Session

    with Session() as session:
        version = (
            session.query(DagVersion)
            .filter(DagVersion.dag_id == dag_id)
            .order_by(DagVersion.version_number.desc())
            .first()
        )
        rows = []
        if version is not None:
            rows = (
                session.query(SerializedDagModel)
                .filter(
                    SerializedDagModel.dag_id == dag_id,
                    SerializedDagModel.dag_version_id == version.id,
                )
                .all()
            )
    return serialized_identity(dag_id, version, rows, dag_path=dag_path, dags_folder=dags_folder)


def check(state_root: Path, airflow_home: Path, *, files_only: bool = False) -> dict[str, Any]:
    record = verify_running_generation(Path(state_root))
    dag_path, dag_digest = active_dag(record)
    dags_folder, cfg_digest = configured_dags_folder(Path(airflow_home))
    require(dags_folder == dag_path.parent, "airflow.cfg does not point at the active install-record DAG")
    require(
        effective_dags_folder(Path(airflow_home)) == dags_folder,
        "Airflow effective core.dags_folder differs from the managed airflow.cfg",
    )
    result = {
        "airflow_cfg_sha256": cfg_digest,
        "dag_id": DAG_ID,
        "dag_path": str(dag_path),
        "dag_sha256": dag_digest,
        "dags_folder": str(dags_folder),
        "install_id": record["install_id"],
        "release_source_commit": record["release"]["source_commit"],
    }
    if not files_only:
        result["serialized"] = serialized_dag(DAG_ID, dag_path=dag_path, dags_folder=dags_folder)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-root", type=Path, required=True)
    parser.add_argument("--airflow-home", type=Path, required=True)
    parser.add_argument("--files-only", action="store_true")
    args = parser.parse_args()
    try:
        result = check(args.state_root, args.airflow_home, files_only=args.files_only)
    except Exception as error:
        print(f"runtime_dag: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
