# Mechanical input specification

Contract: `solidworks-to-urdf.input/v1`. Units: metres, kilograms, radians,
seconds, newtons and newton-metres. Native capture is qualified for SolidWorks
2026, major revision 34, on Windows with an interactive logged-in desktop.

## Package

```text
arm/r1/
  cad-revision.json
  robot.yaml
  cad/robot.SLDASM
  cad/*.SLDPRT
  cad/**/*.SLDASM
  evidence/limits.txt
  evidence/masses.txt       # when documented masses are used
```

Use Pack and Go or an equivalent complete collection of saved native documents.
All dependencies must resolve inside the package. Select one explicit assembly
configuration. Save mechanical changes before handoff. Capture uses saved file
bytes; an unsaved editor state is not a handoff.

Paths are package-relative POSIX strings in configuration. Unicode CAD names
are supported. Symlinks, junctions, path escapes, case-colliding names, duplicate
YAML keys and unsupported fields are rejected. `robot.yaml` is the single YAML
author configuration. SolidWorks `~$` lock files are excluded from inventories.
Original author files are archived without modification; collected and relinked
CAD lives separately in the delivery's `evidence/source/`.

## CAD preparation

| Requirement | Mechanical responsibility |
|---|---|
| Dependency closure | Include every part and subassembly used by the selected configuration |
| Occurrence identity | Use exact instance paths; a repeated part has distinct occurrences |
| Rigid bodies | Group occurrences that move together; cover every included occurrence once |
| Body datum | Create a unique named coordinate system for every body |
| Joint zero | Place the child-body datum at the physical joint origin in the saved zero pose |
| Shaft reference | Provide a stable native cylindrical face/feature selector for each moving joint |
| Coordinate basis | Right-handed rigid transforms; no scale or reflection |
| Material authority | Assign a physical material to every solid, or provide a complete documented mass table |
| Overrides | Remove effective part/instance mass, COM and inertia overrides unsupported by v1 |
| Limits | Supply traceable position, speed and effort limits with reviewed evidence |

The child body's datum is the joint frame. `axis` defines positive motion in
that datum; the native cylindrical shaft establishes the geometric line.
Cylinder surface direction alone does not define the controller's positive
direction. Numeric XYZ/RPY is derived from CAD and must not be repeated in YAML.

## Robot definition

```yaml
schema_version: solidworks-to-urdf.input/v1
hardware_id: arm
source:
  provider: solidworks
  robot_name: arm
  assembly: cad/robot.SLDASM
  configuration: Default
  material_source: cad
  bodies:
    - id: base
      name: base_link
      components: [base-1]
      frame: {coordinate_system: CS_base}
    - id: arm
      name: arm_link
      components: [arm-1]
      frame: {coordinate_system: CS_arm}
  joints:
    - id: shoulder
      name: shoulder_joint
      type: revolute
      parent: base_link
      child: arm_link
      axis: [0, 0, 1]
      axis_reference:
        component: arm-1
        feature_name: shoulder_shaft
        body_type: solid
      limits: {lower: -1.2, upper: 1.2, effort: 8, velocity: 2}
      limit_evidence:
        file: evidence/limits.txt
        sha256: REPLACE_WITH_FILE_SHA256
        anchor: shoulder_joint
checks:
  expected_mass_kg: [1.0, 1.2]
  expected_extent_m: [0.3, 0.4]
```

The example describes the contract; replace selectors, values and evidence with
the actual mechanical design. Native selector resolution must be unique and
reproducible when the saved CAD copy is reopened.

| Field | Rule |
|---|---|
| `hardware_id`, entity `id` | ASCII identifiers, stable across updates |
| Entity `name` | Unique snake_case; root body is `base_link` |
| `bodies[].components` | Exact native occurrence paths; no duplicate ownership |
| `bodies[].frame.coordinate_system` | Named CAD datum; no authored XYZ/RPY |
| `joints[].parent`, `child` | Body names forming one connected acyclic tree |
| `type` | `fixed`, `revolute`, `continuous`, or `prismatic` |
| `axis` | Signed unit vector in the child datum; absent for fixed joints |
| `axis_reference` | Structured native selector; free text is insufficient |
| `limits` | Finite positive effort/speed; ordered position bounds for revolute/prismatic |
| `limit_evidence` | Archived file, exact digest and existing text anchor |
| `frames[]` | Optional massless named CAD datums attached to a body |
| `checks.expected_mass_kg` | Reviewed positive interval for total used mass |
| `checks.expected_extent_m` | Reviewed positive interval for the largest robot extent at CAD zero |
| `checks.documented_exclusions` | Exact owned occurrence, reason and bound evidence for exceptional geometry |

A fixed joint has no axis, limits or limit evidence. A continuous joint has
effort/speed limits and evidence, but no position bounds. A single rigid body
uses `joints: []`. Closed loops, mimic, actuator/sensor extensions and automatic
collision conversion are outside v1.

## Physical authority

`material_source: cad` requires explicit per-solid material assignment evidence.
Positive CAD density or plausible mass alone is insufficient.

`material_source: documented_table` requires `documented_masses` for every
occurrence, with positive `mass_kg`, a reason and an evidence anchor. The shared
`mass_evidence` supplies `reference`, package-relative `file`, SHA-256 and `note`.
Its file must contain each anchor. No occurrence falls back to default density.

Documented masses scale the CAD part tensor under a uniform-density assumption
and preserve that assumption in provenance. This is not measured inertia; it
does not establish simulation or hardware accuracy. Native assembly closure
and unsupported override checks still apply to the underlying CAD readings.

## Mechanical version management

Retain each handoff as an immutable directory or immutable Git/PDM revision.
`cad-revision.json` binds the exact inventory of native CAD files to:

- hardware and revision IDs;
- previous published revision ID;
- mechanical owner and change summary;
- source-control system and retained revision reference.

Seal it with `description revision`; never hand-edit its inventory. Git control
requires a full commit hash. PDM and handoff references identify retained source
records; the pipeline records them but does not query their servers.

Changed CAD requires a new revision ID and the previous published revision as
`--parent`. Reusing an ID with changed CAD is rejected. Robot-definition-only
changes may use the same CAD revision, since YAML is separately bound in every
delivery. Sealing refuses overwrite and is idempotent for identical content.

The mechanical reviewer must approve body membership, datum meanings, axis
sign, limit authority, material correctness and expected mass/size intervals.
Automation proves consistency with those inputs; it cannot infer their design
intent or replace physical measurements.
