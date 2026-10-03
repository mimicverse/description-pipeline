"""Git roles, exact-commit validation and compare-and-swap model promotion."""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import subprocess
import tempfile
import time
from importlib.resources import files
from string import Template
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ..build import (
    PROFILE,
    assess,
    build,
    definition,
    freeze,
    inputs,
    lock_toolchain,
    profile_for,
    semantic_diff,
    verify_toolchain,
)
from ..io import PipelineError, quote_argument, write_json
from .tunnel import worker_health, worker_tunnel

MODEL_DIRECTORIES = {"config", "sources", "model", "urdf", "mjcf", "meshes", "docs"}
MODEL_FILES = {"README.md", "manifest.json", ".gitignore", ".gitattributes"}
PUBLIC_TOOL_REMOTE = "https://github.com/mimicverse/description-pipeline.git"


def git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(root), *args], check=check, capture_output=True, text=True, encoding="utf-8"
    )


def _hardware(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", value) or ".." in value:
        raise PipelineError("Invalid hardware identifier")
    return value


def init_model(root: Path, hardware: str, source: dict, *, repository: Path | None = None, base: str = "HEAD") -> dict:
    hardware = _hardware(hardware)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/@~^-]*", base):
        # A revision that starts with "-" would be read as an option by `git rev-parse`.
        raise PipelineError(f"Invalid base revision: {base!r}")
    if root.exists():
        raise PipelineError("Model destination must not exist")
    if repository:
        if os.name == "nt":
            git(repository, "config", "core.longpaths", "true")
        revision = git(repository, "rev-parse", "--verify", base + "^{commit}").stdout.strip()
        git(repository, "worktree", "add", "-b", "feature/" + hardware, str(root), revision)
        git(root, "rm", "-rf", "--ignore-unmatch", ".")
    else:
        root.mkdir(parents=True)
    write_json(
        root / "config/robot.yaml",
        {"schema_version": "description.definition/v1", "hardware_id": hardware, "source": source, "overrides": []},
    )
    for purpose in ("kinematics", "simulation", "training", "hardware"):
        write_json(
            root / f"config/profiles/{purpose}.json",
            {**PROFILE, "purpose": purpose, "require_native_source": purpose == "hardware"},
        )
    lock_toolchain(root)
    template = files("description_pipeline").joinpath("templates/model")
    for source_name, target_name in {
        "README.md": "README.md",
        "gitignore": ".gitignore",
        "gitattributes": ".gitattributes",
        "decisions.md": "docs/decisions.md",
        "joint_names.yaml": "config/joint_names.yaml",
    }.items():
        target = root / target_name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            Template(template.joinpath(source_name).read_text(encoding="utf-8")).substitute(hardware=hardware),
            encoding="utf-8",
            newline="\n",
        )
    return {"root": str(root), "branch": "feature/" + hardware, "state": "definition_only"}


def check_layout(root: Path, role: str = "model") -> dict:
    if role == "tooling":
        for name in ("config", "sources", "model", "urdf", "mjcf", "meshes"):
            if (root / name).exists():
                raise PipelineError(f"main must remain hardware-free; put {name}/ on an asset branch")
        for path in ("pyproject.toml", "src/description_pipeline", ".github/workflows", "tests", "docs"):
            if not (root / path).exists():
                raise PipelineError(f"Missing tooling contract: {path}")
        return {"role": "tooling", "passed": True}
    for child in root.iterdir():
        if child.name in {".git", "build", ".venv", "__pycache__"}:
            continue
        if child.name not in MODEL_DIRECTORIES | MODEL_FILES:
            raise PipelineError(f"Unexpected model entry (tooling belongs on main): {child.name}")
    for path in ("config/robot.yaml", "config/toolchain.lock.json"):
        if not (root / path).is_file():
            raise PipelineError(f"Missing author contract: {path}")
    return {"role": "model", "passed": True}


def _require_checkout(root: Path) -> Path:
    """Refuse a root that is not a Git checkout, naming both ways out.

    Every repository operation starts with a git command, and each of them used to answer a directory
    that is not a checkout with a raw ``CalledProcessError`` whose message led with a Python argument
    list.  `compare_models` already said it properly for `diff`; the sentence is one place now.
    """

    root = Path(root)
    if git(root, "rev-parse", "--git-dir", check=False).returncode:
        raise PipelineError(
            f"Not a Git checkout: {root.resolve()}; run description model init to create the workspace, "
            "or pass the existing one as --root"
        )
    return root


def repository_slug(root: Path) -> str:
    _require_checkout(root)
    url = git(root, "remote", "get-url", "origin").stdout.strip()
    match = re.fullmatch(r"(?:https://github.com/|git@github.com:)([^/]+/[^/]+?)(?:\.git)?", url)
    if not match:
        raise PipelineError("GitHub origin required for remote workflow operations")
    return match[1]


def github_api(endpoint: str, data: dict | None = None, *, method: str | None = None) -> dict | list | None:
    command = ["gh", "api", endpoint]
    if data is not None:
        # ``gh api`` defaults to POST; updates need an explicit verb.
        command += ["--method", method or "POST", "--input", "-"]
    response = subprocess.run(
        command,
        input=json.dumps(data) if data is not None else None,
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=True,
    )
    return json.loads(response.stdout) if response.stdout.strip() else None


def _error_text(error: BaseException) -> str:
    """One-line diagnostic that keeps what the failing command said on stderr."""

    stderr = getattr(error, "stderr", None) or ""
    return (" ".join(str(stderr).split()) or " ".join(str(error).split()))[:300]


#: git-lfs's pre-push hook reports the lock query after the URL, and the fetch advice before it, so a
#: refusal is read from the whole stderr — the one-line `_error_text` cuts at 300 characters, which is
#: where the interesting part of the observed message lives.  The Windows promotion host hit exactly
#: this shape, and the atomic release push never ran although both remote validations had passed.
_LFS_LOCK_VERIFY_MARKERS = ("locks/verify", "locks verify")
#: The endpoint failures that are worth one retry, and the words the advisory repeats for each.
_LFS_LOCK_VERIFY_ERRORS = (
    ("connection reset", "dropped the connection"),
    ("timed out", "timed out"),
    ("timeout", "timed out"),
    (" 502", "answered 502"),
    (" 503", "answered 503"),
    (" 504", "answered 504"),
    ("eof", "returned EOF"),
)


def _lfs_lock_verify_failure(error: BaseException) -> str:
    """The lock-endpoint failure a push died on, or ``""`` when it died on something else.

    A lock *conflict* is a different answer — the endpoint names the lock holder, and the push must
    keep failing — so only the endpoint shapes above count, and the full stderr is inspected.
    """

    text = str(getattr(error, "stderr", "") or error).lower()
    if not any(marker in text for marker in _LFS_LOCK_VERIFY_MARKERS):
        return ""
    return next((description for token, description in _LFS_LOCK_VERIFY_ERRORS if token in text), "")


def _lfs_lock_disable(root: Path) -> list[str]:
    """The config overrides that turn the lock query off for one push.

    git-lfs caches a successful check as ``lfs.<endpoint>.locksverify=true``, and that URL-scoped
    value wins over the global default, so a retry that only set ``lfs.locksverify`` would still be
    stopped.  ``git lfs env`` prints the endpoint whose key git-lfs actually consults.  Nothing is
    written to any config: the next push queries the endpoint again.

    ``git lfs env`` appends an annotation to that line — it prints
    ``Endpoint=https://…/info/lfs (auth=basic)`` — and the annotation is not part of the config key,
    so only the URL is kept.
    """

    overrides = ["-c", "lfs.locksverify=false"]
    try:
        environment = subprocess.run(
            ["git", "-C", str(root), "lfs", "env"], check=True, capture_output=True, text=True, encoding="utf-8"
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return overrides
    endpoint = next(
        (
            line.split("=", 1)[1].split(" (", 1)[0].strip()
            for line in environment.splitlines()
            if line.startswith("Endpoint=")
        ),
        "",
    )
    return overrides + (["-c", f"lfs.{endpoint}.locksverify=false"] if endpoint else [])


def push(root: Path, *arguments: str) -> list[dict]:
    """Push once; if git-lfs cannot reach the lock endpoint, retry without the lock query.

    The retry keeps everything that carries the release: the same refspec, the same
    ``--force-with-lease``, the same ``--atomic``, and git-lfs's own upload and integrity
    verification of the objects.  Only the lock *query* is dropped, for this push alone — the setting
    is passed with ``-c`` and never written down — and the returned advisory says what the endpoint
    did, so a release record shows the check was unavailable instead of hiding it.
    """

    try:
        git(root, "push", *arguments)
        return []
    except subprocess.CalledProcessError as error:
        failure = _lfs_lock_verify_failure(error)
        if not failure:
            raise
    git(root, *_lfs_lock_disable(root), "push", *arguments)
    return [
        {
            "code": "lfs_lock_verify_unavailable",
            "message": (
                "git-lfs's pre-push lock check could not reach this repository's LFS lock endpoint "
                f"(locks/verify {failure}), so the push was retried with the lock query disabled for "
                "this push only"
            ),
            "then": (
                "the same push uploaded and verified the LFS objects, and the next push queries the "
                "endpoint again; if this repository starts using LFS locks, fix the lock endpoint "
                "rather than relying on the retry"
            ),
        }
    ]


def _verify_github_cli() -> dict:
    """A submission ends in a pull request, so the CLI must exist and be signed in."""

    try:
        subprocess.run(["gh", "--version"], check=True, capture_output=True, text=True, encoding="utf-8")
    except FileNotFoundError as error:
        raise PipelineError("GitHub CLI (gh) is required to submit a candidate") from error
    except subprocess.CalledProcessError as error:
        raise PipelineError(f"GitHub CLI is unusable: {_error_text(error)}") from error
    try:
        subprocess.run(["gh", "auth", "status"], check=True, capture_output=True, text=True, encoding="utf-8")
    except (FileNotFoundError, subprocess.CalledProcessError) as error:
        raise PipelineError(f"GitHub CLI is not authenticated: {_error_text(error)}") from error
    return {"installed": True, "authenticated": True}


def _verify_git_identity(root: Path) -> None:
    """Catch a missing author or committer before CAD capture and model building."""

    for identity in ("GIT_AUTHOR_IDENT", "GIT_COMMITTER_IDENT"):
        if git(root, "var", identity, check=False).returncode:
            raise PipelineError(
                "Git commit identity is missing in this model checkout; configure user.name and user.email "
                "with `git config --local` before running model update"
            )


def dispatch(root: Path, sha: str, profile: str) -> dict:
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise PipelineError("An exact model commit SHA is required")
    if not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", profile):
        raise PipelineError("Invalid profile identifier")
    slug = repository_slug(root)
    endpoint = f"repos/{slug}/statuses/{sha}"
    status = {"context": f"description/{profile}", "description": "Exact model validation queued"}
    github_api(endpoint, {**status, "state": "pending"})
    try:
        github_api(
            f"repos/{slug}/actions/workflows/model-validation.yml/dispatches",
            {"ref": "main", "inputs": {"model_sha": sha, "profile": profile}},
        )
    except subprocess.CalledProcessError:
        github_api(endpoint, {**status, "state": "error", "description": "Dispatch failed; retry required"})
        raise
    return {
        "repository": slug,
        "model_sha": sha,
        "profile": profile,
        "state": "dispatched",
        "workflow": f"https://github.com/{slug}/actions/workflows/model-validation.yml",
    }


def _update_lock(root: Path) -> tuple[Path, int]:
    """Serialise updates on one workspace; a concurrent run is refused, never queued.

    The lock lives in this checkout's real git directory (``git rev-parse
    --git-path``), so a linked worktree locks its own gitdir instead of assuming a
    ``.git`` directory exists next to the files.
    """

    _require_checkout(root)
    path = Path(git(root, "rev-parse", "--git-path", "description-update.lock").stdout.strip())
    if not path.is_absolute():
        path = (root / path).resolve()
    if not path.parent.is_dir():
        raise PipelineError("Model workspace has no writable git directory for the update lock")
    try:
        handle = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as error:
        raise PipelineError(
            f"Another model update holds {path.name}; wait for it or remove a stale lock explicitly"
        ) from error
    os.write(handle, f"{os.getpid()}\n".encode())
    return path, handle


def update_preflight(root: Path, profile: str, *, expect_worker_url: str | None = None) -> dict:
    """Everything that has to hold before a workspace is frozen, built or pushed."""

    root = Path(root).resolve()
    try:
        layout = check_layout(root)
    except PipelineError as error:
        raise PipelineError(f"{error}; run from the model root or pass --root MODEL") from error
    resolved = profile_for(root, profile)
    branch = git(root, "branch", "--show-current").stdout.strip()
    if not branch.startswith(("feature/", "work/model/")):
        raise PipelineError("Model update only runs on a model development branch")
    parts = branch.split("/")
    if branch.startswith("work/model/") and len(parts) < 4:
        raise PipelineError("Use work/model/<hardware>/<change> for model review branches")
    hardware = _hardware(parts[2] if branch.startswith("work/model/") else branch.removeprefix("feature/"))
    declared_hardware = definition(root).get("hardware_id")
    if declared_hardware != hardware:
        raise PipelineError(
            f"Model hardware_id does not match its development branch ({declared_hardware!r} != {hardware!r})"
        )
    _verify_git_identity(root)
    # The one-click entry ends in a pull request; finding out that ``gh`` is missing
    # after the candidate has been pushed would leave a branch nobody can review.
    github = _verify_github_cli()
    # The one-click entry has to end in a review request: without the remote base
    # branch, submit would push a brand-new feature branch and open no pull request.
    if not git(root, "ls-remote", "origin", f"refs/heads/feature/{hardware}").stdout.strip():
        raise PipelineError(
            f"Remote feature/{hardware} is not initialised; push it once before using the one-click entry"
        )
    slug = repository_slug(root)
    # An installed tool that differs from the lock is an explicit stop, never a
    # silent lock upgrade: the candidate must be built by the pinned release.
    toolchain = verify_toolchain(root)
    dirty = [line[3:].strip() for line in git(root, "status", "--porcelain").stdout.splitlines() if line.strip()]
    unexpected = sorted({Path(entry.split(" -> ")[-1]).parts[0] for entry in dirty} - MODEL_DIRECTORIES - MODEL_FILES)
    if unexpected:
        raise PipelineError(
            "Workspace carries changes outside the model contract "
            f"(unexpected={unexpected}); use a dedicated model workspace"
        )
    if expect_worker_url is not None:
        declared = str((definition(root).get("source") or {}).get("worker_url") or "")
        if declared != expect_worker_url:
            raise PipelineError(
                "Model source.worker_url does not match the tunnel this entry provides "
                f"(declared={declared!r}, expected={expect_worker_url!r})"
            )
    return {
        "root": str(root),
        "role": layout["role"],
        "branch": branch,
        "repository": slug,
        "profile": profile,
        "purpose": resolved.get("purpose"),
        "hardware": hardware,
        "toolchain": {"version": toolchain.get("version"), "source_commit": toolchain.get("source_commit")},
        "github": github,
        "dirty_entries": dirty,
        "worker_url": expect_worker_url,
    }


def update(
    root: Path,
    profile: str,
    message: str | None = None,
    *,
    expect_worker_url: str | None = None,
    reuse_source: bool = False,
    worker_host: str | None = None,
    worker_port: int | None = None,
    ci: bool = False,
    mechanical_reference: Path | None = None,
) -> dict:
    """Author one revision end to end: preflight, freeze, build, submit.

    ``root`` is the caller's dedicated model workspace; this never writes to
    another checkout, never upgrades the tool lock and refuses to start while
    another update holds the workspace lock.
    """

    root = Path(root).resolve()
    if mechanical_reference is not None:
        from ..verification.mechanics import reference_path

        mechanical_reference = reference_path(mechanical_reference, root)
    if reuse_source and (worker_host is not None or worker_port is not None or expect_worker_url is not None):
        raise PipelineError("--reuse-source cannot be combined with worker connection options")
    if worker_port is not None and not worker_host:
        raise PipelineError("--worker-port requires --worker-host")
    if worker_host is not None and expect_worker_url is not None:
        raise PipelineError("--worker-host and --expect-worker-url describe different tunnel owners; choose one")
    if message is not None and not message.strip():
        raise PipelineError("A commit message is required")
    path, handle = _update_lock(root)
    try:
        preflight = update_preflight(root, profile, expect_worker_url=expect_worker_url)
        message = message if message is not None else f"Update {preflight['hardware']} model"
        preflight["message"] = message
        connection = None
        if reuse_source:
            frozen = inputs(root)[1]
        elif worker_host is not None:
            with worker_tunnel(
                root, definition(root)["source"], worker_host, 8765 if worker_port is None else worker_port
            ) as connection:
                frozen = freeze(root)
        else:
            source = definition(root)["source"]
            if source.get("provider") == "solidworks" and source.get("worker_url"):
                worker_health(source["worker_url"])
            frozen = freeze(root)
        # ``build`` already attaches the exact preserved diagnostic to a raised error and
        # to a report that did not qualify; both are propagated, never guessed from the
        # workspace's failure history.
        report = build(root, profile, mechanical_reference=mechanical_reference)
        if (
            report.get("blockers") == ["consumer.application"]
            and report.get("profile", {}).get("purpose") == "simulation"
            and (root / "config/simulation-acceptance.json").is_file()
        ):
            from ..verification.simulation import complete_pending

            report = complete_pending(root, profile, report)
        elif report.get("blockers") == ["consumer.application"] and mechanical_reference is not None:
            from ..verification.mechanics import complete_pending as complete_mechanics

            report = complete_mechanics(root, profile, report, mechanical_reference)
        if not report.get("passed"):
            failure = PipelineError(f"Candidate does not qualify: {report.get('blockers')}")
            if report.get("diagnostic_path"):
                failure.diagnostic_path = str(report["diagnostic_path"])
            raise failure
        # The workspace lock is already held here; ``_submit`` is the unlocked body.
        submitted = _submit(root, profile, message, ci=ci, mechanical_reference=mechanical_reference)
        return {
            "ok": bool(submitted.get("passed", False)),
            "state": submitted.get("state"),
            "root": str(root),
            "profile": profile,
            "branch": submitted.get("branch", preflight["branch"]),
            "model_sha": submitted.get("model_sha"),
            "preflight": preflight,
            "source_action": "reused" if reuse_source else "captured",
            "connection": connection,
            "freeze": frozen,
            "build": {
                "passed": report.get("passed"),
                "subject": report.get("subject"),
                "profile_digest": report.get("profile_digest"),
            },
            "submit": submitted,
            "pull_request": submitted.get("pull_request"),
            "central_validation": submitted.get("central_validation"),
            **({"advisories": submitted["advisories"]} if submitted.get("advisories") else {}),
        }
    finally:
        os.close(handle)
        path.unlink(missing_ok=True)


def _review_branch(root: Path, hardware: str) -> str:
    """A fresh review branch name; a taken name is retried, never reused."""

    for _ in range(3):
        branch = f"work/model/{hardware}/{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{secrets.token_hex(3)}"
        if git(root, "rev-parse", "--verify", "refs/heads/" + branch, check=False).returncode != 0:
            return branch
    raise PipelineError("Could not allocate an unused review branch name")


def _base_moved_advisory(root: Path, hardware: str) -> dict | None:
    """Say when the review branch no longer contains the current feature tip.

    A review branch whose base moved on is still a valid pull request, so this never blocks a
    submission.  It is worth saying out loud: the candidate was built against the older base,
    so a reviewer who merges it merges artifacts that never saw the newer commits - and a
    branch that only *looks* mergeable is exactly where a first submission stalls.
    """

    base = f"feature/{hardware}"
    remote = f"refs/remotes/origin/{base}"
    fetched = git(root, "fetch", "origin", f"{base}:{remote}", check=False)
    if fetched.returncode != 0:
        # A failed fetch leaves whatever the last successful one wrote, so the remote-tracking ref
        # can describe a branch that no longer exists (or a state this machine never saw).
        return None
    if git(root, "rev-parse", "--verify", "--quiet", remote, check=False).returncode != 0:
        # Nothing to compare with: a hardware branch that does not exist on the remote yet, or a
        # checkout without that ref.  A note the operator cannot act on is worse than no note.
        return None
    contained = git(root, "merge-base", "--is-ancestor", remote, "HEAD", check=False)
    if contained.returncode == 0:
        return None
    if contained.returncode != 1:
        # git answers 1 for "not an ancestor"; anything else is an error (a broken object store,
        # an unexpected argument), which is not something to turn into a claim about the branch.
        return None
    return {
        "code": "review_branch_behind_base",
        "message": (
            f"this review branch does not contain the current {base}; a reviewer would merge "
            "artifacts built on an older base"
        ),
        "base": base,
        "commands": ["git fetch origin", f"git merge origin/{base}"],
        "then": (
            "re-run the submission from the workspace: description model submit if the built "
            "artifacts are still current, or description model update, which rebuilds, when the "
            "merge changed the model inputs"
        ),
    }


def _mechanical_digest(report: dict) -> str | None:
    for check in report.get("checks", []):
        if check["id"] == "consumer.application":
            return check.get("details", {}).get("authority", {}).get("reference_sha256")
    return None


def _review_request(
    root: Path, branch: str, hardware: str, sha: str, profile: str, report: dict, message: str, *, ci: bool = False
) -> tuple[dict, int]:
    """Create the pull request for this branch, or refresh the one it already has."""

    slug = repository_slug(root)
    base = f"feature/{hardware}"
    pulls = github_api(f"repos/{slug}/pulls?state=open&head={slug.split('/')[0]}:{branch}")
    if pulls is not None and not isinstance(pulls, list):
        raise PipelineError("Invalid GitHub pull request list response")
    pull = next(
        (item for item in pulls or [] if isinstance(item, dict) and item.get("base", {}).get("ref") == base), None
    )
    body = (
        f"Model candidate `{sha}` for `{hardware}`.\n\n"
        f"Profile: `{profile}`. Local subject: `{report['subject']}`.\n"
        "Local independent verification passed. Release re-fetches this exact candidate from the remote "
        "and checks its source, artifacts, pinned tool and intended use.\n\n"
        + ("GitHub CI was explicitly requested; its result is pending.\n\n" if ci else "")
        + f"Re-running on `{branch}` updates this pull request instead of opening another one."
    )
    mechanical_digest = _mechanical_digest(report)
    if mechanical_digest:
        body += (
            f"\n\nKinematics was checked against the operator-selected mechanical reference `{mechanical_digest}`. "
            "This does not establish physical, training or hardware qualification."
        )
    if pull is None:
        created = github_api(
            f"repos/{slug}/pulls",
            {"head": branch, "base": base, "title": message.splitlines()[0], "body": body},
        )
        number = created.get("number") if isinstance(created, dict) else None
        if not isinstance(created, dict) or number is None:
            raise PipelineError("GitHub returned no pull request for the pushed branch")
        return created, number
    # A re-run on the same review branch keeps the thread but describes the new commit.
    number = pull.get("number")
    if number is None:
        raise PipelineError("Open pull request response carries no number to update")
    updated = github_api(
        f"repos/{slug}/pulls/{number}", {"title": message.splitlines()[0], "body": body}, method="PATCH"
    )
    if not isinstance(updated, dict):
        raise PipelineError("Invalid GitHub pull request response")
    return updated, number


def submit(
    root: Path, profile: str, message: str, *, ci: bool = False, mechanical_reference: Path | None = None
) -> dict:
    """Publish one candidate for review, holding the workspace lock.

    ``update`` already owns the lock and calls :func:`_submit` directly, so the
    public entry and the one-click entry can never collect or push the same
    workspace at the same time.
    """

    root = Path(root).resolve()
    path, handle = _update_lock(root)
    try:
        return _submit(root, profile, message, ci=ci, mechanical_reference=mechanical_reference)
    finally:
        os.close(handle)
        path.unlink(missing_ok=True)


def _submit(
    root: Path, profile: str, message: str, *, ci: bool = False, mechanical_reference: Path | None = None
) -> dict:
    """Publish one candidate for review.

    The candidate is committed on a review branch: a run from ``feature/<hardware>``
    creates ``work/model/<hardware>/<timestamp>-<random>`` *before* committing, so the
    development branch is never advanced by a submission.  A re-run from that review
    branch commits there and updates the pull request it already has.

    GitHub CI is opt-in. If requested, dispatch happens after the pull request exists;
    a dispatch failure preserves the review link and the exact retry command.
    """

    check_layout(root)
    report = assess(root, profile, mechanical_reference=mechanical_reference)
    if not report["passed"]:
        raise PipelineError(f"Candidate does not qualify: {report['blockers']}")
    if not message.strip():
        raise PipelineError("A commit message is required")
    current = git(root, "branch", "--show-current").stdout.strip()
    if not current.startswith(("feature/", "work/model/")):
        raise PipelineError("Model submit only runs on a model development branch")
    parts = current.split("/")
    if current.startswith("work/model/") and len(parts) < 4:
        raise PipelineError("Use work/model/<hardware>/<change> for model review branches")
    hardware = _hardware(parts[2] if current.startswith("work/model/") else current.removeprefix("feature/"))
    if report["hardware_id"] != hardware:
        raise PipelineError("Model hardware_id does not match its development branch")
    branch = current if current.startswith("work/model/") else _review_branch(root, hardware)
    if branch != current:
        git(root, "switch", "-c", branch)
    git(root, "add", "--", ".")
    if git(root, "diff", "--cached", "--quiet", check=False).returncode:
        git(root, "commit", "-m", message)
    sha = git(root, "rev-parse", "HEAD").stdout.strip()
    push_notes = push(root, "-u", "origin", branch)
    base_moved = _base_moved_advisory(root, hardware)
    advisories = ([base_moved] if base_moved is not None else []) + push_notes
    response: dict = {
        "repository": repository_slug(root),
        "model_sha": sha,
        "profile": profile,
        "branch": branch,
        "state": "pushed",
        "mechanical_reference_sha256": _mechanical_digest(report),
    }

    def with_advisory(value: dict) -> dict:
        if advisories:
            value["advisories"] = advisories
        return value

    try:
        pull, number = _review_request(root, branch, hardware, sha, profile, report, message, ci=ci)
    except (PipelineError, OSError, subprocess.SubprocessError) as error:
        # The candidate is already on its branch; only the review request is missing.
        response["state"] = "pushed_pending_review"
        response["passed"] = False
        response["review"] = {"state": "not_created", "error": _error_text(error)}
        response["retry"] = (
            "description model submit --root "
            f"{quote_argument(str(root))} --profile {profile} --message-file -{' --ci' if ci else ''}"
            + (f" --mechanical-reference {quote_argument(str(mechanical_reference))}" if mechanical_reference else "")
            + f"  # reuses {branch} and opens the pull request"
        )
        return with_advisory(response)
    response["pull_request"] = pull["html_url"]
    response["pull_request_number"] = number
    if not ci:
        return with_advisory(
            {
                **response,
                "state": "pull_request_open",
                "passed": True,
                "central_validation": {"state": "not_requested"},
            }
        )
    # The review request exists before the central run is triggered: a dispatch failure
    # must leave it discoverable and name the exact retry.
    try:
        dispatched = dispatch(root, sha, profile)
    except (PipelineError, OSError, subprocess.SubprocessError) as error:
        response["state"] = "pull_request_open"
        response["passed"] = False
        response["central_validation"] = {
            "state": "dispatch_failed",
            "error": _error_text(error),
            "retry": f"description model dispatch --root {root} --candidate {sha} --profile {profile}",
        }
        return with_advisory(response)
    response.update(dispatched)
    response["central_validation"] = {"state": "dispatched", "workflow": dispatched.get("workflow")}
    response["passed"] = True
    return with_advisory(response)


def validate_commit(
    repository: Path, sha: str, profile: str, *, remote: bool = False, mechanical_reference: Path | None = None
) -> dict:
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise PipelineError("Validation requires exact 40-character commit SHA")
    _require_checkout(repository)
    if mechanical_reference is not None:
        from ..verification.mechanics import reference_path

        mechanical_reference = reference_path(mechanical_reference, repository)
    temporary = Path(tempfile.mkdtemp(prefix="description-validation-"))
    model = temporary / "model"
    try:
        if remote:
            # A new object/LFS store proves the remote can supply the actual delivery.
            model.mkdir()
            git(model, "init", "--quiet")
            if os.name == "nt":
                git(model, "config", "core.longpaths", "true")
            git(model, "config", "lfs.storage", str(temporary / "lfs"))
            git(model, "remote", "add", "origin", git(repository, "remote", "get-url", "origin").stdout.strip())
            git(model, "fetch", "--depth=1", "origin", sha)
            git(model, "checkout", "--detach", sha)
        else:
            git(repository, "worktree", "add", "--detach", str(model), sha)
        check_layout(model)
        report = assess(model, profile, mechanical_reference=mechanical_reference)
        return {**report, "model_sha": sha, "delivery_retrieved_from_remote": remote}
    finally:
        if model.exists() and not remote:
            git(repository, "worktree", "remove", "--force", str(model), check=False)
        shutil.rmtree(temporary, ignore_errors=True)


def compare_models(repository: Path, before: Path, after: Path) -> dict:
    """Compare directories or exact revisions materialized in detached worktrees."""
    repository = Path(repository)
    # A bare ``git rev-parse`` failure would surface as a raw CalledProcessError whose message names
    # the command, not the mistake.  Name the two things an operator can actually fix.
    probe = git(repository, "rev-parse", "--git-dir", check=False)
    if probe.returncode != 0:
        raise PipelineError(f"Repository is not a Git checkout: {repository}; pass --repository to name one")
    temporary = Path(tempfile.mkdtemp(prefix="description-diff-"))
    worktrees = []
    identities = []
    roots = []
    try:
        for index, target in enumerate((before, after)):
            if target.is_dir():
                roots.append(target.resolve())
                identities.append({"directory": str(target.resolve())})
                continue
            resolved = git(
                repository, "rev-parse", "--verify", "--end-of-options", str(target) + "^{commit}", check=False
            )
            if resolved.returncode != 0:
                detail = " ".join(resolved.stderr.split())[:200]
                raise PipelineError(f"Cannot resolve {target} in {repository}: {detail}")
            sha = resolved.stdout.strip()
            root = temporary / str(index)
            git(repository, "worktree", "add", "--detach", str(root), sha)
            worktrees.append(root)
            roots.append(root)
            identities.append({"commit": sha})
        return {**semantic_diff(*roots), "references": identities}
    finally:
        for root in worktrees:
            git(repository, "worktree", "remove", "--force", str(root), check=False)
        shutil.rmtree(temporary, ignore_errors=True)


def _accepted_tool_main(repository: Path, tool_sha: str) -> str:
    """Require the locked tool commit in the model or public tool main history."""

    git(repository, "fetch", "origin", "main:refs/remotes/origin/main")
    if (
        git(repository, "merge-base", "--is-ancestor", tool_sha, "refs/remotes/origin/main", check=False).returncode
        == 0
    ):
        return "model_main"
    try:
        git(repository, "fetch", "--no-tags", PUBLIC_TOOL_REMOTE, "main")
    except subprocess.CalledProcessError as error:
        raise PipelineError(
            "Locked tool commit is not on model main, and public tool main could not be checked"
        ) from error
    if git(repository, "merge-base", "--is-ancestor", tool_sha, "FETCH_HEAD", check=False).returncode:
        raise PipelineError("Locked tool commit has not been accepted on model or public tool main")
    return "public_tool_main"


def promotion_plan(
    repository: Path,
    hardware: str,
    sha: str,
    profile: str,
    tag: str | None = None,
    *,
    ci: bool = False,
    mechanical_reference: Path | None = None,
) -> dict:
    hardware = _hardware(hardware)
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise PipelineError("Promotion requires an exact candidate SHA")
    _require_checkout(repository)
    if tag:
        git(repository, "check-ref-format", "refs/tags/" + tag)
    git(repository, "fetch", "origin", f"feature/{hardware}:refs/remotes/origin/feature/{hardware}")
    feature = f"refs/remotes/origin/feature/{hardware}"
    if git(repository, "merge-base", "--is-ancestor", sha, feature, check=False).returncode:
        raise PipelineError("Candidate has not been accepted by the hardware feature branch")
    release = f"refs/heads/release/{hardware}"
    remote = git(repository, "ls-remote", "origin", release).stdout.strip()
    old = remote.split()[0] if remote else ""
    if old:
        git(repository, "fetch", "origin", old)
    if old and git(repository, "merge-base", "--is-ancestor", old, sha, check=False).returncode:
        raise PipelineError("Release promotion must fast-forward")
    if tag and git(repository, "ls-remote", "origin", "refs/tags/" + tag).stdout.strip():
        raise PipelineError("Immutable release tag already exists")
    report = validate_commit(repository, sha, profile, remote=True, mechanical_reference=mechanical_reference)
    if report.get("hardware_id") != hardware:
        raise PipelineError("Candidate hardware_id does not match the requested release")
    if not report["passed"] or report["source"]["evidence_class"] != "cad" or report["toolchain"].get("development"):
        raise PipelineError("Release requires qualified native-source evidence and a fixed tool release")
    tool = report["toolchain"]
    tool_sha = tool.get("source_commit", "")
    if not re.fullmatch(r"[0-9a-f]{40}", tool_sha or ""):
        raise PipelineError("Tool lock requires an exact source commit")
    tool_main = _accepted_tool_main(repository, tool_sha)
    return {
        "schema_version": "description.promotion/v1",
        "hardware": hardware,
        "candidate": sha,
        "previous_release": old,
        "release_ref": release,
        "tag": tag,
        "profile": profile,
        "ci": ci,
        "subject": report["subject"],
        "tool_main": tool_main,
        "report": report,
        "mechanical_reference_sha256": _mechanical_digest(report),
    }


def promote(repository: Path, plan: dict, *, mechanical_reference: Path | None = None) -> dict:
    fresh = promotion_plan(
        repository,
        plan["hardware"],
        plan["candidate"],
        plan["profile"],
        plan.get("tag"),
        ci=plan.get("ci", False),
        mechanical_reference=mechanical_reference,
    )
    for key in ("previous_release", "release_ref", "subject", "mechanical_reference_sha256"):
        if fresh.get(key) != plan.get(key):
            raise PipelineError(f"Stale promotion plan: {key} changed")
    review_run = _review(repository, plan)
    sha = plan["candidate"]
    slug = repository_slug(repository)
    github_api(
        f"repos/{slug}/statuses/{sha}",
        {
            "context": "description/release",
            "state": "success",
            "description": (
                "Local and CI verification: exact candidate and remote delivery"
                if plan.get("ci", False)
                else "Local verification: exact candidate and remote delivery"
            ),
            "target_url": review_run,
        },
    )
    push_notes = push(
        repository,
        "--atomic",
        f"--force-with-lease={plan['release_ref']}:{plan['previous_release']}",
        "origin",
        f"{sha}:{plan['release_ref']}",
        *([f"{sha}:refs/tags/{plan['tag']}"] if plan.get("tag") else []),
    )
    return {**plan, **fresh, "state": "published", **({"advisories": push_notes} if push_notes else {})}


def _review(repository: Path, plan: dict) -> str:
    slug = repository_slug(repository)
    reference = plan.get("review_evidence", "")
    if reference:
        _review_evidence(slug, plan, reference)
    if not plan.get("ci", False):
        return reference or f"https://github.com/{slug}/commit/{plan['candidate']}"
    status = github_api(f"repos/{slug}/commits/{plan['candidate']}/status")
    checks = status.get("statuses", []) if isinstance(status, dict) else []
    selected = next((item for item in checks if item["context"] == f"description/{plan['profile']}"), None)
    if not selected or selected["state"] != "success":
        raise PipelineError("Exact candidate lacks successful central qualification")
    url = selected.get("target_url", "")
    run_match = re.fullmatch(rf"https://github.com/{re.escape(slug)}/actions/runs/([0-9]+)", url)
    run = github_api(f"repos/{slug}/actions/runs/{run_match[1]}") if run_match else None
    if (
        not isinstance(run, dict)
        or run.get("conclusion") != "success"
        or run.get("path") != (".github/workflows/model-validation.yml")
        or run.get("event") != "workflow_dispatch"
        or run.get("head_branch") != "main"
        or run.get("display_title") != f"qualify {plan['candidate']} ({plan['profile']})"
    ):
        raise PipelineError("Qualification status must refer to a successful central validation run")
    return url


def _review_evidence(slug: str, plan: dict, reference: str) -> None:
    match = re.fullmatch(rf"https://github.com/{re.escape(slug)}/pull/([0-9]+)", reference)
    if not match:
        raise PipelineError("Review evidence must be a merged model PR URL in this repository")
    pull = github_api(f"repos/{slug}/pulls/{match[1]}")
    if not isinstance(pull, dict) or (
        not pull.get("merged")
        or pull["base"]["ref"] != f"feature/{plan['hardware']}"
        or plan["candidate"] not in {pull.get("merge_commit_sha"), pull["head"]["sha"]}
    ):
        raise PipelineError("Review PR does not establish acceptance of this exact model candidate")
    reviews = _pages(f"repos/{slug}/pulls/{match[1]}/reviews")
    latest = {item["user"]["login"]: item for item in reviews if item["state"] != "COMMENTED"}
    if any(item["state"] == "CHANGES_REQUESTED" for item in latest.values()) or not any(
        item["state"] == "APPROVED" and item["commit_id"] == pull["head"]["sha"] and name != pull["user"]["login"]
        for name, item in latest.items()
    ):
        raise PipelineError("An approval of the final PR head is required; stale approvals do not qualify")


def _pages(endpoint: str) -> list[dict]:
    values = []
    for page in range(1, 10001):
        separator = "&" if "?" in endpoint else "?"
        batch = github_api(f"{endpoint}{separator}per_page=100&page={page}")
        if not isinstance(batch, list):
            raise PipelineError("Expected paginated GitHub array")
        values.extend(batch)
        if len(batch) < 100:
            return values
    raise PipelineError("GitHub pagination exceeded supported limit")


def pending_candidates(root: Path, profile: str, *, ci: bool = False) -> list[dict]:
    slug = repository_slug(root)
    refs = github_api(f"repos/{slug}/git/matching-refs/heads/feature/")
    pulls = _pages(f"repos/{slug}/pulls?state=open")
    candidates = {item["object"]["sha"] for item in refs or []}
    candidates.update(
        item["head"]["sha"]
        for item in pulls or []
        if item["base"]["ref"].startswith("feature/") and (item["head"].get("repo") or {}).get("full_name") == slug
    )
    pending = []
    for sha in sorted(candidates):
        statuses = github_api(f"repos/{slug}/commits/{sha}/status")
        if not isinstance(statuses, dict):
            raise PipelineError("Invalid GitHub status response")
        matching = [status for status in statuses.get("statuses", []) if status["context"] == f"description/{profile}"]
        stale = bool(
            matching
            and matching[0]["state"] == "pending"
            and datetime.fromisoformat(matching[0]["updated_at"].replace("Z", "+00:00"))
            < datetime.now(UTC) - timedelta(hours=2)
        )
        if not matching or stale or matching[0]["state"] == "error":
            pending.append(
                dispatch(root, sha, profile)
                if ci
                else {"repository": slug, "model_sha": sha, "profile": profile, "state": "not_requested"}
            )
    return pending
