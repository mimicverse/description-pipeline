# Contributing

Use a dedicated Python 3.12 environment. Install the runtime lock for your OS,
the builder lock, then the editable package without resolving additional dependencies:

```sh
python -m pip install -r requirements/linux-py312.lock -r requirements/build-py312.lock
python -m pip install --no-deps -e .
```

On Windows, substitute `requirements/windows-py312.lock`. Run:

```sh
PYTHONPATH=src python -B -m unittest discover -s tests -t .
python -m ruff check src tests tools deploy/airflow
```

Keep author inputs, raw native evidence, generation and independent verification
separate. A verifier must not call the generator to obtain its expected answer.
Add analytic or adversarial regressions for semantic changes; retain native
rehearsal evidence for COM/API behavior that mocks cannot establish.

Never commit real robot/CAD data, credentials, workstation state or release
build outputs to this public repository. Neutral unit fixtures are explicitly
synthetic; passing them cannot grant native or hardware qualification.
