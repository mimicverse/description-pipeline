# Contributing

Tool development uses Python 3.12 and the repository's pinned runtime and build
dependencies. Run the following from the repository root in a dedicated environment.

## Environment

Linux:

```sh
python3.12 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements/linux-py312.lock -r requirements/build-py312.lock
python -m pip install --no-deps --no-build-isolation -e .
```

Windows PowerShell:

```powershell
py -3.12 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements/windows-py312.lock -r requirements/build-py312.lock
.venv\Scripts\python.exe -m pip install --no-deps --no-build-isolation -e .
```

The source locks pin versions. Release archives additionally hash the downloaded
wheels; see [RELEASING.md](RELEASING.md). Airflow uses a separate deployment environment.

## Verification

In the activated Linux environment:

```sh
python -B -m unittest discover -s tests -t .
python -m ruff check src tests tools deploy
```

Windows uses the native capture dependency lock; its runtime does not include
MuJoCo. Run native adapter regressions there with `.venv\Scripts\python.exe`.
Run the full suite and consumer checks in the Linux verification environment.
For changes to Airflow deployment, also follow its
[deployment checks](docs/deployment.md#maintenance).

Keep source inputs, raw native evidence, generation and independent verification
separate. A verifier must not obtain its expected answer from the generator.
Semantic changes need analytic or adversarial regressions. COM/API changes need
actual native rehearsal evidence; mocks cannot establish native behavior.

## Documentation and repository boundaries

Keep examples consistent with command help, schemas and measured behavior.
Keep `main` within the [Release 1.0 design](docs/design.md): one current workflow,
no compatibility layers or transitional documentation. Merge implementation and
instructions together after the required acceptance passes.
The [design](docs/design.md) owns architecture, [operations](docs/operations.md)
owns the end-to-end workflow, [deployment](docs/deployment.md) owns availability
and configuration, and [quality](docs/quality.md) owns verification claims.

The six-stage contract is `src/description_pipeline/stage-contract.json`; the
generated table in the design document must match `contract_markdown()`.

Do not commit real robot/CAD data, credentials, workstation state or build
outputs to this public repository. Neutral fixtures are synthetic and cannot
grant hardware qualification. Tool publication follows the
[release procedure](RELEASING.md).
