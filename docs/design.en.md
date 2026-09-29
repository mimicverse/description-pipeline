# MimicVerse URDF / MJCF pipeline design

English · [中文](design.md)

## Part 1: core principles

**One source of truth, automated derivation, publication only after verification.**

## Part 2: how the system works

```text
CAD + robot definition + specifications and measurements + purpose configuration
  → freeze the inputs → canonical model → URDF / MJCF / meshes / configuration
  → independent acceptance → release
```

CAD provides geometry, assembly structure and mass properties; the robot definition adds mechanical
and control semantics; specifications and measurements provide physical and control parameters. When
sources conflict, the effective source is chosen explicitly, and every override, estimate and
default records its basis and scope.

Capture freezes the CAD and its complete dependencies into a source snapshot, preserving the
original units, coordinate frames and inertia reference points, recording the CAD and capture-tool
versions, configuration and export settings, and checking that the source is unchanged before and
after. Raw evidence is preserved as it is; corrections are recorded with their own basis.

A build locks the definition, the purpose configuration, the tool and the runtime, recording versions
and file digests. The consumer environment in a purpose configuration is declared per target
platform; records from Windows and Linux must never stand in for each other. Source snapshots, tools
and dependency packages must be archived or retrievable through immutable references so the locked
environment can be rebuilt offline.

The canonical model uses SI units and one coordinate convention and states the transform direction,
the inertia reference point and the frame it is expressed in, fully expressing mass, centre of mass,
the inertia tensor, joint constraints, reference frames, actuators, sensors and the control mapping.
Source entities, canonical model objects and artifact objects are linked by stable identifiers, and
every included entity instance belongs to exactly one rigid body.

Visuals, collisions and mass properties are handled separately, and the body is kept apart from the
scene. A collision approximation records its algorithm, parameters and error budget, and critical
contact surfaces plus justified contact exclusions must be verified in the target software;
simplifying a collision never changes mass or inertia. Every backend generates its artifacts from
the same canonical model and states its supported scope explicitly for closed loops, couplings and
multi-DOF joints; when the semantics cannot be preserved it either uses a verified extension or
stops generating that format.

Verification establishes its baseline independently from the raw evidence and checks the actual
artifacts plus the effective model after the target software loads them. It compares mass, centre of
mass and the full inertia tensor in one reference point and frame, checks physical validity and the
conservation of mass properties across fixed-body fusion, and also checks mesh scale, placement and
topology, multi-pose position and rotation over every supported degree of freedom, and the order,
units, polarity and zero offset of actions and observations. The target software must additionally
run the contact, dynamics or control tests of the relevant purpose.

An acceptance report lists the expected, checked and missing objects and records errors,
tolerances, test conditions and actual software versions, deciding for kinematics, simulation,
training and hardware control separately. A missing required parameter or piece of evidence, an
unrun or unsupported check, insufficient coverage or a failure all block the corresponding purpose.
Sampling conclusions are limited to the samples and conditions actually tested.

A report records the snapshot's original capture identity separately from this run's capture or
replay mode; a replay does not prove that the current CAD interface works. Synthetic data never
replaces real source evidence, and fitted data never doubles as independent physical acceptance
evidence.

## Part 3: how the engineering is organised

The public `description-pipeline` repository distributes the shared tool. A writable model
repository starts from its `main`; use a private repository when CAD or model evidence is private.
Within that model repository, long-lived branches have these roles:

| Branch | Responsibility |
|---|---|
| `main` | Maintains the shared tool, the standards and the tests; release does not depend on GitHub Actions. |
| `feature/<hardware-id>` | Maintains that hardware's definition, source snapshots, generated artifacts and verification evidence. |
| `release/<hardware-id>` | Points at the latest released model commit for that hardware and only moves forward. |

The shared tool is packaged as one Python package providing capture, normalisation, geometry,
format generation, verification and building. Stages are joined by explicit inputs and outputs,
generic rules are maintained in one place, and the CLI and CI reuse the same flow; scheduling and
storage are chosen for the actual scale. Snapshots and the canonical model use versioned schemas
whose compatibility is checked on read. Model templates, Windows deployment resources and Linux
offline launchers ship with the package:

```text
src/description_pipeline/          # shared tool package
  sources/solidworks/deploy/       # worker.ps1, submit.ps1, host configuration templates, dependency locks
  deploy/linux/                    # install.sh, submit.sh
  templates/model/                # model template
```

The root of a model branch is that hardware's model workspace. The modeller maintains the robot
definition in `config/robot.yaml`; purpose configurations and the tool lock live in `config/` as
well, where `toolchain.lock.json` pins the tool and its dependency versions. `sources/` holds source
snapshots, specifications and measurement evidence.

A build writes the canonical model, URDF, MJCF and meshes into `model/`, `urdf/`, `mjcf/` and
`meshes/`. `docs/` holds engineering decisions and local acceptance reports, and `manifest.json`
records the file digests of inputs and artifacts. The model entry points are fixed at
`urdf/robot.urdf`, `mjcf/robot.xml` and `mjcf/scene.xml`, with relative asset paths.

A build completes in a separate staging directory and submits a complete candidate only after every
required artifact and check is done; a failure keeps diagnostics and never overwrites an existing
delivery. When a cache or an earlier verification result is reused, the inputs, configuration, tool,
environment and criteria must be checked and the reuse recorded.

Runtime responsibilities are split by platform:

| Location | Responsibility |
|---|---|
| Windows workstation | SolidWorks or Onshape capture, build, verification and pull-request submission. `submit.ps1` is the local entry point. |
| Linux workstation | Onshape capture, or a frozen CAD snapshot, followed by an independent build, verification and pull-request submission. |
| Release verification | Fetch the exact candidate from the remote in a local environment matching the tool lock and accept it independently; CI may also do this. |

By default one machine completes the author flow, and both platforms call the same
`description model update`. The Windows restriction belongs to native SolidWorks capture only; a
complete snapshot is usable without CAD. Remote capture is an optional deployment whose connection
is established and released with the job.

Native SolidWorks capture needs a valid licence and a logged-on Windows desktop session. The capture
side calls the COM API serially in a single-threaded apartment (STA) and reads a copy of the saved
assembly and its complete dependencies, isolated from the user's CAD session.

A tool change must be tested against a baseline model with an analytic solution, must verify its
error detection through error injection, and must be regression-tested in the target software. The
SolidWorks interface must be measured on a Windows machine running SolidWorks. Engineering
acceptance must also walk both CAD sources through to release and verify cross-machine offline
rebuilds and interrupted-run recovery.

## Part 4: how to operate it

The steps below use a single hardware model to complete authoring, acceptance, delivery and
iteration in order. First modelling must fix the mechanical definition and its parameter evidence;
afterwards, day-to-day updates use the one-command entry.

### 1. Choose the target and create the model workspace

First decide the hardware id, the CAD assembly and configuration, and the model's purpose and target
software. Install the chosen tool version and run `description model init` to create the model
development branch, the definition template and the tool lock.

Once the structural design is complete, the rigid-body split, joint coordinates and limits,
reference frames and parameter evidence must be written into the robot definition. Reuse the
definition when that hardware already has one; when only the CAD machine changes, update the host
and source paths instead of creating the hardware model again.

Record the modelling scope, required parameters, verification items and error tolerances in the
robot definition and the purpose configurations. These requirements decide which data must be
captured and filled in later, and they define the model's acceptance criteria.

### 2. Prepare CAD and the capture environment

Check the CAD assembly configuration, material information, reference pose and external references.
For Onshape, name the document, assembly and configuration and set up credentials; for SolidWorks,
save the assembly and its dependencies and deploy a capture side matching the tool version on the
Windows machine.

The Windows installation package contains `worker.ps1`, `submit.ps1`, host configuration templates
and dependency locks. After filling in the worker configuration, run `Install` to install and start
it, then use `Doctor` to check that the actual assembly is collectable. The capture side switches
versions while idle and falls back when a self-check fails.

On the chosen Windows or Linux machine, install the complete tool pinned by the model, prepare a
dedicated model workspace, Git/LFS and a GitHub login. Push the model development branch to the
remote after the first initialisation. Windows' `submit-host.json` only needs the local model
workspace and purpose; native capture keeps the desktop logged in and the worker available.

Maintainers provide the complete installation package and its digests. The operator completes the
installation and a Doctor check of the actual assembly on the CAD machine; see the
[first-use guide](solidworks-first-use.en.md) for the concrete steps.

### 3. Freeze the source and confirm the capture scope

Run `description source freeze` to capture the assembly, geometry, raw physical data and complete
dependencies at the chosen revision and configuration. The tool checks the revision, configuration,
instance inventory and file integrity automatically, and the modeller confirms that the included
parts and exclusions match the modelling scope.

The finished source snapshot contains the source identity, revision, configuration, stable object
identities and file digests. A missing dependency, an unclear configuration or a failed capture
keeps its diagnostics and is captured again after a fix; only a complete snapshot enters the build.

A capture job records its job identity and input identity; after a network outage or a CAD timeout it
can be retried or resumed as long as the inputs are unchanged, and unfinished results stay isolated.
Cleanup only ever touches that job's own processes, never the user's CAD session.

### 4. Complete the robot definition and parameter evidence

Using the stable object identities from the snapshot, complete the base and rigid-body split, joint
axes and zero positions, limits and constraints, actuators, sensors and control mapping in
`config/robot.yaml`, and choose the collision strategy.

Data CAD already provides is referenced directly from the snapshot; missing or overridden physical
and control parameters come from specifications, calibration or measurements, with their evidence
and rationale recorded. Check the inputs item by item against the purpose requirements from step 1
and state what data is still missing.

After the rigid bodies, joints or reference frames in the source configuration change, run
`description source freeze` again so the snapshot matches the final definition.
Every movable joint has to be registered in `config/joint_names.yaml`: it is a delivery declaration
(`URDF208`), and the empty template `description model init` writes keeps a candidate from qualifying
until the list is complete.

### 5. Build, review and correct

Run `description build` to generate the canonical model, URDF, MJCF, meshes and configuration from
the robot definition, the purpose configuration and the source snapshot, run the local independent
verification, and produce the local acceptance report and file manifest.

During first modelling, check that the object mapping, zero positions and joint motion match the
design; in later iterations use `description diff` against the previous release or candidate commit
to review changes in mass properties, kinematics, collisions and interfaces - it answers with one
sentence first (changed areas, object counts, whether the delivery digest moved) and leaves the
full JSON report to machine readers. When a check fails,
use the report to locate the source, definition or tool problem and choose a correction path from
step 8.

A purpose that needs application tests first runs the experiments against the complete candidate and
saves the record, then rebuilds the same input identity. Simulation runs automatically inside
`description model update --profile simulation` and can also be run separately with
`description model accept`; later verification replays the experiments and compares the results.
Training and hardware need their own independent evidence. A failure keeps the candidate and its
diagnostics.

### 6. Submit the candidate in one command and complete the review

After first modelling, save the CAD or update the definition and choose the entry point for your
source:

| Entry point | Command |
|---|---|
| Windows deployment directory | `powershell -ExecutionPolicy Bypass -File .\submit.ps1` |
| Linux release bundle | `bash /path/to/bundle/submit.sh --root /path/to/model` |
| Local model workspace, capture again | `description model update` |
| Local model workspace, source unchanged | `description model update --reuse-source` |
| Optional: Linux connecting to remote SolidWorks | `description model update --worker-host windows-cad` |

On Windows the local workspace and purpose come from `submit-host.json`; the CLI defaults to the
current directory and `kinematics` and accepts `--root` and `--profile`. Only cross-machine mode
needs an SSH alias. Reuse mode still checks the source configuration and snapshot digests and
captures again when the source changed.

The entry runs preflight, freezes or reuses the source, builds and verifies, pushes the candidate and
creates or updates the pull request in order. Re-running on the same review branch updates the
existing pull request, and the result gives the model SHA, the pull-request link and the local
verification results. A failure keeps its diagnostics and any completed commit state. When the build
was done step by step, `description model submit` can submit directly.

After reviewing the inputs, the model diff and the verification report, merge the candidate into the
matching model development branch. A successful submission only means the candidate entered review;
release still needs the independent acceptance in the next step.

### 7. Release and connect the consumer

After the candidate is merged into the matching model development branch, run
`description model promote` with the pinned tool. The command fetches the exact commit from the
remote into a fresh Git/LFS store and re-checks the source, the actual artifacts, the tool
environment and the purpose evidence. The acceptance result is bound to the model SHA, the file
digests, the purpose and the runtime; any change requires a new acceptance.

The delivery package contains the model artifacts, all runtime dependencies, the purpose
configuration, the acceptance report and the file manifest. Only after every check passes is the
release branch fast-forwarded to that same accepted model development commit. A fetch or
verification failure keeps the existing release, and a release only claims the purposes that passed
acceptance. GitHub Actions are currently disabled and are not a prerequisite for independent
verification.

A consumer pins the model version by commit SHA. On first integration or after an environment
change, run `description check` to re-verify delivery integrity and the purpose and environment
scope recorded in the report. Outside that scope, add the missing acceptance first, and only then
use the model entry points defined in part 3.

### 8. Keep iterating from the change

Later changes start from the affected input step:

| Change | Action |
|---|---|
| CAD, assembly configuration or references changed | Save the CAD, update the source configuration, and use the one-command entry to capture, build and submit again. |
| Robot definition, specifications or calibration data changed | Update the definition or the evidence; rebuild and submit with `--reuse-source` when the source configuration is unchanged, otherwise capture again. |
| Purpose, target software or runtime changed | Update the purpose configuration and the environment record, rebuild and complete the acceptance for that purpose. |
| Tool or capture program changed | Install the new version, update the tool lock with `description tool lock` and build; when the capture logic or the snapshot format is incompatible, update the capture side and freeze the source again first. |

New artifacts keep going through diff review, independent acceptance and release. When a consumer
needs to roll back, choose the previous accepted commit that still matches the current hardware,
purpose and environment; release a new commit after the fix, and the release branch still only moves
forward.
