# SolidWorks source adapter (Windows capture side)

English · [中文](solidworks.md)

For first use, follow [from a SolidWorks assembly to your first model pull
request](../solidworks-first-use.en.md) to complete installation, connection, definition and
submission. This page maintains the source format, the deployment interface and the acceptance
contract.

Assembly structure, configurations, materials, mass properties and geometry can only be read through
the native SolidWorks API, so capture converges into one **fixed-version worker running inside a
logged-on desktop session**; normalisation, building and acceptance are done by the shared toolchain.

## Components

| Location | Role |
|---|---|
| `.../solidworks/native.py` | Native API: open documents read-only, read components, mass properties, materials, coordinate systems and geometry |
| `.../solidworks/isolation.py` | Job-owned CAD process (Windows Job Object), binds COM to that PID and releases handles |
| `.../solidworks/freeze.py` | Dependency collection, readings, geometry, evidence and snapshot submission |
| `.../solidworks/scene.py` | `load_scene` / `normalize_scene`: raw readings → `description.scene/v1` |
| `.../solidworks/jobs.py` | Persistent jobs, events and heartbeats, attempts, restart recovery, watchdog |
| `.../solidworks/worker.py` | Loopback-only job API (`/health`, `/doctor`, `/jobs`, `/jobs/<id>/*`, `/maintenance`) |
| `.../solidworks/deploy/worker.ps1` | Deployment entry (ships with the package): Install / Start / Stop / Doctor / Update / Rollback / Status |

All paths are prefixed with `src/description_pipeline/sources/`.

## Configuration contract (the `source` mapping in `config/robot.yaml`)

```yaml
source:
  provider: solidworks
  assembly: "D:/models/robot.SLDASM"   # top-level assembly; absolute path
  configuration: "Default"             # required; the "active configuration" is never assumed
  allowed_roots: ["D:/models"]         # dependencies must stay here or freezing is refused
  require_saved: true                  # always true; the adapter never saves the original
  geometry: { enabled: true, format: stl_binary }
  coordinate_systems: [CS_head, CS_arm]
  bodies:                              # explicit native-entity → link ownership
    - { id: trunk, name: trunk_link, components: ["trunk__1", "battery__1"] }
    - id: head
      name: head_link
      components: ["head__1"]
      frame: { xyz: [-0.00204, 0, 0.126], rpy: [0, 0, 1.5708] }
  joints:                              # joint geometry must be explicit, never inferred from mates
    - id: head
      name: head_joint
      type: revolute
      parent: trunk_link
      child: head_link
      xyz: [-0.00204, 0, 0.126]
      rpy: [0, 0, 1.5708]
      axis: [0, 0, 1]
      limits: { lower: -1.5, upper: 1.5, effort: 3.0, velocity: 6.0 }
  frames:
    - { id: imu, parent: trunk_link, xyz: [0.0148, -0.002, 0.0675], rpy: [1.5708, 0, -1.5708] }
```

The values above are a structural example and must not be used on a real robot. Without `bodies` the
snapshot records the raw readings (one link per leaf part, `kinematics: not_defined`); missing joint
coordinates, limits or axes raise an error instead of inventing defaults. Capture reads **files
saved on disk**, never unsaved edits in the desktop session. The worker opens the assembly read-only
in an owned SolidWorks process and never saves the original.
SolidWorks' `GetSaveFlag` is recorded in `raw/document_state.json`, `raw/dependency_closure.json`
and `evidence/collection.json`; Doctor shows it as a notice. The flag describes the worker's
in-memory document, not the operator's unsaved edits, and does not block capture. Save desktop edits
before capturing: the snapshot contains the disk bytes.

`body.frame` is the pose of the link in the assembly frame at the zero position; a joint's `xyz/rpy`
is the pose of the child link in the parent link's frame. Both must describe the same zero position.
Omitting a non-zero body frame transforms mass and geometry twice, and the independent world-frame
check rejects that model.

## Capture flow and snapshot

`freeze` does all its work in a temporary directory and renames it into place only after the checks
pass:

1. Open the original read-only in a separate process, walk `GetDocumentDependencies2` recursively,
   and record the complete file list with digests.
2. Copy the original files and relocate every reference in a second separate process with
   `ReplaceReferencedDocument`; only the copy is modified.
3. Reopen the copy, verify the top-level configuration, the identity and configuration of every
   instance and that all dependencies stay inside the snapshot, then read mass, coordinate systems
   and meshes.
4. Re-check the digests of original and copy, release the capture process and file handles, and only
   then submit the snapshot. Any failing step keeps its diagnostics and publishes no half-product.

```
snapshot/
  manifest.json          # description.source/v1: identity / evidence_class / per-file sha256
  scene.json             # description.scene/v1 (including provenance.expected_entities)
  raw/                   # raw readings: scene_raw / mass_properties / coordinate_systems /
                         # document_state / dependency_closure / geometry
  evidence/              # capture environment (SolidWorks version, worker version) and conditions
  source/                # complete native file copy, references relocated and reopened for verification
  geometry/*.stl         # per-component geometry (complete binary STL)
```

Every capture process joins a Windows Job Object before it starts and COM binds only to that PID;
normal completion, failure and timeouts clean up only that job's process tree and never attach to or
terminate the operator's SolidWorks.

`raw/mass_closure.json` records the assembly document's own reading and the leaf readings, and now
also `component_context`: each component instance's assembly-context mass and its three override
flags; the leaf/document masses stay a separate basis. Totals use only the **disjoint depth-0 rows**;
nested rows exist to detect overrides a clean parent would otherwise hide. Node coverage is derived
as the **full prefix closure** of the scene leaves: missing ancestor rows, absent parent rows,
duplicates, wrong node types or unknown extra rows are rejected, and pure CAD additionally requires
each node's assembly-context mass to match the sum of its selected part-document readings within
tolerance. Missing or duplicate rows, non-boolean flags, or a depth that disagrees with the name make
the independent check (`source.normalization.mass_closure`) fail.

## Mass and inertia contract

### Two material modes

`source.material_source` has exactly two values, and neither allows "quietly using a default
density":

| Mode | Contract |
|---|---|
| `cad` (default) | Every entity must have an **explicit physical material** (a capture reports `cad_material_provenance_missing`); the independent check re-verifies: a component that still carries `reference.material_assignment.unverified_reason` without a documented mass fails (a default density of 1000 kg/m³ is a placeholder) |
| `documented_table` | `source.documented_masses` must **cover every included component**; one missing entry fails, and an entry for a component that is not included (typo, rename) is rejected as well |

The coverage check runs independently in three places: configuration validation
(`validate_source_config`), the generation side (`build_scene`, against the actual readings) and the
independent check (`source.normalization.mass_provenance`, whose details report
`missing_declared` / `unknown_declared` / `cad_mass_without_verified_material`); the check side never
reads the generator's report.

`material_source: cad` also requires that no **component-level** override exists anywhere in the
assembly tree (mass, center of mass or inertia): when one is recorded,
`source.normalization.mass_closure` fails, because pure CAD reads part documents and cannot represent
instance overrides. `documented_table` reports them as a note, never redistributes or rewrites a
declared mass, and the note states the two readings and that **no cause is inferred**. Snapshots
without the record keep their previous verdict.

### Inertia model for documented masses

A documented mass replaces only the component's mass; its inertia is **scaled as a whole** by
`scale = used_mass / CAD_mass`, preserving the uniform-density shape of the CAD geometry:

| Field | Value |
|---|---|
| `provenance.mass_sources` | where each component's mass came from (`documented` / `cad`) |
| `provenance.inertia_model` | `scaled_cad_uniform_density` |
| `provenance.inertia_model_scope` | where it applies (printed parts are an order-of-magnitude estimate; catalogue parts are not) |
| `provenance.declared_masses[]` | per component `raw_mass_kg` / `used_mass_kg` / `scale` / `reason` / `evidence` |

The independent check records the same annotations under `source.normalization.mass_provenance`
(`inertia_model` / `inertia_model_scope` / `scaled_components`). The source side only scales as a
whole; measured mass, centre of mass and inertia can be overridden through the model definition's
[public `overrides`](../pipeline.en.md) with a rationale and evidence, and the independent check
re-verifies them.

### Product-of-inertia sign

`IMassProperty2.GetMomentOfInertia(0)` returns positive products of inertia in the
`solidworks_positive` convention: the layout is `[[Ixx, Ixy, Izx], [Ixy, Iyy, Iyz], [Izx, Iyz, Izz]]`
and the off-diagonal terms are `∫xy dm` / `∫zx dm` / `∫yz dm`; the off-diagonal entries of the
standard inertia tensor are their negatives. The contract:

* the nine raw numbers are preserved verbatim and never rewritten in the snapshot;
* the generation side and the independent check each negate the off-diagonal terms **before**
  rotation and mass scaling (the two implementations do not reference each other);
* a native reading without `product_convention` is never guessed — it fails; fixture readings
  (`used_api == "fixture"`) are already standard tensors by contract and are not converted;
* a documented mass only scales as a whole, applied to the already converted tensor, and never
  negates twice.

Review baseline (analytic cuboids): base 0.08×0.06×0.04 m / 1.4976 kg / RPY (0.2,-0.3,0.4), arm
0.025×0.04×0.10 m / 0.78 kg / RPY (-0.25,0.15,-0.35); recomputed from the nine raw numbers it agrees
with the published tensor to ~5e-19 kg·m².

### Evidence archiving and digest binding

```yaml
source:
  material_source: documented_table
  mass_evidence:
    reference: "material specification (PLA@15% / steel / POM / catalogue datasheets)"
    file: docs/provenance/mass-spec.json
    sha256: "<64 lowercase hexadecimal characters>"
  documented_masses:
    base-1:
      mass_kg: 2.5
      reason: "printed part: PLA@15% effective density"
      evidence: "base-1"                     # anchor inside that file
```

1. Declaring `documented_masses` requires `source.mass_evidence` to be
   `{reference, file, sha256}` (optionally `note`); a bare string is rejected.
2. Each documented mass's `evidence` is an **anchor inside that file**, and the independent check
   requires the exact string to appear in the file content.
3. The independent check `source.normalization.mass_evidence` recomputes from the model repository's
   own bytes (the model root is found by walking up from the snapshot directory to the level that
   contains `config/robot.yaml`; snapshots live at `<model>/sources/snapshots/<digest>/`) and rejects
   `evidence_not_bound` / `evidence_root_not_found` / `evidence_path_escape` /
   `evidence_file_missing` / `evidence_digest_mismatch` / `evidence_anchor_missing:*` /
   `evidence_anchor_not_found:*` / `uncovered_component:*`, with claimed/actual digests and per-anchor
   results in the details.
4. The capture side (the Windows worker) validates shape only (keys, non-empty path, digest format);
   file existence, escaping and digest agreement are re-checked on the model side. The archived file
   is a model input and must be committed with the model.

**Boundary**: an anchor is a substring check (it proves the record exists in the file, not that it is
physically right); a digest proves the content did not change, without a signature or a timestamp;
and the mechanism currently covers mass/material evidence only.

## Deployment and operation

The root of the installation ZIP is the deployment directory (`worker.ps1`,
`worker-host.example.json`, `version.json`, `requirements.txt`, `src/`, `wheels/`). Copy and fill in
the host configuration inside the extracted directory first, then call its `worker.ps1`;
`-Bundle` points at the original zip. Install/Update require a 64-character hexadecimal digest (the
host configuration's `bundle_sha256` or `-BundleSha256`) and refuse to install when it is missing or
does not match.

The extracted directory and `install_root` are separate: day to day you use `submit.ps1` in the
extracted directory with `submit-host.json` next to it. The `assembly` and `configuration` in
`worker-host.json` are for Doctor only; each capture's real target comes from the model's
`config/robot.yaml`.
Keep `install_root` short to avoid PowerShell's legacy archive path limit.

```powershell
$Bundle = '.\description-worker-0.3.24-windows-x86_64.zip'
$Deploy = '.\worker-0.3.24'
$Config = "$Deploy\worker-host.json"
Expand-Archive $Bundle -DestinationPath $Deploy -Force
Copy-Item "$Deploy\worker-host.example.json" $Config
notepad $Config  # fill in install_root, user, python, jobs_root, bundle_sha256

# Install and diagnose inside the logged-on desktop (Install starts the worker; -Action Start restarts it later)
powershell -File "$Deploy\worker.ps1" -Action Install -Bundle $Bundle -Config $Config
powershell -File "$Deploy\worker.ps1" -Action Doctor -Config $Config
```

* Versions live in `install_root\versions\<version>` (each with its own venv) and `current.json`
  points at the active one; upgrade with `-Action Update` — the new archive needs **its own release**
  `SHA256SUMS` beside it or an explicit `-BundleSha256`, because the digest in `worker-host.json`
  belongs to the installed version — roll back with `-Action Rollback`, and old versions are kept
  according to `keep_versions`.
* **A new version takes effect only when healthy**: the pointer switches after the new version
  answers `/health` with a matching version, and a failed start or self-check restores the previous
  version automatically. Entering `/maintenance` first is mandatory (the conditions are under "Jobs,
  cancellation and recovery").
* It runs inside a **logged-on desktop session**: by default it registers an AtLogOn task with
  `LogonType Interactive` (COM needs a desktop) and falls back to the Startup directory, recording
  `mode` in `current.json`, when it cannot register the task. A process started by a fresh SSH
  session or a service-style runner cannot capture CAD.
* Dependencies are pinned by the packaged `.../solidworks/deploy/requirements/win-py312.lock` (the
  public core plus its transitive closure plus `pywin32`, MuJoCo and their complete transitive
  dependencies, buildable and verifiable locally). On a restricted network, download the wheels
  first:

  ```bash
  python -m pip download --only-binary=:all: --platform win_amd64 --python-version 3.12 \
      --implementation cp --abi cp312 -d wheels \
      -r src/description_pipeline/sources/solidworks/deploy/requirements/win-py312.lock
  ```

  `Install` runs `pip install --no-index --find-links wheels -r requirements.txt`; the package also
  carries the public core source and the deployment resources, and `tools/build_release.py` verifies
  they are present before packaging (`--deploy-out DIR` exports them alongside).
* The worker listens on `127.0.0.1` only and answers nothing but the address it was started on
  (`Host`/`Origin` mismatch is a 403, so a browser page cannot drive it); remote use goes through an
  SSH tunnel
  (`ssh -L 8765:127.0.0.1:8765 <host>`). All COM calls are serialised on one STA thread and HTTP
  threads never touch COM directly.
* `Doctor` (the CLI and `/doctor`) reports three states separately: **installed** (files, venv,
  task), **worker alive** (`/health`) and **CAD readable** (start an isolated instance and read the
  document, components and mass properties). It also verifies CPython 3.12 and the required modules
  (`yaml`/`jsonschema`/`numpy`, plus `win32com` on Windows) and reports `installed=false` when any
  is missing.

### One-command candidate submission: one-time setup / daily use

By default the capture, build, MuJoCo verification and pull-request submission all happen on the
Windows machine that runs SolidWorks. Full installation and first modelling are in the
[first-use guide](../solidworks-first-use.en.md). The Windows package contains the complete public
tool and its runtime dependencies; after installation you can run
`python -m description_pipeline` from that version's venv directly.

Fill in the local paths in `submit-host.json` next to the script:

```json
{
  "model_root": "C:\\Users\\YourName\\description\\models\\myrobot",
  "profile": "kinematics",
  "message": "Update myrobot model"
}
```

The script reads `install_root` from `worker-host.json` next to it and finds the installed runtime
through `current.json`; `python` can also be given explicitly. The model's `source.worker_url`
points at the local worker (default `http://127.0.0.1:8765`). Source paths, assembly configuration
and port are controlled by the model definition and the submission script never rewrites them.
Git, Git LFS and an authenticated `gh` must be available, and `feature/<hardware>` must already exist
on the remote. After saving the CAD, run:

```powershell
powershell -ExecutionPolicy Bypass -File .\submit.ps1
```

`-Message`, `-ModelRoot` and `-Profile` override this run's parameters, and `-Config` selects another
configuration. `-DescribeOnly` prints the plan without capturing or pushing; the real preflight and
verification are done by the shared `description model update`. With the complete tool environment
active, you can also run that command directly from the model directory.

When only the definition or the evidence changed and the source did not, either platform can use
`description model update --reuse-source` without touching CAD. Reuse still re-checks the source
configuration and snapshot integrity; a changed CAD or source configuration requires a new capture.
A changed tool or operating system requires an explicit tool-lock update and rebuild, and
verification runs on the locked platform and environment.

Every workspace is locked for the whole run. Re-running on the same review branch updates the same
pull request; a failed build keeps its diagnostics and a failed pull-request creation keeps the
candidate branch and prints the retry command. GitHub CI is not called by default. A created pull
request still needs review, and the release command fetches the exact candidate from the remote and
qualifies it independently.

### Optional remote submission

Configure SSH only when another machine has to be used remotely.

**Linux initiates, Windows captures:** configure the `windows-cad` SSH alias and verify
non-interactive login, point the model's `source.worker_url` at the local forwarded port, then run:

```sh
description model update --worker-host windows-cad
```

The command opens and owns the tunnel for this run and closes the connection when the capture
finishes or fails; it refuses to take over an already occupied port. `--worker-port` sets the
Windows worker port (default 8765) and the Linux port comes from `source.worker_url`.

**Windows initiates, Linux builds:** use a remote configuration and keep the compatibility entry of
the existing deployment:

```json
{
  "build_host": "description-build",
  "remote_python": "/opt/description/venv/bin/python",
  "model_root": "/srv/description/models/myrobot",
  "profile": "kinematics",
  "message": "Update myrobot model",
  "worker_port": 8765,
  "remote_port": 8765
}
```

`build_host` is an SSH alias whose user and key live in the SSH configuration; `identity_file` can
name a private key path explicitly. A legacy configuration that still contains `build_host` keeps
running in remote mode. The model's `source.worker_url` must match
`http://127.0.0.1:<remote_port>`. One SSH session forwards the worker and runs the remote update at
the same time, closing the tunnel at the end. The commit message travels over standard input.

## Jobs, cancellation and recovery

Every `source freeze` creates a new capture request identity; transport retries reuse that request,
and an explicit `job_id` only recovers the original job. The job directory holds `job.json`,
`events.log` and heartbeats; fetching from the remote verifies the file list, assembly path,
configuration and evidence class, and a failed package with its safely extracted files stays in the
diagnostic directory. Fetching on Linux:

```bash
curl -s http://127.0.0.1:8765/jobs/<id>/manifest     # inventory
curl -s http://127.0.0.1:8765/jobs/<id>/files        # per-file sha256
curl -s http://127.0.0.1:8765/jobs/<id>/package -o snapshot.tar
```

* **Restart recovery**: queued jobs that have not started can resume; a freeze that already started
  has lost its original scene, is recorded as failed with its diagnostics, and must be captured
  again. A mismatched request digest or capture-tool version refuses recovery.
* **Cancellation**: it affects only the named job. A job that has not started goes straight to a
  terminal state; a job already inside CAD execution is stopped by reclaiming that job's own process
  and never touches the operator's SolidWorks.
* **Maintenance and version switching**: `/maintenance` and `Update`/`Rollback` are allowed when
  nothing is queued or running and no CAD operation has been left behind. An operation that has timed
  out but not exited is handled by the recovery rule below.
* **Timeout / not exited**: after a CAD operation times out, the worker reclaims that job's own
  processes; until the operation has really exited, every new CAD request is refused with
  `cad_recovery_required` (503). Only when all jobs are terminal, the queue is empty and the worker
  reports that state explicitly may a maintenance restart (including `Update`/`Rollback`) finish the
  job; otherwise wait for the operation to exit first.

## Tests and acceptance

```bash
# Linux fixtures: no CAD, covering freeze/read/tamper refusal/jobs/watchdog/API/doctor
python -m unittest discover -s tests/sources -t .
```

`tests/windows/test_deployment.ps1` uses stand-ins to verify state transitions, failure rollback and
process isolation and never touches real Windows tasks or CAD. Real Windows acceptance must record
four separate results: installation, connection (Doctor's three states), native capture (a snapshot
of the real assembly with per-file digests) and robot-level acceptance (a downstream build and check
that consumes that snapshot); a passing fixture is neither a native assembly nor a robot acceptance.

An independent oracle recomputes from the raw component set, transforms and full tensors and then
reconciles against the q=0 kinematic chain in assembly world coordinates; missing mass evidence
blocks the build.
