"""Stable identities for the published source-to-URDF workflows.

A ``pipeline_id`` names one published workflow definition (for example ``solidworks-to-urdf``).
It is stable across hardware, CAD revisions and runs; ``hardware_id``, the locked tool version and
the deterministic subject digest name the other dimensions.  The catalog below is the single source
of truth for the ids, their accepted source kinds and the stages that actually implement them, and
the accompanying test resolves every code symbol and document path so the catalog cannot drift.

This is an identity contract, not an orchestration engine: every command keeps calling the same
application functions, and the catalog only says which published pipeline those calls implement.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .io import PipelineError

SCHEMA = "description.pipeline/v1"


@dataclass(frozen=True)
class Stage:
    """One stage of a published workflow, bound to the code and the document that define it."""

    name: str
    summary: str
    code: str
    document: str


@dataclass(frozen=True)
class Pipeline:
    """A published workflow definition."""

    id: str
    title: str
    source_kinds: tuple[str, ...]
    stages: tuple[Stage, ...]
    documents: tuple[str, ...]


def _to_urdf_stages(capture: Stage) -> tuple[Stage, ...]:
    """The stages every source-to-URDF workflow runs, with the capture stage specialised."""

    return (
        capture,
        Stage(
            "freeze",
            "Verify the captured tree and write the immutable source lock",
            "description_pipeline.build:freeze",
            "docs/pipeline.en.md",
        ),
        Stage(
            "normalize",
            "Apply the provider semantics, author interfaces and explicit overrides",
            "description_pipeline.build:normalize",
            "docs/pipeline.en.md",
        ),
        Stage(
            "build",
            "Generate the canonical model, URDF and MJCF and write the bundle manifest",
            "description_pipeline.build:build",
            "docs/urdf_standard.en.md",
        ),
        Stage(
            "check",
            "Re-derive every source, physical, consumer and delivery check",
            "description_pipeline.build:assess",
            "docs/validation.md",
        ),
        Stage(
            "accept",
            "Run the declared application acceptance against an operator-selected reference",
            "description_pipeline.verification.mechanics:run_acceptance",
            "docs/mechanical-acceptance.en.md",
        ),
        Stage(
            "submit",
            "Publish the candidate on a review branch and open or refresh its pull request",
            "description_pipeline.repository:_submit",
            "docs/pipeline.en.md",
        ),
        Stage(
            "promote",
            "Re-fetch the candidate, re-run acceptance and fast-forward the release",
            "description_pipeline.repository:promotion_plan",
            "RELEASING.md",
        ),
    )


_SOLIDWORKS_CAPTURE = Stage(
    "capture",
    "Capture a saved SolidWorks assembly through the Windows worker",
    "description_pipeline.sources.solidworks:freeze",
    "docs/solidworks-first-use.en.md",
)
_ONSHAPE_CAPTURE = Stage(
    "capture",
    "Capture a locked Onshape document through the API",
    "description_pipeline.sources.onshape:freeze",
    "docs/onshape_export.md",
)
_FIXTURE_CAPTURE = Stage(
    "capture",
    "Copy and verify an existing frozen snapshot without contacting CAD",
    "description_pipeline.build:freeze",
    "docs/pipeline.en.md",
)

PIPELINES: dict[str, Pipeline] = {
    "solidworks-to-urdf": Pipeline(
        id="solidworks-to-urdf",
        title="SolidWorks assembly to a verified URDF/MJCF model",
        source_kinds=("solidworks",),
        stages=_to_urdf_stages(_SOLIDWORKS_CAPTURE),
        documents=(
            "docs/solidworks-first-use.en.md",
            "docs/pipeline.en.md",
            "docs/urdf_standard.en.md",
            "docs/mechanical-acceptance.en.md",
            "RELEASING.md",
        ),
    ),
    "onshape-to-urdf": Pipeline(
        id="onshape-to-urdf",
        title="Onshape document to a verified URDF/MJCF model",
        source_kinds=("onshape",),
        stages=_to_urdf_stages(_ONSHAPE_CAPTURE),
        documents=(
            "docs/onshape_export.md",
            "docs/pipeline.en.md",
            "docs/urdf_standard.en.md",
            "docs/mechanical-acceptance.en.md",
            "RELEASING.md",
        ),
    ),
    "fixture-to-urdf": Pipeline(
        id="fixture-to-urdf",
        title="Frozen fixture or imported snapshot to a verified URDF/MJCF model",
        source_kinds=("fixture", "snapshot", "imported"),
        stages=_to_urdf_stages(_FIXTURE_CAPTURE),
        documents=(
            "docs/pipeline.en.md",
            "docs/urdf_standard.en.md",
            "docs/mechanical-acceptance.en.md",
            "RELEASING.md",
        ),
    ),
}

#: The frozen source kind a workflow runs on.  ``snapshot`` and ``imported`` are replay kinds: they
#: name how the tree was obtained, not a different workflow, so they share the fixture pipeline.
KIND_IDS = {
    "solidworks": "solidworks-to-urdf",
    "onshape": "onshape-to-urdf",
    "fixture": "fixture-to-urdf",
    "snapshot": "fixture-to-urdf",
    "imported": "fixture-to-urdf",
}

#: Native providers outrank replay kinds when the two are mixed (a SolidWorks snapshot replayed
#: through a fixture wrapper is still the SolidWorks workflow).
NATIVE_KINDS = ("solidworks", "onshape")


def known_ids() -> tuple[str, ...]:
    return tuple(sorted(PIPELINES))


def get(pipeline_id: str) -> Pipeline:
    """The catalog entry, or a refusal that names the published ids."""

    if not isinstance(pipeline_id, str) or pipeline_id not in PIPELINES:
        raise PipelineError(f"Unknown pipeline_id {pipeline_id!r}; published pipelines: {', '.join(known_ids())}")
    entry = PIPELINES[pipeline_id]
    return entry


def catalog() -> list[dict]:
    """JSON-ready catalog listing, in stable id order."""

    return [describe(pipeline_id) for pipeline_id in known_ids()]


def describe(pipeline_id: str) -> dict:
    entry = get(pipeline_id)
    return {
        "id": entry.id,
        "title": entry.title,
        "source_kinds": list(entry.source_kinds),
        "stages": [
            {
                "name": stage.name,
                "summary": stage.summary,
                "code": stage.code,
                "document": stage.document,
            }
            for stage in entry.stages
        ],
        "documents": list(entry.documents),
    }


def effective_source_kind(
    source: dict | None,
    robot: dict | None = None,
    frozen_kind: str | None = None,
    identity_provider: str | None = None,
) -> str | None:
    """The kind whose workflow is really running.

    The frozen kind is the actual captured kind, the snapshot identity names the provider that
    produced it, ``robot.provider`` names the provider of a replayed snapshot and
    ``source.provider`` is the authoring entry point.  A native kind in any position wins in that
    order, so a SolidWorks snapshot replayed through the fixture wrapper resolves to the SolidWorks
    workflow; two different native providers are a contradiction and refuse rather than pick one.
    """

    candidates = [
        frozen_kind,
        identity_provider,
        (robot or {}).get("provider"),
        (source or {}).get("provider"),
    ]
    candidates = [value for value in candidates if isinstance(value, str) and value]
    natives = [value for value in candidates if value in NATIVE_KINDS]
    distinct = sorted(set(natives))
    if len(distinct) > 1:
        raise PipelineError(
            f"Conflicting native source kinds ({', '.join(distinct)}); fix the definition or freeze the matching source"
        )
    if natives:
        return natives[0]
    for value in candidates:
        if value in KIND_IDS:
            return value
    return candidates[0] if candidates else None


def resolve_identity(
    declared: str | None,
    *,
    source: dict | None,
    robot: dict | None = None,
    frozen_kind: str | None = None,
    identity_provider: str | None = None,
) -> dict:
    """Resolve the identity of the workflow this definition runs.

    A declared id is authoritative and must be known and compatible with the actual source kind; the
    tool never remaps it silently.  Without a declaration the id is derived from the source kind and
    recorded as derived, which is the explicit migration path for definitions written before this
    contract existed.
    """

    kind = effective_source_kind(source, robot, frozen_kind, identity_provider)
    if declared is not None:
        if not isinstance(declared, str) or declared not in PIPELINES:
            raise PipelineError(f"pipeline_id must be one of {', '.join(known_ids())}; got {declared!r}")
        entry = PIPELINES[declared]
        if kind not in entry.source_kinds:
            raise PipelineError(
                f"pipeline_id {declared!r} does not match the frozen source kind {kind!r} "
                f"(it accepts {', '.join(entry.source_kinds)}); fix the definition or freeze the "
                "matching source"
            )
        return {
            "schema_version": SCHEMA,
            "id": declared,
            "declared": True,
            "resolved_from": "config/robot.yaml",
            "source_kind": kind,
        }
    resolved = KIND_IDS.get(kind) if kind else None
    if resolved is None:
        raise PipelineError(
            f"Cannot resolve a published pipeline for the source kind {kind!r}; declare "
            f"pipeline_id: one of {', '.join(known_ids())}"
        )
    return {
        "schema_version": SCHEMA,
        "id": resolved,
        "declared": False,
        "resolved_from": "source_kind",
        "source_kind": kind,
    }


def verify_lock(locked: dict, config: dict, manifest: dict) -> dict:
    """Bind the definition, the source lock and the frozen snapshot into one identity.

    Editing a declared id, removing it, or tampering with the lock fails here, before any build.  A
    lock written before this contract carries no pipeline block; that is accepted only while the
    definition stays undeclared, and the resolved identity is reported as legacy.
    """

    if not isinstance(locked, dict) or not isinstance(config, dict) or not isinstance(manifest, dict):
        raise PipelineError("Pipeline identity needs the definition, source lock and frozen manifest")
    declared = config.get("pipeline_id")
    has_block = "pipeline" in locked
    has_id = "pipeline_id" in locked
    kind = manifest.get("kind")
    identity = manifest.get("identity")
    provider = identity.get("provider") if isinstance(identity, dict) else None
    if not has_block:
        if has_id:
            raise PipelineError(
                "The source lock carries a pipeline_id without its provenance block (torn identity); "
                "run `description source freeze --root .` to write it again"
            )
        if declared is not None:
            raise PipelineError(
                "The source lock predates pipeline identities; run "
                "`description source freeze --root .` to bind the declared pipeline_id"
            )
        resolved = resolve_identity(
            None,
            source=config.get("source"),
            robot=config.get("robot"),
            frozen_kind=kind,
            identity_provider=provider,
        )
        resolved["resolved_from"] = "legacy_lock"
        return resolved
    stored = locked["pipeline"]
    if not isinstance(stored, dict) or stored.get("schema_version") != SCHEMA:
        raise PipelineError("Unsupported pipeline identity in sources/source.lock.json; freeze the source again")
    if not isinstance(stored.get("id"), str) or not stored["id"]:
        raise PipelineError("The source lock carries no pipeline id; freeze the source again")
    expected = resolve_identity(
        declared,
        source=config.get("source"),
        robot=config.get("robot"),
        frozen_kind=kind,
        identity_provider=provider,
    )
    for key in ("id", "declared", "resolved_from", "source_kind"):
        if stored.get(key) != expected[key]:
            raise PipelineError(
                f"Pipeline identity changed ({key}): the source lock says {stored.get(key)!r}, "
                f"the definition and frozen source say {expected[key]!r}. "
                "Restore the definition or freeze the source again"
            )
    if locked.get("pipeline_id") != expected["id"]:
        raise PipelineError(
            f"Pipeline identity changed (pipeline_id): the source lock says {locked.get('pipeline_id')!r}, "
            f"the definition and frozen source say {expected['id']!r}. "
            "Restore the lock or freeze the source again"
        )
    return expected


def workspace_identity(root: Path) -> dict:
    """Describe the identity of one workspace, for `description pipeline show --root`."""

    from .build import definition, inputs

    root = Path(root).resolve()
    if not (root / "sources/source.lock.json").is_file():
        config = definition(root)
        identity = resolve_identity(config.get("pipeline_id"), source=config.get("source"), robot=config.get("robot"))
        identity["resolved_from"] = "definition_only"
        return {
            "root": str(root),
            "declared_id": config.get("pipeline_id"),
            "frozen": False,
            "identity": identity,
        }
    # The frozen view reuses the build boundary, so source_config_digest, manifest_digest and the
    # pipeline binding are all verified; a tampered lock is never described as a valid identity.
    config, locked, _source, manifest = inputs(root)
    return {
        "root": str(root),
        "declared_id": config.get("pipeline_id"),
        "frozen": True,
        "identity": verify_lock(locked, config, manifest),
    }
