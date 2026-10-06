# Changelog

## [Unreleased]

### Added

- Operator-selected mechanical kinematics acceptance. `--mechanical-reference` selects an approved
  held-out JSON outside the model workspace; the pipeline compares rigid ownership, mechanical
  drive identity, signed joints, limits, couplings and independent poses against the actual URDF
  and both MuJoCo entries. Update can accept, rebuild and submit in one command; validation and
  promotion independently select and replay the same reference. Linux and Windows launchers support
  the flow. This qualifies kinematics against the reference only; no physical, training or hardware
  claims follow. `interfaces.mechanical_drives` records mechanical identity and mounting without
  inventing simulation actuator parameters.

- A capture records the assembly's per-component context masses in `raw/mass_closure.json`
  (`component_context`): every component instance's assembly-context mass and its three override
  flags next to the part-document basis, with disjoint depth-0 totals, per-row errors and leaf
  coverage. Nested rows exist to detect overrides a clean parent would hide; nothing is distributed
  or forced — the documented table stays the author's. `description check` reports the two bases as
  a **note** for a `documented_table` model, and fails a `cad` model when any instance override
  (mass, COM or inertia) is recorded, when the full ancestor closure derived from the scene leaves
  is not covered exactly, or when a node's effective mass disagrees with its selected part-document
  reading beyond tolerance; the context being unavailable/incomplete fails `cad` as well. Older
  snapshots without the record keep their verdict. Displayed bases and totals are recomputed from the
  raw readings, and a cached basis that contradicts them is rejected. The `require_material=False`
  fallback also keeps the full material-assignment detail the CAD error carried, instead of reducing
  it to a code and a message.

- Published source-to-URDF workflows carry stable `pipeline_id` identities
  (`solidworks-to-urdf`, `onshape-to-urdf`, `fixture-to-urdf`) with a shared catalog
  (`description pipeline list` and `description pipeline show`). `config/robot.yaml` may declare `pipeline_id`;
  `sources/source.lock.json` records the resolved identity
  `{schema_version, id, declared, resolved_from, source_kind}`, legacy locks stay valid only for
  undeclared definitions, and declaring or changing an id requires a re-freeze. `manifest.json`,
  `docs/quality.json` and the update/pull-request/validate/promotion outputs carry and compare the
  identity. A unique `run_id` is written only to the CLI output and the ignored
  `build/runs/<run_id>.json`, never to the subject, manifest, quality report or snapshot.

### Changed

- The mass-closure advisory states the two readings and that no cause is inferred, instead of
  speculating that the CAD tree was repaired or re-materialed.

### Fixed

- `description check` rejects non-finite or misshaped mass-closure readings. A NaN or infinite
  mass, COM component or inertia entry — and a COM or inertia that is not a 3-vector or 3x3 tensor —
  used to read as "no difference" and pass, because every comparison with NaN is false. The new
  component-context rows get the same finite/positive and shape validation.
- One unreadable component instance no longer discards the whole component-context section: a name,
  hierarchy or mass-property failure is recorded as a staged row error and the walk keeps the rows
  it could read.
- A Windows App Control rejection of a MuJoCo DLL (for example Smart App Control blocking the
  unsigned `plugin\elasticity.dll`) is reported as such — with the affected library and the original
  `OSError` — by `description doctor`, the worker's local-pipeline probe and `quickstart`'s error
  payload, instead of being classified as a missing package or parsed as a MuJoCo version. This is a
  diagnostic classification fix only: it does not change the host policy and does not make a blocked
  native acceptance pass. Version 0.3.25 stays unreleased and untagged until the native rehearsal
  succeeds in an administrator-approved runtime.

## [0.3.24] - 2026-09-29

### Added

- A capture records the assembly's own mass properties next to the parallel-axis combination of its
  leaf readings (`raw/mass_closure.json`), and `description check` reports a difference between the
  two as a **note** — never a blocker. A leaf that was repaired or re-materialed after the assembly
  was built (a uniform density difference, as the M3.0 servo recovery showed) is otherwise invisible:
  the leaf reader answers per part, the gyration-based rules are mass-scale invariant, and the
  material checks say nothing about magnitude. Snapshots that predate the record stay
  `not_applicable`. The native assembly read is exercised on Windows by the release rehearsal.
- When `CreateMassProperty2` is unavailable, the capture falls back to the legacy
  `Extension.GetMassProperties2` read and records a **mass-only** closure: only the mass the M3.0
  recovery reports and the 2026-09-29 native pairing corroborate, with volume as context — COM and
  inertia are never inferred from the vector. `description check` evaluates such a record on its
  mass alone (a mismatch is still just a note), while a record with no usable mass stays a defect
  and a capture whose assembly read is unavailable stays `not_applicable`.

## [0.3.23] - 2026-09-29

First public release of the CAD-to-URDF/MJCF tool in
[`mimicverse/description-pipeline`](https://github.com/mimicverse/description-pipeline).
It provides frozen source capture, canonical modeling, URDF/MJCF generation, independent
qualification, offline Windows and Linux distributions, and model submission and promotion.
The earlier development and model history remains in the private source repository.

The package version is defined by `description_pipeline.__version__`.
