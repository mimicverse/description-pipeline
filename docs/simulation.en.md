# Simulation application acceptance

English · [中文](simulation.md)

`description model accept` runs stance holding and single-joint sine tracking in real MuJoCo and
produces a digest-bearing acceptance record with per-step telemetry. It accepts only the declared
simulation scenarios and grants neither training nor hardware qualification.

## Prepare the model

First complete the physical parameters, collision geometry, contact parameters, actuators and
control mapping in the model workspace.
A profile may use `validation_poses` to name the poses checked for contact, and the first pose is
used for the reset; without it every sampled pose is checked. Full-range FK and dynamics sampling
still run independently.

Declare the experiments in `config/simulation-acceptance.json`, for example. Replace the example
names and parameters with the model under test:

```json
{
  "schema": "description.simulation-acceptance/v1",
  "purpose": "simulation",
  "model": {"mjcf": "mjcf/scene.xml"},
  "timestep_s": 0.002,
  "control_period_s": 0.002,
  "initial_state": {"joints": {"joint": 0.0}},
  "pd": {"default": {"kp": 3.0, "kd": 0.05}, "joints": {}},
  "torque_limits": {"default": 0.3, "joints": {}},
  "tests": [{
    "id": "track-joint",
    "kind": "joint_sine",
    "joint": "joint",
    "amplitude_rad": 0.08,
    "frequency_hz": 0.4,
    "duration_s": 5.0,
    "thresholds": {
      "tracking_rmse_rad": 0.04,
      "max_tracking_error_rad": 0.08,
      "max_torque_nm": 0.3,
      "max_saturation_steps_ratio": 0.01,
      "penetration_tol_m": 0.001
    }
  }]
}
```

`initial_state.joints` must cover every controlled joint. A floating model also needs
`base.position` and `base.quaternion` (wxyz).
The current consumer supports one `motor` per axis on revolute joints with `gear=1`; PD parameters
and limits use SI units. The control period must be an integer multiple of the physics timestep, and
the limits must fall inside every valid control/torque range of the model.
These experiment parameters are not manufacturer continuous ratings and do not replace actuator
identification.

A `hold` test may use `support` to declare the ground-contact ratio and the minimum base height; a
floating test may use `max_base_tilt_deg` to limit tipping.
Sine tracking is judged on the driven joint and holding tests on the worst joint; per-joint error and
torque-saturation ratios are reported as well. Test durations and thresholds must be fixed before
acceptance, and the tracking tolerance should be able to detect a target joint that does not move.

`max_torque_nm` checks the torque actually applied after limiting; `max_saturation_steps_ratio`
limits the fraction of control steps (0–1) in which any joint saturates, which catches torque demand
hidden by the limit. `max_required_torque_nm` in the report is for analysis only.

A profile's `acceptance_suites` correspond one-to-one with the test ids; `consumer_environment` pins
the actual software versions on the target platform and may declare `mujoco`, `numpy`, `python`,
`platform` and `controller`, where the controller is `pd-torque/v1`.

When Windows and Linux differ in Python or system version, run and record acceptance in each locked
environment separately; a record from one platform must not be copied to the other.

## Day-to-day updates

With the definitions above in place, run
`description model update --root MODEL --profile simulation`. The command captures, builds, runs the
experiments, writes the evidence, re-verifies and submits the pull request; add `--reuse-source` when
only the definition changed. On Windows, setting `profile: simulation` in `submit-host.json` makes
`submit.ps1` run the same flow. The experiments run only while `consumer.application` is awaiting
acceptance; fix other build failures first, and a failed experiment neither submits nor overwrites
existing evidence.

## Standalone runs and diagnostics

To run the experiments separately or inspect an intermediate candidate, first build a simulation
candidate with the tool pinned by the model's lock:

```sh
description build --root MODEL --profile simulation --report RESULT/build.json
```

The first build fails because the application acceptance record is missing. Only when `blockers` is
exactly `consumer.application` should you run the application tests against the complete candidate at
the report's `diagnostic_path`; fix the input first for any other failure.

```sh
description model accept \
  --root CANDIDATE --profile simulation --out RESULT/acceptance
```

The runner checks the MJCF reference closure first, re-verifies the candidate independently, and
loads only the already verified `mjcf/scene.xml`. Every physics step is checked for state, warnings,
joint range and penetration; the tests also check tracking, actual torque and the support condition.
Results are written to a fresh directory, and a failure keeps its own diagnostics and the telemetry
already collected. The input model stays read-only.
Telemetry files and their digests are in `acceptance.json`; the compressed JSONL can be read with
`gzip -dc`.

## Bringing acceptance results back

Once every test passes, copy the generated record and telemetry back into the author workspace and
rebuild:

```sh
python -c "import shutil; shutil.copytree('RESULT/acceptance/docs/acceptance', 'MODEL/docs/acceptance', dirs_exist_ok=True)"
description build --root MODEL --profile simulation
description check --root MODEL --profile simulation
```

These commands are the same on Windows and Linux. `MODEL` is the author workspace and `CANDIDATE` is
the `diagnostic_path` from the first build report; never write results into the read-only candidate.
A failed experiment keeps its diagnostics — fix the definition or the experiment conditions and
start again.

A local record carries `attestation.kind: local_replay`, which selects the verification route only
and is not a trusted claim. Every build, check and release verification re-runs the complete
experiments and compares the pinned tool, input identity, purpose, actual environment, test
conditions, thresholds, measurements and telemetry digests. A record must reference
`config/simulation-acceptance.json`; a same-named test may not be swapped for a different experiment
configuration. The original run's time, paths and oracle are diagnostic information, and the current
qualification is decided by this verification.

Replay requires the results and the compressed telemetry to be byte-identical. The tool and runtime
under test are those of the tool lock; a different system, Python or compression library may produce
differences, so the target environment has to be re-locked, rebuilt and re-accepted rather than
loosening the comparison or reusing an old record.
Each acceptance run is limited to 256 experiments, one million physics steps and 250,000 telemetry
rows in total; adjust the experiment design when that is exceeded.

On success, submit the model pull request, merge it into the feature branch and run
`description model promote --profile simulation`. The release still fetches the exact candidate from
the remote and verifies it independently. A passing simulation covers only the declared scenarios,
proves nothing about physical parameter calibration, and grants neither training nor hardware
qualification; those purposes still need their own evidence.

## Optional hosted acceptance

GitHub Actions are currently disabled and take no part in simulation qualification. If they are
restored, `simulation-acceptance.yml` runs the same experiments for an exact model SHA; the
compatibility script `tools/run_simulation_acceptance.py --external` produces external attestation
material.

Put the record and logs from a successful artifact into `docs/acceptance/` and add
`attestation.repository`, `run_id` and `artifact_id` to `simulation.json`. The shared tool verifies
the registered executor, run identity, artifact and log digests, then rebuilds the same subject.
External records and local replays take their own verification routes; filling in `passed` by hand is
never valid.
