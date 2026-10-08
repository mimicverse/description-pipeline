# Operations

Operators use one authenticated page: **SolidWorks folder → run → checks,
URDF preview and review PR**. Installation belongs to
[deployment](deployment.md).

## 1. Complete the engineering model

Follow the [mechanical specification](mechanical-handoff-spec.md). Establish
scope, names, rigid connections, motion, datums, zero, signed directions,
limits, materials and component identities in the native engineering model.

Resolve drive specifications and independent mass/dimension budgets through
fixed controlled records identified by CAD. Responsible engineers confirm
facts that automatic consistency checks cannot establish, including actual
assembly behavior, full-range clearance and applicability of specifications.

Do not prepare pipeline YAML, revision manifests or exported URDF files.
The pipeline derives those artifacts from the saved engineering facts.
Missing or ambiguous facts produce findings requiring correction at their source.

## 2. Collect and save a controlled version

Save the designated delivery configuration and collect its native dependencies
with Pack and Go or an equivalent method. Reopen the collected assembly without
relying on the original directory and confirm the intended configuration.

Retain the product identity, structural revision, owner and change record in
native engineering properties or linked controlled records. A submitted version
must remain retrievable and unchanged. Engineering corrections create a new
structural version.

Make the folder accessible to the platform. The path may name a directory on
the Linux server or the configured Windows worker. Linux folders must be within
`SOLIDWORKS_HANDOFF_ROOT`; Windows folders must be within the endpoint's
`handoff_roots`. A browser cannot read an arbitrary folder on another computer
merely from its path.

## 3. Start and inspect the run

Sign in to the operator page with Feishu. Select the engineering
folder and click **Start**. Hardware, revision, repository and branch are derived
or configured by the platform; they are not operator form fields.

The page shows the [six engineering steps](design.md#2-how-the-system-works):
freeze inputs, discover structure, capture evidence, generate URDF, verify and
publish the review PR. Open each step's **Input**, **Input QC**, **Output** and
**Output QC** to inspect checks, affected objects, recorded values and file hashes.
Generation-input inspection belongs to capture; it is not another operator step.

A completed step requires its boundary checks to pass. Failed checks retain their
diagnostics, and later steps show blocked. Checks whose prerequisites failed show
not run. Each run records which steps actually ran; steps outside the run are
shown as not executed. Engineering confirmations stay pending under the relevant
step, and unsupported items never become implicit passes.
Airflow's DAG documentation shows the contract; `wait_for_job` logs and its
`engineering_stages` XCom retain terminal results even when `confirm_job` is blocked.
The detailed local receipt is `reports/stages.json`.

Airflow transport retries retain the frozen input and native job UUID; they
reconnect to the existing job without repeating capture. A terminal native failure,
including an endpoint restart during capture, requires a new run. Changed inputs
also require a new run. Automatic checks and engineering confirmations remain
separate; pending or unsupported items never become implicit passes.

## 4. Correct findings

Use the affected CAD object, mate, configuration, property or specification
reference shown in the result to locate the problem.

| Finding | Correction |
|---|---|
| Dependency, configuration or saved-state error | Repair and save the native engineering package |
| Ambiguous body, joint, name or frame | Correct native connections, approved names and reference geometry |
| Zero, direction, limit or drive disagreement | Correct its mechanical definition or controlled specification |
| Material, mass or inertia disagreement | Correct material assignments, scope or the documented physical source |
| Independent verification disagreement | Trace the native evidence and generation rule; preserve the failed diagnostics |
| Infrastructure error | Restore the service; inspect diagnostics and start a new run if the native job failed |
| PR error | Restore publication access and submit the retained verified delivery |

Return corrections to CAD, controlled records or tool rules, then submit a new
version. Do not edit generated definitions, XML, meshes or reports. A failed
required quality gate prevents publication. A publication-service failure
retains the verified delivery and any available commit/PR receipt for recovery.

## 5. Review and release the model

On the same page, inspect the actual verified URDF. Check body placement,
individual positive motions, limits, endpoint poses, mirrored occurrences and
tool/sensor frames against the native engineering state and reports.

Confirm that input revision, tool identity, verified subject, commit and
engineering confirmations refer to the same version. Review the PR and approve
the intended uses before freezing the model release. Automatic PR creation is
candidate submission; model release requires engineering approval.
Record approvals in the subject-bound PR or controlled engineering records.

URDF loading, kinematic consistency, simulation, training and hardware control
have separate acceptance criteria. Retain native inputs and bound evidence
with the approved model.

## 6. Independent review and recovery

Platform maintainers use the recorded tool environment to verify or rebuild a
complete frozen delivery on Linux or Windows without opening SolidWorks:

```sh
description check /path/to/delivery
description rebuild /path/to/delivery --output /path/to/rebuilt-delivery
```

These commands require a complete delivery. A failed or interrupted capture
cannot be rebuilt from partial evidence; preserve its diagnostics and start a new
run after resolving the cause.

Maintenance runs declare a narrower `execution_scope`: `description rebuild`
executes generation and verification (plus publication when a repository is
given) against the frozen evidence, and `description submit` executes publication
only. They do not reopen or re-qualify the native stages freeze, discover or
capture; their `reports/stages.json` marks those stages as out of scope. The
detailed six-stage completeness belongs to a complete endpoint job.

For publication recovery, use the retained verified delivery and a dedicated
clean model clone with GitHub access:

```sh
description submit /path/to/verified-delivery --repository /path/to/model-clone --base feature/arm
```

Rebuild preserves native evidence and generated definitions. Submission
reverifies the copied delivery and committed Git blobs before updating its PR.
These maintenance commands do not create an alternative engineering input path.
