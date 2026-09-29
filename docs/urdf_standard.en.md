# URDF contract and strict quality checks

English · [中文](urdf_standard.md)

A model is checked with `description check --root MODEL --profile PROFILE`, which runs the source,
physical-semantics, consumer and delivery-integrity checks together. This page maintains the
`URDF###` rule numbers used inside those checks and the parameters of the standalone diagnostic tool
`tools/audit.py`. The standalone audit inspects model files only; it never replaces full purpose
acceptance and never grants release qualification.

```sh
description check --root /path/to/model --profile kinematics
# Diagnose an existing URDF separately; the report goes under build/ and changes no delivered bytes
python tools/audit.py --root /path/to/model --policy strict --mujoco --report /path/to/model/build/urdf-audit.json
```

Exit codes: `0` passed; `1` there is an error, or an unwaived warning under `strict`; `2` usage or
parse error.

## Parameters

| Parameter | Effect |
|---|---|
| `--root DIR` | Workspace to check (default: the current directory); from `main` it points at a model branch workspace |
| `--urdf PATH` | URDF to check (default `<root>/urdf/robot.urdf`) |
| `--mjcf PATH` | MJCF to check (default `<root>/mjcf/robot.xml`; consistency is checked only when it exists) |
| `--joint-names PATH` | Structural joint ledger (default `<root>/config/joint_names.yaml`) |
| `--waivers PATH` | Exception ledger (default `<root>/config/urdf_quality.json`) |
| `--policy strict\|advisory` | `strict` (default): errors and unwaived warnings fail; `advisory`: only errors fail |
| `--mujoco` | Additionally compare at the compiled level (mass/centre of mass/inertia/forward kinematics/multi-pose self-contact); requires mujoco |
| `--today YYYY-MM-DD` | Date used to decide whether an exception expired (default: today, so runs are reproducible) |
| `--json` | Print the JSON report only |
| `--report PATH` | Write the JSON report to that path (a standalone diagnostic report; keep it under `build/`) |
| `--verify-report PATH` | Only verify an already committed report: it exists, passed, and matches the current URDF bytes (historical report integrity); with `--json` it prints `{ok, reason, report, summary}` for CI |

## Scope

`description check` selects rules from the purpose and the canonical model. The joint inventory is the
workspace's own `config/joint_names.yaml` when it ships one — a ledger that has drifted from the URDF
fails qualification with `URDF208` — and the joints the model itself declares when it ships none; the
delivered layouts require the file and the standalone audit reports it missing. Massless reference
frames come straight from the model, declared and verified mesh scaling is allowed, a kinematic
purpose does not require collisions, and left/right symmetry must be declared explicitly. A
uniform-density declaration is verified by the full-tensor geometry oracle; `URDF310/311` remain as
historical diagnostics of the standalone audit.
Purpose requirements and the consumer checks are in the [runbook](pipeline.en.md).

## `description check` and the delivery audit

Both tools read the rule tables below, and they differ by selection on purpose: `check` qualifies the
canonical model for one purpose, the strict audit judges the delivered files (add `--mujoco` for the
compiled layer). Every difference below is a place where a workspace can pass `check` and still be
refused by the audit, so it is worth knowing before the refusal happens:

| Aspect | `description check` | `tools/audit.py --policy strict` |
|---|---|---|
| Rule selection | selected from the purpose and the canonical model | the whole set, plus the compiled layer under `--mujoco` |
| Collisions | not required for a kinematic purpose | `URDF407` warning, so strict fails until it is waived |
| `left_*` / `right_*` pairs | checked only when the model declares `mirror_symmetry_required` | always checked — `URDF601`–`URDF603` warnings |
| Uniform density | the full-tensor geometry oracle replaces `URDF310`/`URDF311` | `URDF310`/`URDF311` remain |
| Compiled layer | the pipeline's own MuJoCo consumer checks | `URDF507`, `URDF510`, `URDF511` |

The joint ledger and the mesh scale are **not** in that table: a `config/joint_names.yaml` that is
missing, unreadable or out of step with the URDF is `URDF208` in both tools (`description model init`
writes the template with an empty list, and nothing qualifies until every movable joint is
registered), and a mesh whose `scale` is not 1 is `URDF405` in both — unit conversion belongs to the
export stage. Both published source adapters deliver unit scaling (SolidWorks writes
`scale="1 1 1"`, Onshape omits the attribute entirely), so the strict scale boundary only affects
hand-written meshes.

Where the difference is a **warning** (`URDF407`, `URDF601`–`URDF603`), the workspace-level exception
ledger is the sanctioned bridge: write the code, subject, reason, owner and date, and then both tools
accept the workspace — a run only calls an exception dead (`URDF702`) for a rule it actually
evaluated. An **error** cannot be waived: neither `URDF208` nor `URDF405` can be cleared through the
exception ledger.

## Rule tables

### Structure (URDF1xx)

| Rule | Level | Meaning |
|---|---|---|
| `URDF101` | error | `<robot>` has no name |
| `URDF102` | error | no link at all (templates excepted) |
| `URDF103` / `URDF104` | error | duplicate link / joint names |
| `URDF105` | error | a joint references a parent/child link that does not exist |
| `URDF106` | error | the joint graph is not a single-root tree (number of root links ≠ 1) |
| `URDF107` | error | a cycle, a detached subgraph, or a link with several parent joints |
| `URDF108` | error | illegal joint type |
| `URDF109` | error | a `floating` / `planar` joint (it cannot be re-derived from CAD and is not a single-axis drive) |
| `URDF110` | error | no movable joint at all |

### Joint semantics (URDF2xx)

| Rule | Level | Meaning |
|---|---|---|
| `URDF201` | error | a movable joint lacks effort/velocity; revolute/prismatic also need lower/upper |
| `URDF202` | error | limits are not finite, the bounds are inverted, or effort/velocity is not positive |
| `URDF203` | error | a revolute limit exceeds 2π in absolute value (degrees were probably used as radians) |
| `URDF204` | warning | a continuous joint carries ineffective lower/upper; effort/velocity must still be present |
| `URDF205` | error | a movable joint lacks `<axis>`, or the axis is the zero vector |
| `URDF206` | error | the axis is not normalized (error > 1e-3), which skews dynamics and limit semantics |
| `URDF207` | error | origin/axis contains a non-finite value, or the origin offset exceeds 10 m |
| `URDF208` | error | `config/joint_names.yaml` disagrees with the URDF's movable joints (missing, extra or duplicate entries, or a file that cannot be read as the ledger) |

### Inertia (URDF3xx)

| Rule | Level | Meaning |
|---|---|---|
| `URDF301` | error | a link with meshes lacks `<inertial>` (a massless reference frame must be written into the exception ledger) |
| `URDF302` | error | mass is not positive; a link with geometry must have positive mass |
| `URDF303` | error | mass/centre of mass/inertia contains a non-finite value |
| `URDF304` | error | the inertia is not positive definite or violates the triangle inequality of the principal moments |
| `URDF305` | error | the radius of gyration exceeds the geometric bound (physically impossible: wrong inertia or units) |
| `URDF306` | warning | the radius of gyration is close to the geometric bound (> 0.85×) or too small (< 0.02×) |
| `URDF307` | warning | the centre of mass is outside the link's geometric bounding box (±2 mm tolerance) |
| `URDF308` | warning | the equivalent density is outside the 100–20000 kg/m³ order of magnitude |
| `URDF309` | info/warning | massless reference frame (info); inertia without any geometry (warning — a legal primitive still counts as geometry) |
| `URDF310` | warning | a principal moment differs from the **uniform-density mesh inertia** by more than ~33% (suspicious magnitude, tensor or mass ownership) |
| `URDF311` | warning | the largest principal axis differs from the mesh's principal axis by more than 20° (suspicious mass distribution or coordinate frame) |

The geometric bound is the largest distance from a corner of the geometric bounding box to the
centre of mass; no mass distribution inside that geometry can exceed this radius of gyration, so
`URDF305` is a hard constraint while `URDF306` is a prompt for review.
`URDF310`/`URDF311` compute a "uniform-density rigid body" from the same meshes: it is not the true
value (a mesh may include a servo housing or a shell), but it catches order-of-magnitude errors,
misfilled tensors, misplaced principal axes and wrong mass ownership.

### Geometry (URDF4xx)

| Rule | Level | Meaning |
|---|---|---|
| `URDF401` | error | a mesh uses an absolute path, `package://` or a URL (this repository accepts relative paths only) |
| `URDF402` | error | the mesh file does not exist |
| `URDF403` | error | the mesh cannot be parsed (neither binary nor ASCII STL) |
| `URDF404` | error | the mesh's largest edge exceeds 1e-4–5 m |
| `URDF405` | error | the mesh `scale` is not 1 (unit conversion belongs to the export stage) |
| `URDF406` | warning | more than 1% degenerate (zero-area) triangles |
| `URDF407` | warning | a visual without a collision |
| `URDF408` | info | several mesh files have identical content (content hash) |
| `URDF409` | warning | the mesh is not a closed solid (signed volume ≈ 0), so volume-based checks are unavailable |
| `URDF410` | info | open-edge statistics (T-joints from CAD export; recorded only) |
| `URDF411` | warning | the mesh normals point inwards overall (negative signed volume) |

### MJCF consistency (URDF5xx, checked only when `mjcf/robot.xml` exists)

| Rule | Level | Meaning |
|---|---|---|
| `URDF502` | error | `meshdir` is not a relative path |
| `URDF503` | error | the MJCF references a mesh the URDF does not have |
| `URDF504` | error | an MJCF body has no link of the same name in the URDF |
| `URDF505` | error | the mass of a same-named body differs from the URDF link (> 1e-6 kg) |
| `URDF506` | error | joint limits differ from the URDF (> 1e-5 rad) |
| `URDF507` | error | `--mujoco`: the **compiled** mass differs from the URDF (catches density inference and default inheritance) |
| `URDF508` | error | `--mujoco`: the compiled centre of mass differs from the URDF by more than 1 mm |
| `URDF509` | error | `--mujoco`: the compiled full inertia tensor (including orientation) differs from the URDF |
| `URDF510` | error | `--mujoco` was requested but the consumer/MJCF is missing, or the MJCF cannot be compiled |
| `URDF511` | error | `--mujoco`: a movable joint is missing after compilation, or a body's translation differs by more than 0.1 mm / its orientation by more than 0.04° (prismatic included) |
| `URDF512` | info | `--mujoco`: multi-pose self-contact statistics (the collision policy still needs mechanical or simulation sign-off; not a gate) |

`URDF511`'s two thresholds are calibrated to storage precision: the URDF stores `rpy` and the MJCF
stores `quat`, each with only about six significant digits, so rounding alone is on the order of
0.001°; a real axis or origin misalignment is on the order
of degrees, so the gate uses 0.1 mm and 0.04°. Positions are compared at five sampled poses, and the
orientation comparison also covers poses that coincide in position but differ in direction.

### Left/right mirroring (URDF6xx, only for `left_*`/`right_*` name pairs)

| Rule | Level | Meaning |
|---|---|---|
| `URDF601` | warning | left and right link masses differ by more than 5% |
| `URDF602` | warning | the Frobenius-relative difference of the left/right inertias exceeds 10% |
| `URDF603` | warning | the left/right joint limits differ in shape (different width, or a centre that is neither equal nor opposite) |

Because the left and right axis signs may be opposite, the limit comparison uses the **shape of the
interval** (width and centre) rather than identical `lower`/`upper` values.

### Ledger and exceptions (URDF7xx)

| Rule | Level | Meaning |
|---|---|---|
| `URDF701` | error | the exception ledger is malformed (not JSON, a missing field, an illegal rule number, a non-ISO date) |
| `URDF702` | error | a dead exception: it matches no finding (delete it when the model changes; a rule this run never evaluates — its gate is off, or the purpose does not select it — does not make its exception dead) |
| `URDF704` | error | an attempt to waive an error-level finding (never allowed) |
| `URDF705` | error | the exception expired (`review_after` is before today); review it and update |

## Exception ledger: `config/urdf_quality.json`

```json
{
  "massless_links": ["imu_frame"],
  "waivers": [
    {
      "code": "URDF308",
      "subject": "battery",
      "reason": "the battery mesh is a shell, so an equivalent density does not apply; the 45 g mass comes from the datasheet",
      "owner": "andy",
      "date": "2026-09-18",
      "review_after": "2027-03-18"
    }
  ]
}
```

Discipline: **an error can never be waived** (fix the model, or change the rule and write a test);
every warning needs a reason, an owner and a date, plus an optional review deadline; an exception
that stops matching after a model change is a `URDF702` error, and an expired one is a `URDF705`
error. The ledger therefore cannot rot or turn into silent tolerance. The ledger is a workspace-level
document while a run sees a subset of the rules, so a waiver for a rule the run never evaluated — the
compiled layer without `--mujoco`, or `URDF407` under a kinematic purpose — is not called dead;
otherwise the deliverable audit and `description check` would demand opposite things.

## Acceptance boundary

A passing rule means the model satisfies the checks that ran; it does not prove that CAD materials,
actuator ratings or physical calibration are correct. Source revisions, exclusion reasons,
action/observation mappings, collision scenarios and application/HIL evidence are verified
separately by the [complete check chain](pipeline.en.md).

Full acceptance checks an exact model SHA with the model's pinned tool, and a release fetches the
actual remote bytes again. The optional `model-validation.yml` and `validate.yml` workflows validate
models and the tool respectively. The model release flow is in the
[runbook](pipeline.en.md#submission-acceptance-and-release).

## Maintenance

A new rule requires three things at once: an implementation with a stable number in
`src/description_pipeline/verification/urdf_quality/rules.py`, an entry on this page, and a seeded
counter-example in `tests/test_urdf_quality.py`; `test_every_rule_has_a_seeded_defect` fails when
the counter-example is missing.

Rule applicability for a new profile, the full report binding and the application acceptance
contract are in the [engineering contract](pipeline.en.md).
