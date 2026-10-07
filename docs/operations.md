# Operations

## Operator workflow

The target workflow is **one URL → login → one handoff-folder path → progress →
verified URDF and review PR**. The mechanical team prepares and reviews the
folder; the operator selects it; the model reviewer assesses the delivery.

1. **Prepare** the revision directory as
   [mechanical-handoff-spec.md](mechanical-handoff-spec.md) requires —
   native dependencies, body datums and stable shaft references, `robot.yaml`,
   limit and material evidence, and reviewed mass/size bounds. Section 1 below
   covers preparation: `description revision` seals the CAD files and
   `description inspect` must pass without errors; review its warnings before
   opening CAD.
2. **Select or paste** the sealed folder path on the operator page and start the
   run. The pipeline reads the identity from `cad-revision.json`.
3. **Follow progress** through validation, native capture, generation,
   verification and submission.
   Routing follows the package's `hardware_id`; the operator does not name an
   endpoint, repository, branch or digest.
4. **Inspect the result**: the actual verified URDF with joint and limit
   interaction, the quality decision and the review PR. A failing run retains
   its diagnostics. A failed quality gate prevents submission. Retrying the same
   DAG run reuses its native job; starting a new DAG run creates a new job.

The operator interface is a single path field. Its value may be an absolute
folder on the Linux orchestration host (transferred as an authenticated ZIP), an
absolute folder on the Windows worker (copied), or a folder relative to the
configured `package_root`. The pipeline freezes the received author bytes,
derives the sealed revision and inventory digests, and routes by `hardware_id`.

Incomplete or inconsistent folders and unknown or ambiguous hardware routes
are rejected before CAD opens. A frozen input changed while queued fails;
changes to the original folder do not alter an already frozen copy.
There are no digest, repository, branch or connection fields for the operator to
fill in.

**Availability.** The single-path DAG and embedded viewer are not yet released
and commissioned. v1.0.0 uses the existing Airflow form; its executable trigger
is in [deployment status](deployment.md#release-and-deployment-status). Sections
1 and 3–6 below also document the available local commands. Infrastructure,
accounts, roots, routing and worker setup belong to [deployment.md](deployment.md).

## 1. Prepare the mechanical handoff

Prepare the assembly, body datums and stable shaft references on the SolidWorks
computer as specified in [mechanical-handoff-spec.md](mechanical-handoff-spec.md).
Force-rebuild and save the selected configuration, collect its dependencies
into a revision directory, then reopen the collected copy. Complete `robot.yaml`,
limits/material evidence, reviewed mass/size bounds and the mechanical review.

The directory must be complete before sealing. A new CAD design uses a new
revision directory; retain the previous published handoff.

```powershell
description revision C:\handoffs\arm\r2 --hardware arm --id r2 --parent r1 --owner mechanical --control pdm --reference "PDM/arm/r2" --summary "Updated wrist travel"
description inspect C:\handoffs\arm\r2 --report C:\reviews\arm-r2-input.json
```

`inspect` must return `passed: true` with no errors. Review warnings, including
the notice that CAD datum existence still requires native capture. Static
inspection does not prove that SolidWorks can resolve dependencies, read the
datums or confirm their mechanical meaning.

## 2. Confirm execution readiness

The platform maintainer commissions the [Linux server and Windows worker](deployment.md)
once. Before submitting a handoff, confirm access to the operator URL and that
its hardware route is configured. The selected folder must be readable by the
Linux server or Windows worker that receives it; a path on an unrelated laptop
is not accessible merely because it was pasted into a browser.

Authoring, sealing and static inspection can run on Linux or Windows. Fresh
capture requires the commissioned, licensed SolidWorks Windows worker;
frozen-delivery checks and rebuilds do not require CAD.

## 3. Execute and submit

For released v1.0.0, use the [existing Airflow trigger](deployment.md#linux-scheduler)
or the local command below. The following single-path instructions describe
the target deployment.

For the target deployment, enter the folder path on the operator page or
trigger `solidworks_to_urdf` with the same `handoff_path` field through its API.
Everything else — validation, revision and inventory digests, hardware routing,
the Windows endpoint connection and the review expectations — comes from the
package and the deployment described in [deployment.md](deployment.md).

The DAG freezes and validates the input, queues one Windows job, waits for
verification and confirms its PR receipt. View the logs and final result for the run
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

Retries within one Airflow run reuse its job UUID and cannot trigger duplicate
captures.
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
