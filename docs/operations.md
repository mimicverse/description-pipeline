# Operations

## 1. Prepare the mechanical handoff

On the SolidWorks computer, save the selected assembly configuration and collect
its native dependencies into a revision directory. Create the body datums and
stable shaft references specified in the [mechanical handoff specification](mechanical-handoff-spec.md). Complete
`robot.yaml`, limits/material evidence and reviewed mass/size bounds.

The directory must be complete before sealing. A new CAD design uses a new
revision directory; retain the previous published handoff.

```powershell
description revision C:\handoffs\arm\r2 --hardware arm --id r2 --parent r1 --owner mechanical --control pdm --reference "PDM/arm/r2" --summary "Updated wrist travel"
description inspect C:\handoffs\arm\r2 --report C:\reviews\arm-r2-input.json
```

`inspect` reports all static findings. Resolve them before opening a pipeline
CAD session. It does not assert that SolidWorks can resolve references or read
the required datums; native capture establishes those facts.

## 2. Set up the execution computer

Install the pinned release in a dedicated Python 3.12 environment. Fresh capture
requires licensed SolidWorks revision 34 and a logged-in interactive Windows
desktop. Keep the computer awake during jobs. Git and GitHub CLI are required
for submission. Configure your Git commit identity, authenticate GitHub once,
and create a clean dedicated model clone.

```powershell
description doctor
gh auth status
gh auth setup-git
git clone https://github.com/<owner>/<model-repository>.git C:\description\models
```

Replace `<owner>/<model-repository>` with the repository assigned to your models.
`doctor` distinguishes runtime availability from native capture availability.
It does not open CAD or certify a particular package. Use the model branch
assigned to that hardware. If it does not exist, a model owner creates it once
in this dedicated clean clone:

```powershell
git -C C:\description\models switch --orphan feature/arm
Set-Content -Encoding utf8 C:\description\models\README.md "# arm model"
git -C C:\description\models add README.md
git -C C:\description\models commit -m "Initialize arm model branch"
git -C C:\description\models push origin feature/arm
```

This branch contains model data; its history is separate from tool source.
Existing hardware branches require no initialization.

## 3. Execute and submit

Use the Airflow UI to trigger `solidworks_to_urdf`, or submit to the same DAG
through its API. Supply the package path relative to the Windows handoff root,
the exact `cad-revision.json` digest, and the configured repository target and
review expectations described in [deployment.md](deployment.md).

The DAG validates the request, queues one Windows job, waits for verification
and confirms its PR receipt. View the task logs and final result for the run
UUID, quality decision, commit and PR URL. Airflow is the standard operator
entry; no Linux-to-Windows command sequence is required for each handoff.

For worker commissioning and diagnosis, execute the same workflow locally:

```powershell
description run C:\handoffs\arm\r2 --output C:\deliveries\arm --repository C:\description\models --base feature/arm --message "Update arm wrist travel from mechanical r2"
```

The command inspects, captures, generates, verifies and submits. It prints a run
UUID, quality decision and PR receipt. Only a passing delivery reaches the PR
stage. Subsequent passing deliveries update the same hardware review branch
and open PR; the publisher does not force-push or merge it.

Omit `--repository` to build and verify locally. The output must be separate from
the handoff and repository. The pipeline replaces only an output it owns and
whose inventory is intact. Operator annotations cause replacement to be refused.

Follow [deployment.md](deployment.md) once to install the endpoint, connection
and scheduler. Subsequent handoffs use the same Airflow interface.

## 4. Diagnose a failed run

Use the returned `diagnostic_path`. `reports/input.json` records static findings;
`reports/quality.json` records independent artifact findings when generation
reached verification. `reports/run.json` identifies the failed stage. Native
capture failures retain their partial snapshot and `failure.json`.

| Failed stage | Corrective action |
|---|---|
| Inspect | Repair YAML, evidence bindings, inventory or mechanical revision |
| Capture | Repair CAD closure/configuration/datums/shaft selector/materials; inspect native error |
| Generate | Repair unsupported semantics or physically invalid source values |
| Verify | Follow each failed gate to its author input, raw reading or generation rule |
| Submit | Restore repository/authentication/PR service; retain the verified bundle and receipt |

Never repair generated XML or mesh files directly. Correct author inputs or tool
rules and rebuild. Changed CAD requires a new revision. A failed submission
preserves the pushed SHA when GitHub fails after the push; retry submission
without recapturing CAD:

```powershell
description submit C:\deliveries\arm --repository C:\description\models --base feature/arm
```

Airflow retries reuse the same job UUID and cannot trigger duplicate captures.
An endpoint restart marks an interrupted running job failed. After investigating
its diagnostics, use a new DAG run for a corrected handoff. A changed request
cannot reuse the old UUID.

## 5. Review and release the model

Review the PR's native CAD revision, body grouping, datum and axis meanings,
limits and physical authority. Inspect `reports/quality.json` and verify that
the delivery's subject matches the PR commit. Preview `urdf/robot.urdf` with
its adjacent `meshes/` in the team's URDF viewer. Include left/right and limit
pose review where relevant to the hardware.

```sh
description check /path/to/reviewed/delivery
```

Approval of the input meanings and independent checks precedes the model
release. Keep the approved input, evidence and reports together with the URDF.
Simulation, training and hardware use need their additional acceptance records.

## 6. Rebuild on Linux or Windows

Copy a complete verified frozen delivery. Install the same tool release, then:

```sh
description check /path/to/frozen-delivery
description rebuild /path/to/frozen-delivery --output /path/to/rebuilt-delivery
description submit /path/to/rebuilt-delivery --repository /path/to/model-clone --base feature/arm
```

Rebuild uses archived native readings and requires no CAD process. With the same
code and inputs, the canonical model, URDF and mesh bytes reproduce; the current
runtime is separately recorded in `reports/tool.json`. A fresh native export on
Linux requires the deployed Windows endpoint.
