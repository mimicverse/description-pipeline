"""Publish a checked URDF bundle to a dedicated model repository through one review PR.

Public API: ``submit_bundle(bundle, repository, *, base, branch, message=None) -> dict``.

The bundle layout is fixed: ``input/robot.yaml``, ``evidence/`` (frozen capture), ``model/robot.json``,
``urdf/robot.urdf``, ``meshes/`` and ``reports/input.json``; ``reports/quality.json`` must be the
*recomputed* verifier report, never a stale passed file. Nothing is pushed unless the verifier
recomputes a passing report whose subject digest matches the staged bundle bytes.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import yaml

from ..delivery import subject_digest
from . import _hardware, check_layout, git

REQUIRED_FILES = ("README.md", "input/robot.yaml", "model/robot.json", "urdf/robot.urdf",
                  "reports/input.json", "reports/tool.json")
REQUIRED_DIRS = ("evidence", "meshes")
OWNED_PATHS = ("README.md", "input", "evidence", "model", "urdf", "meshes", "reports")
REVIEW_BRANCH = "work/solidworks/{hardware}"


class PrError(RuntimeError):
    def __init__(self, code: str, detail: object | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


def _fail(code: str, detail: object | None = None) -> dict:
    return {"state": "failed", "error": code, "detail": detail}


def subject_hash(bundle: Path) -> str:
    """Deterministic SHA-256 over every bundle file except the quality report itself."""
    return subject_digest(Path(bundle))


def _serialize(report: dict) -> str:
    return json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def _load_verifier():
    from description_pipeline.verification.solidworks_urdf import check_bundle

    return check_bundle


def _gh(*args: str) -> str:
    result = subprocess.run(["gh", *args], check=True, capture_output=True, text=True, encoding="utf-8")
    return result.stdout


def _validate(bundle: Path) -> tuple[str, dict]:
    if not bundle.is_dir():
        raise PrError("bundle_missing", str(bundle))
    for name in REQUIRED_FILES:
        if not (bundle / name).is_file():
            raise PrError("bundle_incomplete", name)
    for name in REQUIRED_DIRS:
        if not (bundle / name).is_dir():
            raise PrError("bundle_incomplete", name)
    manifest = yaml.safe_load((bundle / "input/robot.yaml").read_text(encoding="utf-8"))
    if not isinstance(manifest, dict) or not manifest.get("hardware"):
        raise PrError("robot_yaml_invalid", "hardware is required")
    hardware = _hardware(str(manifest["hardware"]))
    return hardware, manifest


def _check(bundle: Path) -> tuple[str, dict]:
    subject = subject_hash(bundle)
    report = _load_verifier()(bundle)
    if not isinstance(report, dict) or report.get("passed") is not True:
        raise PrError("verification_failed", report if isinstance(report, dict) else None)
    reported = report.get("subject_sha256") or report.get("subject")
    if reported != subject:
        raise PrError("verification_subject_mismatch", {"expected": subject, "reported": reported})
    quality = bundle / "reports/quality.json"
    if not quality.is_file():
        raise PrError("missing_quality_report", "reports/quality.json is required")
    if quality.read_text(encoding="utf-8") != _serialize(report):
        raise PrError("stale_quality_report", "on-disk reports/quality.json differs from the recomputed report")
    return subject, report


def _remote_head(repository: Path, branch: str) -> str:
    out = git(repository, "ls-remote", "origin", f"refs/heads/{branch}").stdout.strip()
    return out.split()[0] if out else ""


def _stage_and_push(repository: Path, bundle: Path, base: str, branch: str, subject: str, message: str) -> str:
    base_sha = git(repository, "rev-parse", f"origin/{base}").stdout.strip()
    expected = _remote_head(repository, branch)
    staging = Path(tempfile.mkdtemp(prefix="urdf-pr-"))
    worktree = staging / "worktree"
    try:
        git(repository, "worktree", "add", "--detach", str(worktree), base_sha)
        # Replace only the governed delivery paths; inherited config/sources/docs stay untouched.
        for name in OWNED_PATHS:
            target = worktree / name
            if target.is_dir():
                shutil.rmtree(target)
            elif target.exists():
                target.unlink()
        for path in sorted(p for p in bundle.rglob("*") if p.is_file()):
            target = worktree / path.relative_to(bundle)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)
        if subject_digest(worktree) != subject:
            raise PrError("staged_subject_mismatch", "delivered bytes differ from the verified bundle")
        git(worktree, "add", "--all")
        git(worktree, "commit", "-m", message)
        commit = git(worktree, "rev-parse", "HEAD").stdout.strip()
        if expected:
            git(worktree, "push", f"--force-with-lease=refs/heads/{branch}:{expected}", "origin", f"HEAD:refs/heads/{branch}")
        else:
            git(worktree, "push", "origin", f"HEAD:refs/heads/{branch}")
        if _remote_head(repository, branch) != commit:
            raise PrError("remote_head_mismatch", {"expected": commit})
        return commit
    finally:
        subprocess.run(["git", "-C", str(repository), "worktree", "remove", "--force", str(worktree)],
                       capture_output=True, text=True)
        shutil.rmtree(staging, ignore_errors=True)


def _pr(repository: Path, base: str, branch: str, subject: str, commit: str,
        report: dict, message: str | None) -> tuple[str, str]:
    existing = json.loads(_gh("pr", "list", "--head", branch, "--state", "open", "--json", "url,number") or "[]")
    body = (f"Automatic SolidWorks-to-URDF publication.\n\n- bundle subject: `{subject}`\n"
            f"- commit: `{commit}`\n- verifier: passed (recomputed)\n")
    if existing:
        return "reused", existing[0]["url"]
    title = message.splitlines()[0] if message else "SolidWorks-to-URDF bundle"
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as handle:
        handle.write(body)
        body_path = handle.name
    try:
        url = _gh("pr", "create", "--base", base, "--head", branch, "--title", title,
                  "--body-file", body_path).strip()
    finally:
        Path(body_path).unlink(missing_ok=True)
    return "published", url


def submit_bundle(bundle: Path, repository: Path, *, base: str, branch: str,
                  message: str | None = None, dry_run: bool = False) -> dict:
    """Validate, verify and publish one bundle; returns state/commit/url/branch/subject or a receipt."""
    bundle = Path(bundle).resolve()
    repository = Path(repository).resolve()
    try:
        hardware, _ = _validate(bundle)
        if branch != REVIEW_BRANCH.format(hardware=hardware):
            raise PrError("branch_not_deterministic", {"expected": REVIEW_BRANCH.format(hardware=hardware)})
        if git(repository, "status", "--porcelain").stdout.strip():
            raise PrError("dirty_repository", "commit, stash or remove unrelated changes first")
        subject, report = _check(bundle)
        git(repository, "fetch", "origin", base, check=True)
        if dry_run:
            return {"state": "dry_run", "branch": branch, "base": base, "subject": subject,
                    "commit": "", "url": ""}
        commit = _stage_and_push(repository, bundle, base, branch, subject,
                                 message or f"feat({hardware}): publish SolidWorks-to-URDF bundle")
        state, url = _pr(repository, base, branch, subject, commit, report, message)
        return {"state": state, "branch": branch, "base": base, "subject": subject,
                "commit": commit, "url": url}
    except PrError as error:
        return _fail(error.code, error.detail)
    except subprocess.CalledProcessError as error:
        return _fail("git_or_gh_failed", {"command": error.cmd, "stderr": (error.stderr or "")[-400:]})
    except Exception as error:  # noqa: BLE001 - receipts must never crash the caller
        return _fail(type(error).__name__, str(error)[:400])
