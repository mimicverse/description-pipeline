# Operations

Operators use one authenticated page: **SolidWorks folder → run → checks,
URDF preview and review PR**. Installation belongs to
[deployment](deployment.md).

## 1. Complete the engineering model

Follow the [mechanical specification](mechanical-handoff-spec.md). Establish
scope, names, rigid connections, motion, datums, zero, signed directions,
limits, materials and component identities in the native engineering model.

Resolve drive specifications and independent mass/dimension budgets through
CAD references to approved, versioned records in the platform's controlled library.
Mechanical engineers maintain these references in SolidWorks; copying a record
into the submitted folder does not override the library. Responsible engineers confirm
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

For an existing model, `dp.parent_revision` names the structural revision in its
current review-branch delivery, or in the configured base if no review delivery
exists. Each new delivery continues that baseline. Reusing a revision requires
identical revision content.

Choose the complete project folder from your own computer in the operator page.
The browser uploads its files, preserving relative paths, to the platform's
dedicated Linux intake before anything is frozen; server-side admission applies
the same naming, case and generated-input rules as the native handoff
(`robot.yaml` and `cad-revision.json` are generated and must not be included).
Platform-managed paths are never entered by hand.

When the folder contains more than one saved `.SLDASM`, the page always lists their
relative paths and you must choose the main assembly before starting; a single
assembly is selected automatically. The choice is recorded with the frozen evidence
and kept by retries and reruns, and it resolves only the entry point — identity,
dependency and physical checks still apply to the native data.

## 3. Start and inspect the run

Sign in to the operator page with Feishu. Select the engineering folder and, when
it offers more than one `.SLDASM`, choose the main assembly; then click **Start**.
Hardware, revision, repository and branch are derived or configured by the platform;
they are not operator form fields.

Approved Feishu users may start new pipeline runs and view shared results. A run
records its initiator's stable authenticated identity and shows that operator's
original Feishu username.

### Manage run history

Each record represents one run. The list shows its project name, start time,
status and initiator. A rerun keeps the project name and identifies its starting
step; the detail view links to the original run. A display name is a label for
the record, not a structural revision or a qualification claim.

The initiator and platform administrators can manage a record:

| Action | Effect |
|---|---|
| Rename | Change the display name; the original engineering folder and model identity remain unchanged |
| Delete | Move a completed record to **已删除** for all operators |
| Restore | Return the record from **已删除** to the run list |

Deletion is reversible and does not remove Airflow jobs, native inputs, model
files, verification evidence or PRs. Running jobs and jobs whose execution state
cannot be established cannot be deleted. Deleted records remain readable from
linked runs; restore them before retrying or starting another rerun.

### Rerun from a step

A run's initiator and platform administrators may select any engineering step
and choose **从此步骤重新运行**. The selected step and every downstream step run
in a new linked run; the original run and its diagnostics remain unchanged.
This applies to completed steps as well as failed ones. Other approved users
can view shared results but cannot rerun another operator's work.

| Start from | Reuse after validation | Execute again |
|---|---|---|
| Freeze inputs | Retained uploaded folder | All six steps |
| Discover structure | Frozen input | Discovery through publication |
| Capture evidence | Frozen input and prepared native definition | Capture through publication |
| Generate URDF | Prepared input and complete native evidence | Generation, verification and publication |
| Independent verification | Generated files and their bound evidence | Verification and publication |
| Publish review PR | Independently verified delivery | Publication, including re-verification of the delivery |

The platform checks source digests, tool identity, dependency snapshots and
upstream checkpoints before reuse, and rechecks them when execution starts.
Unavailable prerequisites disable that starting point and identify the earlier
step needed. Reused results retain their original timestamps and parent-run
identity; they are labelled **复用已验证结果**. A repeated submission while the
linked attempt is active returns that attempt instead of creating duplicate work.

Changing CAD requires a new folder upload and a new run from freeze. A tool
change also requires starting from freeze, using the retained unchanged upload
when available. A partial capture cannot serve as complete evidence for generation.
The recorded main assembly is part of the frozen evidence: retries and reruns keep
it, and choosing a different main assembly requires a new folder upload and a new
run.

**继续原作业** is a separate action for recoverable transport failures. It
reconnects the same Airflow run to the same native job without repeating CAD work.
It does not rerun an engineering step.

### Inspect results

Run history and details show the original submitter's verified Feishu username,
including when viewed by another operator. The platform reads the name through
Feishu's authentication API and fills it in automatically.

The page shows the [six engineering steps](design.md#2-how-the-system-works):
freeze inputs, discover structure, capture evidence, generate URDF, verify and
publish the review PR. Each step shows its **Input**, **Input QC**, **Output** and
**Output QC**, with readable check names, criteria, actual results and status.
Failures show the affected object and next action first; file hashes and raw
evidence are available in collapsed details. Without a verified URDF, the report
uses the available workspace instead of reserving an empty preview pane.
Generation-input inspection belongs to capture; it is not another operator step.

During native work, the activity card shows the operation reported by the worker,
the current CAD object, available item counts and the last progress update. Recent
activity explains the work between boundary checks. A check count such as **1/3**
means one of three checks passed; it is not a completion percentage. Item counts
cover only the named operation and do not predict the remaining run time.

Queued work, reported activity and a finished run have distinct states. If no new
activity arrives, the page shows how old the last update is; successful polling
does not prove that CAD is advancing, and silence does not prove it has stalled.
Runs without recorded activity explicitly report that detailed progress is
unavailable. A failed run retains its last activity and check diagnostics.

A completed step requires its boundary checks to pass. Failed checks retain their
diagnostics, and later steps show blocked. Checks whose prerequisites failed show
not run. Each run records which steps actually ran; steps outside the run are
shown as not executed. Passed automatic checks need no repeated manual sign-off.
After verification, the engineering view lists only facts outside automatic
coverage; it does not claim that external approvals are pending or complete.
Unsupported items never become implicit passes.
The detailed delivery receipt is `reports/stages.json`. Maintainers can also
inspect native results in Airflow's `wait_for_job` task logs and portable results in
the generation, verification and publication task logs; see [deployment maintenance](deployment.md#maintenance).

An engineer cannot override a failed automatic check by signing a review.
Existing approvals may be reused only while the relevant structure,
configuration and supporting facts remain applicable; review changes and their
effects in the matching PR or controlled record.

## 4. Correct findings

Use the affected CAD object, mate, configuration, property or specification
reference shown in the result to locate the problem.

Finding counts describe recorded observations, not a count of defective parts.
One unresolved connection or ownership rule can affect many instances. Review
the evidence before deciding whether the correction belongs in CAD or the reader;
an unsupported native pattern is not proof of a mechanical design error.

| Finding | Correction |
|---|---|
| Dependency, configuration or saved-state error | Repair and save the native engineering package |
| Ambiguous body, joint, name or frame | Check native evidence coverage, then correct the reader or the unresolved engineering definition |
| Zero, direction, limit or drive disagreement | Correct its mechanical definition or controlled specification |
| Material, mass or inertia disagreement | Correct material assignments, scope or the documented physical source |
| Independent verification disagreement | Trace the native evidence and generation rule; preserve the failed diagnostics |
| Infrastructure error | Restore the service; continue transport or rerun from the earliest valid engineering step shown |
| PR error | Restore publication access and rerun from publication using the retained verified delivery |

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
complete frozen delivery on Linux without opening SolidWorks:

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
detailed six-stage completeness belongs to a complete Airflow run.

For publication recovery, use the retained verified delivery and a dedicated
clean model clone with GitHub access. Work on a copy of the delivery so the
failed run and its diagnostic receipts remain intact:

```sh
description submit /path/to/verified-delivery --repository /path/to/model-clone --base feature/arm
```

Rebuild preserves native evidence and generated definitions. Submission
reverifies the copied delivery and committed Git blobs before updating its PR.
These maintenance commands do not create an alternative engineering input path.
