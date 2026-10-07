# Quality specification

Report: `solidworks-to-urdf.quality/v1`. Pipeline ID: `solidworks-to-urdf`.
All required gates must pass before automatic publication. Missing required evidence is
a failure. There is no public bypass or waiver switch.

Native SolidWorks engineering is the input. Generated YAML is an internal
artifact and must be verified against original native observations.

## Verification gates

Implementation paths below are relative to `src/description_pipeline/`.

| Gate | Evidence and acceptance | Implementation |
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

Captured transforms use the native adapter protocol: SI, row-major 4×4
homogeneous matrices with column-vector multiplication. The adapter converts
SolidWorks' vendor MathTransform layout at the COM boundary. Verification reads
the captured protocol directly and derives zero-pose FK from the native root
datum, independently of model provenance.

## Report identity

The subject digest binds every file under `input/`, `evidence/`, `model/`,
`urdf/`, `meshes/`, plus `README.md`, `reports/input.json` and
`reports/tool.json`. The quality report refers to that subject and cannot hash
itself. Local run/PR receipts are also outside the subject and are not published
as model evidence.

`description check` recomputes every gate and compares the entire deterministic
report. A stale green flag, incomplete check set or changed file cannot qualify.
Publication repeats this after copying and against the actual committed Git
blobs, including any effects of newline conversion or clean filters.

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
exercises. It does not qualify M3 or another hardware model; each model needs
its own reviewed inputs, passing delivery and evidence for its intended use.

## Native definition verification

The mechanical team supplies only SolidWorks engineering contents under the
[mechanical specification](mechanical-handoff-spec.md). The pipeline
generates definitions, manifests and evidence, and records a source for every
derived mechanical fact.

Independent verification checks body membership, joint adjacency/type,
signed axes, zero configuration, datum placement and limits against original
native observations and engineering annotations. Merely validating generated
YAML against a schema or comparing two derivatives does not prove these facts.

Drive and physical specifications must resolve to fixed controlled records or
qualified native engineering properties. Missing, conflicting or ambiguous
facts block the delivery/use that requires them; no fabricated limits, default
efforts or guessed directions are acceptable. Source and library revisions,
original units, transformations and provenance remain bound to the delivery.

The per-item report distinguishes automatic results from engineering
confirmations as specified in [the mechanical standard](mechanical-handoff-spec.md#112-检查报告与-airflow-展示).
Each confirmation applies to the recorded structural version; missing,
unsupported or unexecuted checks cannot become implicit passes.

Effective native mass, COM or inertia overrides are rejected. A controlled
parameter source must satisfy the complete physical contract; recording a
source does not waive verification.
