# Mechanical kinematics acceptance

English · [中文](mechanical-acceptance.md)

This workflow checks the actual URDF, MuJoCo robot and MuJoCo scene against an independently
acquired reference. A pass qualifies **kinematics against that reference**. It does not establish
physical calibration, actuator performance, contact dynamics, training readiness or hardware safety.

## 1. Establish the mechanical definition

Complete the rigid-body partition, joint identities, signed shaft lines, zero positions, position
limits and couplings in `config/robot.yaml`. For every movable joint, add the mechanical drive
mapping under `interfaces.mechanical_drives`, keyed by **canonical joint name**, not source id:

```yaml
interfaces:
  mechanical_drives:
    shoulder_joint:
      kind: active
      id: shoulder-motor-1
      stator: ["assembly/motor-case-1"]
      rotor: ["assembly/output-shaft-1"]
    passive_joint:
      kind: passive
```

The lists contain exact frozen CAD instance identifiers. Stator instances belong to the joint's
parent body; rotor instances belong to its child body. Identities are unique and the two sets are
disjoint. This mapping describes physical mounting and identity. Simulation actuators, torque
limits and controller parameters remain separate inputs; an active mechanical drive does not
supply those values. The mapping is carried into `model/robot.json` and independently re-derived
from author inputs during verification.

In `config/profiles/kinematics.json`, declare one mechanical suite in `acceptance_suites` and the
target `consumer_environment`, including the installed MuJoCo version. Choose tolerances appropriate
to the mechanism. The mechanical replay currently supports fixed, revolute, continuous, prismatic
and mimic joints; unsupported closed-loop constraints block generation.

## 2. Acquire and approve the independent reference

The design owner records the approved mapping and independently acquired zero and held-out motion
observations. Preserve acquisition evidence, frame conversion, producer, date, CAD configuration
and approval in the reference's `conditions` and the associated engineering evidence.

Store the reference **outside every model workspace**, in the consumer's approved evidence store.
Distribute its SHA-256 with the review. The operator selects that file explicitly; the candidate
cannot nominate its authority. Approval and acquisition provenance are the operator's responsibility:
the software checks agreement with the selected bytes and cannot prove that a file was independently
measured or approved. Copying generated transforms into an external file is not independent evidence.
If observations were used to fit the model, acquire fresh held-out observations before acceptance.

The UTF-8 JSON uses `schema_version: description.mechanical-reference/v1` and these fields:

| Field | Required content |
|---|---|
| `hardware_id`, `source_manifest_digest` | Exact hardware id and `manifest_digest` from `sources/source.lock.json`. |
| `suite`, `environment` | The profile's sole suite and exact `consumer_environment`. |
| `evidence_class`, `data_role`, `used_for_fitting` | `cad` or `fixture`, matching the snapshot; `validation`; `false`. |
| `conditions` | Nonempty acquisition and approval provenance, including the independent conversion from named CAD frames. |
| `conventions` | `{"units":"SI","joint_origin":"parent_link","joint_axis":"joint","poses":"world"}`. |
| `tolerances` | Positive finite `position_m` and `rotation_rad`. Comparison uses the stricter reference/profile tolerance. |
| `ownership` | Every URDF body name mapped to its exact included CAD instance set; generated reference frames have empty sets. |
| `joints` | Every URDF joint, including generated frame joints, mapped to `type`, `parent`, `child`, `origin`, `axis`, `limits`, `mimic`. |
| `drives` | Every movable joint mapped to its approved active/passive mechanical drive definition. |
| `constraints` | Complete approved constraint list; currently empty for supported backends. |
| `zero_pose`, `poses` | Zero-pose name and 2–512 uniquely named observations, covering every joint and body. |

`origin` is a 4×4 transform from the parent link to the joint/child frame at zero. `axis` is a
**signed unit vector in the joint frame**, or `null` for fixed joints. `limits` contains `lower`
and `upper`, or is `null` for fixed/continuous joints. `mimic` is `null` or contains `joint`,
`multiplier` and `offset`. Transform the designer's shaft line from its recorded CAD frame into
these frames independently; the pipeline does not infer that conversion.

Each pose contains `name`, `joints`, `base` and `links`. Joint values use radians or metres;
`base` and every body in `links` are 4×4 world transforms. A fixed-root model uses its root frame
as world and an identity `base`. The zero pose sets every independent joint to zero; dependent
joints follow their approved coupling. Every independent joint needs observable held-out travel
greater than `max(10 × effective tolerance, 1e-6)`. These are sampled observations, not a continuous
workspace guarantee. Duplicate keys, non-finite numbers, incomplete coverage and reused fitting
files are rejected. The reference is limited to 32 MiB and 100,000 pose/body observations.

## 3. Build, accept and submit

After freezing the saved CAD, run the entire author flow on Windows or Linux in the model's locked
environment. Use `--reuse-source` when only the definition or evidence changed:

```sh
description model update --root MODEL --profile kinematics --reuse-source \
  --mechanical-reference /approved/mechanism.json --message "Update mechanical definition"
```

The command builds a candidate. If application acceptance is the sole blocker, it runs the replay
against that preserved candidate, copies successful records, rebuilds and submits the PR. Other
failures stop it. Failed measurements and execution diagnostics are preserved under `build/`;
they do not replace existing acceptance evidence or the delivered artifacts.

The Linux `submit.sh` forwards the same options. On Windows, use
`submit.ps1 -MechanicalReference C:\approved\mechanism.json`, or set `mechanical_reference` in
`submit-host.json`. For optional remote builds the reference path belongs to the **build host**;
the launcher does not transfer it. Native SolidWorks capture still requires Windows, while an
already frozen snapshot can be built and accepted on either platform.

To inspect acceptance before submission, build first and use the report's `diagnostic_path` as
`CANDIDATE` when only application acceptance is missing:

```sh
description build --root MODEL --profile kinematics
description model accept --root CANDIDATE --profile kinematics \
  --mechanical-reference /approved/mechanism.json --out /results/new-run
```

`--out` must be new and outside `CANDIDATE`. Inspect `acceptance.json` and
`docs/acceptance/mechanical-observations.json`. For a successful run, copy its `docs/acceptance/`
files into the model workspace's `docs/acceptance/`, then rebuild with `--mechanical-reference`. Submit an
already accepted build with `description model submit`, passing that option again.

## 4. Validate, release and consume

Each consumer explicitly selects its approved reference; a record alone never qualifies a delivery:

```sh
description check --root MODEL --profile kinematics \
  --mechanical-reference /consumer/approved/mechanism.json
description model validate --root REPOSITORY --candidate FULL_SHA --profile kinematics --remote \
  --mechanical-reference /consumer/approved/mechanism.json
description model promote --root REPOSITORY --hardware HARDWARE --candidate FULL_SHA --profile kinematics \
  --mechanical-reference /consumer/approved/mechanism.json
```

Validation retrieves the exact commit and replays the stored observations. Promotion additionally
requires native CAD evidence, an accepted fixed tool commit and the normal review; inspect the plan
before adding `--apply`. It binds the reference digest and revalidates immediately before publishing.
A released kinematics model retains this limited purpose; other purposes need their own acceptance.
GitHub Actions are not required.

Replay binds the subject, profile, environment, tool, runtime, reference digest and measurement
artifacts. A missing selection is `not_run`; changed bytes or inconsistent measurements fail.
A blocked native runtime is unexecuted and grants no qualification. Correct inputs or obtain an
approved runtime, then rebuild and repeat acceptance. Never alter generated files or relax the
reference to obtain a pass.
