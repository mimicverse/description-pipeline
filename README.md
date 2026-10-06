# SolidWorks to URDF

`solidworks-to-urdf` converts a versioned mechanical CAD handoff into a verified,
self-contained URDF delivery and submits it as a pull request.

```text
CAD package + robot.yaml + cad-revision.json
  → inspect → native capture → URDF → independent verification → PR
```

Fresh capture runs on Windows with licensed SolidWorks 2026 (revision 34).
Windows can complete the entire workflow locally. Linux can check, rebuild and
submit an existing frozen delivery. Apache Airflow on Linux coordinates the
same workflow through an authenticated Windows endpoint.

## Start on a SolidWorks computer

Install Python 3.12, Git and GitHub CLI. Download and extract the release asset
`description-1.0.0-windows-cp312-x86_64.zip`. From the extracted directory,
install the tool and its hashed dependencies into a dedicated environment:

```powershell
py -3.12 -m venv C:\description\.venv
C:\description\.venv\Scripts\python.exe -m pip install --no-index --require-hashes --find-links wheels -r requirements.lock
C:\description\.venv\Scripts\description.exe doctor
gh auth login
gh auth setup-git
git clone https://github.com/mimicverse/description.git C:\description\models
```

The structural design must include the CAD datums and robot semantics required
by the [input specification](docs/input.md). A saved assembly alone cannot
establish body grouping, joint direction, limits or physical authority.

Seal each mechanical revision once, then run the complete pipeline:

```powershell
C:\description\.venv\Scripts\description.exe revision C:\handoffs\arm\r1 --hardware arm --id r1 --owner mechanical --control handoff --reference arm/r1 --summary "Initial mechanical handoff"
C:\description\.venv\Scripts\description.exe run C:\handoffs\arm\r1 --output C:\deliveries\arm --repository C:\description\models --base feature/arm
```

GitHub authentication is a one-time setup. Use a dedicated clean model clone;
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
