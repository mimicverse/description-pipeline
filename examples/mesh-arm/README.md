# mesh-arm — offline example with STL meshes

The second offline example. It runs **without CAD, credentials or network** and shows the path that
real CAD delivers: geometry as binary STL meshes instead of primitives.

Like `demo-arm` it is fixture evidence — `sources/source.lock.json` records `evidence_class: fixture`
and the report keeps `source.native: not_applicable` — so nothing here claims CAD provenance.

Requires CPython 3.12 and `description` (release 0.3.17 or a source checkout).

## What it adds over `demo-arm`

| Aspect | Result |
| --- | --- |
| Source geometry | Two binary STL boxes under `sources/fixture/geometry/parts/` |
| Delivered assets | `meshes/visual/*.stl` and `meshes/collision/*.stl`, referenced as `../meshes/...` |
| Inertia evidence | Both links declare `inertia_model: uniform_density_visual`, so the independent geometry oracle recomputes the centre of mass and the full tensor from the meshes |
| URDF geometry rules | Exercises `URDF4xx` (mesh path, parse, size, closure, normals) on real files |

The mesh dimensions are exactly representable in `float32`, so the binary STL carries the analytic
box exactly and the uniform-density comparison is not excused by rounding.

## Layout

| Path | Meaning |
| --- | --- |
| `sources/fixture/` | frozen fixture: `scene.json`, `manifest.json`, `geometry/parts/*.stl` |
| `config/robot.yaml` | author definition; `source.provider: fixture` |
| `config/joint_names.yaml` | structural joint ledger required by the URDF contract (`URDF208`) |
| `config/toolchain.lock.json` | the released tool that produced the committed artifacts |
| `urdf/robot.urdf`, `mjcf/robot.xml`, `mjcf/scene.xml` | generated consumer entries |
| `meshes/visual`, `meshes/collision` | the copied and renamed meshes the URDF points at |
| `docs/quality.md`, `docs/quality.json` | the qualification report behind `description check` |
| `manifest.json` | content-addressed delivery manifest; subject `e6913ad5…` |

## Run it

```sh
cd examples/mesh-arm
description tool lock --root .        # re-pin the lock to your installed tool (demos only)
description source freeze --root .
description build --root . --profile kinematics
description check --root . --profile kinematics
```

In a tool checkout the repository URDF contract audit must pass as well:

```sh
python tools/audit.py --root examples/mesh-arm --policy strict --mujoco
```

The audit reports three `info` findings (mesh statistics and the shared content of the fixture
meshes) and no error or warning.

## What this example does not claim

The meshes are hand-written boxes, not a CAD export: mesh simplification, contact surfaces and
material assumptions still need the author's policy and the application acceptance described in
[`docs/simulation.en.md`](../../docs/simulation.en.md).
