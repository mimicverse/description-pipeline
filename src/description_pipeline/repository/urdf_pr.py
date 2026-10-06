"""Publish a verified URDF delivery bundle as one fast-forward review branch and PR.

Public API: ``submit_bundle(bundle, repository, *, base, branch, message=None, dry_run=False)``.

Guarantees:

* only governed paths are staged (``README.md`` + ``input, evidence, model, urdf, meshes, reports``);
* the bundle subject digest is re-bound to the staged bytes *and* the committed tree before any push;
* a stale or tampered ``reports/quality.json`` can never escape, even if it says ``passed``;
* the review branch is only ever fast-forwarded on top of its own remote head (no force pushes,
  no base rewrites, foreign branches are refused);
* existing PRs are updated with the latest metadata instead of being duplicated;
* a GitHub failure after a successful push is reported with the pushed commit preserved.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path

from ..delivery import subject_digest
from ..io import PipelineError, read_data
from ..sources.solidworks import revision as cad_revision

GOVERNED_PATHS = ("README.md", "input", "evidence", "model", "urdf", "meshes", "reports")
REPORTS_FILES = ("input.json", "tool.json", "quality.json")
REQUIRED_FILES = ("README.md", "input/robot.yaml", "model/robot.json", "urdf/robot.urdf",
                  "reports/input.json", "reports/tool.json")
REQUIRED_DIRS = ("evidence", "meshes")
REVIEW_BRANCH = "work/solidworks/{hardware}"
PUBLISHER_MARKER = "Urdf-Publisher: description-pipeline"


class PrError(RuntimeError):
    def __init__(self, code: str, detail: object | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


def _git(repository: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repository), *args], check=check,
                          capture_output=True, text=True, encoding="utf-8")


def _slug_hardware(value: str) -> str:
    slug = re.sub(r"[^a-z0-9_.-]", "-", value.strip().lower())
    if not slug or slug.startswith("-"):
        raise PrError("hardware_id_invalid", value)
    return slug


def _origin_slug(repository: Path) -> str:
    url = _git(repository, "remote", "get-url", "origin").stdout.strip()
    match = re.fullmatch(r"(?:https://github\.com/|git@github\.com:)([^/]+/[^/]+?)(?:\.git)?", url)
    if not match:
        raise PrError("origin_not_github", url)
    return match.group(1)


def _remote_branch(repository: Path, branch: str) -> tuple[str, str]:
    listing = _git(repository, "ls-remote", "origin", f"refs/heads/{branch}").stdout.strip()
    if not listing:
        return "", ""
    sha = listing.split()[0]
    _git(repository, "fetch", "--quiet", "origin", branch)
    message = _git(repository, "log", "-1", "--format=%B", "FETCH_HEAD").stdout
    return sha, message


def _serialize(report: dict) -> str:
    return json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def _load_verifier():
    from description_pipeline.verification.solidworks_urdf import check_bundle

    return check_bundle


def _gh(repository: Path, *args: str) -> str:
    result = subprocess.run(["gh", *args], cwd=repository, check=True,
                            capture_output=True, text=True, encoding="utf-8")
    return result.stdout


def _validate(bundle: Path) -> tuple[str, dict, dict]:
    for name in REQUIRED_FILES:
        if not (bundle / name).is_file():
            raise PrError("bundle_incomplete", name)
    for name in REQUIRED_DIRS:
        if not (bundle / name).is_dir():
            raise PrError("bundle_incomplete", name)
    seen: dict[str, str] = {}
    for path in bundle.rglob("*"):
        if path.is_symlink():
            raise PrError("bundle_symlink", path.relative_to(bundle).as_posix())
        if path.is_file():
            rel = path.relative_to(bundle).as_posix()
            key = rel.lower()
            if key in seen and seen[key] != rel:
                raise PrError("duplicate_path", {"first": seen[key], "second": rel})
            seen[key] = rel
    manifest = read_data(bundle / "input/robot.yaml")
    if not isinstance(manifest, dict) or not manifest.get("hardware_id"):
        raise PrError("robot_yaml_invalid", "hardware_id is required")
    hardware = _slug_hardware(str(manifest["hardware_id"]))
    try:
        current = cad_revision.read_revision(bundle / "input", hardware_id=manifest["hardware_id"])
    except PipelineError as error:
        raise PrError("cad_revision_invalid", str(error)) from error
    return hardware, manifest, current


def _previous_revision(worktree: Path, hardware: str) -> dict | None:
    if not (worktree / "input/cad-revision.json").exists():
        return None
    try:
        return cad_revision.read_revision(worktree / "input")
    except PipelineError as error:
        raise PrError("previous_cad_revision_invalid", str(error)) from error


def _validate_ref(repository: Path, name: str, *, branch: bool) -> None:
    if not name or name.startswith("-") or name.endswith(".lock") or ".." in name:
        raise PrError("invalid_ref", name)
    args = ("check-ref-format", "--branch", name) if branch else ("check-ref-format", f"refs/heads/{name}")
    if _git(repository, *args, check=False).returncode != 0:
        raise PrError("invalid_ref", name)


def _verify(bundle: Path) -> tuple[str, dict]:
    subject = subject_digest(bundle)
    report = _load_verifier()(bundle)
    if not isinstance(report, dict) or report.get("passed") is not True:
        raise PrError("verification_failed", report if isinstance(report, dict) else None)
    if report.get("subject_sha256") != subject:
        raise PrError("verification_subject_mismatch",
                      {"expected": subject, "reported": report.get("subject_sha256")})
    on_disk = bundle / "reports/quality.json"
    if not on_disk.is_file():
        raise PrError("missing_quality_report", "reports/quality.json is required")
    try:
        saved = json.loads(on_disk.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise PrError("quality_report_invalid", str(error)) from error
    if saved != report:
        raise PrError("stale_quality_report", "on-disk quality report differs from the recomputed report")
    return subject, report


def _reverify(root: Path, subject: str) -> None:
    report = _load_verifier()(root)
    if not isinstance(report, dict) or report.get("passed") is not True or report.get("subject_sha256") != subject:
        raise PrError("reverification_failed", report if isinstance(report, dict) else None)
    try:
        saved = json.loads((root / "reports/quality.json").read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise PrError("reverification_missing_quality", None) from error
    if saved != report:
        raise PrError("reverification_binding_mismatch", None)


def _lock(repository: Path):
    git_dir = Path(_git(repository, "rev-parse", "--absolute-git-dir").stdout.strip())
    lock = git_dir / "urdf-pr.lock"
    try:
        handle = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, stat.S_IRUSR | stat.S_IWUSR)
    except FileExistsError as error:
        raise PrError("repository_locked", str(lock)) from error
    os.write(handle, str(os.getpid()).encode("ascii"))
    return lock, handle


def _copy_governed(bundle: Path, worktree: Path) -> None:
    reports = worktree / "reports"
    if reports.is_symlink() or reports.is_file():
        reports.unlink()
    elif reports.is_dir():
        shutil.rmtree(reports)
    for name in GOVERNED_PATHS:
        if name == "reports":
            continue
        target = worktree / name
        if target.is_symlink():
            target.unlink()
        elif target.is_dir():
            shutil.rmtree(target)
        elif target.exists():
            target.unlink()
        source = bundle / name
        if source.is_dir():
            shutil.copytree(source, target)
        elif source.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
    reports.mkdir(parents=True, exist_ok=True)
    for name in REPORTS_FILES:
        source = bundle / "reports" / name
        if source.is_file():
            shutil.copyfile(source, reports / name)


def _prepare_worktree(repository: Path, worktree: Path, base_sha: str, head_sha: str) -> None:
    start = head_sha or base_sha
    _git(repository, "worktree", "add", "--detach", str(worktree), start)
    if head_sha and _git(worktree, "merge-base", "--is-ancestor", base_sha, "HEAD", check=False).returncode != 0:
        merge = _git(worktree, "merge", "--no-edit", base_sha, check=False)
        if merge.returncode != 0:
            _git(worktree, "merge", "--abort", check=False)
            raise PrError("base_conflict", merge.stderr[-400:])


def _stage_commit(bundle: Path, worktree: Path, subject: str, message: str) -> tuple[str, bool]:
    _copy_governed(bundle, worktree)
    _reverify(worktree, subject)
    changed = _git(worktree, "status", "--porcelain").stdout.splitlines()
    for line in changed:
        path = line[3:].strip().strip('"')
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        if path and not any(path == name or path.startswith(name + "/") for name in GOVERNED_PATHS):
            raise PrError("ungoverned_change", path)
    if subject_digest(worktree) != subject:
        raise PrError("staged_subject_mismatch", "staged worktree bytes differ from the verified bundle")
    _git(worktree, "add", "--all")
    staged = _git(worktree, "diff", "--cached", "--quiet", check=False).returncode
    if staged == 0:
        return _git(worktree, "rev-parse", "HEAD").stdout.strip(), True
    body = f"{message}\n\n{PUBLISHER_MARKER}\nUrdf-Subject: {subject}\n"
    _git(worktree, "commit", "-m", body)
    commit = _git(worktree, "rev-parse", "HEAD").stdout.strip()
    if _git(worktree, "status", "--porcelain").stdout.strip():
        raise PrError("commit_left_dirty", "staged worktree is not clean after commit")
    if _git(worktree, "diff", "--quiet", "HEAD", check=False).returncode != 0:
        raise PrError("commit_worktree_mismatch", "worktree differs from the committed tree")
    if subject_digest(worktree) != subject:
        raise PrError("committed_subject_mismatch", "committed tree bytes differ from the verified bundle")
    diff = _git(worktree, "diff", "--name-only", "HEAD^", "HEAD").stdout.splitlines()
    for path in diff:
        if path and not any(path == name or path.startswith(name + "/") for name in GOVERNED_PATHS):
            raise PrError("commit_touched_ungoverned", path)
    return commit, False


def _body(subject: str, commit: str, report: dict) -> str:
    checks = report.get("checks") or []
    return (f"Automatic SolidWorks-to-URDF publication.\n\n"
            f"- subject: `{subject}`\n- verified commit: `{commit}`\n"
            f"- verifier: recomputed, passed ({len(checks)} checks)\n")


def _pr(repository: Path, slug: str, base: str, branch: str, subject: str, commit: str,
        report: dict, message: str | None) -> tuple[str, str]:
    listing = json.loads(_gh(repository, "pr", "list", "--repo", slug, "--base", base, "--head", branch,
                             "--state", "open", "--json", "url,number,baseRefName,headRefName") or "[]")
    matches = [item for item in listing if item.get("baseRefName") == base and item.get("headRefName") == branch]
    if len(matches) > 1:
        raise PrError("pr_ambiguous", [item["url"] for item in matches])
    title = (message.splitlines()[0] if message else f"SolidWorks-to-URDF bundle ({commit[:12]})")
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as handle:
        handle.write(_body(subject, commit, report))
        body_path = handle.name
    try:
        if matches:
            _gh(repository, "pr", "edit", str(matches[0]["number"]), "--repo", slug,
                "--title", title, "--body-file", body_path)
            return "updated", matches[0]["url"]
        url = _gh(repository, "pr", "create", "--repo", slug, "--base", base, "--head", branch,
                  "--title", title, "--body-file", body_path).strip()
        return "published", url
    finally:
        Path(body_path).unlink(missing_ok=True)


def submit_bundle(bundle: Path, repository: Path, *, base: str, branch: str,
                  message: str | None = None, dry_run: bool = False) -> dict:
    """Validate, verify and publish one bundle through a fast-forward review branch and PR."""
    bundle = Path(bundle).resolve()
    repository = Path(repository).resolve()
    lock_info = None
    staging = None
    pushed = ""
    subject = ""
    slug = ""
    try:
        hardware, _, current_revision = _validate(bundle)
        expected = REVIEW_BRANCH.format(hardware=hardware)
        if branch != expected:
            raise PrError("branch_not_deterministic", {"expected": expected})
        _validate_ref(repository, base, branch=False)
        _validate_ref(repository, branch, branch=True)
        slug = _origin_slug(repository)
        lock_info = _lock(repository)
        if _git(repository, "status", "--porcelain").stdout.strip():
            raise PrError("dirty_repository", "commit, stash or remove unrelated changes first")
        subject, report = _verify(bundle)
        _git(repository, "fetch", "--quiet", "origin", base)
        base_sha = _git(repository, "rev-parse", "FETCH_HEAD").stdout.strip()
        head_sha, head_message = _remote_branch(repository, branch)
        if head_sha and PUBLISHER_MARKER not in head_message:
            raise PrError("branch_foreign", {"branch": branch, "head": head_sha})
        if dry_run:
            return {"state": "dry_run", "branch": branch, "base": base, "subject": subject,
                    "commit": head_sha, "url": ""}
        staging = Path(tempfile.mkdtemp(prefix="urdf-pr-"))
        worktree = staging / "worktree"
        try:
            _prepare_worktree(repository, worktree, base_sha, head_sha)
            previous = _previous_revision(worktree, hardware)
            if previous is not None:
                try:
                    cad_revision.check_successor(previous, current_revision)
                except PipelineError as error:
                    raise PrError("cad_revision_conflict", str(error)) from error
            commit, noop = _stage_commit(bundle, worktree, subject,
                                         message or f"feat({hardware}): publish SolidWorks-to-URDF bundle")
            _reverify(worktree, subject)
            if not noop or commit != head_sha:
                _git(worktree, "push", "origin", f"HEAD:refs/heads/{branch}")
            pushed = commit
        finally:
            _git(repository, "worktree", "remove", "--force", str(worktree), check=False)
            _git(repository, "worktree", "prune", check=False)
            shutil.rmtree(staging, ignore_errors=True)
        if _git(repository, "ls-remote", "origin", f"refs/heads/{branch}").stdout.split()[:1] != [pushed]:
            raise PrError("remote_head_mismatch", {"expected": pushed})
        state, url = _pr(repository, slug, base, branch, subject, pushed, report, message)
        content_noop = noop and pushed == head_sha
        return {"state": "noop" if content_noop else state, "branch": branch, "base": base,
                "subject": subject, "commit": pushed, "url": url}
    except PrError as error:
        return {"state": "failed", "error": error.code, "detail": error.detail,
                "commit": pushed, "branch": branch, "subject": subject}
    except subprocess.CalledProcessError as error:
        if pushed:
            return {"state": "gh_failed_after_push", "error": "github_failed", "commit": pushed,
                    "branch": branch, "subject": subject, "base": base,
                    "retry": {"command": "gh pr list --head " + branch, "stderr": (error.stderr or "")[-300:]}}
        return {"state": "failed", "error": "git_failed", "detail": (error.stderr or "")[-300:],
                "commit": pushed, "branch": branch, "subject": subject}
    except Exception as error:  # noqa: BLE001 - receipts must never crash the caller
        return {"state": "failed", "error": type(error).__name__, "detail": str(error)[:300],
                "commit": pushed, "branch": branch, "subject": subject}
    finally:
        if lock_info is not None:
            lock, handle = lock_info
            os.close(handle)
            lock.unlink(missing_ok=True)
