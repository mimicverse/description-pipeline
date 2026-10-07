# SolidWorks to URDF

`solidworks-to-urdf` captures SolidWorks engineering, builds a self-contained
URDF delivery, independently verifies it, and submits a model pull request.
Apache Airflow orchestrates the workflow; a licensed Windows worker performs
native CAD capture and publication.

## Availability

Published **v1.0.0** supports native capture from a prepared package, verified
URDF generation, PR submission and frozen replay on Linux or Windows. It still
requires platform-maintained `robot.yaml` and `cad-revision.json` inputs and a
six-field Airflow trigger.

The **target workflow** accepts only a SolidWorks engineering directory and
generates those definitions and records automatically. CAD-only definition
discovery, the one-folder operator page, the embedded viewer and detailed
engineering-check display are not yet released. The RTX 4080 deployment is
not commissioned. See [deployment status](docs/deployment.md#release-and-deployment-status).

## Target workflow

Mechanical engineers prepare saved native files under the
[SolidWorks engineering specification](docs/mechanical-handoff-spec.md).
They do not author pipeline YAML, manifests, evidence reports or exported models.

```text
SolidWorks engineering directory
  → freeze → read CAD → derive definition → build URDF
  → independently verify → submit review PR
```

Once the target interface is commissioned, operators use one authenticated page:

1. Select the engineering directory accessible to the platform.
2. Start the run and inspect progress, findings and engineering confirmations.
3. Review the verified URDF, joint motion, quality report and PR.

Platform configuration supplies repository routing and worker access. Corrections
return to CAD or controlled specifications, followed by a new run. The
[operations guide](docs/operations.md) covers preparation through model release.

## Runtime and delivery

Fresh capture requires Windows, licensed SolidWorks 2026 (revision 34), Python
3.12, Git and GitHub CLI. Linux hosts Airflow and can check, rebuild and submit
a frozen delivery without SolidWorks. Follow the
[deployment guide](docs/deployment.md) for installation and commissioning.

The delivery contains `urdf/robot.urdf`, local `meshes/`, original inputs,
collected CAD, raw observations, the canonical model and bound quality reports.
After installing the recorded tool release and activating its environment,
check a copied delivery with:

```sh
description check /path/to/delivery
```

A pass establishes the checks in the [quality specification](docs/quality.md).
Simulation, training and hardware control require separate acceptance. v1.0.0
exports visual geometry; it does not construct a collision model or control interfaces.

## Documentation

| Document | Purpose |
|---|---|
| [Design](docs/design.md) | Principles, architecture, organization and workflow |
| [SolidWorks engineering specification](docs/mechanical-handoff-spec.md) | Requirements for the mechanical team, including automatic checks and engineer confirmations |
| [Operations](docs/operations.md) | Preparation, execution, correction, review and model release |
| [Quality](docs/quality.md) | Implemented gates, tolerances, evidence and acceptance limits |
| [Deployment](docs/deployment.md) | Availability, Windows worker and Linux orchestration |
| [Airflow installation](deploy/airflow/README.md) | Executable setup and released trigger instructions |
| [Contributing](CONTRIBUTING.md) | Tool development and verification |
| [Release procedure](RELEASING.md) | Tool acceptance, packaging and publication |
| [Changelog](CHANGELOG.md) | Release history |

The project uses the [Apache License 2.0](LICENSE) and
[Contributor Covenant Code of Conduct](CODE_OF_CONDUCT.md).
