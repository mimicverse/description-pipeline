"""Publish a verified URDF delivery bundle as one fast-forward review branch and PR.

Public API: ``submit_bundle(bundle, repository, *, base, branch, message=None, dry_run=False)``.

Guarantees:

* only delivery paths and generated Git byte-preservation metadata are staged;
* the bundle subject digest is re-bound to the staged bytes *and* the committed tree before any push;
* a stale or tampered ``reports/quality.json`` can never escape, even if it says ``passed``;
* the review branch is only ever fast-forwarded on top of its own remote head (no force pushes,
  no base rewrites, foreign branches are refused);
* existing PRs are updated with the latest metadata instead of being duplicated;
* a GitHub failure after a successful push is reported with the pushed commit preserved.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from ..delivery import check_layout, read_delivery_manifest, read_source_record, subject_digest
from ..interface_records import REQUEST_PURPOSES, validate_source_record
from ..io import PipelineError, acquire_process_lock, confined, file_digest
from ..request_evidence import read_request_evidence
from ..stages import record_check

#: The target delivery tree (contracts delivery format and digest): manifest.json is the
#: subject manifest, tool.json carries the tool identity, reports/ holds only the
#: quality report; all other files travel inside their subject directories.
DELIVERY_PATHS = ("manifest.json", "input", "evidence", "model", "urdf", "meshes", "tool.json", "reports")
GOVERNED_PATHS = (*DELIVERY_PATHS, ".gitattributes")
BYTE_ATTRIBUTES = ("text", "filter", "working-tree-encoding")
_DIRECTORY_PATHS = {"input", "evidence", "model", "urdf", "meshes", "reports"}
GIT_ATTRIBUTES = (
    "# Preserve verified delivery bytes in Git and on checkout.\n"
    + "".join(
        f"/{name}{'/**' if name in _DIRECTORY_PATHS else ''} -text -filter -working-tree-encoding\n"
        for name in GOVERNED_PATHS
    )
).encode("ascii")
#: Reports copied into a publication branch: only the deterministic quality
#: report stays outside the subject; every other receipt lives in platform run
#: storage (contracts delivery format and digest).
REPORTS_FILES = ("quality.json",)
REQUIRED_FILES = (
    "manifest.json",
    "input/handoff.json",
    "input/request.json",
    "input/source.json",
    "model/robot.json",
    "urdf/robot.urdf",
    "tool.json",
    "reports/quality.json",
)
REQUIRED_DIRS = ("input", "evidence", "model", "urdf", "meshes")
REVIEW_BRANCH = "work/solidworks/{hardware}/{purpose}"
PUBLISHER_MARKER = "Urdf-Publisher: description-pipeline"
log = logging.getLogger(__name__)

#: Bound on the best-effort fetch that resolves a previous published revision.
PUBLISHED_REVISION_FETCH_TIMEOUT_SECONDS = 60


class PrError(RuntimeError):
    def __init__(self, code: str, detail: object | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


def _git(repository: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repository), *args], check=check, capture_output=True, text=True, encoding="utf-8"
    )


def _slug_hardware(value: str) -> str:
    slug = re.sub(r"[^a-z0-9_.-]", "-", value.strip().lower())
    if not slug or slug.startswith("-"):
        raise PrError("hardware_id_invalid", value)
    return slug


def review_branch(hardware: str, purpose: str) -> str:
    """The one managed review branch of a (repository, hardware, purpose) candidate."""

    if purpose not in REQUEST_PURPOSES:
        raise PrError("purpose_not_supported", {"hardware": hardware, "purpose": purpose})
    return REVIEW_BRANCH.format(hardware=_slug_hardware(hardware), purpose=purpose)


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


def _remote_ref(repository: Path, ref: str) -> str:
    """One remote ref exactly as the remote currently answers (empty when absent)."""

    listing = _git(repository, "ls-remote", "origin", ref).stdout.strip()
    return listing.split()[0] if listing else ""


def _observe_remote_ref(repository: Path, ref: str) -> str | None:
    """One remote ref, or None when the observation itself is unavailable."""

    try:
        return _remote_ref(repository, ref)
    except subprocess.CalledProcessError:
        return None


def _unresolved(code: str, detail, *, commit: str, branch: str, subject: str, base: str, url: str = "") -> dict:
    """A side-effect-unknown or baseline-changed receipt that blocks on reconciliation."""

    return {
        "state": "unresolved",
        "error": code,
        "detail": detail,
        "commit": commit,
        "branch": branch,
        "subject": subject,
        "base": base,
        "url": url,
        "retry": {
            "command": f"git ls-remote origin refs/heads/{base} refs/heads/{branch}",
            "note": (
                "re-read both remote refs; nothing was written"
                if not commit
                else "reconcile the retained candidate write against the remote state"
            ),
        },
    }


def _serialize(report: dict) -> str:
    return json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def _load_verifier():
    from description_pipeline.verification.solidworks_urdf import check_bundle

    return check_bundle


def _gh(repository: Path, *args: str) -> str:
    result = subprocess.run(["gh", *args], cwd=repository, check=True, capture_output=True, text=True, encoding="utf-8")
    return result.stdout


def _identity_of(source: dict) -> dict:
    """Publication identity adapter over the frozen ``input/source.json`` record."""

    return {
        "hardware_id": source["selection"]["hardware_id"],
        "revision": source["metadata"]["revision"],
        "source_revision_id": source["source_revision_id"],
    }


def _check_successor(previous: dict, current: dict) -> None:
    """Platform successor semantics for the review branch.

    The CAD label is raw engineering metadata, not a content identity, so a new
    content identity under the same label is allowed; lost-update protection is
    the request's fixed base/head, the published subject chain and CAS/ff.  This
    guard only refuses a changed hardware target.
    """

    if previous["hardware_id"] != current["hardware_id"]:
        raise PrError("cad_revision_conflict", "CAD hardware identity changed across the review branch")


def _bundle_purpose(bundle: Path) -> str:
    """The frozen request's submission purpose: the one read-only identity source."""

    try:
        record, _sha = read_request_evidence(bundle / "input" / "request.json")
    except PipelineError as error:
        raise PrError("bundle_records_invalid", str(error)) from error
    purpose = str(record.get("purpose") or "")
    if purpose not in REQUEST_PURPOSES:
        raise PrError("bundle_records_invalid", {"purpose": purpose})
    return purpose


def _validate(bundle: Path) -> tuple[str, str, dict, dict]:
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
    try:
        check_layout(bundle)
    except PipelineError as error:
        raise PrError("bundle_layout", str(error)) from error
    try:
        manifest = read_delivery_manifest(bundle)
        source = read_source_record(bundle)
    except PipelineError as error:
        raise PrError("bundle_records_invalid", str(error)) from error
    purpose = _bundle_purpose(bundle)
    hardware = _slug_hardware(str(source["selection"]["hardware_id"]))
    current = _identity_of(source)
    # The CAD/PDM revision label is optional engineering metadata: a missing
    # value stays missing and is never a publication rejection reason.
    return hardware, purpose, manifest, current


def _previous_revision(worktree: Path, hardware: str) -> dict | None:
    if not (worktree / "input/source.json").is_file():
        return None
    try:
        source = read_source_record(worktree)
    except PipelineError as error:
        raise PrError("previous_source_invalid", str(error)) from error
    # Content identity exists even when the CAD revision label is absent; the
    # published subject chain is the platform-level successor evidence.
    return _identity_of(source)


def published_revision(repository: Path, hardware: str, purpose: str) -> dict | None:
    """Read the published input/source.json of one hardware review branch (read-only).

    Best-effort identity resolution for the submission lane: it fetches the
    deterministic review branch and reads the identity from the fetched commit
    (``FETCH_HEAD``, so a local refspec never changes the baseline), never
    creating a worktree.  Any unavailability returns ``None`` so the caller
    keeps ``parent_revision`` absent; publication's own guard remains the
    deciding gate.
    """

    branch = review_branch(hardware, purpose)
    _validate_ref(repository, branch, branch=True)
    try:
        fetched = subprocess.run(
            ["git", "-C", str(repository), "fetch", "--quiet", "origin", branch],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=PUBLISHED_REVISION_FETCH_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        raise PrError("published_revision_unknown", f"fetch timed out for {branch}") from None
    if fetched.returncode != 0:
        stderr = (fetched.stderr or "").strip()
        if "couldn't find remote ref" in stderr:
            # The remote answered: this branch provably does not exist yet.
            return None
        raise PrError("published_revision_unknown", stderr[-200:] or "fetch failed")
    show = _git(repository, "show", "FETCH_HEAD:input/source.json", check=False)
    if show.returncode != 0:
        stderr = (show.stderr or "").strip()
        if "does not exist" in stderr or "but not in" in stderr:
            # The branch was fetched: the file is provably absent there.
            return None
        raise PrError("published_revision_unknown", stderr[-200:] or "source record unreadable")
    if not show.stdout.strip():
        raise PrError("published_revision_unknown", "empty source record")
    try:
        payload = json.loads(show.stdout)
    except json.JSONDecodeError:
        raise PrError("published_revision_unknown", "source record is not JSON") from None
    try:
        payload = validate_source_record(payload)
    except PipelineError as error:
        raise PrError("published_revision_unknown", str(error)) from error
    identity = _identity_of(payload)
    if identity["hardware_id"] != hardware:
        raise PrError("published_revision_mismatch", {"expected": hardware, "observed": identity["hardware_id"]})
    return identity


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
        raise PrError("verification_subject_mismatch", {"expected": subject, "reported": report.get("subject_sha256")})
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


def _verify_committed(repository: Path, commit: str, subject: str) -> None:
    """Verify Git blob bytes, including changes made by clean filters."""
    tree = subprocess.run(
        ["git", "-C", str(repository), "ls-tree", "-rz", commit, "--", *GOVERNED_PATHS], check=True, capture_output=True
    ).stdout
    with tempfile.TemporaryDirectory(prefix="urdf-committed-") as directory:
        root = Path(directory)
        for entry in tree.split(b"\0"):
            if not entry:
                continue
            metadata, encoded_path = entry.split(b"\t", 1)
            mode, kind, oid = metadata.split()
            if mode not in {b"100644", b"100755"} or kind != b"blob":
                raise PrError("committed_nonregular_file", encoded_path.decode("utf-8"))
            path = confined(root, encoded_path.decode("utf-8"), exists=False)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("wb") as stream:
                subprocess.run(
                    ["git", "-C", str(repository), "cat-file", "blob", oid.decode("ascii")],
                    check=True,
                    stdout=stream,
                    stderr=subprocess.PIPE,
                )
        attributes = root / ".gitattributes"
        if not attributes.is_file() or attributes.read_bytes() != GIT_ATTRIBUTES:
            raise PrError("committed_git_metadata_mismatch", ".gitattributes")
        _reverify(root, subject)


def _lock(repository: Path):
    git_dir = Path(_git(repository, "rev-parse", "--absolute-git-dir").stdout.strip())
    lock = git_dir / "urdf-pr.lock"
    try:
        handle = acquire_process_lock(lock)
    except (OSError, PipelineError) as error:
        raise PrError("repository_locked", str(lock)) from error
    return lock, handle


def _copy_governed(bundle: Path, worktree: Path) -> None:
    reports = worktree / "reports"
    if reports.is_symlink() or reports.is_file():
        reports.unlink()
    elif reports.is_dir():
        shutil.rmtree(reports)
    for name in DELIVERY_PATHS:
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


def _preserve_git_bytes(worktree: Path) -> None:
    """Publish one canonical policy and refuse effective overriding attributes."""
    attributes = worktree / ".gitattributes"
    if attributes.is_symlink() or attributes.is_junction() or attributes.is_dir():
        raise PrError("git_metadata_nonregular", ".gitattributes")
    attributes.write_bytes(GIT_ATTRIBUTES)
    files = sorted(
        path.relative_to(worktree).as_posix()
        for name in GOVERNED_PATHS
        for path in ((worktree / name).rglob("*") if (worktree / name).is_dir() else [worktree / name])
        if path.is_file()
    )
    result = subprocess.run(
        ["git", "-C", str(worktree), "check-attr", "-z", "--stdin", *BYTE_ATTRIBUTES],
        input=b"\0".join(name.encode("utf-8") for name in files) + b"\0",
        check=True,
        capture_output=True,
    )
    fields = result.stdout.split(b"\0")[:-1]
    if len(fields) != len(files) * len(BYTE_ATTRIBUTES) * 3:
        raise PrError("git_attributes_unreadable", "Incomplete Git attribute inventory")
    for offset in range(0, len(fields), 3):
        path, attribute, value = (item.decode("utf-8") for item in fields[offset : offset + 3])
        if value != "unset":
            raise PrError("git_attributes_conflict", {"file": path, "attribute": attribute, "value": value})


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
    _preserve_git_bytes(worktree)
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
    diff = _git(worktree, "diff", "--name-only", "-z", "HEAD^", "HEAD").stdout.split("\0")
    for path in diff:
        if path and not any(path == name or path.startswith(name + "/") for name in GOVERNED_PATHS):
            raise PrError("commit_touched_ungoverned", path)
    return commit, False


def _body(subject: str, commit: str, report: dict, purpose: str) -> str:
    checks = report.get("checks") or []
    # Every (fact_id, scope, object_id) identity prints independently in a stable
    # order (object_id, then fact_id): a two-joint fact never collapses to one
    # line, so flipping the report order cannot flip a conclusion.
    qualifications = sorted(
        (
            str(item.get("object_id") or ""),
            str(item.get("fact_id") or ""),
            str(item.get("status") or ""),
        )
        for item in (report.get("qualifications") or [])
        if isinstance(item, dict)
    )
    qualification_lines = "".join(
        f"- {fact_id}{f'[{object_id}]' if object_id else ''}: {status}"
        f"{' (unqualified)' if status == 'unqualified' else ''}\n"
        for object_id, fact_id, status in qualifications
    )
    provenance = next(
        (
            check.get("details")
            for check in checks
            if check.get("id") == "provenance.value_warnings" and check.get("state") == "passed"
        ),
        {},
    )
    provenance_line = (
        f"- value provenance: {provenance.get('objects')} non-native field/object warnings; "
        f"codes: {', '.join(provenance.get('codes') or [])}\n"
        if isinstance(provenance, dict) and provenance.get("codes")
        else ""
    )
    return (
        f"Automatic SolidWorks-to-URDF publication.\n\n"
        f"- subject: `{subject}`\n- verified commit: `{commit}`\n"
        f"- purpose: `{purpose}`\n"
        f"- verifier: recomputed, passed ({len(checks)} checks)\n"
        f"{provenance_line}"
        f"{qualification_lines}"
    )


def _pr(
    repository: Path,
    slug: str,
    base: str,
    branch: str,
    subject: str,
    commit: str,
    report: dict,
    purpose: str,
    message: str | None,
) -> tuple[str, str]:
    listing = json.loads(
        _gh(
            repository,
            "pr",
            "list",
            "--repo",
            slug,
            "--base",
            base,
            "--head",
            branch,
            "--state",
            "open",
            "--json",
            "url,number,baseRefName,headRefName",
        )
        or "[]"
    )
    matches = [item for item in listing if item.get("baseRefName") == base and item.get("headRefName") == branch]
    if len(matches) > 1:
        raise PrError("pr_ambiguous", [item["url"] for item in matches])
    title = message.splitlines()[0] if message else f"SolidWorks-to-URDF bundle ({commit[:12]})"
    body = _body(subject, commit, report, purpose)
    with tempfile.NamedTemporaryFile(
        "w", suffix=".json" if matches else ".md", delete=False, encoding="utf-8"
    ) as handle:
        handle.write(_serialize({"title": title, "body": body}) if matches else body)
        body_path = handle.name
    url = ""
    try:
        if matches:
            url = matches[0]["url"]
            _gh(
                repository,
                "api",
                "--method",
                "PATCH",
                f"repos/{slug}/pulls/{matches[0]['number']}",
                "--input",
                body_path,
            )
            _verify_pr(repository, slug, url, base, branch, commit)
            return "updated", url
        url = _gh(
            repository,
            "pr",
            "create",
            "--repo",
            slug,
            "--base",
            base,
            "--head",
            branch,
            "--title",
            title,
            "--body-file",
            body_path,
        ).strip()
        _verify_pr(repository, slug, url, base, branch, commit)
        return "published", url
    except Exception as error:
        error.pr_url = url
        raise
    finally:
        Path(body_path).unlink(missing_ok=True)


def _verify_pr(repository, slug, url, base, branch, commit):
    if re.fullmatch(r"https://github\.com/" + re.escape(slug) + r"/pull/[1-9][0-9]*", url) is None:
        raise PrError("pr_identity_mismatch", "GitHub returned another repository PR URL")
    observed = None
    for attempt in range(4):
        observed = json.loads(
            _gh(
                repository,
                "pr",
                "view",
                url,
                "--repo",
                slug,
                "--json",
                "url,state,baseRefName,headRefName,headRefOid,isCrossRepository",
            )
        )
        if (
            observed.get("url") == url
            and observed.get("state") == "OPEN"
            and observed.get("baseRefName") == base
            and observed.get("headRefName") == branch
            and observed.get("headRefOid") == commit
            and observed.get("isCrossRepository") is False
        ):
            return
        if attempt < 3:
            time.sleep(2**attempt)
    raise PrError("pr_identity_mismatch", {"expected_commit": commit, "observed": observed})


def submit_bundle(
    bundle: Path,
    repository: Path,
    *,
    base: str,
    branch: str,
    message: str | None = None,
    dry_run: bool = False,
    on_event=None,
) -> dict:
    """Validate, verify and publish one bundle through a fast-forward review branch and PR."""
    bundle = Path(bundle).resolve()
    repository = Path(repository).resolve()
    lock_info = None
    staging = None
    pushed = ""
    subject = ""
    slug = ""
    phase = "publication.inputs"

    def observed(identifier, state, details):
        record_check(
            on_event, "publish", "input" if identifier == "publication.inputs" else "output", identifier, state, details
        )

    try:
        hardware, purpose, _manifest, current_revision = _validate(bundle)
        expected = review_branch(hardware, purpose)
        if branch != expected:
            raise PrError("branch_not_deterministic", {"expected": expected})
        _validate_ref(repository, base, branch=False)
        _validate_ref(repository, branch, branch=True)
        slug = _origin_slug(repository)
        lock_info = _lock(repository)
        if _git(repository, "status", "--porcelain").stdout.strip():
            raise PrError("dirty_repository", "commit, stash or remove unrelated changes first")
        subject, report = _verify(bundle)
        observed(
            "publication.inputs",
            "passed",
            {"subject_sha256": subject, "base": base, "branch": branch, "repository_slug": slug},
        )
        phase = "publication.git"
        _git(repository, "fetch", "--quiet", "origin", base)
        base_sha = _git(repository, "rev-parse", "FETCH_HEAD").stdout.strip()
        head_sha, head_message = _remote_branch(repository, branch)
        if head_sha and PUBLISHER_MARKER not in head_message:
            raise PrError("branch_foreign", {"branch": branch, "head": head_sha})
        if dry_run:
            return {
                "state": "dry_run",
                "branch": branch,
                "base": base,
                "subject": subject,
                "commit": head_sha,
                "url": "",
            }
        staging = Path(tempfile.mkdtemp(prefix="urdf-pr-"))
        worktree = staging / "worktree"
        try:
            _prepare_worktree(repository, worktree, base_sha, head_sha)
            previous = _previous_revision(worktree, hardware)
            if previous is not None:
                _check_successor(previous, current_revision)
            commit, noop = _stage_commit(
                bundle, worktree, subject, message or f"feat({hardware}): publish SolidWorks-to-URDF bundle"
            )
            _reverify(worktree, subject)
            _verify_committed(worktree, commit, subject)
            observed(
                "publication.git",
                "passed",
                {
                    "subject_sha256": subject,
                    "commit": commit,
                    "copied_staged_committed": "reverified",
                    "git_attributes_sha256": file_digest(worktree / ".gitattributes"),
                    # Published subject chain evidence: the previous content
                    # identity (CAD label optional, kept raw) and the new one.
                    **({} if previous is None else {"previous_source_revision_id": previous["source_revision_id"]}),
                    "source_revision_id": current_revision["source_revision_id"],
                },
            )
            phase = "publication.receipt"
            # IF-12: re-check the baseline immediately before the write.  A base
            # that moved since planning refuses this stale merge without any
            # write; the receipt is unresolved for reconciliation.
            observed_base = _remote_ref(repository, f"refs/heads/{base}")
            if observed_base != base_sha:
                observed(
                    "publication.receipt",
                    "failed",
                    {"error": "base_moved_before_write", "planned_base": base_sha, "observed_base": observed_base},
                )
                return _unresolved(
                    "base_moved_before_write",
                    {"planned_base": base_sha, "observed_base": observed_base},
                    commit="",
                    branch=branch,
                    subject=subject,
                    base=base,
                )
            if not noop or commit != head_sha:
                # Conditional write: the candidate branch may only move from the
                # exact head this plan read (an empty expectation means the
                # branch must still not exist).  A rewound or re-created head
                # must never be overwritten.
                try:
                    _git(
                        worktree,
                        "push",
                        f"--force-with-lease=refs/heads/{branch}:{head_sha}",
                        "origin",
                        f"HEAD:refs/heads/{branch}",
                    )
                except subprocess.CalledProcessError as error:
                    # The response was lost.  Compare the actual remote head
                    # against the candidate fixed before the write first: a
                    # confirmed candidate write is retained for reconciliation,
                    # and an unobservable side effect stays unresolved - the
                    # base alone may never assert "nothing was written".
                    observed_head = _observe_remote_ref(repository, f"refs/heads/{branch}")
                    observed_base = _observe_remote_ref(repository, f"refs/heads/{base}")
                    detail = {
                        "candidate": commit,
                        "expected_head": head_sha,
                        "observed_head": observed_head,
                        "planned_base": base_sha,
                        "observed_base": observed_base,
                        "stderr": (error.stderr or "")[-300:],
                    }
                    if observed_head == commit:
                        observed("publication.receipt", "failed", {"error": "push_response_lost", **detail})
                        return _unresolved(
                            "push_response_lost",
                            detail,
                            commit=commit,
                            branch=branch,
                            subject=subject,
                            base=base,
                        )
                    if observed_head is None or observed_base is None:
                        observed("publication.receipt", "failed", {"error": "push_unverified", **detail})
                        return _unresolved(
                            "push_unverified",
                            detail,
                            commit=commit,
                            branch=branch,
                            subject=subject,
                            base=base,
                        )
                    if observed_base != base_sha:
                        observed("publication.receipt", "failed", {"error": "base_moved_before_write", **detail})
                        return _unresolved(
                            "base_moved_before_write",
                            detail,
                            commit="",
                            branch=branch,
                            subject=subject,
                            base=base,
                        )
                    if observed_head != head_sha:
                        observed("publication.receipt", "failed", {"error": "candidate_head_changed", **detail})
                        return _unresolved(
                            "candidate_head_changed",
                            detail,
                            commit="",
                            branch=branch,
                            subject=subject,
                            base=base,
                        )
                    raise
            pushed = commit
        finally:
            _git(repository, "worktree", "remove", "--force", str(worktree), check=False)
            _git(repository, "worktree", "prune", check=False)
            shutil.rmtree(staging, ignore_errors=True)
        if _remote_ref(repository, f"refs/heads/{branch}") != pushed:
            raise PrError("remote_head_mismatch", {"expected": pushed})
        state, url = _pr(repository, slug, base, branch, subject, pushed, report, purpose, message)
        # Final confirmation: the baseline must still be the one this candidate
        # was planned and merged against.  When it moved after the candidate
        # write, the write is retained and the receipt stays unresolved for
        # reconciliation - never a success.
        confirmed_base = _remote_ref(repository, f"refs/heads/{base}")
        if confirmed_base != base_sha:
            observed(
                "publication.receipt",
                "failed",
                {"error": "base_moved_after_push", "planned_base": base_sha, "observed_base": confirmed_base},
            )
            return _unresolved(
                "base_moved_after_push",
                {"planned_base": base_sha, "observed_base": confirmed_base, "commit": pushed},
                commit=pushed,
                branch=branch,
                subject=subject,
                base=base,
                url=url,
            )
        content_noop = noop and pushed == head_sha
        result = {
            "state": "noop" if content_noop else state,
            "branch": branch,
            "base": base,
            "subject": subject,
            "commit": pushed,
            "url": url,
            "repository_slug": slug,
            "purpose": purpose,
        }
        observed("publication.receipt", "passed", {**result, "subject_sha256": subject})
        return result
    except PrError as error:
        observed(phase, "failed", {"error": error.code, "detail": error.detail})
        return {
            "state": "failed",
            "error": error.code,
            "detail": error.detail,
            "commit": pushed,
            "branch": branch,
            "subject": subject,
            "url": getattr(error, "pr_url", ""),
        }
    except subprocess.CalledProcessError as error:
        observed(
            phase,
            "failed",
            {"error": "github_failed" if pushed else "git_failed", "detail": (error.stderr or "")[-300:]},
        )
        if pushed:
            return {
                "state": "gh_failed_after_push",
                "error": "github_failed",
                "commit": pushed,
                "branch": branch,
                "subject": subject,
                "base": base,
                "retry": {"command": "gh pr list --head " + branch, "stderr": (error.stderr or "")[-300:]},
                "url": getattr(error, "pr_url", ""),
            }
        return {
            "state": "failed",
            "error": "git_failed",
            "detail": (error.stderr or "")[-300:],
            "commit": pushed,
            "branch": branch,
            "subject": subject,
        }
    except Exception as error:  # noqa: BLE001 - receipts must never crash the caller
        observed(phase, "failed", {"error": type(error).__name__, "detail": str(error)[:300]})
        return {
            "state": "failed",
            "error": type(error).__name__,
            "detail": str(error)[:300],
            "commit": pushed,
            "branch": branch,
            "subject": subject,
        }
    finally:
        if lock_info is not None:
            _, handle = lock_info
            os.close(handle)
