# Releasing the tool

This is the maintainer checklist for a tool release (a model release lives on its own branch; see the
[runbook](docs/pipeline.en.md#submission-acceptance-and-release)). GitHub Actions are disabled for
this repository, so a release is produced and verified locally, from an exact commit, by the
maintainer.

**Current status.** 0.3.25 is not published or tagged. Publication happens only after a fresh native
Windows rehearsal on the candidate bundle (installation, Doctor, capture and the consumer checks)
and the exact-commit acceptance pass; the current and historical gate state is in the
[0.3.25 validation history](docs/history/validation-0.3.25.md). A diagnostic improvement, a preserved
runtime probe, generated files or a submitted pull request do not satisfy the gate, and no model
candidate is mechanically accepted before its exact commit passes.

## 0. Decide the version

`description_pipeline.__version__` is the single source of truth. After every release, move `main`
to the next patch version so two different byte sets never share a version label; the published
release stays pinned to its own commit.
The bundle `manifest.json` and `docs/quality.json` carry the workflow `pipeline_id`; validation
compares it with the definition and `sources/source.lock.json`. A removed, changed or mismatched
declared id is a release blocker. The per-execution `run_id` is not part of the release identity.

## 1. Verify the exact commit

```sh
git switch main && git pull --ff-only
git rev-parse HEAD                     # the commit this release will pin
python tools/quality.py all
```

The full gate must pass (lint, format, both type passes — Linux and the Windows stubs — the
regression suite and the layout contract). For
release evidence it is worth repeating it in a fresh clone of the same commit, with only
`requirements/linux-py312.lock` installed.

The suite writes a few hundred megabytes of temporary data; point `TMPDIR` at a filesystem with at
least 2 GB free before running it, otherwise a test fails with `Disk quota exceeded` and the reason
looks like a product defect.

Audit the pinned sets against the advisory database, including the two the release ships to Windows.
`pip-audit` installs what it audits, so it cannot reach the Windows sets from Linux — `pywin32` has no
Linux wheel — and this asks OSV about the exact pins instead:

```sh
python tools/audit_dependencies.py requirements/linux-py312.lock requirements/win-py312-dev.lock \
  src/description_pipeline/sources/solidworks/deploy/requirements/win-py312.lock
```

Every pin must come back clear. A finding is fixed by moving the pin wherever it is recorded
(`pyproject.toml` and every lock that carries it) and rebuilding — never by ignoring it.

Scan the history for credentials before the repository is published or mirrored, and again after any
history rewrite. `gitleaks` is a single binary release, not a repository dependency:

```sh
gitleaks detect --source . --config .gitleaks.toml
```

`.gitleaks.toml` keeps the default rules and allow-lists exactly two patterns that were inspected by
hand on 2026-09-24 — a build `generation_key` digest in the generated `docs/quality.json`, and a module
attribute a test patches. Everything else is a finding: rotate the credential, rewrite the history and
re-run before publishing, rather than adding it to the allow-list.

The workflow files are audited statically by the test suite — every command, path, job reference, step
output and action reference has to resolve in this checkout — but their expressions, action inputs,
runner labels and the shell inside `run:` blocks are only covered by `actionlint`, with `shellcheck` on
`PATH` for the snippets. Both are single binary releases, not repository dependencies:

```sh
actionlint .github/workflows/*.yml
```

Run this whenever a workflow changes, and before a release: Actions do not start in this repository, so
a bad expression is a job that would fail later or never run at all. The last run (2026-09-28, all seven
workflows, `actionlint` 1.7.12 with `shellcheck` 0.11.0) reported nothing; a finding is fixed in the
workflow, not waived.

## 2. Build and prove reproducibility

```sh
python tools/build_release.py --require-clean --offline --out dist/<short-sha>-a
python tools/build_release.py --require-clean --offline --out dist/<short-sha>-b
for f in dist/<short-sha>-a/*; do cmp "dist/<short-sha>-a/$(basename "$f")" "dist/<short-sha>-b/$(basename "$f")"; done
```

`--require-clean` refuses a dirty tree, a development tool identity or a missing source commit. The
same commit and lock file must produce byte-identical artifacts: the builder normalizes build time,
ownership and permissions, and the Windows worker bundle is written with the same fixed metadata.
**构建器版本本身就是产物字节的一部分**（setuptools 把版本写进 wheel 的 `WHEEL`，`RECORD` 随之变化），
所以 `pyproject.toml` 把隔离构建的 setuptools 固定为锁里的同一版本，`build_release.py` 也会在
`setuptools`/`wheel` 与锁不一致时直接拒绝构建——0.3.15 的 wheel 就是用 84.0.0 构建的，导致跟随本文
重建的人拿不到相同字节。

Keep `dist/<short-sha>-a`: its `SHA256SUMS` is what the release and the validation record quote.

## 3. Verify the distributions

```sh
python tools/verify_distribution.py dist/<short-sha>-a/*-linux-*.zip  --report dist/<short-sha>-a/linux-verification.json
python tools/verify_distribution.py dist/<short-sha>-a/*-windows-*.zip --windows --report dist/<short-sha>-a/windows-verification.json
```

The Linux check installs the bundle offline in a fresh environment, rebuilds a fixture model after
deleting the original source and cache, and requires the four tamper classes to be rejected. The
Windows check validates the archive content and resolves its wheel closure without running Windows.

The Linux check also runs `bash submit.sh --help` through the freshly installed environment and
checks both launcher scripts for valid shell syntax. The checks extract a full environment, so point
`TMPDIR` at a filesystem with at least 2 GB free; a `Disk quota exceeded` inside `pip` is an
environment problem, not a distribution problem.

## 4. Exercise the native Windows host

Windows App Control must allow MuJoCo's native dependencies. `WinError 4551` means the machine's
policy rejected loading a file, even if the package is installed. Ask the administrator to review
that dependency or use an approved runtime. Keep the failed rehearsal as evidence; a successful
older installation does not establish that a fresh install is allowed. Reporting `WinError 4551`
precisely is a diagnostic improvement, not an acceptance pass.

Rehearse the candidate on the machine that runs SolidWorks. One script installs it into an isolated
root, proves the bytes, runs the CAD probe and the offline first run, and keeps the evidence:

```powershell
powershell -ExecutionPolicy Bypass -File tools\native\win-rehearsal.ps1 `
    -Bundle .\description-worker-<version>-windows-x86_64.zip `
    -Assembly 'D:\models\robot.SLDASM'
```

Add `-UpdateFrom .\description-worker-<previous-version>-windows-x86_64.zip` to rehearse the upgrade
as well: the previous release is installed first, this archive updates it with its own launcher, and a
rollback has to bring the previous version back. That path is what every user takes between releases,
and it is where the `worker-host.json` digest trap lived. The summary's `update` object keeps what the
updater and the rollback themselves printed, in `update.log` and `update.rollback_log` — a child
process's console output never reaches the transcript, so quote those fields in the release record
instead of a terminal scrollback.

The rehearsal also asks the running worker for the answers `SECURITY.md` promises — a request whose
`Host` names another address, one carrying a foreign `Origin`, and an unknown route — and refuses to
pass unless they are 403, 403 and 404. Those are the checks that keep a page in the operator's own
browser from driving CAD.

The script refuses the production task name and an install root that already belongs to another
worker, checks the bundle against the `SHA256SUMS` beside it, and then runs Setup (configure + offline
install), Start, Status, Doctor and `quickstart --run` with the installed runtime. It writes
`native-rehearsal.json` and `native-rehearsal.log` next to the bundle, and stops the worker,
unregisters the task and removes its own root afterwards; `-KeepInstalled` leaves the environment for
inspection.

Require `"passed": true` in that summary: four zero exit codes, `local pipeline: ok mujoco …`,
`collectable=True` from the read-only CAD walk, and a qualified first run. Then run the candidate's
runtime against the model workspace and require `freeze`/`build`/`check` to pass — re-lock it first,
as the checked-in lock belongs to the previous release. A native SolidWorks capture is the stronger
claim and is recorded separately when the change touches capture, the worker or the pipeline.

Keep the summary and transcript, plus the complete native job directory when a capture ran. The
rehearsal removes its isolated install root, including `worker-host.json`; preserve that file before
cleanup only if the release record needs it. The summary itself records the bundle digest, install
root and task name.

## 5. Accept the candidate

Keep the gate log, native summary, artifact hashes and the packaged commit together. Run independent
acceptance against the candidate directory before publishing:

```sh
python tools/accept_release.py candidate <version> --local dist/<short-sha>-a --ref <packaged-commit>
```

This checks the bytes that will be uploaded. The published release needs a separate download and
acceptance check after step 6; only then can the validation record claim the release page passed.

## 6. Tag and publish

```sh
git tag v<version> <packaged-commit>          # the packaged commit, not the documents commit
git push origin v<version>
gh release create v<version> --verify-tag --title "<version>" --notes-file release-notes.md \
  dist/<short-sha>-a/description-worker-<version>-windows-x86_64.zip \
  dist/<short-sha>-a/mimicverse_description-<version>-linux-x86_64.zip \
  dist/<short-sha>-a/mimicverse_description-<version>-py3-none-any.whl \
  dist/<short-sha>-a/mimicverse_description-<version>.tar.gz \
  dist/<short-sha>-a/SHA256SUMS
```

If an upload fails, inspect the release page before retrying. Use `gh release upload` for missing
assets on an existing release, then run step 7; never treat a partial upload as published evidence.

Do not infer upload integrity from `gh release create` succeeding. The GitHub asset API currently
returns `digest: null` for this repository; step 7 downloads every asset and recomputes its SHA-256
against the uploaded `SHA256SUMS`.

Any release can be re-checked later, not just the one just published — which is what a repository
move, a mirror or a suspicious upload needs. `tools/audit_releases.py` walks every release on the
release page, verifies each asset against the `SHA256SUMS` beside it, and requires every artifact that
carries a packaged identity to name the commit its tag points at:

```sh
python tools/audit_releases.py                     # downloads each release into a temporary directory
python tools/audit_releases.py --assets <dir>      # or reads <dir>/<tag>/ asset sets already downloaded
```

A release whose bytes have moved, or whose artifact names a different commit than its tag, is a
finding: re-upload the verified bytes rather than rebuild.

### Publishing the same artifacts to PyPI

The wheel and the sdist are what `pip install` would fetch, so check them before an upload:

```sh
python -m pip install twine           # twine is not in any lock; it only renders the metadata
twine check dist/<short-sha>-a/*.whl dist/<short-sha>-a/*.tar.gz
```

The public repository starts with 0.3.23. Audit both packages for private data before any PyPI
upload; a valid GitHub release does not itself authorize a PyPI publication.

## 7. Accept the published release

Read the release back from the release page and re-derive the claims above instead of trusting this
checklist:

```sh
python tools/accept_release.py v<version> <version> --report dist/acceptance-<version>.json
```

`tools/accept_release.py` reads the public release page over HTTPS and requires: every artifact to match the digest `SHA256SUMS` publishes, the release page to
carry nothing the checksum file does not list, the Linux bundle to install offline into an empty
environment whose CLI reports `<version>`, both archives to pass `tools/verify_distribution.py`, the
wheel and the sdist to install offline and pass their own `doctor`, both shipped examples (`demo-arm`
and `mesh-arm`, the latter including its mesh assets) to rebuild byte-identically from the packaged
commit, and `quickstart --run` to qualify on the released runtime.
It imports no pipeline code, so a defect in the pipeline cannot accept its own release, and it writes
nothing outside its temporary workspace.

Candidate and published acceptance each need room for two extracted environments and roughly 115 MB
of archives, so point `TMPDIR` at a filesystem with room to spare. The check installs the Linux bundle;
run it on Linux, macOS or WSL. A native Windows shell is refused up front.

The bundle names the exact CPython patch version and the platform it was built with, and the smoke
test refuses any other — run this step with that interpreter. Measured on 2026-09-29: the v0.3.21
Linux bundle pins CPython 3.12.10 (the v0.3.19 and v0.3.20 bundles pin 3.12.14), and verifying
v0.3.21 under 3.12.14 is refused with `Installed tool identity differs — python '3.12.10' packaged vs
'3.12.14' installed … verify it with that exact interpreter`. A refusal that misses the interpreter
is usually a dependency-set difference, and the same message names the missing, extra and changed
pins.

## 8. Record the release and open the next version

Update [`docs/validation.md`](docs/validation.md) with the packaged commit, package content digest,
all artifact SHA-256 values, gate counts with skips, native results, the published acceptance result
and each deferred qualification. Preserve the previous record in `docs/history/`. Point the onboarding
guides and `SECURITY.md` at the release that actually exists. `tests/test_release_references.py`
checks those version pointers so a first run cannot lead to an unpublished tag.

Move `main` to the next patch version after the release record lands; the immutable tag stays on the
packaged commit. Documentation commits after the tag do not rebuild that version's package. A
correction to released bytes needs a new version. If a consumer model pins this tool, update its
`config/toolchain.lock.json` explicitly and rebuild; never hand-edit lock digests.
