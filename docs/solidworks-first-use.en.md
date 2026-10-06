# Model and submit a robot from a SolidWorks workstation

English · [中文](solidworks-first-use.md)

**One Windows machine is enough: capture CAD → generate URDF/MJCF → verify locally → open a pull
request.** After the first installation and robot definition, daily work is a single `submit.ps1`.
The default flow runs on that machine and does not call GitHub CI.

The currently tested native platform is Windows 11 Home China 25H2 (build 26200) x64, SolidWorks 2026
SP3.2 (revision 34.3.2) and Python 3.12.10. Other versions must pass Doctor, a native capture and a
model check before use; compatibility is not claimed.

This guide uses `myrobot` as the hardware id, `D:\robots\myrobot\robot.SLDASM` as the assembly and
`Default` as the configuration; replace them with your own values. If the hardware already has a
model, reuse its definition and branch instead of initializing again. Complete the kinematic model
first, then add the parameters and evidence that simulation, training or hardware need.

## First-run checklist

First use takes five steps. The designer must review the assembly and complete the mechanical
definition; the time this takes depends on the hardware. Each step links to its commands and
failure guidance.

1. **Download the archive** ([section 1](#1-save-the-assembly-and-prepare-the-tools)): put
   `description-worker-<version>-windows-x86_64.zip` and `SHA256SUMS` from the distribution page into
   one directory, your Downloads folder for example.
2. **Install and check** ([section 2](#2-install-and-check-the-local-environment)):
   `powershell -ExecutionPolicy Bypass -File "$Deploy\worker.ps1" -Action Setup -Bundle $Bundle -Assembly '<assembly path>' -AssemblyConfiguration Default`
   Success looks like `install complete` followed by Doctor exiting 0.
3. **Create the model workspace** ([section 3](#3-create-the-model-workspace-on-this-machine)):
   `description model init --repository <tools checkout> --root <model directory> --hardware <robot name> --provider solidworks --assembly '<assembly path>' --configuration Default`
   Success writes `config/robot.yaml` and prints `next:` on stderr.
4. **Freeze, complete the definition, build and check** ([section 4](#4-capture-once-and-complete-the-robot-definition)):
   capture and review the component instances, then define bodies, joints and limits. Run
   `description build --root <model directory> --profile kinematics` and
   `description check --root <model directory> --profile kinematics`.
   Success is `qualified_for: ["kinematics"]` in the check output.
5. **Submit once** ([section 5](#5-configure-the-submission-entry-and-run-it-daily)):
   `powershell -ExecutionPolicy Bypass -File .\submit.ps1`
   Success pushes the candidate and creates or updates the pull request.

When a step is unclear, run `description doctor --root <model directory>` first: it reports the
environment and the workspace item by item and names the fix for every failure. Every `next:` line a
command prints is the next step; anything else is in [Troubleshooting](#troubleshooting).

## Canonical SolidWorks-to-URDF pipeline

The published workflow is `pipeline_id: solidworks-to-urdf` (siblings: `onshape-to-urdf`,
`fixture-to-urdf`). `description pipeline list` prints the catalog;
`description pipeline show solidworks-to-urdf --json` prints this workflow's stages with their
Python entry points and documents; `description pipeline show --root MODEL` reports a workspace's
declared and effective identity. Identity semantics are defined once in the
[pipeline contract](pipeline.en.md#inputs-and-authoritative-sources); the stage map below uses them
and points at the detailed command blocks.

| # | Stage (details) | Command | Code entry | Output / report |
|---|---|---|---|---|
| 1 | Prepare and save the CAD ([section 1](#1-save-the-assembly-and-prepare-the-tools)) | Save the top assembly and all references in SolidWorks | operator/CAD | Saved files; unsaved-edit/save-flag evidence |
| 2 | Install and diagnose ([section 2](#2-install-and-check-the-local-environment)) | `worker.ps1 -Action Setup/Install/Doctor`; `description doctor --root MODEL`; `description worker doctor --target URL` | `description_pipeline.doctor:run`; worker deployment `sources/solidworks/deploy/worker.ps1` | `worker-host.json`; Doctor exit 0 with `install=True worker=True solidworks=True collectable=True` |
| 3 | Ownership, pivots and identity ([section 4](#4-capture-once-and-complete-the-robot-definition)) | Edit `config/robot.yaml`: `source.bodies`, `source.joints`, `interfaces.mechanical_drives`, optional `pipeline_id` | `description_pipeline.model:Robot`; `description_pipeline.pipeline:resolve_identity` | `model/robot.json`; `source.pipeline` check; the `sources/source.lock.json` `pipeline` block |
| 4 | Save and freeze ([section 4](#4-capture-once-and-complete-the-robot-definition)) | `description source freeze --root MODEL` | `description_pipeline.sources.solidworks:freeze`; `description_pipeline.build:freeze` | `sources/source.lock.json` + `sources/snapshots/<digest>/raw/*` and geometry; failures in `build/failed-source/` |
| 5 | Author semantics and evidence ([section 4](#4-capture-once-and-complete-the-robot-definition)) | Edit `overrides`, documented masses/evidence, profiles and the joint ledger | `description_pipeline.build:normalize` | Evidence-bound canonical model; advisories/blockers in `docs/quality.*` |
| 6 | Generate ([section 4](#4-capture-once-and-complete-the-robot-definition)) | `description build --root MODEL --profile kinematics` | `description_pipeline.build:build`; `description_pipeline.backends:generate` | `urdf/robot.urdf`, `mjcf/robot.xml`, `mjcf/scene.xml`, `meshes/`, `manifest.json`, `docs/quality.*`; failures in `build/failed/` |
| 7 | Independent acceptance ([section 5](#5-configure-the-submission-entry-and-run-it-daily)) | `description check --root MODEL --profile kinematics`; `description model accept ...` | `description_pipeline.build:assess`; `description_pipeline.verification.mechanics:run_acceptance` (kinematics) or `description_pipeline.verification.simulation:run_acceptance` | `docs/acceptance/<purpose>.json` + telemetry; `consumer.application`; the kinematics replay binds the reference digest |
| 8 | Candidate pull request ([section 5](#5-configure-the-submission-entry-and-run-it-daily)) | `description model update` / `submit.ps1`; `description model submit`; `description diff OLD_SHA CANDIDATE --repository REPO` | `description_pipeline.repository:update`; `description_pipeline.repository:_submit` | Review branch `work/model/<hardware>/<change>`, pull-request URL; outputs carry `pipeline_id` |
| 9 | Exact-commit acceptance and publication ([release flow](pipeline.en.md#submission-acceptance-and-release)) | `description model validate ... --remote`; `description model promote ... [--apply]` | `description_pipeline.repository:validate_commit`; `description_pipeline.repository:promotion_plan`; `description_pipeline.repository:promote` | Validation report; `release/<hardware>` branch and `description/release` status; optional tag; identity compared throughout |
| 10 | Daily re-export and recovery ([failure handling](pipeline.en.md#failure-handling)) | `submit.ps1` / `description model update`; `description doctor --root MODEL`; `description recover --root MODEL` | `description_pipeline.repository:update`; `description_pipeline.build.publication:recover`; `description_pipeline.runlog:finish` | New subject/snapshot/review; failures stay in `build/failed-source/` or `build/failed/`; the run record is `build/runs/<run_id>.json` |

Gate rules (details in the linked stages and the [pipeline contract](pipeline.en.md)):

* **Ownership and pivots.** Every included CAD instance belongs to exactly one rigid body (partition
  by captured instance name, not file name). Every movable joint declares parent/child links, the
  physical pivot in the parent-link frame and the signed axis in the joint frame. Define these from
  design and geometry; never infer a pivot, direction or limit from a CAD mate, a nearest cylinder
  or an unsigned axis. See [mechanical acceptance](mechanical-acceptance.en.md).
* **Save and freeze.** Freezing starts from saved bytes on disk; a missing or escaping dependency,
  an unverified copy or a geometry failure blocks the run. `GetSaveFlag` is evidence, not a
  substitute for saving, and an interrupted freeze that cannot prove its inputs must be re-captured.
* **Acceptance.** Generating files, loading the URDF/MJCF or agreeing between formats is not
  acceptance. Simulation runs declared experiments; kinematics requires an operator-selected
  external `--mechanical-reference`; training and hardware need their own evidence. Local replay is
  deterministic and needs no CI.
* **Publication.** `model promote` re-fetches the exact candidate into a fresh Git/LFS store,
  re-runs acceptance in the pinned tool environment and refuses a stale plan. A pull request is not
  a release; the acceptance record binds subject, profile, environment, tool, runtime and reference
  digest.
* **Daily re-export and recovery.** Save the CAD (or add `--reuse-source` when only the definition
  or evidence changed) and run `submit.ps1`/`model update`; a new capture creates a new subject and
  review. Capture failures stay in `build/failed-source/`, build/acceptance failures in
  `build/failed/`; run `description recover --root MODEL` if `publication.json` exists. A worker
  restart resumes queued jobs but never reuses an unproven interrupted freeze.
* **Linux snapshot replay.** On Linux a frozen snapshot can be built, checked and accepted without a
  SolidWorks worker (`description build`, `description check`, `description model accept`); the
  tool lock's platform/Python must match, and CAD capture remains Windows-only.

**Release gate.** 0.3.25 is not published or tagged. A release requires a successful fresh native
Windows rehearsal (installation, Doctor, capture and the consumer checks) on the candidate bundle,
followed by the exact-commit acceptance; the current and historical gate state is in the
[0.3.25 validation history](history/validation-0.3.25.md). A diagnostic improvement, a preserved
runtime probe, generated files or a submitted pull request never pass that gate, and no candidate is
"mechanically accepted" until its exact commit passes the declared acceptance.

## 1. Save the assembly and prepare the tools

In SolidWorks pick the configuration and save the top-level assembly together with every referenced
file. Check that the references are complete and that every included body has an explicit physical
material; appearance colour is not a material.
The worker opens the saved assembly read-only in its own SolidWorks process. It does not use unsaved
changes from your desktop session or save the original files. Keep the Windows desktop logged in
while capture runs; failed captures leave diagnostics under `build/failed-source/`.
SolidWorks may mark the worker's read-only document as needing a save. Doctor reports that flag as a
notice and capture records it as evidence; the snapshot still uses the saved disk bytes. Save any
desktop edits before capturing if you want them included.
This guide computes mass properties from CAD materials. When you use specification or measured
masses, prepare the complete declaration table and evidence described by the
[mass evidence contract](sources/solidworks.en.md#mass-and-inertia-contract).

Windows needs a valid SolidWorks licence, x64 CPython 3.12, Git (with Git LFS) and the GitHub CLI.
Windows App Control must also allow MuJoCo's native dependencies. If Doctor reports `WinError 4551`,
the package is installed but Windows refused to load a file. Use an approved runtime or ask the
administrator to review that dependency before building and checking models on Windows.

Without 3.12, install one first (any 3.12.x; 3.13 and 3.14 are not supported here):

```powershell
winget install Python.Python.3.12
py -3.12 --version        # prints 3.12.x; if `python --version` opens the Microsoft Store, use the py launcher
```

Windows 11 ships "app execution aliases" for `python.exe`/`python3.exe` that open the Microsoft Store
while no interpreter is installed. After installing, open a **new** PowerShell and confirm that
`python --version`, `git --version`, `git lfs version` and `gh --version` all run. The GitHub account
needs write access to the target repository. Log in once:

```powershell
gh auth login
gh auth setup-git
gh auth status
```

If GitHub HTTPS negotiation stalls on the current network, pin Git to protocol v0 before continuing:

```powershell
git config --global protocol.version 0
```

Download `description-worker-0.3.24-windows-x86_64.zip` and `SHA256SUMS` from the
[0.3.24 distribution page](https://github.com/mimicverse/description-pipeline/releases/tag/v0.3.24) and read
the validation status on that page first. The archive contains the complete runtime for capture,
build and MuJoCo verification; the offline installation downloads no Python dependencies.

## 2. Install and check the local environment

Run the installation and everything after it in a normal PowerShell as the logged-in user; no
administrator rights are needed. Put the ZIP and `SHA256SUMS` in your Downloads folder, then let
`Setup` write the configuration, install and run Doctor in one command (it verifies the archive
against the adjacent `SHA256SUMS`):

```powershell
$Bundle = "$env:USERPROFILE\Downloads\description-worker-0.3.24-windows-x86_64.zip"
$Deploy = "$env:USERPROFILE\description-setup\0.3.24"
Expand-Archive -LiteralPath $Bundle -DestinationPath $Deploy -Force
powershell -ExecutionPolicy Bypass -File "$Deploy\worker.ps1" -Action Setup -Bundle $Bundle `
    -InstallRoot "$env:USERPROFILE\dw" -Assembly 'D:\robots\myrobot\robot.SLDASM' `
    -AssemblyConfiguration Default
if ($LASTEXITCODE -ne 0) { throw 'Doctor failed' }
$Python = "$env:USERPROFILE\dw\versions\0.3.24\venv\Scripts\python.exe"
& $Python -m description_pipeline --version
```

`Setup` writes `worker-host.json` (installing under `%USERPROFILE%\dw` as shown, port 8765,
the current user and the CPython 3.12 on PATH), then runs `Install` and `Doctor`; the archive digest
is checked against the adjacent `SHA256SUMS`, and when that file is missing the digest of the file as
provided is pinned with a note. Add `-NoInstall` to only write the configuration, or keep editing
`worker-host.json` and calling `-Action Install` / `-Action Doctor` yourself.
Keep `-InstallRoot` short: a longer custom root made PowerShell's archive extraction exceed its
legacy path limit in the Windows release rehearsal.

`Setup` is safe to repeat: when the command line matches the installed machine it skips the
installation and only re-checks, and to change a setting add `-Force` and then `-Action Install` to
put it in use - that never reinstalls files, it rebuilds the launcher and restarts the same version.
The port and listen address change how the script finds the running worker, so stop it first with the
configuration it was installed with:

```powershell
powershell -ExecutionPolicy Bypass -File "$Deploy\worker.ps1" -Action Stop `
    -Config "$env:LOCALAPPDATA\DescriptionWorker\worker-host.json"
```

`Install` installs the full runtime and starts the local worker; it prints `install complete` when it
succeeds. If it prints `using the Startup folder` and then succeeds, the worker starts from the
current user's login item. `Doctor` must exit 0 and its collection line must read:

```text
collection: install=True worker=True solidworks=True collectable=True
```

Keep the extracted scripts and the local configuration. The runtime, jobs and logs live under
`install_root`. After a successful first installation you never repeat `Install`; use `-Action Start`
when the worker has stopped and `-Action Update` to upgrade. Repeating `Install` is harmless: it
only restarts the installed version with the configuration in use. **Upgrade with the new archive and
its own release `SHA256SUMS` beside it** (or pass `-BundleSha256 <64 hex>`): the digest inside
`worker-host.json` belongs to the version that is installed and says nothing about the new archive,
and an update without either expectation is refused rather than decided from the file itself.
The assembly
path in `worker-host.json` is what Doctor checks; the model's `config/robot.yaml` decides what is
actually captured.

## 3. Create the model workspace on this machine

Stay in the same PowerShell. The following only applies to new hardware:

```powershell
$Tools = "$env:USERPROFILE\description\tools"
$Model = "$env:USERPROFILE\description\models\myrobot"
$ModelRepo = '<your-account>/myrobot-description'  # choose an account or organisation you control
New-Item -ItemType Directory -Force "$env:USERPROFILE\description\models" | Out-Null
git clone -c core.longpaths=true --branch main https://github.com/mimicverse/description-pipeline.git $Tools
gh repo create $ModelRepo --private
git -C $Tools remote rename origin upstream
git -C $Tools remote add origin "https://github.com/$ModelRepo.git"
git -C $Tools push -u origin main
git -C $Tools lfs install --local
git -C $Tools config user.name 'Your Name'
git -C $Tools config user.email 'your-git-email@example.com'
& $Python -m description_pipeline model init --repository $Tools --root $Model --hardware myrobot `
    --provider solidworks --assembly 'D:\robots\myrobot\robot.SLDASM' --configuration Default
if ($LASTEXITCODE -ne 0) { throw 'Model initialization failed' }
git -C $Model add -A
git -C $Model commit -m 'Initialize myrobot model workspace'
git -C $Model push -u origin feature/myrobot
```

The `--provider` form writes exactly the `source` mapping documented above (the allowed root defaults
to the assembly directory, the worker to `http://127.0.0.1:8765`); pass a complete file with
`--source-config source.yaml` when additional keys are needed (`documented_masses`,
`coordinate_systems`, `elements`, …). After initialising, run `& $Python -m description_pipeline doctor --root $Model` to
confirm the workspace state before continuing.
Replace the repository name and Git identity, and only continue after each command succeeds. The
new private repository holds the tool on `main` and your CAD/model evidence on `feature/myrobot` and
`release/myrobot`; the public repository remains available as `upstream` for tool updates. The first
model push creates the development branch; later changes reach it through pull requests. After
initialization you only maintain `$Model\config\robot.yaml`; `source-myrobot.yaml` is no longer an
active input.

For existing hardware, check that hardware's branch out into a dedicated workspace on this machine
and run `git lfs pull` to fetch the complete assets. Set the commit identity in that checkout if needed:

```powershell
git -C $Model config user.name 'Your Name'
git -C $Model config user.email 'your-git-email@example.com'
```

Then check the assembly path and configuration. If the model was locked to a Linux environment or an
older tool, first run
`& $Python -m description_pipeline tool lock --root $Model` to update the tool lock explicitly and
rebuild; a changed source configuration also requires a new capture.

## 4. Capture once and complete the robot definition

The worker runs on the local loopback address, so no SSH or port forwarding is needed:

```powershell
& $Python -m description_pipeline source freeze --root $Model
if ($LASTEXITCODE -ne 0) { throw 'Source capture failed' }
$Lock = Get-Content "$Model\sources\source.lock.json" -Raw | ConvertFrom-Json
$Scene = Get-Content (Join-Path $Model "$($Lock.snapshot)/raw/scene_raw.json") -Raw | ConvertFrom-Json
$Scene.components | Select-Object -ExpandProperty name
notepad "$Model\config\robot.yaml"
```

At this point there is no usable kinematic chain yet. The mechanical designer edits
**`config/robot.yaml`** and completes the existing `source` mapping:

| Field | What the structural design has to decide |
|---|---|
| `bodies` | Which component instances are rigidly connected and belong to the same link; use the captured instance names, not just part file names. |
| `bodies[].frame` | Pose of that link in the assembly frame at the reference configuration. |
| `joints` | Parent and child link, joint type, joint pose in the parent link frame, axis in the joint frame. |
| `joints[].limits` | Position, velocity and effort/force limits you can justify. Lengths in m, angles in rad, masses in kg. |
| `frames` | Reference frames that must ship, such as IMU or tool frames. |

The `.yaml` file written during initialization uses JSON syntax; both formats are supported. Keep
editing it as JSON, or convert the whole file to YAML — never append YAML fragments to JSON.
Below is a **complete YAML example** of a new model's `config/robot.yaml`. Keep your own hardware id
and source configuration and replace the instance names, groups, coordinates and limits with the real
design:

```yaml
schema_version: description.definition/v1
hardware_id: myrobot
source:
  provider: solidworks
  worker_url: http://127.0.0.1:8765
  assembly: D:/robots/myrobot/robot.SLDASM
  configuration: Default
  allowed_roots: [D:/robots/myrobot]
  require_saved: true
  geometry: {enabled: true, format: stl_binary}
  material_source: cad
  bodies:
    - id: base
      name: base_link
      components: [base_instance]
      frame: {xyz: [0, 0, 0], rpy: [0, 0, 0]}
    - id: arm
      name: arm_link
      components: [arm_instance]
      frame: {xyz: [0, 0, 0.1], rpy: [0, 0, 0]}
  joints:
    - id: shoulder
      name: shoulder_joint
      type: revolute
      parent: base_link
      child: arm_link
      xyz: [0, 0, 0.1]
      rpy: [0, 0, 0]
      axis: [0, 1, 0]
      limits: {lower: -1.0, upper: 1.0, effort: 1.0, velocity: 1.0}
overrides: []
```

The body frame, joint zero position and reference configuration must agree, and every included
physical instance belongs to exactly one rigid body. Missing limits, control mappings or measured
values must be completed with evidence by the author; the capture never guesses them from SolidWorks
mates. The complete format is in the
[SolidWorks source contract](sources/solidworks.en.md#configuration-contract-the-source-mapping-in-configrobotyaml).

Capture the final definition again and build:

```powershell
& $Python -m description_pipeline source freeze --root $Model
if ($LASTEXITCODE -ne 0) { throw 'Source capture failed' }
& $Python -m description_pipeline build --root $Model --profile kinematics
if ($LASTEXITCODE -ne 0) { throw 'Model build failed' }
& $Python -m description_pipeline check --root $Model --profile kinematics
if ($LASTEXITCODE -ne 0) { throw 'Model check failed' }
```

Read `$Model\docs\quality.md` and review the zero positions, joint directions, geometry placement and
mass properties. The delivered entries are `urdf/robot.urdf`, `mjcf/robot.xml` and `mjcf/scene.xml`,
with meshes under `meshes/`. When something fails, fix the CAD or the definition and rebuild; never
edit the generated XML.

## 5. Configure the submission entry and run it daily

Create `submit-host.json` in the extracted directory. It only needs the local model path, the purpose
and the commit message:

```powershell
@{
    model_root = $Model
    profile = 'kinematics'
    message = 'Update myrobot model'
} | ConvertTo-Json | Set-Content "$Deploy\submit-host.json" -Encoding UTF8
powershell -ExecutionPolicy Bypass -File "$Deploy\submit.ps1" -DescribeOnly
```

The script finds the installed runtime through `worker-host.json` next to it by default.
`-DescribeOnly` validates the configuration and prints the plan; it does not capture, push, or prove
that the GitHub login works.

From then on: save the CAD, keep the desktop logged in and the machine awake, and run from any
directory:

```powershell
powershell -ExecutionPolicy Bypass -File "$env:USERPROFILE\description-setup\0.3.24\submit.ps1"
```

That command performs preflight, capture, build, verification, Git push and pull-request creation or
update on this machine. A successful result contains `model_sha`, `pull_request` and the local
verification results; re-running on the same review branch updates the same pull request. A
successful submission is not a release: after it lands on the matching feature branch, fetch the
candidate again and qualify it independently following the
[release flow](pipeline.en.md#submission-acceptance-and-release).

When only the definition or the evidence changed and `source` did not, you can reuse the snapshot
offline:

```powershell
& $Python -m description_pipeline model update --root $Model --reuse-source
```

Changing the CAD or the rigid bodies, joints, paths or configuration under `source` requires a new
capture. Remote capture is an optional deployment; see the
[SolidWorks manual](sources/solidworks.en.md#optional-remote-submission).

## Troubleshooting

| Symptom | Check |
|---|---|
| Doctor reports `collectable=False` | Assembly path, saved configuration, missing references, licence and desktop session. |
| Python, Git or gh not found | Install the dependency (Python 3.12: `winget install Python.Python.3.12`) and reopen PowerShell; `git lfs version` must run. |
| Tool lock mismatch | Confirm the selected tool and platform, then update the lock explicitly and rebuild; never edit digests inside the lock. |
| Unclear environment or workspace state | Run `description doctor --root $Model`: it lists the state of Python, the dependencies, MuJoCo, git/gh, the tool lock, the source snapshot and the profiles, and names the fix for each problem. |
| Source capture fails | CAD save state, referenced folders, materials and declared mass evidence. |
| Capture reports `document_not_open` | Check the `path` in the error against the saved assembly and its references. The worker opens it in its own process; inspect `build/failed-source/` for the failed step. |
| Build fails | Read the diagnostics under `build/` and fix instance ownership, coordinates, joints or parameter evidence. |
| Push succeeds but the pull request fails | The candidate branch is kept; use the printed retry command to create or update the pull request. |
| Release verification fails | Read which parameter, asset or purpose evidence is missing, add it, and rebuild. |
