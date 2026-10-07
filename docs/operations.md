# Operations

This guide follows the target workflow from engineering preparation to model
release: **one URL → login → one SolidWorks directory → verification → review PR**.
The mechanical team supplies native engineering; the pipeline generates model
definitions, manifests and reports.

This interface is not yet released. v1.0.0 requires a prepared package and
six-field Airflow trigger maintained by the platform team. See
[deployment status](deployment.md#release-and-deployment-status) and the
[released trigger procedure](../deploy/airflow/README.md#run-and-inspect-a-delivery).

## 1. Prepare the SolidWorks engineering model

Follow the [SolidWorks engineering specification](mechanical-handoff-spec.md).
Confirm assembly organization and dependencies, rigid connections and motion,
root/body/interface datums, mechanical zero, signed directions, limits,
materials and the real component identities used for controlled specifications.
Its responsibility matrix distinguishes automatic consistency checks from the
engineering facts that the responsible engineers must inspect and confirm.

Complete native engineering properties or platform-provided CAD annotations
only where standard assembly contents cannot express a necessary fact. Do not
create `robot.yaml`, revision JSON, pipeline evidence texts or a separate
manual joint/body mapping file.

Review every mechanism's actual connection order, axis relationships, positive
motion, interface attachment and clearance throughout its working range.
For coupled mechanisms, preserve and check the actual coupling.

## 2. Save and collect the engineering directory

Select the declared delivery configuration in SolidWorks. Confirm its zero or
reference pose and any declared zero offsets, force-rebuild, resolve errors
and save all referenced documents. Collect dependencies with
Pack and Go or an equivalent native mechanism. Reopen the collected copy and
verify that it resolves without the original workstation paths.

Retain the controlled structural version. Changed engineering contents create
a new version; the previous input remains available.

The directory must be readable by the Linux server or Windows worker. A path
on a different laptop is not accessible merely because it is pasted into a
browser. Platform maintainers establish shared/drop locations once; operators
need no per-run SSH or transfer command sequence.

## 3. Run from the single operator page

Once commissioned, open the operator URL, log in, select or paste the engineering
directory path and start. Access, routing and run identity are managed by the platform.

The target interface accepts an absolute Linux folder, an absolute folder on
the configured Windows worker, or a path relative to its configured package
root (`package_root`).
Linux inputs are transported automatically; all inputs become a fixed native
inventory before execution.

Follow these stages:

| Stage | Expected result |
|---|---|
| Collect and freeze | Main assembly, source version, configuration and native file inventory recorded |
| Read CAD | Actual instances, mates, datums, materials and engineering annotations captured |
| Derive definition | Robot semantics and applicable specifications resolved; `robot.yaml` or equivalent model generated |
| Build | Canonical model, URDF and local meshes generated |
| Verify | Independent native, physical, XML, mesh and loading gates evaluated |
| Submit | Passing delivery bound to its Git commit and review PR |

The platform resolves the hardware destination from native identity. Missing
or ambiguous identity blocks publication; missing mechanical facts produce
findings at the corresponding CAD object. Parameters are never invented to
make a stage pass.

The required check view separates automatic results from engineering
confirmations. Each item shows its evidence, affected CAD objects and corrective
guidance. Failed, unexecuted, unsupported and unconfirmed items remain visible;
automatic success does not complete an outstanding engineering review.

Retries within one Airflow run reuse its frozen input and native UUID. Starting
a new run creates a new job; changing source contents never silently changes
an already frozen run.

## 4. Resolve findings

The page reports the failed stage and relevant CAD object, property, mate,
configuration or specification source. Diagnostic records remain available to
platform maintainers.

| Finding | Correction |
|---|---|
| Missing dependency or wrong configuration | Repair and save the native engineering package |
| Ambiguous rigid body or joint | Repair hierarchy, mates or the approved native CAD annotation |
| Missing/incorrect datum, zero or positive direction | Repair reference geometry and the declared CAD state |
| Limit or drive specification missing | Correct native limit definitions, component identity or the controlled specification record |
| Material or mass inconsistency | Correct actual material, configuration, physical authority or model simplification |
| Verification disagreement | Trace the finding to CAD evidence, the resolved specification or a generation rule |
| Repository/PR failure | Platform maintainer restores publication; retain the verified delivery |

Correct the source and run a new version. Do not hand-edit generated YAML, XML,
meshes, inertia or reports. A failed quality gate prevents publication. A PR
service failure after a verified push retains the commit/receipt and does not
require recapturing CAD merely to retry publication.

## 5. Review and release the model

Inspect the actual verified URDF on the same operator page. Check root/body
placement, individual positive motions, mirrored-instance differences, ranges,
endpoint poses and interface motion. Compare with the native engineering
state and the independent report, not just the overall silhouette.

Confirm the source version, frozen identity, tool identity, quality subject and
PR commit, and that necessary engineering confirmations apply to that version.
The model reviewer approves mechanical meanings and accepted uses;
a candidate PR is not automatic model release.

A portable copied delivery can be independently rechecked after installing the
recorded tool release and activating its environment:

```sh
description check /path/to/reviewed/delivery
```

URDF loading, kinematic consistency, simulation, training and hardware control
are separate acceptance results. Keep native inputs, generated provenance and
reports with the approved model.

## 6. Platform diagnostics and frozen replay

Platform maintainers use the following commands in the recorded tool environment.
Fresh native reading requires the licensed Windows worker. A complete frozen
delivery can be checked and rebuilt on Linux or Windows without opening CAD:

```sh
description check /path/to/frozen-delivery
description rebuild /path/to/frozen-delivery --output /path/to/rebuilt-delivery
description submit /path/to/rebuilt-delivery --repository /path/to/model-clone --base feature/arm
```

The final command publishes a candidate PR after verification; use a dedicated,
clean model clone with GitHub authentication. Rebuild preserves the
archived native evidence and generated definitions; it does not turn generated
YAML into a human authoring input. Infrastructure, authentication and worker
commissioning belong to [deployment.md](deployment.md).
