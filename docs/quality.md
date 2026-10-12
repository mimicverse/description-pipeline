# Quality specification

Report: `solidworks-to-urdf.quality/v1`. Pipeline ID: `solidworks-to-urdf`.
All required gates must pass before automatic publication. Missing required evidence is
a failure. There is no public bypass or waiver switch.

Native SolidWorks engineering is the input. Generated YAML is an internal
artifact and must be verified against original native observations.

## Step and result contract

The [six-step architecture](design.md#2-how-the-system-works) owns the execution
boundaries. Each step records input QC and output QC; generation-input inspection
belongs to capture. Independent verification repeats the engineering checks against
frozen evidence and actual files rather than trusting earlier success flags.

| Result | States and meaning |
|---|---|
| Step | `not_run`, `running`, `completed`, `failed`, `blocked`; completed requires every boundary check to pass |
| Automatic check | `passed`, `failed`, `not_run`; missing prerequisites leave a required check unexecuted and prevent qualification |
| Unsupported check | `unsupported`, with its scope and responsible stage stated; it is never an automatic pass |
| Stage warning | `warning`: a nonblocking observation with its affected objects and qualification impact. It never substitutes for a required check. An unspecified optional budget needs no manual confirmation. |
| Engineering review | `not_ready` before automatic verification completes, then `external_review`: only facts outside automatic coverage; the portal does not read or infer approval status from the matching PR or controlled records |

`quality.json` records the required gate inventory and every executed or unexecuted
gate. `stages.json` records boundary results, inputs/outputs, file hashes and
responsibility references. Airflow task logs retain detailed terminal results,
including failures; XCom retains the compact stage summary. Automatic publication
creates a candidate PR and cannot set an engineering-approved release state.
Passed automatic checks do not require manual repetition. Failed checks require
correction, not human override. Applicable approvals of unchanged engineering
facts can be reused; changed facts and their effects need review.
Each receipt declares its `execution_scope`: Windows covers the first three
stages, including ingestion and derivation of the saved exporter declaration,
and runs no URDF generation, MuJoCo verification or Git operations; Linux
covers generation, verification and publication, including an independent
re-decode of the sealed declaration. A complete
Airflow run binds both; maintenance runs cover only the stages they execute ([operations](operations.md#6-independent-review-and-recovery));
unexecuted native stages are shown as out of scope rather than qualified.

## Capture transfer

The Windows worker seals archived inputs, native evidence, the input report,
native tool identity and native stage receipt. Linux admits this bundle only
when its closed inventory, every file hash, run ID, handoff digest and selected
assembly agree with the request. Both hosts must use the same tool source release;
their dependency records identify their different execution roles.

The sealed native bytes are the captured inputs: readings, configuration and
meshes travel as captured (`raw/scene_raw.json`, `raw/source_config.json`,
evidence and mesh files). Scene construction, closure comparison math, mesh
analysis and the structural-team report all run on Linux; the Windows worker
only performs the native reads and geometry export plus identity, serialization
and transfer — it renders no report and stores no derived scene.

When present, the saved exporter declaration payload and its PID resolution rows
travel with the sealed bundle. The declared structure is derived from the raw
native record during Linux derivation; Linux verification re-decodes the sealed
bytes independently and checks the declared topology and native geometry. Saved-pose mate restrictions
remain diagnostics; conflicting authored operating limits still fail. Authored limits are preserved verbatim: a
zero-width range or a zero effort/velocity is a stage warning that is kept for
capture and generation and never qualifies motion. Stored meshes, inertias and
non-customized origins are regenerated from native CAD and are never adopted.

`reports/native-stages.json` preserves the native receipt. The final
`reports/stages.json` records the full run. Native completion does not grant
model qualification. Linux still independently reconstructs engineering facts,
checks the delivered bytes and loads the URDF with MuJoCo before publication.

## Verification gates

Implementation paths below are relative to `src/description_pipeline/`.

| Gate | Independent evidence and acceptance | Implementation |
|---|---|---|
| Derived-input contract | Reinspect generated YAML, native provenance, controlled records, inventories and generated revision | `sources/solidworks/input.py`, `revision.py` |
| Native definition | Independently reconstruct membership, adjacency, motion, names, frames and limits from native observations; required gate `source.native_discovery` | `verification/native_discovery.py` |
| Declared native facts | Exporter declaration ingested at discovery and independently re-decoded at verification; declared membership, topology, frames, axes and limits reconciled with native observations and with the generated source (`discovery.declarations`); contradictions fail closed | `sources/solidworks/exporter.py`, `verification/v14_declarations.py`, `verification/native_declarations.py` |
| Native source | Native SolidWorks evidence, qualified API/build, matching collection identity and environment | `verification/solidworks_urdf.py` |
| Dependency closure | No unresolved reference: the full uploaded folder is staged, the native engine resolves its references, and the closure seals only when every engine-reported binding lies inside the staged root and matches the upload index — or is an engine-materialized import admitted only through the sealed owner/occurrence evidence matched to its staged source by native identity (never by temporary path or file name); a missing or outside-stage reference fails the run after resolution (closure admission over reported bindings, not an OS sandbox); original CAD belongs to the sealed selection; collected-copy hashes and configuration agree | `verification/solidworks_urdf.py` |
| Occurrence coverage | Every included native occurrence belongs to exactly one body; unowned occurrences and missing explicit material assignments are stage warnings for engineering inspection (material warnings retain the measured CAD mass/centre-of-mass/inertia and never claim verified material); exact whole-CAD mass equality remains a strict check - instances or their mass are never excluded to clear a warning; identity survives normalization | `verification/solidworks_urdf.py` |
| Coordinates | Finite SI homogeneous transforms, orthonormal right-handed bases, CAD-derived body/joint/reference frames | `verification/solidworks_urdf.py` |
| Shaft alignment | Numeric shaft line from the independently re-read reference axis or cylindrical interface, stable native identity, origin on shaft and axis collinearity | `verification/solidworks_urdf.py` |
| Names and topology | Unique snake_case names; exactly one `base_link`; connected acyclic tree; input/model/XML entity agreement | `verification/solidworks_urdf.py` |
| Joint semantics | Exact type, parent/child, signed axis and limits; evidence bound to archived inputs | `verification/solidworks_urdf.py` |
| Physical authority | Native numerical consistency for the recorded CAD values; missing explicit material coverage is warning-level with the measured values preserved and no verified-material claim — real material, density and mass boundaries remain external engineering confirmation; no implicit density or unknown convention is ever evidence | `verification/solidworks_physics.py` |
| Mass, COM and tensor | Independently rotate/translate raw part readings, apply the parallel-axis theorem and convert to each CAD link datum | `verification/solidworks_physics.py` |
| Assembly closure | Independent part sum agrees with full whole-assembly mass/COM/tensor and complete component-context readings | `verification/solidworks_physics.py` |
| Whole-CAD mass equality | Delivered URDF XML inertial mass sum equals the bound full whole-assembly reading; missing or invalid whole evidence fails; masses are never normalized | `verification/solidworks_physics.py` |
| Inertia validity | Finite positive mass; positive principal inertia; triangle inequality; XML full tensor agrees with raw-verified model | `verification/solidworks_urdf.py` |
| Geometry | Native mesh coverage, exact file hashes, SI scale, finite nonempty triangles, CAD placement of actual vertices | `verification/solidworks_urdf.py`, `geometry/stl.py` |
| Physical plausibility | COM within body bounds; inertia within geometry radius bound; total mass and largest whole-robot extent within declared intervals when a design budget is supplied; a missing budget applies no acceptance intervals and is never manufactured from measured values, while an invalid supplied record fails | `verification/solidworks_urdf.py` |
| Consumer loading | MuJoCo loads delivered URDF and meshes and retains expected bodies and movable joints | `verification/solidworks_urdf.py` |
| Report binding | Recomputed deterministic report equals the saved report and bound file subject | `delivery.py`, `verification/solidworks_urdf.py` |
| Publication | Reverify copied output and actual Git blob bytes; revision succession; fast-forward push; exact PR head/base | `repository/urdf_pr.py` |

## Numerical contract

| Quantity | Acceptance tolerance |
|---|---|
| Joint/frame position and radial shaft offset | 0.05 mm |
| Frame rotation and shaft line angle | 0.05 degrees |
| Signed XML/declared axis | Absolute component difference ≤ 1e-12 |
| Unit joint axis | Norm error ≤ 1e-9 |
| Independent mass | Relative 1e-6, absolute 1e-12 kg |
| Whole-CAD mass equality | Absolute 1e-12 kg, relative 0 |
| Independent COM | Absolute 0.05 mm per component |
| Independent full tensor | Max-entry error ≤ 1e-4 of tensor scale + 1e-15 kg·m² |
| XML/canonical full tensor | Relative 1e-10, absolute 1e-15 kg·m² |
| COM/body bounds | 0.1 mm allowance for tessellation |
| Inertia/radius bound | 1% allowance for tessellation |
| Degenerate triangles | At most 0.1% of a mesh's triangles |
| Near-zero mesh volume | Requires a documented exceptional-geometry occurrence |

Raw part tensors must declare the measured standard signed convention for
`IMassProperty2.GetMomentOfInertia(0)`. Cross terms are retained. Unqualified
fallback arrays are rejected. A native analytic fixture verifies nonzero cross
terms, component rotation and assembly aggregation; diagonal-only examples do
not establish the convention.

Each reading declares its scope, axes, COM reference, SI-unit setting and
override flags. Per-part readings use part-document axes; whole-assembly
readings use assembly-document axes. Missing declarations block publication. A
CAD-authored effective instance — an overridden instance, or a surface-only
component whose part document cannot answer mass questions — is admitted as a
non-blocking warning that keeps its exact scope, axes and override flags: a
parent override replaces its subtree only within one rigid body and is never
distributed to moving links, the whole mass/COM/inertia closure must still hold
exactly, and the report states plainly that the value is CAD-authored, not
geometry-derived and not independently physically qualified.
A flagless leaf instance whose assembly-context measurement differs from its
part-document measurement is reported the same way, with both measurements kept
verbatim and the difference recorded as basis `cad_assembly_instance_measurement`
with an unproven cause — never rescaled, substituted or silently qualified.
The derived selection accounting is projected as warnings: every unassigned
occurrence is reported with its native recorded mass, and the native
whole-vs-selected mass gap is reported as a gap with its qualification withheld
— never as missing input bytes or a pipeline failure. An accounting residual
outside the 1e-12 kg equality tolerance, or an invalid selection record, fails closed.
A sheet face that the native tessellation returns with zero triangles and zero
facets while the native record still measures a positive area is reported as the
non-blocking warning `cad_sheet_face_untessellated` (per component entry,
naming the component and face), stating the native area measurement as recorded
and the absence of facets; the report never calls it zero-area, and the
completeness and usability of the surface geometry remain independently
unqualified. All other native triangles are retained unchanged; malformed
geometry metadata fails closed.

Native capture rebuilds the collected read-only assembly in its owned session
before measurements. This refreshes assembly caches, including mass properties;
it never saves over CAD. The snapshot records the rebuild and verifies both
original and collected file hashes after capture. A failed rebuild blocks export.
Original configuration and save-state observations are initial evidence;
the retired source is not reread. Live configuration, occurrence and geometry
guards apply to the collected copy that supplies all measurements.

Datums and mass are read in each occurrence's exact referenced configuration.
Temporary document selections are restored and verified. Meshes use the
occurrence's solid and surface bodies in component coordinates, without switching
the shared part document's configuration. Mesh and shaft reads acquire current
occurrences from the recorded assembly and verify their full names, paths and
referenced configurations. One assembly traversal owns the complete mesh batch;
its parent interfaces remain alive through all body and face reads. Mesh destinations
are unique and ordered by full occurrence identity. An unreadable body, invalid face
triangles or a body with no display mesh blocks capture. Final checks verify
the document state through each recorded path in the owned session, and compare
a fresh assembly traversal with the captured active occurrences. Changed
names, paths, references or suppression block capture. Unreadable state retains
the affected document and occurrence in diagnostics.
Environment and owned-session identities are saved before native readings, so
reading and geometry failures retain those facts with the partial capture.
Suppressed datums are excluded. Unreadable suppression state or an unreadable
active datum blocks discovery and capture; active frames cannot disappear
silently. An unreadable selection or failed restoration blocks capture.

Captured transforms use the native adapter protocol: SI, row-major 4×4
homogeneous matrices with column-vector multiplication. The adapter converts
SolidWorks' vendor MathTransform layout at the COM boundary. Verification reads
the captured protocol directly and derives zero-pose FK from the native root
datum, independently of model provenance.

Assembly reference geometry is recorded as a frame, never a physical part.
Top-assembly datums may belong to the base body only when all assembly-frame
mates resolve to one rigid cluster, their combined constraints have rank six,
and that cluster owns `CS_base_link`. The generator and verifier derive this
attachment separately from the raw observations. Partial, ambiguous and nested
assembly-frame attachments block derivation with the affected mate and scope;
no world joint, mass or rigid membership is inferred from an `IsFixed` flag.

## Report identity

The subject digest binds every file under `input/`, `evidence/`, `model/`,
`urdf/`, `meshes/`, plus `README.md`, `reports/input.json` and
`reports/tool.json`. Transferred captures also bind `reports/native-tool.json`,
`reports/native-stages.json` and `transfer-manifest.json`; a partial set fails.
Independent verification rechecks the manifest and native provenance from the
delivered files. The quality report refers to that subject and cannot hash itself. Local run/PR receipts are also outside the subject and are not published
as model evidence. `reports/stages.json` is also a run receipt: it records
the subject and contract hashes, and its own file hash is retained in
`reports/run.json`. It stays outside the subject to avoid circular hashes and
post-publication changes to model evidence.

`description check` recomputes every gate and compares the entire deterministic
report. A stale green flag, incomplete check set or changed file cannot qualify.
Publication repeats this after copying and against the actual committed Git
blobs. The publisher generates `.gitattributes` to preserve delivery bytes through
Git storage and checkout, and records its hash in the publication receipt.
Effective newline, filter or encoding overrides block publication; the native
subject remains unchanged.

A retained failed-verification checkpoint may be served as an explicitly
unverified diagnostic preview carrying its failing checks and warnings; it never
asserts verification and is forbidden from publication. Generation success alone
is not verification.

## Scope of acceptance

A pass establishes the listed consistency, physical arithmetic, geometry and
URDF-loading checks for its exact files. It does not prove actual CAD rigidity
or degrees of freedom, physical zero or positive motion, real mechanical limits,
drive capability, material correctness or full-range clearance. Those facts
require engineering confirmation. Simulation, training and control require
their own acceptance evidence.

Unassigned rigid ownership, explicit zero parameters and missing explicit
material assignments are stage warnings so generation can continue; material
warnings retain the measured CAD mass/centre-of-mass/inertia and never claim
verified material; exact whole-CAD mass equality and usable motion remain
strict independent qualification checks, and conflicting comparable authored
operating limits still fail.

Administrative metadata (hardware ID, revision, owner, control record) is a
publication-readiness requirement, not a measurement blocker: capture,
generation and inspection continue when it is absent, the raw record keeps the
absence, and no revision, owner, hardware ID or neutral technical name is
invented to fill it.

Stage warnings never substitute for acceptance: they stay visible with their
objects, and the warned facts (material coverage, usable ranges or drives) are
not verified until the corresponding independent checks pass; a material
warning must never be reported as verified material, and numeric evidence that
is unreadable, non-positive or non-physical still fails closed. Missing
required evidence remains a failure.

Source integrity is required before evidence can be used. Every imported CAD
occurrence must bind to a file in the frozen handoff — or, for an
engine-materialized import, to the sealed closure's owner/occurrence evidence
matched to that staged source by native identity. A temporary document or
visible cached geometry without that sealed binding, and any external source,
remain errors, with the affected occurrences and reference paths retained in
diagnostics.

Mechanical review owns input meaning and measured authority. v1 exports visual
meshes and physical properties; collision simplification, contact behavior,
actuator/control interfaces and dynamic validation require separate acceptance.
The [mechanical specification](mechanical-handoff-spec.md) assigns each handoff
check to its automatic coverage and required engineering confirmation.
Unit tests with mocked CAD cannot grant native qualification. The release review
must include actual Windows capture and a complete passing delivery.

Acceptance on the neutral analytic fixture qualifies the tool behaviors it
exercises. It does not qualify a specific hardware model; each model needs
its own reviewed inputs, passing delivery and evidence for its intended use.

## Native definition verification

The [mechanical specification](mechanical-handoff-spec.md) owns the handoff
requirements, naming and engineering confirmations. Definitions, manifests and
reports are generated; every derived mechanical fact keeps its source.

Native admission reconstructs supported coincident, concentric, distance,
parallel, perpendicular, angle and lock constraints, including position limits.
Their combined six-dimensional constraint space must establish rigid membership
or one supported relative motion. Temporary fixed flags and mate names cannot
establish motion. Unresolved entities, unsupported constraints, ambiguous motion
and non-tree topology block derivation.

Constraint reconstruction accepts concentric circular edges and cylindrical
faces when their recorded axes are coaxial in the assembly frame. A circular
edge coincident with a plane must lie in that plane, with aligned normals.
Both entities require usable geometry; a missing entity cannot supply a
constraint. Supporting an edge for mate reconstruction does not qualify it as
the independently reread cylindrical interface required for a joint axis.

Capture requires a rereadable native axis reference for each motion axis: a
named reference axis bound by the saved declaration's axis name, or a rereadable
cylindrical interface on the reconstruction path. Serialized axis numbers alone
never qualify. Explicit declarations retain their exact named frames and signed
axes. On the inferred path, owned `CS_<link>` datums
identify body frames and `CS_base_link` the root; an optional JCS must match its
child body frame and owner. Native `axis_sign` supplies the confirmed positive
direction. Installation, TCP and sensor datums retain their interface names and
owners.

Independent verification reconstructs membership, joint adjacency/type, signed
axes, zero configuration, datum placement and limits from native observations
and engineering annotations. Comparing generated YAML with a model does not
establish these facts. Controlled physical and drive records must be fixed,
retrievable and applicable to the engineering revision.

Effective native mass, COM and inertia overrides are carried as qualified
warnings, not rejections: their scope, axes and override flags are recorded, a
parent override replaces its subtree only within one rigid body without
distribution to moving links, and the whole mass/COM/inertia closure is checked
strictly. No physical accuracy is claimed; complete physical source records
remain subject to independent checks. The
[per-item responsibility table](mechanical-handoff-spec.md#111-自动检查与工程师确认)
separates automatic coverage from engineer-confirmed physical meaning.
