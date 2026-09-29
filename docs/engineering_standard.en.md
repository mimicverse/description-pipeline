# Implementation constraints

English · [中文](engineering_standard.md)

Design principles and flow are in the [design document](design.en.md); input formats and run steps are
in the [runbook](pipeline.en.md). This page is for code review.

| Boundary | Requirement |
|---|---|
| Source adapter | Keep the raw readings, revision, configuration, complete dependencies and a stable identity; handle source-specific semantics without growing a second consumer generator. |
| Canonical model | Use SI, and state the transform direction, the inertia reference point and the frame it is expressed in; verify entity ownership, references and tree structure. Fixed-body fusion preserves mass, centre of mass and the full tensor. |
| Format generation | URDF, MJCF, meshes and configuration come from the same model; a constraint whose semantics cannot be preserved blocks explicitly. Visuals, collisions and mass properties are handled separately. |
| Independent verification | Recompute the baseline from the raw evidence, read the artifacts back and check the actual consumer. List the expected, checked and missing objects; unknown, unrun and failed never pass. |
| Build and release | Lock the inputs and the environment, generate a complete candidate before replacing a delivery, and keep diagnostics on interruption. A cache hit is still verified, and a release fetches the same SHA from the remote and qualifies it independently. |
| Maintenance cost | One public package, one CLI; reuse generic rules and keep the independent physical verification. One Windows or Linux machine completes the author flow, and release does not depend on GitHub Actions. |

A capability claim for a model or a physical purpose needs matching evidence. Fixture regression, a
successful installation, CAD being readable, a complete native freeze, simulation and hardware
acceptance are recorded separately and never substitute for one another. Current limits are
collected in the [validation status](validation.md).
