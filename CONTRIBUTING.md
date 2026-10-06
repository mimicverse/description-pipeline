# Contributing

Use Python 3.12 and the locked development environment. Run:

```sh
PYTHONPATH=src python -B -m unittest discover -s tests -t .
python -m ruff check src tests tools
```

Keep author inputs, raw native evidence, generation and independent verification
separate. A verifier must not call the generator to obtain its expected answer.
Add analytic or adversarial regressions for semantic changes; retain native
rehearsal evidence for COM/API behavior that mocks cannot establish.

Never commit real robot/CAD data, credentials, workstation state or release
build outputs to this public repository. Neutral unit fixtures are explicitly
synthetic; passing them cannot grant native or hardware qualification.
