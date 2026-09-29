# Onshape source adapter (`description_pipeline.sources.onshape`)

English · [中文](onshape.md)

Freeze any Onshape assembly into an **immutable source snapshot** and replay the same
`description.scene/v1` offline on any machine. The adapter only has to "read the CAD correctly":
rigid-body grouping, which limits to keep, effort/velocity, the collision policy and material
assumptions all belong to the robot definition and the normalisation layer and are never guessed
here.

## Quick start

```python
from pathlib import Path

from description_pipeline.sources.onshape import freeze, load_scene

manifest = freeze(
    {
        "url": "https://cad.onshape.com/documents/<did>/w/<wid>/e/<eid>",
        "cache": ".cache/onshape",  # reuse immutable responses; an online capture refreshes the workspace head
        "configuration": "default",
        "capture": {"evidence": "cad", "reason": "2026-09-17 real API capture"},
    },
    Path("out/snapshot"),
)
scene = load_scene(Path("out/snapshot"))  # verifies the manifest and every digest, then returns the scene
```

Change the same configuration to `{"cache": ".cache/onshape", "offline": True}` to replay the same
scene on a machine with no credentials and no network; a mismatched digest fails immediately
(`onshape_snapshot_tampered`).

## Configuration keys

The `config` passed to `freeze` is the `source` mapping from `config/robot.yaml`.
**Key names are a whitelist**: an undefined key reports `onshape_source_config_invalid` (listing the
accepted keys), and a wrong type (a non-boolean `offline`/`include_geometry`, a `tolerance` that is
not a finite positive number, `elements` that is not a list of strings) is rejected as well — a
misspelled switch may never change the capture behaviour silently.

| Key | Required | Meaning |
|---|---|---|
| `url` | one of two | Onshape document URL; `/w/<id>` is a workspace, `/v/<id>` a version |
| `document_id` / `element_id` / `workspace_id` or `version_id` | one of two | Explicit ids, taking precedence over the URL; workspace and version are mutually exclusive |
| `stack` | no | Defaults to `https://cad.onshape.com` |
| `configuration` | no | Assembly configuration, default `default`; recorded in the identity and the manifest |
| `cache` | no | Cache directory (`json/` + `bytes/`); immutable responses are reused first, the online workspace head is always refreshed |
| `offline` | no | Offline: read the cache only and issue no request |
| `include_geometry` | no | Default `true`; `false` reads physical readings only |
| `tolerance` | no | Part matching tolerance when splitting glTF, default `0.05` |
| `elements` | no | Additional part-studio elements; captured on demand when the offline cache lacks the directory |
| `capture` | no | Capture declaration, see below; an override must state a `reason` |
| `client` | no | Inject a custom transport layer (tests or a corporate proxy) |

Credentials are read only from the environment (`ONSHAPE_ACCESS_KEY` / `ONSHAPE_SECRET_KEY`,
optionally `ONSHAPE_API`, `ONSHAPE_SECRET_BEARER`) or `~/.onshape_api_keys.json`, and no output
contains a key.

## Snapshot layout

```text
manifest.json            shared manifest: schema_version/kind/identity/evidence_class/scene/files
scene.json               description.scene/v1, the source semantics (the only scene entry point)
raw/assembly_*.json      raw API responses: assembly tree, features, mate values, mass properties, glTF
geometry/parts/*.stl     one mesh per part (file names sanitised by the cache rules)
geometry/parts.json      mesh readings: sha256, byte size, triangles, bounding box, volume, origin
```

`manifest.files` covers the digest of every file except the manifest itself; one extra file, one
missing file or one changed byte is rejected by `load_scene`. The manifest `identity` records the
source lock (document/workspace or version/configuration/microversion), the dependency closure
(elements, sub-assemblies, part studios, parts) and the capture settings.

## Evidence classes

`identity.capture.mode` describes the data channel while `evidence_class` describes where these bytes
came from; the two are independent:

| Situation | `capture.mode` | `evidence_class` | Meaning |
|---|---|---|---|
| The API really was visited now | `live_api` | `cad` | The only case that grants native CAD provenance |
| Replaying a real capture cache with `capture.evidence=cad` | `cache_replay` | `cad` | The cache is an immutable copy of the real data |
| A repository fixture with `capture.evidence=fixture` | `cache_replay` | `fixture` | For structural tests; never a CAD acceptance |
| A cache with no capture declaration | `cache_replay` | `imported` | A `capture_provenance_missing` gap is appended as well |

Overriding `capture` requires a `reason`, otherwise `freeze` reports
`onshape_snapshot_incomplete`: the provenance of the evidence is an input to how much a conclusion
can be trusted and may never be rewritten silently.

## Scene mapping rules

| Object | Rule | provenance fields |
|---|---|---|
| `links[]` | One link per **leaf Part instance**; the frame is the part-studio coordinate system | `source_entities` (instance path), `source_part`, `source_name`, `source_mass`, `source_geometry` |
| Mass | The nominal `massproperties` value (each scalar is `[nominal, lower, upper]`; the first is used) | `source_mass` points at the position in the raw response |
| Geometry | Meshes split out of glTF; falls back to a legacy per-part STL when glTF is unavailable | `geometry_source` = `gltf_split` / `cached_part_stl` |
| `joints[]` | mate → `revolute`/`prismatic`/`fixed`; parent/child follow the `matedEntities` order | `parent_child_from`, `limits_from` |
| Joint pose | Origin and z axis of the first mate connector in the world frame, converted into the parent link frame | `axis_from` = `mate_connector_z` |
| `frames[]` | `frame_*` mates (positioned by mate connectors) → named coordinate frames | `source_mate`, `source_mate_entities` |
| Naming | `"femur (1) <1>"` → `femur_1`, with a suffix for duplicates; the original name is kept | `source_name` |

`scene.provenance` reconciles with three tiers of fields so an assembly container is never mistaken
for a mass entity: a link claims its own physical parts through `source_entities`, the layers above
check for omissions and duplicates, and `entity_counts` gives the counting convention. Direction,
sign and rigid-body grouping are always left to the robot definition to override.

| Field | Content |
|---|---|
| `provenance.expected_occurrences` | Every CAD instance path (containers included) |
| `provenance.expected_entities` | Only physical part instances — the mass entities |
| `provenance.non_physical` | `containers` (assembly containers) and `suppressed` (suppressed part instances) with their basis; neither enters the model, but their existence is kept |

## Normalisation by definition (`normalize_scene`)

The source scene is **assembly semantics** (one link per leaf part), not a robot tree. The shared core
calls a pure function from the source package before `Robot.from_dict`:

```python
from description_pipeline.sources.onshape import load_scene, normalize_scene

model = normalize_scene(load_scene(snapshot_root), author_definition, snapshot_root)
```

`author_definition` is the whole `config/robot.yaml` (it reads its `robot` mapping), not the source
capture configuration — grouping and joint semantics come from the author definition, while `source`
still only describes where the data came from, so this step needs no further API access.

### Definition fields (`robot:`)

| Key | Meaning |
|---|---|
| `schema` | Fixed to `description.robot-definition/v1` |
| `root` | Link name of the kinematic tree root |
| `reference` | Reference model and reconciliation method (free-form; state the basis) |
| `links[]` | `name` + `members` (member selectors: entity keys or top-level instance keys) + `reference` (frame basis of the root link) + `source` |
| `joints[]` | `name`, `mate` (a source mate name or id that must resolve uniquely), `parent`/`child`, `type`, `axis.source`/`axis.sign`, `limits.source\|lower/upper`, `zero`, `source` |
| `frames[]` | `name`, `link` (the generated massless link), `mate`, `parent` (the fixed joint's parent) |
| `mass.overrides[]` | `part_ids`/`names` + `density_kg_m3` + `source` (mass assumptions; effective only for the explicit list) |
| `non_physical[]` | `entities` + `basis` + optional `evidence`: the raw JSON must bind the exact instance or a part of the same studio, and the geometry must be that part's STL; an exclusion without a real frame consumer and without such evidence is rejected |
| `collision` / `effort_velocity` | Optional; omitting them leaves the value missing and the purpose rules block it |

Parsing is strict: an unknown key, an illegal identifier, a duplicate link or a reference to a
non-existent link reports `onshape_definition_invalid`.
The same holds for a `mate` selector: when the assembly has duplicate names (or a name collides with
another mate's id) the mate cannot be addressed uniquely, so it reports `onshape_definition_invalid`
with the candidates instead of guessing "the first one" — a silent binding connects a joint to the
wrong part.
The numbers in `robot.reference` (reconciliation error, matching limit counts) are **author
declarations** and the pipeline does not recompute them; every machine-checkable conclusion comes
from `verify_normalization` below and the shared quality layer. The ambiguous `tolerance_m` was
removed from the definition and is rejected as an undefined field if written.

### Independent verification (`verify_normalization`)

```python
from description_pipeline.sources.onshape import verify_normalization

checks = verify_normalization(raw_scene, definition, snapshot_root, canonical)
# → [{id, version, status, expected, checked, missing, details}, ...]
```

All four checks **recompute from the raw readings** and never call the fusion or positioning
implementation of `normalize`:

| id | Check |
|---|---|
| `source.occurrences` | Re-derive the instance/part/container sets and counts from `raw/assembly_*.json` and compare them with `expected_occurrences`, `expected_entities`, `non_physical.containers` and `entity_counts` |
| `source.entities` | Raw physical parts = covered (`source_entities`) + explicitly excluded, with no duplicates and no overlap |
| `source.exclusions` | Every exclusion needs a real frame consumer, or an evidence file **inside the capture layer** (`raw/`, `geometry/`) that binds exactly that instance or part identity; it must also give the excluded mass, part ids and source geometry; details carry `excluded_mass_total_kg` |
| `source.mass_conservation` | Recompute world-frame Σm, Σm·c/Σm and Σ(R·I·Rᵀ + m·parallel axis) from the raw readings using the author's density assumptions, then move each link's inertia into that same world frame with the model tree's q=0 FK and compare item by item (mass 1e-12 kg / centre of mass 1e-9 m / inertia 1e-12 + 1e-6 relative) |

### Source identity and revision locking

`identity.capture_settings` records the configuration and export settings; `source_semantics`
records SI units, tensor order, the inertia reference point and the transform direction. Raw JSON is
stored by content, STL keeps its original bytes, and every request records a digest.
An online capture reads the workspace head once more at the end; a change is recorded as
`workspace_moved_during_capture` and never rewrites the pinned revision.

* **Workspace reference**: read the root assembly once through `/w/` (the probe request) to obtain
  `rootAssembly.documentMicroversion`, then **switch every later request to `/m/<microversion>`**; a
  **version reference** uses `/v/<version>` directly. Every byte in the chain therefore belongs to
  one immutable revision.
* **Dependency identity**: every instance must share the root's **document, configuration and
  microversion**, otherwise it is refused explicitly — `onshape_foreign_document` (linked document),
  `onshape_revision_mismatch` (mixed revisions or a missing microversion) and
  `onshape_identity_collision` (an unrequested configuration). Cache names are namespaced per single
  document and configuration and are never renamed silently.
* **Cache request binding**: a newly written cache records each entry's `(path, query, sha256)` in
  `json/_requests.json`; reads verify them entry by entry, and a name pointing at a different request
  or a mismatched content digest reports `onshape_cache_identity_mismatch`.
* **Legacy caches**: caches from the `tools/onshape_export` era have no index and can still be
  replayed offline, but every entry is recorded as `unbound`, with
  `capture.cache_binding = "legacy_unverified"`, `identity.revision_locked = false` and the gaps
  `cache_request_identity_unverified` and `revision_not_locked` — nothing pretends to be locked.
* **Manifest fields**: `identity.revision_locked` / `identity.revision_evidence` (microversion, probe
  request, pinned request list, element-level microversion) and `identity.request_bindings` (per-entry
  path/query/digest plus an aggregate digest).
* **Capture time**: `capture.at` means only "when these bytes were captured" and carries `at_source`
  (`caller` / `cache_capture_record` / `cache_source_json` / `run` / `unrecorded`) — a replay never
  writes the run time as the capture time, and an unknown value is recorded honestly as `null` plus
  `unrecorded`.
* **Original record and this run are separate**: `identity.snapshot_origin.recorded_capture` reports
  only the fields the original record **actually has** (mode, tool_version, at…; a missing field
  simply does not appear and is never filled from this run), while `identity.this_run` records this
  run's `method`, current `tool_version`, actual run time `at` with `at_source=run` and the request
  verification. `capture` is split into three layers as well: `origin` (the historical record,
  unchanged), `declared` (the caller's explicit choice or addition in `config.capture` plus the
  `reason`) and `effective` (the merged value used by compatibility fields, which never writes back
  to origin). `this_run.transport` is `api` / `cache_replay` / `mixed`: with a mixed cache and API
  only the entries listed in `network_requests` count as this capture, and `this_run.capture` records
  this capture's identity (mode/tool version/time).
* **Dependency closure**: every instance must carry an `elementId` of type `Part`/`Assembly`; a
  missing or unknown value reports `onshape_dependency_incomplete`, because an entire assembly
  subtree could otherwise disappear silently.

A `partId` is unique only **inside a part studio**. Mass-property readings are scoped by
`(element, partId)` on both the generation side and in the oracle, so same-named parts in different
studios keep their own readings and never overwrite each other.

**Geometry and cache file names, however, still key on a global `partId`**
(`geometry/parts/<safe_name(partId)>.stl`, `bytes/stl_<safe_name(partId)>.stl` in the cache), so the
following three cases always fail closed (`onshape_identity_collision`) instead of silently keeping
the last one:

1. the same `partId` appears in two part studios and both have geometry;
2. two `partId`s sanitise to the same name (for example `PART-B` and `PART/B` both becoming
   `PART_B`);
3. the same element is referenced by several configurations in one freeze (cache file names carry no
   configuration).

The cache directory records no configuration, so use a different `source.cache` directory per
configuration; a snapshot's `identity.configuration` only states the configuration this freeze
requested. Same-named elements across documents are equally unprotected and must be frozen into their
own snapshots separately (a complete multi-document naming scheme is out of scope for this layer).
Use a new cache directory after a workspace revision change as well; an existing immutable response
that disagrees with a new request is refused explicitly and never overwrites an old identity.

### Normalisation rules

* **Grouping**: the members listed in the definition are fused into one rigid body; a physical part
  that is not covered, is assigned twice, or references a non-existent entity reports
  `onshape_attribution_mismatch`.
* **Fusion conservation**: masses add, the centre of mass is mass-weighted, and the inertia is
  composed as a full tensor through `Σ(R·I·Rᵀ + m·parallel axis)` and then rotated into the link
  frame; a density assumption scales mass and inertia proportionally by the mass ratio while shape
  and centre of mass stay fixed.
* **Coordinate frames**: in URDF the child link's frame at q=0 **is** the joint frame that drives it,
  so a child link takes the pose of its mate connector in the world frame (directly measurable
  evidence), and the root link takes the reference entity from the definition.
* **Mesh placement**: every member mesh is placed by the relative rigid transform from "member
  instance → link frame", using the same transform as the inertia.
* **Kinematic chain**: a single root, at most one parent per link, no cycles and full connectivity
  are enforced before returning (`onshape_definition_invalid`).
* **No invented defaults**: `effort`/`velocity` and `collisions` stay missing; every joint carries
  `provenance.effort_velocity = "not_defined"` and
  `conventions.collision_policy = {"policy": "undefined"}`.

Current qualification boundaries are in the [validation status](../validation.md).
When the source lacks effort/velocity, rated-parameter evidence must be supplied; filling in 0 or an
engine default to pass the URDF check is not allowed.

## Never guess, never fill in: `provenance.gaps`

| `kind` | Meaning |
|---|---|
| `mass_reading_missing` | The mass properties have no mass for that part (`inertial` stays empty) |
| `inertia_reading_missing` | A mass exists but the inertia or centre-of-mass reading is missing |
| `geometry_missing` | The part has no usable mesh (`visuals` stays empty) |
| `geometry_invalid` | The mesh bytes fail the shared STL acceptance standard; the bytes are kept as evidence but never become a visual |
| `gltf_unavailable` | The glTF endpoint is unavailable and the legacy cached per-part STL was used |
| `gltf` / `gltf_split` / `geometry_unmatched` | glTF missing, the split failed, or a part did not match |
| `joint_limits_missing` | A movable mate has no limits in features |
| `joint_child_unresolved` | The other end of the mate is not a resolvable leaf part |
| `mate_parent_unresolved` | The first mate end is not in the instance tree |
| `mate_frame_unresolved` / `frame_unresolved` | The mate did not solve, so no coordinate frame is available |
| `mate_type_unsupported` | `CYLINDRICAL`/`BALL`/`PLANAR`/`PARALLEL` have no joint semantics yet |
| (suppressed instances no longer record a gap) | See `provenance.non_physical.suppressed`: existence is kept, the model is not |
| `capture_provenance_missing` | The cache has no capture declaration, so it can only be recorded as `imported` |
| `unresolved_occurrences` (a `provenance` field) | An instance path has no matching instance in the response |

## Error codes

| Code | Trigger |
|---|---|
| `onshape_reference_invalid` | Incomplete URL/explicit ids, or both a workspace and a version |
| `onshape_credentials_missing` | API access is needed but no usable credentials exist |
| `onshape_api_error` | HTTP error (`detail.status`; 402 adds a quota hint) |
| `onshape_api_unavailable` | A request in offline mode, or network retries exhausted |
| `onshape_cache_miss` | The cache lacks an entry and no client is available |
| `onshape_snapshot_incomplete` | The destination is not empty, a manifest is missing, the source kind does not match, or the capture declaration is illegal |
| `onshape_snapshot_tampered` | The manifest disagrees with the files (missing, rewritten or extra files) |
| `onshape_scene_invalid` | The derived scene does not satisfy the shared `description.scene/v1` schema |
| `onshape_definition_invalid` | The robot definition is illegal (unknown key, bad identifier, duplicate or dangling link, a non-tree kinematic graph, a non-unique mate selector) |
| `onshape_dependency_incomplete` | An instance lacks an `elementId` or has an unknown type, so the dependency closure cannot be guaranteed |
| `onshape_attribution_mismatch` | The definition disagrees with the snapshot (missing coverage, double assignment, undefined mate) |
| `onshape_evidence_missing` | Normalisation lacks required evidence (raw response, mass reading, volume); an exclusion whose evidence is outside the capture layer or unbound to that entity reports the same |
| `pipeline_shared_helper_missing` | The integration environment lacks the shared snapshot helper or the model schema |

## Authenticity and boundaries

- The repository fixture `tests/fixtures/onshape/cache` (captured 2026-09-17) is for offline
  regression only and is recorded as `fixture` when replayed; a real cache (including 34 part meshes)
  is recorded as `cad`. The two are not interchangeable.
- The adapter does **not** validate physical plausibility (positive-definite inertia, a connected
  kinematic tree, limit intervals); that is the normalisation and quality layer's job. It guarantees
  only that the structure is valid, the numbers come from the source and the gaps are visible.
- Quantities that are not in the source (effort/velocity, collision policy, material density, zero
  position and drive polarity) never get defaults; they are recorded as `not_in_source` or appear in
  `gaps` and must be provided explicitly by the robot definition.
- The `assembly.py` / `linalg.py` / `geometry.py` / `stl.py` modules migrated from the old tool now
  live in this package, no longer modify third-party module globals, and no longer modify
  `sys.argv`.

## Tests

```bash
python -m unittest discover -s tests -t tests -k sources.onshape
```

Coverage: source identity parsing, cache and data channels, error-code mapping, scene derivation
(naming/readings/gaps), the shared schema gate, manifest tamper detection, and endpoint replay plus
reproducibility of the repository fixture; the end-to-end data flow is in
`tests/sources/onshape/test_replay.py`.
