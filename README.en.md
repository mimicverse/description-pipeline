# description

A CAD → canonical model → URDF / MJCF pipeline: **one source of truth, automated derivation, and
publication only after independent verification.**

[Design](docs/design.en.md) · [Runbook](docs/pipeline.en.md) · [Validation status](docs/validation.md) · [Downloads](https://github.com/mimicverse/description-pipeline/releases/latest) · [Apache-2.0](LICENSE)

**First SolidWorks assembly?** Follow the five-step [first-run
checklist](docs/solidworks-first-use.en.md#first-run-checklist) to install the tool on one Windows
machine, complete the robot definition and open your first pull request. The time needed to define
the links, joints and physical parameters depends on the design; the [first-use
guide](docs/solidworks-first-use.en.md) gives the commands and failure handling.

> Every current user-facing and engineering document exists in English; the Chinese originals stay
> authoritative when the two ever disagree.

## Try it without CAD

One command writes an offline example workspace: it runs freeze → build → check from a hand-written
fixture source, with no CAD account, no network and no SolidWorks.

```sh
description quickstart --run
```

`--run` executes `tool lock → source freeze → build → check` in order and prints
`qualified_for: ["kinematics"]`; without it only the workspace is written (to `./demo-arm` by
default), together with the four commands to run next. A checkout carries the same workspace as
[`examples/demo-arm`](examples/demo-arm/). The whole chain takes about five seconds on an ordinary
laptop, MuJoCo compilation included, with no CAD, account or network.

Start with the self-check: `description doctor` reports this machine's environment (Python,
dependencies, MuJoCo, git/git-lfs, and `gh` with `--github`), and adding `--root` also checks the
tool lock, the source snapshot and the purpose profiles of a model workspace. Every failure names the
command that fixes it.

```sh
cd demo-arm
description doctor --root .
description tool lock --root .        # re-pin the tool lock to your install (demos only)
description source freeze --root .
description build --root . --profile kinematics
description check --root . --profile kinematics
```

The outputs are `urdf/robot.urdf`, `mjcf/robot.xml`, `mjcf/scene.xml` and `docs/quality.*`. The check
re-derives the model from the frozen source, compiles it with MuJoCo and reports
`qualified_for: ["kinematics"]`. The example is fixture evidence (`evidence_class: fixture`), not a
native CAD capture; to see the pipeline reject tampering, change one joint limit and run the check
again as described in the example README.
[`examples/mesh-arm`](examples/mesh-arm/) is the second offline example: the same flow with binary STL
meshes and the independent uniform-density geometry oracle.

## One command to submit

Once the model and the host are set up, one command does the whole job on Windows or Linux:

```text
preflight → capture and freeze CAD → generate URDF / MJCF / meshes / config
          → local verification → push the candidate and create or update the pull request
```

**Windows.** Save the SolidWorks assembly and its references, keep the machine awake with the worker
running, then from the deployment directory:

```powershell
powershell -ExecutionPolicy Bypass -File .\submit.ps1
```

The script reads the local model workspace and purpose from `submit-host.json` next to it and uses
the installed runtime. CAD capture, the build, MuJoCo verification and the Git commit all happen on
that machine. With `simulation` as the purpose and complete experiment definitions, the entry also
runs the experiments, updates the acceptance evidence, revalidates and submits only after they pass.

**Linux.** The Linux release bundle includes `install.sh` and `submit.sh`. After extracting it, run
`bash install.sh` once (or set `DESCRIPTION_VENV` for a shared environment), then use the launcher
from any directory:

```sh
bash /path/to/bundle/submit.sh --root /path/to/model --profile kinematics
```

It invokes the same `description model update` pipeline as Windows. You can also activate the pinned
environment and call that command directly. Pick the mode that matches your change:

| Situation | Command |
| --- | --- |
| Onshape: capture again through the API | `description model update` |
| Source unchanged; only definitions, purpose or evidence changed | `description model update --reuse-source` |
| Optional remote SolidWorks capture | `description model update --worker-host windows-cad` |

`windows-cad` is an SSH host alias; the command opens and closes the connection itself, and the
Windows host must stay capturable. `--reuse-source` verifies and reuses the existing snapshot without
touching CAD; a changed CAD state or source configuration requires a new capture. On Linux the
defaults are the current directory, the `kinematics` purpose and an English commit message;
`--root`, `--profile` and `--message` override them.

Both platforms use the same pipeline and lock their own model workspace. Re-running on the same
review branch updates the existing pull request. Failures keep their diagnostics and any completed
commit state; a failed pull-request creation prints the retry command. The default flow never calls
GitHub CI.

**The one-command entry submits a candidate.** After it lands on the model development branch, the
release command fetches the complete candidate from the remote and qualifies it independently;
only then can it be released. Kinematics, simulation, training and hardware control are qualified
separately.

The tool environment and submission configuration are set up once. Linux handles Onshape and
existing snapshots on its own; native SolidWorks capture requires Windows. For a new machine follow
the [first-use guide](docs/solidworks-first-use.en.md); for remote capture see the
[SolidWorks manual](docs/sources/solidworks.en.md#optional-remote-submission).

## From authoring to delivery

A first model has to fix the mechanical definition, the evidence for its parameters and the purpose
requirements:

Keep CAD and model evidence in a writable repository you control. For a private model, seed its
`main` from this public tool repository before `model init`:

```sh
MODEL_REPO=your-account/myrobot-description
gh repo create "$MODEL_REPO" --private
git clone https://github.com/mimicverse/description-pipeline.git /path/to/description
git -C /path/to/description remote rename origin upstream
git -C /path/to/description remote add origin "https://github.com/$MODEL_REPO.git"
git -C /path/to/description push -u origin main
```

`model init --repository /path/to/description` then creates the hardware branch in that private
repository. The public repository supplies tool updates; it never receives your CAD. On Windows,
the [first-use guide](docs/solidworks-first-use.en.md) gives the PowerShell form.

1. **Choose the target and initialize.** Pick the hardware, the CAD configuration and the purpose,
   and install the chosen tool as described in the offline package README. Run `description model
   init --repository /path/to/description --root /path/to/model --hardware myrobot --provider
   solidworks --assembly D:/robots/myrobot/robot.SLDASM --configuration Default` (Onshape:
   `--provider onshape --url <document URL>`; pass `--source-config source.yaml` when extra source
   keys are needed), then check the workspace with `description doctor --root /path/to/model`.
2. **Prepare the environment.** On the machine you chose: the full tool, a dedicated model
   workspace, Git/LFS and an authenticated `gh`, with the writable model repository reachable on the
   remote. Configure Onshape credentials, or install the Windows worker for SolidWorks and pass
   Doctor. Source configuration: [Onshape](docs/sources/onshape.en.md),
   [SolidWorks](docs/sources/solidworks.en.md).
3. **Freeze the source.** Run `description source freeze --root /path/to/model` and check the
   revision, configuration, instances and exclusions.
4. **Complete the definition.** Declare the mechanical semantics and the evidence for parameters in
   `config/robot.yaml`, and register every movable joint in `config/joint_names.yaml` (`model init`
   writes the template; a missing or stale ledger is refused with `URDF208`). Fix tolerances, contact
   and application acceptance in the purpose profiles. If you change rigid bodies, joints or other
   source configuration, freeze again.
5. **Build and review.** Run `description build --root /path/to/model --profile kinematics`. Review
   later changes with `description diff OLD_SHA /path/to/model --repository /path/to/model`: one
   sentence on stderr names the areas and objects that changed and whether the delivery digest
   moved, the full JSON report stays on stdout (redirect it to a file to keep it), and `--json`
   prints that report alone. Failure diagnostics stay in `build/`; fix the input and rebuild.
6. **Application acceptance and submission.** For simulation, run the declared experiments with
   `description model accept` as described in the [simulation guide](docs/simulation.en.md) and rebuild;
   for other purposes supply the independent evidence their procedure requires. Then submit the
   candidate with `description model submit --root /path/to/model --profile kinematics --message
   "Update model"`, replacing the profile with the target purpose. Later checks and releases replay
   the experiments, so no CI is required.
7. **Qualify and release.** Once the candidate is on its feature branch, run the command below with
   the model's pinned tool environment to inspect the plan; add `--apply` to execute it. The release
   fetches the complete candidate from the remote, verifies the actual bytes and the purpose
   evidence, and fast-forwards the release branch.

   ```sh
   description model promote --root /path/to/description \
     --hardware myrobot --candidate FULL_MODEL_SHA --profile kinematics
   ```

8. **Consume and iterate.** Check out the released SHA, run `description check --root
   /path/to/model --profile kinematics` to confirm the purpose and environment, then load the model.
   Day-to-day updates use the one-command entry; to roll back, use an older SHA that still matches
   the hardware and environment.

The public entries are fixed at `urdf/robot.urdf`, `mjcf/robot.xml` and `mjcf/scene.xml`; meshes and
supporting configuration ship with the model. A missing parameter, an incomplete source, an
unexecuted check or insufficient evidence keeps the corresponding purpose blocked.

## Repository layout

| Branch | Responsibility |
| --- | --- |
| `main` | the public Python tool, standards and tests; releases do not depend on GitHub Actions |
| `feature/<hardware>` | definitions, source snapshots, model assets and evidence for one robot |
| `release/<hardware>` | the latest accepted release commit; moves forward only |

Models pin the tool commit, package digest and runtime in `config/toolchain.lock.json`; consumers pin
the model commit SHA. The Windows capture and submission scripts live in
`src/description_pipeline/sources/solidworks/deploy/`; the Linux installation and submission
launchers live in `src/description_pipeline/deploy/linux/`. Model templates and deployment resources
ship with the tool package.

## Tool development and packaging

The tool supports CPython 3.12 and MuJoCo 3.13.0. Tested environments are Linux x86_64 with Python
3.12.14 and Windows 11 Home China 25H2 x64 with Python 3.12.10 and SolidWorks 2026 SP3.2. Other
SolidWorks versions have not been tested.

In a `main` tool workspace:

```sh
python3.12 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements/linux-py312.lock
python -m pip install --no-deps --no-build-isolation -e .
python tools/quality.py all
```

`python3.12 -m venv` needs the `python3.12-venv` package on Debian/Ubuntu, and the interpreter `uv`
installs cannot bootstrap `venv` at all (it exits from `ensurepip`); use
`uv venv --python 3.12 --seed .venv` there — `--seed` is what brings `pip`. Users do not need this
step: the installers in the release bundles create the environment themselves and report the same
clear errors.

`python tools/build_release.py --require-clean --offline --out dist/COMMIT` produces the wheel, the
source archive and the complete Linux and Windows offline bundles; use a new output directory every
time. Rebuilding the same commit with the same lock file produces byte-identical artifacts: the
builder normalizes build time, ownership and permissions, so comparing two `SHA256SUMS` files
verifies a release. The release record pins the SHA-256 of exactly those bytes, which is what makes
the distribution independently rebuildable and checkable.

`python tools/verify_distribution.py dist/COMMIT/*-linux-*.zip` verifies installation, relocation
without cache and tamper rejection in a fresh environment.
`python tools/verify_distribution.py dist/COMMIT/*-windows-*.zip --windows` checks the Windows
bundle content and its offline dependency closure; native CAD acceptance runs separately on Windows.

## Engineering references

[Engineering standard](docs/engineering_standard.en.md) · [URDF rules](docs/urdf_standard.en.md) · [Contributing](CONTRIBUTING.en.md) · [Quality entry point](tools/quality.py)

[Simulation acceptance](docs/simulation.en.md) · [Model template](src/description_pipeline/templates/model/README.md)

[Historical Onshape migration](docs/onshape_export.md) · [Historical SolidWorks migration](docs/solidworks_export.md)
