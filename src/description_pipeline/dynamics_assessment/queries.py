"""Read optional assessment facts through the caller's database snapshot."""

from __future__ import annotations

from pathlib import Path

from ..artifacts import ArtifactStore
from ..artifacts.store import path_has_link_ancestor
from ..pipeline.contracts import ArtifactRef
from .contracts import (
    RESULT_SCHEMA,
    TERMINAL_STATES,
    AssessmentRequest,
    MemberRef,
    parse_json,
    project_attempt,
    require,
)


def _rows(connection, query: str, parameters=()) -> list[dict]:
    with connection.cursor() as cursor:
        cursor.execute(query, parameters)
        fields = [field[0] for field in cursor.description]
        return [dict(zip(fields, values, strict=True)) for values in cursor.fetchall()]


def read_lifecycle_rows(connection) -> list[dict]:
    """Classify shared attempt projections without opening another transaction."""
    rows = _rows(
        connection,
        "SELECT attempt.*,attempt.lease_until<=now() AS lease_expired,request.source_run_id "
        "FROM assessment_attempt attempt JOIN assessment_request request "
        "ON request.request_id=attempt.request_id ORDER BY attempt.attempt_id",
    )
    projected = []
    for record in rows:
        attempt = project_attempt(record)
        settled = attempt["resources_settled"]
        require(
            type(settled) is bool and settled == (attempt["state"] in TERMINAL_STATES),
            "Assessment resource settlement differs from its terminal state",
        )
        bucket = (
            "unresolved" if attempt["state"] == "unknown"
            else "terminal" if settled else "in_flight"
        )
        projected.append(
            {
                "object_kind": "assessment",
                "object_id": attempt["attempt_id"],
                "run_id": attempt["source_run_id"],
                "state": attempt["state"],
                "bucket": bucket,
                "updated_at": attempt["updated_at"],
                "resources_settled": settled,
                "detail": attempt["detail"],
            }
        )
    return projected


def _registered_artifacts(connection, identities) -> dict[str, ArtifactRef]:
    rows = _rows(
        connection,
        "SELECT artifact_id,kind,locator,manifest_sha256,set_sha256,producer_run_id,"
        "producer_stage,producer_tool_sha256 FROM artifact WHERE artifact_id=ANY(%s)",
        (sorted(identities),),
    )
    artifacts = {row["artifact_id"]: ArtifactRef.from_record(row) for row in rows}
    require(set(artifacts) == set(identities), "Assessment recovery reference is absent from the artifact registry")
    return artifacts


def read_recovery_artifacts(connection, artifact_root: Path) -> tuple[ArtifactRef, ...]:
    """Expand sealed requests and bind every direct or nested reference to the registry.

    The caller keeps its exported snapshot alive during this read and file capture.
    No artifact, request or result is reconstructed from mutable work directories.
    """
    requests = _rows(connection, "SELECT * FROM assessment_request ORDER BY request_id")
    results = _rows(
        connection,
        "SELECT attempt_id,request_id,state,result_artifact_id FROM assessment_attempt "
        "WHERE result_artifact_id IS NOT NULL ORDER BY attempt_id",
    )
    if not requests and not results:
        return ()
    root = Path(artifact_root)
    require(
        root.is_absolute()
        and root.is_dir()
        and (root / "objects").is_dir()
        and not path_has_link_ancestor(root / "objects"),
        "Assessment recovery artifact root is missing or unsafe",
    )
    store = ArtifactStore(root)
    direct_ids = {row["request_artifact_id"] for row in requests}
    direct_ids.update(row["result_artifact_id"] for row in results)
    artifacts = _registered_artifacts(connection, direct_ids)
    expanded = dict(artifacts)
    sealed_requests = {}
    for row in requests:
        artifact = artifacts[row["request_artifact_id"]]
        store.verify(artifact)
        data, _member, _manifest = store.read_metadata_member(artifact, "request.json", max_bytes=1024 * 1024)
        request = AssessmentRequest(parse_json(data))
        require(
            request.sha256 == row["request_sha256"]
            and request.record["principal"] == row["principal"]
            and request.source.record["run_id"] == row["source_run_id"]
            and artifact.kind == "dynamics_request"
            and artifact.producer_run_id == row["request_id"]
            and artifact.producer_stage is None
            and artifact.producer_tool_sha256 == request.record["executor_sha256"],
            "Assessment recovery request differs from its registered identity",
        )
        sealed_requests[row["request_id"]] = request
        nested = [
            MemberRef.from_record(record).artifact
            for record in (
                request.source.record["urdf"],
                request.source.record["mjcf"],
                *request.source.record["certificates"].values(),
            )
        ]
        if request.bundle is not None:
            nested.append(request.bundle)
        for reference in nested:
            previous = expanded.setdefault(reference.artifact_id, reference)
            require(previous == reference, "Assessment recovery references disagree on an artifact identity")
    registered = _registered_artifacts(connection, expanded)
    require(registered == expanded, "Assessment recovery reference differs from the complete registered ArtifactRef")
    for artifact in expanded.values():
        store.verify(artifact)
    for request in sealed_requests.values():
        request.source.verify(store)
    for row in results:
        request = sealed_requests[row["request_id"]]
        artifact = artifacts[row["result_artifact_id"]]
        data, _member, _manifest = store.read_metadata_member(
            artifact, "assessment_result.json", max_bytes=1024 * 1024
        )
        result = parse_json(data)
        require(
            artifact.kind == "dynamics_result"
            and artifact.producer_stage is None
            and artifact.producer_run_id == row["attempt_id"]
            and artifact.producer_tool_sha256 == request.record["executor_sha256"]
            and result.get("schema") == RESULT_SCHEMA
            and result.get("request_id") == row["request_id"]
            and result.get("attempt_id") == row["attempt_id"]
            and result.get("state") == row["state"]
            and result.get("resources_settled") is True
            and result.get("request_sha256") == request.sha256
            and result.get("runtime_sha256") == request.runtime.sha256
            and result.get("source_sha256") == request.source.sha256,
            "Assessment recovery result differs from its immutable request or terminal state",
        )
    return tuple(expanded[key] for key in sorted(expanded))
