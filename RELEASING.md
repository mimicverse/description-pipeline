# Release procedure

A release contains one complete operator workflow, its specifications and
source-bound distributions. Native robot and CAD history stays in the private
model repository. Retain local acceptance evidence; GitHub CI is not required.

## Acceptance

1. Run the regression suite and static checks in the
   [pinned development environment](CONTRIBUTING.md). Test semantic mutations
   after resealing evidence, as well as malformed inputs and digest changes.
2. Capture the neutral moving-joint analytic fixture on licensed Windows
   SolidWorks. Verify native discovery, occurrence transforms, shaft identity,
   off-diagonal inertia and full assembly closure. Retain the native API/build
   and independent verification report. Include repeated part occurrences with
   distinct saved configurations and a nested rigid assembly to verify context,
   geometry and mass coverage.
3. Build twice from one clean committed checkout. Compare distribution hashes
   and verify installation outside that checkout on Linux and Windows.
4. Accept the deployed workflow: HTTPS login → native folder → Airflow run →
   detailed checks → actual verified URDF and joint limits → review PR.
   Exercise retry, changed input, quality failure, service restart and
   publication failure under the [deployment criteria](docs/deployment.md#acceptance).
5. Review code, command help, schemas and all documentation together. Keep
   `main` the smallest complete current system. Remove intermediate workflows,
   compatibility paths, duplicate entry points and transitional instructions.
   Regenerate contract-derived tables from `contract_markdown()` and confirm the
   six-stage contract still matches the code before merging.
6. Merge the reviewed result into public `main`, create the release tag as
   `v<package version>` from the distribution's `__version__` (currently
   `1.0.1`) and publish distributions, their SHA-256 manifest and acceptance
   evidence. Never reuse or rewrite a published tag or asset. Verify the remote
   commit, tag, downloaded bytes and installed identity.

All steps are required. Mocks do not qualify native behavior, and a passing
neutral fixture does not qualify a hardware model. Each model needs reviewed
inputs and evidence for its intended use.

## Distribution

Activate the environment in [CONTRIBUTING.md](CONTRIBUTING.md), then build
into two empty directories outside the clean checkout:

```sh
python tools/build_release.py --output /path/to/release-a --offline
python tools/build_release.py --output /path/to/release-b --offline
diff /path/to/release-a/SHA256SUMS.json /path/to/release-b/SHA256SUMS.json
```

Verify the resulting wheel with its complete passing native delivery. Replace
`<version>` with the version embedded in that wheel:

```sh
python tools/verify_distribution.py /path/to/release-a/mimicverse_description-<version>-py3-none-any.whl --runtime-lock requirements/linux-py312.lock --bundle /path/to/native-passing-delivery --report /path/to/installed-linux.json
```

The builder binds the source commit and package files, normalizes distribution
metadata, and emits the wheel, source, deployment and Linux/Windows offline
runtime archives. Each offline archive contains exact runtime wheels and a
hash-locked installation file. Installed code checks its embedded inventory.
Exercise both runtime archives and deploy the same tool identity on both hosts.

Published tags and assets are immutable. A changed implementation uses a new
version; release status comes from retained acceptance evidence.
