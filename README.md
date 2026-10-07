# SolidWorks to URDF

One engineering folder, one operator page, one verified model PR.

`solidworks-to-urdf` freezes native SolidWorks engineering, derives the robot
model, generates URDF and meshes, independently verifies the delivery, and
creates or updates its review PR. Airflow coordinates execution on a licensed
Windows SolidWorks worker.

## Use

1. Prepare saved native engineering under the
   [mechanical specification](docs/mechanical-handoff-spec.md).
2. Sign in with Feishu, select its accessible engineering-folder path,
   and start the run.
3. Review progress and findings, inspect the verified URDF and joint limits,
   and open the PR for engineering approval.

Mechanical engineers provide SolidWorks files and engineering facts. Robot
YAML, version manifests, model artifacts and reports are generated. Corrections
return to CAD or controlled specifications, followed by a new run.

See [operations](docs/operations.md) for the full design-to-release workflow
and [deployment](docs/deployment.md) for installing the Linux server and
Windows endpoint. Repository routing and credentials are configured once by
the platform maintainer.

## Delivery

The self-contained delivery contains `urdf/robot.urdf`, `meshes/`, frozen
engineering, raw evidence, the canonical model, and bound quality reports.
With the recorded tool environment installed, recheck it on Linux or Windows:

```sh
description check /path/to/delivery
```

A pass establishes the [verification gates](docs/quality.md). Model approval,
simulation, training and hardware control require their own evidence. Release
1.0 exports visual geometry and physical properties; it does not generate
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
