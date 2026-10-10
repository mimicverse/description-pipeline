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
| Engineering review | `not_ready` before automatic verification completes, then `external_review`: only facts outside automatic coverage; the portal does not read or infer approval status from the matching PR or controlled records |

`quality.json` records the required gate inventory and every executed or unexecuted
gate. `stages.json` records boundary results, inputs/outputs, file hashes and
responsibility references. Airflow task logs retain detailed terminal results,
including failures; XCom retains the compact stage summary. Automatic publication
creates a candidate PR and cannot set an engineering-approved release state.
Passed automatic checks do not require manual repetition. Failed checks require
correction, not human override. Applicable approvals of unchanged engineering
facts can be reused; changed facts and their effects need review.
Each receipt declares its `execution_scope`: a complete endpoint job covers all
six stages, while maintenance generation/verification runs cover only the stages
they execute ([operations](operations.md#6-independent-review-and-recovery));
unexecuted native stages are shown as out of scope rather than qualified.

## Verification gates

Implementation paths below are relative to `src/description_pipeline/`.

| Gate | Independent evidence and acceptance | Implementation |
|---|---|---|
| Derived-input contract | Reinspect generated YAML, native provenance, controlled records, inventories and generated revision | `sources/solidworks/input.py`, `revision.py` |
| Native definition | Independently reconstruct membership, adjacency, motion, names, frames and limits from native observations; required gate `source.native_discovery` | `verification/native_discovery.py` |
| Native source | Native SolidWorks evidence, qualified API/build, matching collection identity and environment | `verification/solidworks_urdf.py` |
| Dependency closure | No unresolved reference; original CAD belongs to the handoff; collected-copy hashes and configuration agree | `verification/solidworks_urdf.py` |
| Occurrence coverage | Every native occurrence belongs to exactly one body; identity survives normalization | `verification/solidworks_urdf.py` |
| Coordinates | Finite SI homogeneous transforms, orthonormal right-handed bases, CAD-derived body/joint/reference frames | `verification/solidworks_urdf.py` |
| Shaft alignment | Numeric cylinder line, stable native identity, origin on shaft and axis collinearity | `verification/solidworks_urdf.py` |
| Names and topology | Unique snake_case names; exactly one `base_link`; connected acyclic tree; input/model/XML entity agreement | `verification/solidworks_urdf.py` |
| Joint semantics | Exact type, parent/child, signed axis and limits; evidence bound to archived inputs | `verification/solidworks_urdf.py` |
| Physical authority | Explicit material coverage or complete documented masses; no implicit density or unknown convention | `verification/solidworks_physics.py` |
| Mass, COM and tensor | Independently rotate/translate raw part readings, apply the parallel-axis theorem and convert to each CAD link datum | `verification/solidworks_physics.py` |
| Assembly closure | Independent part sum agrees with full whole-assembly mass/COM/tensor and complete component-context readings | `verification/solidworks_physics.py` |
| Whole-CAD mass equality | Delivered URDF XML inertial mass sum equals the bound full whole-assembly reading; missing or invalid whole evidence fails; masses are never normalized | `verification/solidworks_physics.py` |
| Inertia validity | Finite positive mass; positive principal inertia; triangle inequality; XML full tensor agrees with raw-verified model | `verification/solidworks_urdf.py` |
| Geometry | Native mesh coverage, exact file hashes, SI scale, finite nonempty triangles, CAD placement of actual vertices | `verification/solidworks_urdf.py`, `geometry/stl.py` |
| Physical plausibility | COM within body bounds; inertia within geometry radius bound; total mass and largest whole-robot extent within declared intervals | `verification/solidworks_urdf.py` |
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
readings use assembly-document axes. Missing declarations or effective
mass/COM/inertia overrides block publication.

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
`reports/tool.json`. The quality report refers to that subject and cannot hash
itself. Local run/PR receipts are also outside the subject and are not published
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

## Scope of acceptance

A pass establishes the listed consistency, physical arithmetic, geometry and
URDF-loading checks for its exact files. It does not prove actual CAD rigidity
or degrees of freedom, physical zero or positive motion, real mechanical limits,
drive capability, material correctness or full-range clearance. Those facts
require engineering confirmation. Simulation, training and control require
their own acceptance evidence.

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

Capture requires a rereadable cylindrical interface for each motion axis.
Named axes alone do not qualify this capture path. Owned `CS_<link>` datums
identify body frames and `CS_base_link` the root; an optional JCS must match its
child body frame and owner. Native `axis_sign` supplies the confirmed positive
direction. Installation, TCP and sensor datums retain their interface names and
owners.

Independent verification reconstructs membership, joint adjacency/type, signed
axes, zero configuration, datum placement and limits from native observations
and engineering annotations. Comparing generated YAML with a model does not
establish these facts. Controlled physical and drive records must be fixed,
retrievable and applicable to the engineering revision.

Effective native mass, COM and inertia overrides are rejected. Complete physical
source records remain subject to independent checks. The
[per-item responsibility table](mechanical-handoff-spec.md#111-自动检查与工程师确认)
separates automatic coverage from engineer-confirmed physical meaning.
