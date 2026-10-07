# SolidWorks to URDF

`solidworks-to-urdf` converts a versioned mechanical CAD handoff into a verified,
self-contained URDF delivery and submits it as a pull request.

```text
SolidWorks assembly and native engineering contents
  → collect and freeze → read CAD → derive robot definition
  → URDF → independent verification → PR
```

Fresh capture runs on Windows with licensed SolidWorks 2026 (revision 34).
Apache Airflow is the operator interface. Linux hosts its UI, API and scheduler;
an authenticated Windows endpoint executes the complete workflow. Linux can
also check and rebuild a frozen delivery without CAD.

## Operator workflow

Mechanical engineers provide only the saved SolidWorks engineering directory
under the [mechanical specification](docs/mechanical-handoff-spec.md).
The pipeline generates `robot.yaml`, revision manifests, evidence and reports;
none is a mechanical-team input.
The target operator interface is one authenticated URL and one folder field:

1. **Select or paste one compliant handoff-folder path.** Absolute Linux paths,
   absolute Windows paths and paths relative to the configured `package_root`
   are all accepted.
2. **Start the run** and follow validation, native capture, generation,
   verification and submission.
3. **Inspect the actual verified URDF** with joint and limit interaction, then
   read the quality decision and the review PR.

The pipeline freezes the native files, reads engineering identity and routes
the hardware. Platform configuration supplies the repository, branch and
Windows connection. Every submission goes through the Airflow DAG. See
[operations.md](docs/operations.md) for the complete workflow and
[deployment.md](docs/deployment.md) for platform setup.

**Availability.** CAD-only automatic definition, the one-folder interface,
embedded viewer and RTX 4080 deployment are not yet released and commissioned.
Published v1.0.0 requires its legacy prepared package and explicit Airflow
package, revision and routing fields. It does not implement this CAD-only contract.
[Deployment status and the v1.0.0 trigger](docs/deployment.md#release-and-deployment-status)
describe the released interface and platform tooling.

Track validation, native execution and publication in the DAG run. A successful
run returns the verified subject, Git commit and PR URL. A failed run retains
diagnostics; a failed quality gate prevents submission. Task retries reuse the
same native job.

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

Replace `<owner>/<model-repository>` with the team's model repository, separate
from this tool repository. GitHub authentication is a one-time setup. Use a
dedicated clean model clone;
the hardware branch must already exist. For a new hardware branch, follow
[model repository setup](docs/deployment.md#model-repository-setup).

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
| [Mechanical handoff specification](docs/mechanical-handoff-spec.md) | CAD preparation, robot semantics and revision management |
| [Quality specification](docs/quality.md) | Required gates, tolerances and evidence |
| [Operations](docs/operations.md) | Handoff preparation, execution, diagnosis and model review |
| [Airflow deployment](docs/deployment.md) | Linux orchestration and Windows execution |
| [Release procedure](RELEASING.md) | Tests, native rehearsal, distribution and release |
