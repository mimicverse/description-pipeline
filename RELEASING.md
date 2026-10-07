# Release procedure

The public release is the tool and its specifications. Native robot/CAD history
stays in the private model repository. GitHub CI is not part of the release gate.

1. Run the maintained regression suite and static checks with the pinned build
   environment. Test meaningful malformed inputs and semantic mutations after
   resealing; hash mismatch alone is insufficient proof of a quality rule.
2. Capture the neutral moving-joint analytic CAD fixture on Windows. Verify
   native transforms, shaft identity, off-diagonal inertia and full assembly
   closure. Retain actual API, scope, software version and error evidence.
3. Build wheel and source distributions from a clean committed checkout. Embed
   the source commit and code digest; verify installed CLI and package identity
   outside that checkout. Build twice and compare distribution bytes.
4. Rehearse the installed tool on Windows through capture → verify → PR and on
   Linux through frozen check → rebuild → submit. Repeat submission and confirm
   one PR. A bad input or artifact must produce no publication.
5. Deploy the tested Airflow environment and Windows endpoint. Exercise the
   actual DAG against the native endpoint, including retry and failure cases.
   A release claiming the single-path interface must also demonstrate folder
   transfer, hardware routing, operator login and the verified URDF viewer as
   required by [deployment acceptance](docs/deployment.md#deployment-acceptance).
6. Audit documentation against command help, schemas, tests and measured
   behavior. Record limitations without implying physical/control qualification.
7. Push the reviewed implementation to public `main`, tag the new tool version,
   and publish the distributions, SHA-256 manifest and acceptance evidence.
   Verify remote commit, tag, release assets and installed version.

`v1.0.0` is already published. Subsequent releases use a new version and tag;
do not replace the existing release assets or move its tag. The commands below
show the v1.0.0 asset names; substitute the version being released.

Completion requires all seven steps. Unit tests, native capture alone, a draft
PR or a documentation-only deployment do not establish release readiness.

Build from a clean committed checkout with the environment in
[CONTRIBUTING.md](CONTRIBUTING.md):

```sh
PYTHONPATH=src python tools/build_release.py --output /path/to/release-a --offline
PYTHONPATH=src python tools/build_release.py --output /path/to/release-b --offline
diff /path/to/release-a/SHA256SUMS.json /path/to/release-b/SHA256SUMS.json
PYTHONPATH=src python tools/verify_distribution.py /path/to/release-a/mimicverse_description-1.0.0-py3-none-any.whl --runtime-lock requirements/linux-py312.lock --bundle /path/to/native-passing-delivery --report /path/to/installed-linux.json
```

The builder archives the commit into an isolated directory, embeds the complete
package-file digest, normalizes archive metadata, and emits wheel, source,
deployment and offline runtime assets. It never publishes. Installed code
checks its embedded file inventory before use. The offline archives contain
all target runtime wheels and a hash-locked installation file; exercise the
Windows archive on the actual CAD computer and the Linux archive independently.
