# SolidWorks to URDF

`solidworks-to-urdf` converts a versioned mechanical CAD handoff into a verified,
self-contained URDF delivery and submits it as a pull request.

```text
CAD package + robot.yaml + cad-revision.json
  → inspect → native capture → URDF → independent verification → PR
```

Fresh capture runs on Windows with licensed SolidWorks 2026 (revision 34).
Apache Airflow is the operator interface. Linux hosts its UI, API and scheduler;
an authenticated Windows endpoint executes the complete workflow. Linux can
also check and rebuild a frozen delivery without CAD.

## Submit through Airflow

Prepare the sealed CAD package and robot definition according to the
[input specification](docs/input.md). Place the package under the Windows
endpoint's configured handoff root. In the Airflow UI, trigger
`solidworks_to_urdf` with the package path, revision digest and configured model
target. The [deployment guide](docs/deployment.md) defines setup and all trigger
fields. API submissions use the same DAG.

Track validation, native execution and publication in the DAG run. A successful
run returns the verified subject, Git commit and PR URL. A failed run retains
diagnostics and prevents publication. Task retries reuse the same native job.

## Set up the Windows worker

Install licensed SolidWorks, Python 3.12, Git and GitHub CLI. Download and
extract the release asset
`description-1.0.0-windows-cp312-x86_64.zip`. From the extracted directory,
install the tool and its hashed dependencies into a dedicated environment:

```powershell
py -3.12 -m venv C:\description\.venv
C:\description\.venv\Scripts\python.exe -m pip install --no-index --require-hashes --find-links wheels -r requirements.lock
C:\description\.venv\Scripts\description.exe doctor
gh auth login
gh auth setup-git
git clone https://github.com/<owner>/<model-repository>.git C:\description\models
```

The structural design must include the CAD datums and robot semantics required
by the [input specification](docs/input.md). A saved assembly alone cannot
establish body grouping, joint direction, limits or physical authority.

Seal each mechanical revision once. The local command below is also available
for commissioning and diagnosis; Airflow invokes this same workflow:

```powershell
C:\description\.venv\Scripts\description.exe revision C:\handoffs\arm\r1 --hardware arm --id r1 --owner mechanical --control handoff --reference arm/r1 --summary "Initial mechanical handoff"
C:\description\.venv\Scripts\description.exe run C:\handoffs\arm\r1 --output C:\deliveries\arm --repository C:\description\models --base feature/arm
```

Replace `<owner>/<model-repository>` with the team's model repository, separate
from this tool repository. GitHub authentication is a one-time setup. Use a
dedicated clean model clone;
the hardware branch must already exist. For a new hardware branch, follow
[execution setup](docs/operations.md#2-set-up-the-execution-computer).
The second command performs all five stages and prints the PR URL. A failed check
keeps diagnostics and does not submit or replace a previous passing delivery.

## Delivery

`urdf/robot.urdf` and `meshes/` form the portable model. The bundle also contains
the original inputs, collected CAD copy, raw readings, canonical model, and
reports binding the verified file bytes. After copying a delivery, run:

```sh
description check /path/to/delivery
```

A passing report establishes the documented URDF and evidence checks. It does
not establish simulation, training or hardware-control qualification. v1 emits
visual geometry; collision-model construction and control interfaces require
their own contracts and acceptance.

| Document | Purpose |
|---|---|
| [Design](docs/design.md) | Principles, workflow, organization and operation |
| [Input specification](docs/input.md) | CAD preparation, robot semantics and revision management |
| [Quality specification](docs/quality.md) | Required gates, tolerances and evidence |
| [Operations](docs/operations.md) | Complete local workflow, diagnosis and model review |
| [Airflow deployment](docs/deployment.md) | Linux orchestration and Windows execution |
| [Release procedure](RELEASING.md) | Tests, native rehearsal, distribution and release |
