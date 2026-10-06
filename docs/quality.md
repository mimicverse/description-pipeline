# Quality specification

Report: `solidworks-to-urdf.quality/v1`. Pipeline ID: `solidworks-to-urdf`.
All required gates must pass before automatic publication. Missing evidence is
a failure. There is no public bypass or waiver switch.

## Required checks

| Gate | Evidence and acceptance | Implementation |
|---|---|---|
| Input contract | Reinspect archived YAML, evidence anchors, inventories and sealed revision | `sources/solidworks/input.py`, `revision.py` |
| Native source | Native SolidWorks evidence, qualified API/build, matching collection identity and environment | `verification/solidworks_urdf.py` |
| Dependency closure | No unresolved reference; original CAD belongs to the handoff; collected-copy hashes and configuration agree | `verification/solidworks_urdf.py` |
| Occurrence coverage | Every native occurrence belongs to exactly one body; identity survives normalization | `verification/solidworks_urdf.py` |
| Coordinates | Finite SI homogeneous transforms, orthonormal right-handed bases, CAD-derived body/joint/reference frames | `verification/solidworks_urdf.py` |
| Shaft alignment | Numeric cylinder line, stable native identity, origin on shaft and axis collinearity | `verification/solidworks_urdf.py` |
| Names and topology | Unique snake_case names; exactly one `base_link`; connected acyclic tree; author/model/XML entity agreement | `verification/solidworks_urdf.py` |
| Joint semantics | Exact type, parent/child, signed axis and limits; evidence bound to archived inputs | `verification/solidworks_urdf.py` |
| Physical authority | Explicit material coverage or complete documented masses; no implicit density or unknown convention | `verification/solidworks_physics.py` |
| Mass, COM and tensor | Independently rotate/translate raw part readings, apply the parallel-axis theorem and convert to each CAD link datum | `verification/solidworks_physics.py` |
| Assembly closure | Independent part sum agrees with full whole-assembly mass/COM/tensor and complete component-context readings | `verification/solidworks_physics.py` |
| Inertia validity | Finite positive mass; positive principal inertia; triangle inequality; XML full tensor agrees with raw-verified model | `verification/solidworks_urdf.py` |
| Geometry | Native mesh coverage, exact file hashes, SI scale, finite nonempty triangles, CAD placement of actual vertices | `verification/solidworks_urdf.py`, `geometry/stl.py` |
| Physical plausibility | COM within body bounds; inertia within geometry radius bound; total mass and whole-robot extent within author intervals | `verification/solidworks_urdf.py` |
| Consumer loading | MuJoCo loads delivered URDF and meshes and retains expected bodies and movable joints | `verification/solidworks_urdf.py` |
| Report binding | Recomputed deterministic report equals the saved report and bound file subject | `delivery.py`, `verification/solidworks_urdf.py` |
| Publication | Reverify copied output and actual Git blob bytes; revision succession; fast-forward push; exact PR head/base | `repository/urdf_pr.py` |

## Numerical contract

| Quantity | Acceptance tolerance |
|---|---|
| Joint/frame position and shaft offset | 0.05 mm |
| Frame rotation and shaft line angle | 0.05 degrees |
| Signed XML/author axis | Absolute component difference ≤ 1e-12 |
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
URDF-loading checks for its exact files. It does not prove material assignment
matches real hardware, free-form evidence states correct limits, every motion
is collision-free, or the model is ready for simulation, training or control.

Mechanical review owns input meaning and measured authority. v1 exports visual
meshes and physical properties; collision simplification, contact behavior,
actuator/control interfaces and dynamic validation require separate acceptance.
Unit tests with mocked CAD cannot grant native qualification. The release review
must include actual Windows capture and a complete passing delivery.
