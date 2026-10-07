# Release procedure

A tool release contains the implementation, specifications and distributions.
Native robot/CAD history remains in the private model repository. GitHub CI
is not a release gate; maintainers retain the following acceptance evidence.

## Acceptance sequence

1. **Verify the change.** Run the maintained regression suite and static checks
   in the [pinned development environment](CONTRIBUTING.md). Exercise malformed
   inputs and semantic mutations after resealing; digest rejection alone does
   not prove a semantic quality rule.
2. **Qualify native behavior.** Capture the neutral moving-joint analytic CAD
   fixture on Windows. Check transforms, shaft identity, off-diagonal inertia
   and assembly closure. Retain actual API scope, SolidWorks build and error evidence.
3. **Build and verify distributions.** Build from a clean committed checkout,
   bind source/code identity, compare two builds, and verify the installed CLI
   and package outside that checkout.
4. **Rehearse delivery.** On Windows, execute capture → verify → PR. On Linux,
   execute frozen check → rebuild → submit. Repeated submission must update one
   PR; rejected input or artifacts must produce no publication.
5. **Accept deployment.** Run the actual Airflow DAG against the native endpoint,
   including retries and failures, under [deployment acceptance](docs/deployment.md#deployment-acceptance).
   A release claiming the CAD-only interface must also pass automatic definition,
   transfer/routing, login, verified preview and per-item engineering-report acceptance.
6. **Review documentation.** Match instructions and claims to command help,
   schemas, tests and measured behavior. State unsupported features and the
   limits of physical and control qualification.
7. **Publish and verify.** Merge the reviewed change into public `main`, create
   a new version tag, and publish distributions, SHA-256 manifest and acceptance
   evidence. Verify the remote commit, tag, asset bytes and installed version.

All seven steps are required. Synthetic tests, native capture alone or a
successful PR do not establish complete release acceptance.

## Distribution commands

Activate the environment in [CONTRIBUTING.md](CONTRIBUTING.md). From a clean,
committed repository, build into two empty directories outside the checkout:

```sh
python tools/build_release.py --output /path/to/release-a --offline
python tools/build_release.py --output /path/to/release-b --offline
diff /path/to/release-a/SHA256SUMS.json /path/to/release-b/SHA256SUMS.json
python tools/verify_distribution.py /path/to/release-a/mimicverse_description-1.0.0-py3-none-any.whl --runtime-lock requirements/linux-py312.lock --bundle /path/to/native-passing-delivery --report /path/to/installed-linux.json
```

The builder archives the source commit, embeds package-file identity and
normalizes distribution metadata. It emits wheel, source, deployment and
Linux/Windows offline runtime archives; it does not publish. Offline archives
contain runtime wheels and a hash-locked installation file. Exercise the
Windows archive on the CAD computer and the Linux archive independently.
Installed code checks its embedded inventory before use.

`v1.0.0` is already published. Substitute the new version in asset examples
for a subsequent release. Never move an existing release tag or replace its
published assets. Documentation changes alone do not create a new tool release.
