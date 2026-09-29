# demo-arm — offline example workspace

A complete model workspace that runs **without CAD, without credentials and without network**. The
source is a hand-written analytic fixture: `sources/source.lock.json` records
`evidence_class: fixture`, and `description check` reports `source.native: not_applicable`. Nothing
here claims CAD provenance — the example exists so you can run the whole pipeline, read the generated
URDF/MJCF and see what a passing qualification report looks like.

The committed artifacts were produced by the released tool recorded in `config/toolchain.lock.json`.

## Layout

| Path | Meaning |
| --- | --- |
| `sources/fixture/` | frozen source snapshot: `scene.json` + `manifest.json` |
| `sources/snapshots/<digest>/` | the immutable snapshot that `description source freeze` materialises |
| `config/robot.yaml` | author definition; here `source.provider: fixture` |
| `config/profiles/*.json` | one profile per purpose; only `kinematics` is qualified in this example |
| `config/joint_names.yaml` | structural joint ledger required by the URDF contract (`URDF208`) |
| `config/toolchain.lock.json` | exact tool release, Python and dependency set |
| `urdf/robot.urdf`, `mjcf/robot.xml`, `mjcf/scene.xml` | generated consumer entries |
| `docs/quality.md`, `docs/quality.json` | the qualification report behind `description check` |
| `manifest.json` | content-addressed delivery manifest; subject `b987661b…` |

## Run it

Requires CPython 3.12 and `description` (release 0.3.23 or a source checkout).

```sh
description quickstart --run
```

writes this workspace to `./demo-arm` and runs the four commands below in one go, so an installed
release needs no checkout to try the pipeline.

```sh
cd examples/demo-arm
description tool lock --root .        # re-pin the lock to your installed tool (demos only)
description source freeze --root .    # copy the fixture into sources/snapshots/<digest>
description build --root . --profile kinematics
description check --root . --profile kinematics
```

`build` writes the URDF/MJCF entries, `model/`, `docs/quality.*` and `manifest.json`. `check`
re-derives the model from the frozen snapshot, compiles it with MuJoCo and qualifies it for
`kinematics`. Both commands print JSON and exit non-zero when a check fails.
`description model layout --root . --role model` confirms the workspace layout.
The four commands take a few seconds in total on an ordinary laptop; the slower part is the MuJoCo
compilation inside `build` and `check`.

In a tool checkout, the repository's own URDF contract audit must pass as well:

```sh
python tools/audit.py --root examples/demo-arm --policy strict
```

It reads `config/joint_names.yaml` and `config/urdf_quality.json`, so a workspace can be copied into
a real model repository without tripping the repository rules.

## Try breaking it

Change a joint limit in `urdf/robot.urdf` (for example `2.2000000000000002` → `2.4000000000000002`)
and run `description check --root . --profile kinematics`. It exits 1 and reports `urdf.joints`,
`URDF506`, `consumer.joints` and `bundle.identity` as blockers. That is the point of the pipeline:
generated files are not trusted, they are re-derived and re-checked against the frozen source.

## What this example does not claim

It is fixture evidence, not a CAD capture: `source.native` is `not_applicable` and the report never
claims native provenance. Simulation, training and hardware qualification each need their own
evidence — see [`docs/simulation.en.md`](../../docs/simulation.en.md).
