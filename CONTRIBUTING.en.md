# Contributing

English · [中文](CONTRIBUTING.md)

This public repository accepts tool changes on `main`. Hardware `feature`/`release` branches belong
in a writable model repository, which can stay private. Branch and release contracts are in the [README](README.md) and the
[engineering contract](docs/pipeline.md).

## Tool changes

Branch `work/tooling/<change>` from `main`, install `requirements/linux-py312.lock` and the editable
package. The single entry point is `python tools/quality.py all`; it runs ruff, formatting, types
under both the Linux and the Windows stubs, the regression suite and the tooling layout contract.
Documentation-only changes may use `--fast`,
but a real delivery still needs the full gate. By default you check the pull-request candidate tool
code on your own machine; GitHub CI is optional.
The regression suite also carries the documentation and contract guards: local links and anchors,
command and section-structure parity between the two languages, Markdown table widths and closed
code fences, renderable Mermaid diagrams, the source and tooling directory contracts, every JSON
field `submit.ps1`/`worker.ps1` read still being produced by the tool, and the static references in
`.github/workflows` - which matters while Actions cannot start. The release contracts are guards too:
what the acceptance tool and the dependency audit check, that the release page and the support table
only name published versions, that an artifact's own identity equals the commit its tag points at,
that the secret-scan allow-list cannot grow, and that the native rehearsal keeps its fields and its
guards. Two PowerShell stand-in suites run on Windows (see below).
Coverage is not part of the gate, but it is one command away once `coverage` is installed
(`python -m pip install coverage` — it is not in the lock):
`python -m coverage run --source=src/description_pipeline -m unittest discover -s tests` followed by
`python -m coverage report`. The baseline is **88%** of 11,136 statements, 1,389 lines uncovered
(measured 2026-09-28 on a fresh clone of main `77dc3f5`); new
code should not lower it, and a **fail-closed check has to be executed by a test** - a guard nobody
has seen reject is not a guard.
The full gate takes about **4 minutes** on an ordinary laptop (measured 2026-09-28 on a fresh clone
of main `77dc3f5`: 1,200 tests, 207 s for the test step and 223 s for the whole gate; slower on a busy machine) and needs **at least 2 GB** of temporary space (`TMPDIR`, which has to be
an existing **absolute** directory; the gate checks it before the tests and says how to fix it). When
the space runs out, the failure appears inside some test as `Disk quota exceeded` rather than as "the
disk is full".

On Windows the same gate runs like this (verified natively on Windows 11 with CPython 3.12.10):
same commit, same six steps plus the two PowerShell suites, measured 2026-09-28 on a clean checkout
of `77dc3f5` at 1,200 tests, `OK (skipped=17)` in 428 s for the test step and 434 s for the whole
run, 8/8 steps (Linux skips 3 of the same suite; the extra Windows skips are the POSIX-only cases), with the offline environment from
`requirements/win-py312-dev.lock`. Windows adds two steps: `powershell-deployment` and
`powershell-submit` run the stand-in suites in `tests/windows/*.ps1`, which use `C:\...` fixture paths
and therefore cannot run anywhere else — and which nothing else executed while GitHub Actions was
down. They are not part of the plan on Linux, because PowerShell there cannot even resolve their
fixture paths.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements\win-py312-dev.lock
.\.venv\Scripts\python.exe -m pip install --no-deps --no-build-isolation -e .
.\.venv\Scripts\python.exe tools\quality.py all
```

`requirements/win-py312-dev.lock` is the native worker's runtime set (including MuJoCo) plus every
tool the gate needs, in one install. `tools/quality.py` runs each step in UTF-8 mode, so the machine
code page cannot change the result. When several versions are installed, use `py -3.12 -m venv .venv`
for the first line.

A source adapter only touches data capture, identity, raw readings and source semantics; it must not
grow its own URDF/MJCF generator, its own generic inertia judge, or its own release gate. Shared code
lives in `src/description_pipeline`, and compatibility entry points only forward. A new consumer
semantic needs all four of: a canonical field, a backend projection, an independent consumer check
and an error-injection regression. A constraint that cannot be projected must fail loudly.

## Model changes

For existing hardware, branch `work/model/<hardware>/<change>` from `feature/<hardware>`. The inputs
are the author definition, evidence-backed overrides and the source snapshot. Never hand-edit the
generated XML; fix the input and rebuild. A tool upgrade requires an explicit tool-lock update and a
rebuild.

From the model root, `description model update` performs preflight → freeze → build → submit in one
command; add `--worker-host SSH_ALIAS` for remote SolidWorks capture, or `--reuse-source` when only
definitions or evidence changed. `--root`, `--profile` and `--message-file` override the defaults.
The whole update holds the workspace lock; `description model submit` is the public entry with the
same submission semantics (it takes the same lock, so it cannot run concurrently with an update).

Submission only happens on a review branch: running on `feature/<hardware>` first creates
`work/model/<hardware>/<timestamp>-<random>` and commits there — it **never advances the feature
branch**; running again on that review branch commits to the same branch and PATCHes the existing
pull request (re-running on the same review branch updates the same pull request) instead of opening
another one. The submitting machine must have an installed, logged-in `gh`; preflight rejects a
missing or unauthenticated CLI before any capture. When the `simulation` purpose declares complete
experiments, `update` runs them, installs the evidence and revalidates; a failed experiment blocks
the submission. A passing local verification creates or updates the pull request, and by default no
Actions are called. A failed pull-request creation keeps the candidate on the pushed branch and
prints the retry command. Only an explicit `--ci` dispatches hosted validation; a failed dispatch
keeps the pull request and prints the `description model dispatch` retry command. A design that does
not pass verification can be submitted manually as a draft pull request with its diagnostics; the
one-command entry never submits it, and release stays blocked.

Use `description model init` for a first feature branch; initialising an existing feature must not
overwrite the original worktree. Native CAD files use Git LFS, and consumers and release verification
must check out the real LFS bytes. The model template disables Git line-ending conversion so input
and artifact digests agree across machines; archived evidence keeps its original bytes.

## Tool and model releases

The tool version lives only in the package `__version__`, and distribution metadata reads it from
there; models pin the tool commit, package digest and runtime. The release script refuses a dirty
source tree and writes the package digest and source commit into the wheel and sdist.
`tool-release.yml` takes an exact commit from `main` history, re-verifies it and produces artifacts
with checksums; giving it an optional tag publishes a GitHub Release.
The step-by-step checklist for a tool release — reproducibility proof, the two distribution checks,
the native Windows exercise, the validation record and the post-publish digest check — is in
[RELEASING.md](RELEASING.md).

A model release is decided by the exact candidate of `description model promote`: the same hardware
feature accepts it, the same purpose is re-qualified, the tool is pinned, CAD evidence is present,
the remote delivery is complete and the release branch fast-forwards. It runs locally by default;
`--ci` additionally requires hosted validation for that SHA. Existing repository and organisation
rules must not be disabled. A historical release with extra pull-request protection fails explicitly
and cannot be bypassed by force.

A Windows worker change additionally needs the offline regression and native Windows acceptance.
Test fixtures, an installed runtime, a live process, a collectable real CAD session and a
fully-simulatable machine are different claims, and each is recorded as its own evidence.
