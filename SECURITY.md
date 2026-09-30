# Security policy

## Supported versions

| Version | Supported |
| --- | --- |
| Latest published release (currently `0.3.24`) | fixes are released here |
| Older public releases | supported only when stated in their release notes |

## Reporting a vulnerability

Report privately through GitHub's
[private vulnerability reporting](https://github.com/mimicverse/description-pipeline/security/advisories/new).
Please do not open a public issue for a vulnerability. Private reporting has to be enabled in the
repository settings — it is free on public repositories — and while the link is not available yet,
write to andy.cui@mimicverse.ai instead.

Include the affected version or commit, the entry point and command, the steps to reproduce, and the
impact you believe it has. We aim to acknowledge within three working days and to publish an advisory
after a fix ships.

## Trust model

This section states what the pipeline promises so a report can be judged against it.

- **Local by default.** `description build`, `check`, `diff`, `recover` and the model workspace
  operations read only the paths you pass. They do not contact CAD services.
- **Credentials stay out of artifacts.** `description source freeze` and `description model update`
  contact Onshape or the SolidWorks worker only when you run them, with credentials read from the
  environment (`ONSHAPE_ACCESS_KEY`, `ONSHAPE_SECRET_KEY`, optional bearer) or
  `~/.onshape_api_keys.json`. Source identities, locks, manifests and retained failure diagnostics
  are designed to carry no credentials;
  `tests/sources/onshape/test_reference.py::test_identity_and_lock_carry_no_credentials` and the
  SolidWorks failure-retention regression assert it. A path that lets a secret reach a committed
  artifact is a vulnerability.
- **The Windows worker is privileged.** It drives licensed SolidWorks as the logged-in user. Its
  `allowed_roots`, workspace lock and job protocol confine writes to declared model workspaces, but
  the host must stay single-user and be updated only from a verified distribution archive.
- **Artifacts are verifiable.** Releases are content-addressed offline bundles with `SHA256SUMS`,
  a pinned source commit and a package digest. `description check` and
  `tools/verify_distribution.py` reject tampered manifests, artifacts and dependency closures; a
  check that lets modified bytes pass is a vulnerability.
- **Dependencies are audited, and the audit is a command.** Every release audits all three pinned
  sets it ships — the Linux runtime, the Windows development environment and the packaged worker
  runtime — against the OSV advisory database with `tools/audit_dependencies.py`, which works from any
  platform because it asks about the exact pins instead of installing them. The step is in
  `RELEASING.md` §1, and a release with a known advisory in a pinned dependency is a defect.
- **Untrusted input is the CAD tree you point at.** Assembly names, remote API responses and mesh
  files are treated as data and validated before use; a crafted response that escapes the workspace
  or executes code is a vulnerability.
