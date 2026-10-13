# SolidWorks to URDF

One engineering folder, one operator page, one verified model PR.

`solidworks-to-urdf` freezes native SolidWorks engineering, derives the robot
model, generates URDF and meshes, independently verifies the delivery, and
creates or updates its review PR. Airflow stages the complete uploaded
engineering folder to the serialized Windows worker, which resolves the main
assembly's references there, reads the raw native record and later captures the
sealed engineering; the robot definition is derived, generated,
independently MuJoCo-verified and published on Linux. Windows requires licensed
SolidWorks; it runs no structure derivation, URDF generation, MuJoCo
verification or Git operations, and needs no GitHub credentials.

## Use

Open the single HTTPS address provided by the platform maintainer.
The page is **SolidWorks2URDF 交付操作台**.

1. Prepare saved native engineering on your own computer under the
   [mechanical specification](docs/mechanical-handoff-spec.md).
2. Sign in with Feishu, choose the complete project folder on your computer
   (Chrome or Edge) and start the run. The browser uploads the folder's files to
   the platform before anything is frozen; no server path is entered by hand.
   When the folder holds several saved `.SLDASM` assemblies you must choose the
   main assembly before starting
   ([operations](docs/operations.md#2-collect-and-save-a-controlled-version));
   a single assembly is selected automatically.
   Confirm the configured publication target suggested in the upload window,
   or select the intended target explicitly. Submission metadata follows the
   [identity contract](docs/mechanical-handoff-spec.md#12-工程身份).
   The complete uploaded folder is staged to the Windows worker; dependency
   closure is admitted only from the engine's resolved bindings inside that
   staging ([design](docs/design.md#2-how-the-system-works)). The worker reads
   the raw native record and later captures that staging, while the robot
   definition is derived on Linux and returns to the worker only as generated
   text. The full folder is never re-imported on Windows.
3. Review each step's actual check results, warnings and failure diagnoses.
   Automatic passes need no manual repetition; warnings never count as passes
   ([quality](docs/quality.md#step-and-result-contract)), and external
   engineering scope remains explicit. Inspect the verified URDF and joint
   limits, and open the PR
   for engineering approval. A retained delivery after a failed independent
   verification stays viewable only as an explicitly unverified diagnostic
   preview: generation success alone is not verification, and such artifacts are
   forbidden from publication.

The picker uploads only the chosen folder; the page shows the platform's fixed
upload limits (2 GiB total, 4 096 files, 512 MiB per file). Empty
subfolders are not representable; temporary SolidWorks lock files (`~$…`) must
be removed first — the platform rejects any selection containing them. Generated platform inputs such as `robot.yaml` and
`cad-revision.json` must not be included — the platform generates them.

If the main assembly carries the saved native URDF exporter declaration
(`URDF Export Configuration (v1.4)`), the pipeline reads it from the CAD as
declared link/joint evidence; zero or placeholder operating values are retained
and flagged as warnings, never filled with defaults.

The raw record preserves missing CAD metadata. Publication may resolve absent
administrative fields from the authenticated submission; this never supplies
missing mechanical facts or engineering approval
([mechanical identity](docs/mechanical-handoff-spec.md#12-工程身份)).

Run history and details show the original submitter's Feishu username, obtained
automatically through Feishu's authentication API.

Mechanical engineers provide SolidWorks files and engineering facts. Robot
YAML, version manifests, model artifacts and reports are generated. Corrections
return to CAD or controlled specifications, followed by a new run.

To repeat work, select any step, including a completed one, and choose
**从此步骤重新运行**. A new linked run reuses validated upstream results and
reruns that step and everything after it. Changed CAD requires a new folder
upload. For recoverable transport failures, **继续原作业** reconnects to the
existing job.

Run history shows the project name, time, status and initiator. Initiators and
administrators can rename records or move completed runs to **已删除**, then
restore them. These actions preserve engineering files, evidence and PRs.

See [operations](docs/operations.md) for the full design-to-release workflow
and [deployment](docs/deployment.md) for installing the Linux server and
Windows endpoint. Repository routing and credentials are configured once by
the platform maintainer.

## Delivery

The self-contained delivery contains `urdf/robot.urdf`, `meshes/`, frozen
engineering, raw evidence, the canonical model and bound quality reports.
`reports/stages.json` records each step's inputs, checks, outputs and evidence,
and which steps actually ran.
With the recorded Linux verification environment installed, recheck it:

```sh
description check /path/to/delivery
```

A pass establishes the [verification gates](docs/quality.md) and is required for
publication. Model approval,
simulation, training and hardware control require their own evidence. The pipeline
exports visual geometry and physical properties; it does not generate
collision models or control interfaces.

## Documentation

| Document | Purpose |
|---|---|
| [Design](docs/design.md) | Principles, workflow and engineering organization |
| [Mechanical specification](docs/mechanical-handoff-spec.md) | SolidWorks requirements, naming and engineering confirmations |
| [Operations](docs/operations.md) | Prepare, execute, correct, review and release a model |
| [Deployment](docs/deployment.md) | Install and commission the platform |
| [Quality](docs/quality.md) | Verification, evidence and acceptance limits |
| [Contributing](CONTRIBUTING.md) | Development and testing |
| [Release procedure](RELEASING.md) | Tool acceptance and publication |

[Apache License 2.0](LICENSE) · [Code of Conduct](CODE_OF_CONDUCT.md)
