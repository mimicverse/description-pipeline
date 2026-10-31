"""Deterministic Git candidate construction and pull-request leaves for Publish."""

from __future__ import annotations

import json
import hashlib
import os
import re
import shutil
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import unquote, urlsplit

from ..artifacts.store import path_has_link_ancestor
from ..delivery import subject_digest, SUBJECT_DIRECTORIES, PROJECTION_DIRECTORIES, SUBJECT_FILES
from ..io import PipelineError, digest, inventory, read_data, safe_process_output, write_json
from ..schemas import REPOSITORY_SNAPSHOT_SCHEMA
from .review_branch import is_review_branch

GOVERNED_PATHS = (
    "manifest.json",
    "input",
    "evidence",
    "model",
    "urdf",
    "meshes",
    "mjcf",
    "tool.json",
    "reports",
    ".gitattributes",
)
GIT_ATTRIBUTES = (
    "# Preserve published delivery bytes and verification receipts.\n"
    + "".join(
        f"/{name}{'/**' if name in {*SUBJECT_DIRECTORIES, *PROJECTION_DIRECTORIES, 'reports'} else ''} "
        "-text -filter -working-tree-encoding\n"
        for name in GOVERNED_PATHS
    )
).encode("ascii")
_OID = re.compile(r"\A[0-9a-f]{40}\Z")
_HEX64 = re.compile(r"\A[0-9a-f]{64}\Z")
_CONTROL_OR_SPACE = re.compile(r"[\x00-\x20\x7f]")
_SCP_REMOTE = re.compile(r"\A(?:[^/@:]+@)?([^/:]+):(.+)\Z")
REPOSITORY_SNAPSHOT_FILE = "repository.bundle"
REPOSITORY_SNAPSHOT_METADATA = "snapshot.json"
PROTECTED_PUBLICATION_BRANCHES = frozenset({"main", "refs/heads/main"})
GITHUB_MAX_BLOB_BYTES = 100 * 1024**2
GITHUB_LARGE_FILE_DOCUMENTATION = (
    "https://docs.github.com/en/repositories/working-with-files/"
    "managing-large-files/about-large-files-on-github"
)


class PublicationError(PipelineError):
    def __init__(self, code: str, detail: object | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail



def validate_publication_target(*, base_ref: str, branch_ref: str) -> None:
    """Require one non-base update to the existing deterministic PR branch."""

    reason = None
    if branch_ref == base_ref:
        reason = "same-as-base"
    elif branch_ref in PROTECTED_PUBLICATION_BRANCHES:
        reason = "protected-main"
    elif not is_review_branch(branch_ref):
        reason = "not-review-branch"
    if reason is not None:
        raise PublicationError(
            "publish.target_forbidden",
            {"base_ref": base_ref, "branch_ref": branch_ref, "reason": reason},
        )


def _repository_target_from_remote(remote: str) -> tuple[str, str]:
    """Return the GitHub host and ``owner/repository`` without credentials."""

    if not isinstance(remote, str) or not remote or _CONTROL_OR_SPACE.search(remote):
        raise PublicationError("publish.remote_invalid")
    host = None
    path = None
    if "://" in remote:
        try:
            parsed = urlsplit(remote)
            _ = parsed.port
        except ValueError as error:
            raise PublicationError("publish.remote_invalid") from error
        scheme = parsed.scheme.casefold()
        if scheme not in {"https", "ssh"} or parsed.query or parsed.fragment:
            raise PublicationError("publish.remote_invalid")
        if parsed.password is not None or (
            scheme == "https" and parsed.username is not None
        ):
            raise PublicationError("publish.remote_credentials")
        if not parsed.hostname:
            raise PublicationError("publish.remote_invalid")
        host = parsed.hostname.casefold()
        path = unquote(parsed.path)
    else:
        scp = _SCP_REMOTE.fullmatch(remote)
        if scp is None:
            raise PublicationError("publish.remote_invalid")
        host = scp.group(1).casefold()
        path = scp.group(2)
    relative = path.strip("/")
    if relative.casefold().endswith(".git"):
        relative = relative[:-4]
    parts = relative.split("/")
    if len(parts) != 2 or any(not item or item in {".", ".."} for item in parts):
        raise PublicationError("publish.remote_invalid")
    return host, "/".join(parts)


def validate_repository_remote(
    repository_slug: str,
    fetch_url: str,
    push_url: str,
) -> str:
    """Require fetch, push and configured slug to name one exact Git repository."""

    if (
        not isinstance(repository_slug, str)
        or repository_slug.count("/") != 1
        or _CONTROL_OR_SPACE.search(repository_slug)
    ):
        raise PublicationError("publish.remote_invalid", {"where": "repository_slug"})
    if fetch_url != push_url:
        raise PublicationError("publish.remote_target_mismatch")
    host, actual = _repository_target_from_remote(fetch_url)
    if host != "github.com":
        raise PublicationError(
            "publish.remote_target_mismatch",
            {"expected": "github.com", "actual": host},
        )
    if actual != repository_slug:
        raise PublicationError(
            "publish.remote_target_mismatch",
            {"expected": repository_slug, "actual": actual},
        )
    return fetch_url


def _remote_urls(repository: Path) -> tuple[str, str]:
    """Read one fetch and one push URL after rejecting Git URL rewrites."""

    rewrites = _run(
        repository,
        "config",
        "--get-regexp",
        r"^url\..*\.insteadof$",
        check=False,
    )
    if rewrites.returncode not in {0, 1}:
        raise PublicationError("publish.remote_invalid")
    if rewrites.returncode == 0 and rewrites.stdout.strip():
        raise PublicationError("publish.remote_rewrite")
    try:
        fetch_urls = [
            item
            for item in _text_run(
                repository,
                "remote",
                "get-url",
                "--all",
                "origin",
            ).splitlines()
            if item
        ]
        push_urls = [
            item
            for item in _text_run(
                repository,
                "remote",
                "get-url",
                "--push",
                "--all",
                "origin",
            ).splitlines()
            if item
        ]
    except subprocess.CalledProcessError as error:
        raise PublicationError("publish.remote_invalid") from error
    if len(fetch_urls) != 1 or len(push_urls) != 1:
        raise PublicationError("publish.remote_target_mismatch")
    return fetch_urls[0], push_urls[0]


def validate_git_remote(repository: Path, repository_slug: str) -> str:
    """Validate the effective Git fetch and push targets for one repository."""

    fetch_url, push_url = _remote_urls(repository)
    return validate_repository_remote(repository_slug, fetch_url, push_url)


def _run(repository: Path, *args: str, check: bool = True, env=None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repository), *args],
        check=check,
        capture_output=True,
        text=False,
        env=env,
    )


def _text_run(repository: Path, *args: str, check: bool = True) -> str:
    return _run(repository, *args, check=check).stdout.decode("utf-8").strip()


def validate_ref(repository: Path, ref: str) -> None:
    if not isinstance(ref, str) or not ref or ref.startswith("-") or ".." in ref or ref.endswith(".lock"):
        raise PublicationError("publish.invalid_ref", {"ref": ref})
    if _run(repository, "check-ref-format", f"refs/heads/{ref}", check=False).returncode != 0:
        raise PublicationError("publish.invalid_ref", {"ref": ref})


@dataclass(frozen=True, slots=True)
class RepositorySnapshot:
    repository: str
    origin_url: str
    base_ref: str
    base_oid: str
    branch_ref: str
    head_oid: str | None
    bundle_sha256: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.repository, str)
            or "/" not in self.repository
            or self.repository.startswith("/")
            or self.repository.endswith("/")
        ):
            raise PublicationError("publish.snapshot_repository")
        if not isinstance(self.origin_url, str) or not self.origin_url or "\x00" in self.origin_url:
            raise PublicationError("publish.snapshot_origin")
        for name in ("base_ref", "branch_ref"):
            value = getattr(self, name)
            if (
                not isinstance(value, str)
                or not value
                or value.startswith("-")
                or ".." in value
                or value.endswith(".lock")
            ):
                raise PublicationError("publish.snapshot_ref", {"field": name})
        if _OID.fullmatch(self.base_oid) is None:
            raise PublicationError("publish.snapshot_base")
        if self.head_oid is not None and _OID.fullmatch(self.head_oid) is None:
            raise PublicationError("publish.snapshot_head")
        if _HEX64.fullmatch(self.bundle_sha256) is None:
            raise PublicationError("publish.snapshot_bundle_sha256")

    def to_record(self) -> dict[str, Any]:
        return {
            "schema_version": REPOSITORY_SNAPSHOT_SCHEMA,
            "repository": self.repository,
            "origin_url": self.origin_url,
            "base_ref": self.base_ref,
            "base_oid": self.base_oid,
            "branch_ref": self.branch_ref,
            "head_oid": self.head_oid,
            "bundle_sha256": self.bundle_sha256,
        }

    @classmethod
    def from_record(cls, record: object) -> RepositorySnapshot:
        expected = {
            "schema_version",
            "repository",
            "origin_url",
            "base_ref",
            "base_oid",
            "branch_ref",
            "head_oid",
            "bundle_sha256",
        }
        if not isinstance(record, Mapping) or set(record) != expected:
            raise PublicationError("publish.snapshot_shape")
        if record["schema_version"] != REPOSITORY_SNAPSHOT_SCHEMA:
            raise PublicationError("publish.snapshot_schema")
        return cls(
            repository=record["repository"],
            origin_url=record["origin_url"],
            base_ref=record["base_ref"],
            base_oid=record["base_oid"],
            branch_ref=record["branch_ref"],
            head_oid=record["head_oid"],
            bundle_sha256=record["bundle_sha256"],
        )


def seal_repository_snapshot(
    repository: Path,
    destination: Path,
    *,
    repository_slug: str,
    origin_url: str,
    base_ref: str,
    base_oid: str,
    branch_ref: str,
    head_oid: str | None,
) -> RepositorySnapshot:
    """Persist every Git object required to rebuild a Publish checkout."""

    validate_publication_target(base_ref=base_ref, branch_ref=branch_ref)
    source = Path(repository).resolve()
    actual_origin, actual_push = _remote_urls(source)
    if actual_origin != origin_url or actual_push != origin_url:
        raise PublicationError("publish.origin_mismatch", {"expected": origin_url, "actual": actual_origin})
    validate_ref(source, base_ref)
    validate_ref(source, branch_ref)
    if _OID.fullmatch(base_oid) is None or (head_oid is not None and _OID.fullmatch(head_oid) is None):
        raise PublicationError("publish.snapshot_ref")
    destination = Path(destination)
    destination.mkdir(parents=True)
    bundle = destination / REPOSITORY_SNAPSHOT_FILE
    _run(source, "bundle", "create", str(bundle), "--all")
    snapshot = RepositorySnapshot(
        repository=repository_slug,
        origin_url=origin_url,
        base_ref=base_ref,
        base_oid=base_oid,
        branch_ref=branch_ref,
        head_oid=head_oid,
        bundle_sha256=hashlib.sha256(bundle.read_bytes()).hexdigest(),
    )
    write_json(
        destination / REPOSITORY_SNAPSHOT_METADATA,
        {
            "schema_version": REPOSITORY_SNAPSHOT_SCHEMA,
            "repository": snapshot.repository,
            "origin_url": snapshot.origin_url,
            "base_ref": snapshot.base_ref,
            "base_oid": snapshot.base_oid,
            "branch_ref": snapshot.branch_ref,
            "head_oid": snapshot.head_oid,
            "bundle_sha256": snapshot.bundle_sha256,
        },
    )
    return snapshot


def materialize_repository_snapshot(
    source: Path,
    destination: Path,
    *,
    expected_repository: str,
    expected_origin_url: str,
    expected_base_ref: str,
    expected_base_oid: str,
    expected_branch_ref: str,
    expected_head_oid: str | None,
) -> RepositorySnapshot:
    """Reconstruct a local checkout only from the sealed snapshot artifact."""

    validate_publication_target(base_ref=expected_base_ref, branch_ref=expected_branch_ref)
    source = Path(source)
    payload = read_data(source / REPOSITORY_SNAPSHOT_METADATA)
    expected_fields = {
        "schema_version",
        "repository",
        "origin_url",
        "base_ref",
        "base_oid",
        "branch_ref",
        "head_oid",
        "bundle_sha256",
    }
    if not isinstance(payload, dict) or set(payload) != expected_fields:
        raise PublicationError("publish.snapshot_shape")
    if payload["schema_version"] != REPOSITORY_SNAPSHOT_SCHEMA:
        raise PublicationError("publish.snapshot_schema")
    snapshot = RepositorySnapshot(**{key: payload[key] for key in expected_fields - {"schema_version"}})
    bundle = source / REPOSITORY_SNAPSHOT_FILE
    if hashlib.sha256(bundle.read_bytes()).hexdigest() != snapshot.bundle_sha256:
        raise PublicationError("publish.snapshot_bundle")
    expected = RepositorySnapshot(
        repository=expected_repository,
        origin_url=expected_origin_url,
        base_ref=expected_base_ref,
        base_oid=expected_base_oid,
        branch_ref=expected_branch_ref,
        head_oid=expected_head_oid,
        bundle_sha256=snapshot.bundle_sha256,
    )
    if snapshot != expected:
        raise PublicationError("publish.snapshot_binding")
    destination = Path(destination)
    if destination.exists():
        raise PublicationError("publish.snapshot_destination", {"path": str(destination)})
    _run(destination.parent, "clone", "--quiet", str(bundle), str(destination), check=True)
    _run(destination, "remote", "set-url", "origin", expected_origin_url)
    fetch_url, push_url = _remote_urls(destination)
    if fetch_url != expected_origin_url or push_url != expected_origin_url:
        raise PublicationError("publish.origin_mismatch")
    for oid in (expected.base_oid, expected.head_oid):
        if oid is not None and _run(destination, "cat-file", "-e", f"{oid}^{{commit}}", check=False).returncode != 0:
            raise PublicationError("publish.snapshot_object_missing", {"oid": oid})
    return snapshot


def clone_publication_repository(origin_url: str, destination: Path) -> PublicationRepository:
    """Create a fresh checkout for read-only reconciliation of one configured origin."""

    destination = Path(destination)
    if destination.exists():
        raise PublicationError("publish.reconciliation_destination", {"path": str(destination)})
    destination.parent.mkdir(parents=True, exist_ok=True)
    _run(destination.parent, "clone", "--quiet", origin_url, str(destination), check=True)
    fetch_url, push_url = _remote_urls(destination)
    if fetch_url != origin_url or push_url != origin_url:
        raise PublicationError("publish.origin_mismatch")
    return PublicationRepository(destination, expected_origin=origin_url)


@dataclass(frozen=True, slots=True)
class CommitSpec:
    author_name: str
    author_email: str
    authored_at: str
    committer_name: str
    committer_email: str
    committed_at: str
    subject: str
    body: str

    def __post_init__(self) -> None:
        for name in (
            "author_name",
            "author_email",
            "authored_at",
            "committer_name",
            "committer_email",
            "committed_at",
            "subject",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value or "\x00" in value:
                raise PublicationError("publish.commit_metadata", {"field": name})
        if not isinstance(self.body, str) or "\x00" in self.body:
            raise PublicationError("publish.commit_metadata", {"field": "body"})
        for name in ("authored_at", "committed_at"):
            value = getattr(self, name)
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError as error:
                raise PublicationError("publish.commit_metadata", {"field": name}) from error
            if parsed.tzinfo is None:
                raise PublicationError("publish.commit_metadata", {"field": name, "reason": "timezone"})

    @property
    def message(self) -> str:
        return self.subject + ("\n\n" + self.body.rstrip() if self.body.strip() else "") + "\n"


@dataclass(frozen=True, slots=True)
class PreparedCandidate:
    repository: Path
    worktree: Path
    staging_root: Path
    commit: str
    tree: str
    parents: tuple[str, ...]
    subject_sha256: str
    bundle: Path

    def cleanup(self) -> None:
        _run(self.repository, "worktree", "remove", "--force", str(self.worktree), check=False)
        _run(self.repository, "worktree", "prune", check=False)
        shutil.rmtree(self.staging_root, ignore_errors=True)


@dataclass(frozen=True, slots=True)
class PullRequestObservation:
    number: int
    url: str
    state: str
    base_ref: str
    head_ref: str
    head_oid: str
    title: str
    body: str

    def to_record(self) -> dict[str, Any]:
        return {
            "number": self.number,
            "url": self.url,
            "state": self.state,
            "base_ref": self.base_ref,
            "head_ref": self.head_ref,
            "head_oid": self.head_oid,
            "title": self.title,
            "body": self.body,
        }


class PullRequestClient(Protocol):
    """Exact observation and two explicit writes for one repository."""

    def observe(
        self,
        *,
        repository: str,
        base_ref: str,
        head_ref: str,
        candidate_commit: str,
    ) -> tuple[PullRequestObservation, ...]: ...

    def create(
        self,
        *,
        repository: str,
        base_ref: str,
        head_ref: str,
        candidate_commit: str,
        title: str,
        body: str,
    ) -> PullRequestObservation: ...

    def update(
        self,
        *,
        repository: str,
        number: int,
        candidate_commit: str,
        title: str,
        body: str,
    ) -> PullRequestObservation: ...


class GitHubCliPullRequestClient:
    """GitHub CLI adapter; tests inject a local controlled implementation."""

    def __init__(self, repository: Path) -> None:
        self.repository = Path(repository)

    def _gh(self, *args: str, input_path: Path | None = None) -> str:
        command = ["gh", *args]
        env = {**os.environ, "GH_HOST": "github.com"}
        if input_path is not None:
            with input_path.open("rb") as stream:
                result = subprocess.run(
                    command,
                    cwd=self.repository,
                    env=env,
                    input=stream.read(),
                    check=True,
                    capture_output=True,
                )
        else:
            result = subprocess.run(
                command,
                cwd=self.repository,
                env=env,
                check=True,
                capture_output=True,
            )
        return result.stdout.decode("utf-8").strip()

    @staticmethod
    def _observation(value: Mapping[str, Any]) -> PullRequestObservation:
        expected = {
            "number",
            "url",
            "state",
            "base_ref",
            "head_ref",
            "head_oid",
            "title",
            "body",
        }
        if set(value) != expected:
            raise PublicationError("publish.pr_response", {"reason": "shape"})
        number = value["number"]
        head_oid = value["head_oid"]
        if (
            type(number) is not int
            or number <= 0
            or not _OID.fullmatch(str(head_oid))
            or value["state"] != "OPEN"
            or not isinstance(value["base_ref"], str)
            or not isinstance(value["head_ref"], str)
            or not isinstance(value["url"], str)
            or not isinstance(value["title"], str)
            or not isinstance(value["body"], str)
        ):
            raise PublicationError("publish.pr_response", {"reason": "value"})
        return PullRequestObservation(**dict(value))

    def observe(
        self,
        *,
        repository: str,
        base_ref: str,
        head_ref: str,
        candidate_commit: str,
    ) -> tuple[PullRequestObservation, ...]:
        payload = self._gh(
            "pr",
            "list",
            "--repo",
            repository,
            "--base",
            base_ref,
            "--head",
            head_ref,
            "--state",
            "open",
            "--json",
            "number,url,state,baseRefName,headRefName,headRefOid,title,body",
        )
        rows = json.loads(payload or "[]")
        if not isinstance(rows, list):
            raise PublicationError("publish.pr_response", {"reason": "list"})
        observations = []
        for row in rows:
            if not isinstance(row, dict):
                raise PublicationError("publish.pr_response", {"reason": "row"})
            normalized = {
                "number": row.get("number"),
                "url": row.get("url"),
                "state": row.get("state"),
                "base_ref": row.get("baseRefName"),
                "head_ref": row.get("headRefName"),
                "head_oid": row.get("headRefOid"),
                "title": row.get("title"),
                "body": row.get("body"),
            }
            observation = self._observation(normalized)
            if (
                observation.base_ref == base_ref
                and observation.head_ref == head_ref
                and observation.head_oid == candidate_commit
            ):
                observations.append(observation)
        return tuple(observations)

    def create(
        self,
        *,
        repository: str,
        base_ref: str,
        head_ref: str,
        candidate_commit: str,
        title: str,
        body: str,
    ) -> PullRequestObservation:
        if _OID.fullmatch(candidate_commit) is None:
            raise PublicationError("publish.candidate_commit", {"value": candidate_commit})
        url = self._gh(
            "pr",
            "create",
            "--repo",
            repository,
            "--base",
            base_ref,
            "--head",
            head_ref,
            "--title",
            title,
            "--body",
            body,
        )
        return self._view(repository=repository, url=url)

    def update(
        self,
        *,
        repository: str,
        number: int,
        candidate_commit: str,
        title: str,
        body: str,
    ) -> PullRequestObservation:
        if _OID.fullmatch(candidate_commit) is None:
            raise PublicationError("publish.candidate_commit", {"value": candidate_commit})
        self._patch(repository=repository, number=number, title=title, body=body)
        return self._view(repository=repository, url=f"https://github.com/{repository}/pull/{number}")

    def _patch(self, *, repository: str, number: int, title: str, body: str) -> None:
        payload = json.dumps({"title": title, "body": body}, sort_keys=True).encode("utf-8")
        result = subprocess.run(
            ["gh", "api", "--method", "PATCH", f"repos/{repository}/pulls/{number}", "--input", "-"],
            cwd=self.repository,
            input=payload,
            check=True,
            capture_output=True,
        )
        if not result.stdout and not result.stderr:
            return

    def _view(self, *, repository: str, url: str) -> PullRequestObservation:
        payload = self._gh(
            "pr",
            "view",
            url,
            "--repo",
            repository,
            "--json",
            "number,url,state,baseRefName,headRefName,headRefOid,title,body",
        )
        row = json.loads(payload)
        if not isinstance(row, dict):
            raise PublicationError("publish.pr_response", {"reason": "view"})
        return self._observation(
            {
                "number": row.get("number"),
                "url": row.get("url"),
                "state": row.get("state"),
                "base_ref": row.get("baseRefName"),
                "head_ref": row.get("headRefName"),
                "head_oid": row.get("headRefOid"),
                "title": row.get("title"),
                "body": row.get("body"),
            }
        )


class PublicationRepository:
    """A local checkout bound to one exact origin and remote state."""

    def __init__(self, path: Path, *, expected_origin: str) -> None:
        supplied = Path(path)
        if path_has_link_ancestor(supplied) or not supplied.is_dir():
            raise PublicationError("publish.repository_missing", {"path": str(supplied)})
        self.path = supplied.resolve()
        self.expected_origin = expected_origin

    def validate(self) -> None:
        self._validate_remote(self.path)
        if _text_run(self.path, "status", "--porcelain"):
            raise PublicationError("publish.repository_dirty", {"path": str(self.path)})

    def _validate_remote(self, path: Path) -> None:
        fetch_url, push_url = _remote_urls(path)
        if fetch_url != push_url:
            raise PublicationError("publish.remote_target_mismatch")
        if fetch_url != self.expected_origin:
            raise PublicationError("publish.origin_mismatch", {"expected": self.expected_origin})

    def fetch_expected(self, ref: str, expected_oid: str) -> None:
        validate_ref(self.path, ref)
        _run(self.path, "fetch", "--quiet", "origin", f"refs/heads/{ref}:refs/remotes/origin/{ref}")
        actual = _text_run(self.path, "rev-parse", f"refs/remotes/origin/{ref}")
        if actual != expected_oid:
            raise PublicationError("publish.base_moved", {"expected": expected_oid, "actual": actual})

    def observe(self, branch: str) -> str:
        validate_ref(self.path, branch)
        output = _text_run(self.path, "ls-remote", "origin", f"refs/heads/{branch}")
        if not output:
            return ""
        oid = output.split()[0]
        if not _OID.fullmatch(oid):
            raise PublicationError("publish.remote_oid", {"value": oid})
        return oid

    def observe_github_commit(self, *, repository_slug: str, commit: str) -> dict[str, Any]:
        """Observe one commit through the authenticated, exact GitHub repository authority."""

        validate_git_remote(self.path, repository_slug)
        if _OID.fullmatch(commit) is None:
            raise PublicationError("publish.candidate_commit", {"value": commit})

        def query(resource: str) -> tuple[int, dict[str, Any], dict[str, Any]]:
            result = subprocess.run(
                ["gh", "api", "--hostname", "github.com", "--include", resource],
                cwd=self.path,
                env={**os.environ, "GH_HOST": "github.com"},
                capture_output=True,
                timeout=30,
                check=False,
            )
            process = {
                "returncode": result.returncode,
                "stdout": safe_process_output(result.stdout), "stderr": safe_process_output(result.stderr),
            }
            header, separator, payload = result.stdout.replace(b"\r\n", b"\n").partition(b"\n\n")
            first = header.splitlines()[0] if header else b""
            match = re.fullmatch(rb"HTTP/[12](?:\.\d+)? (\d{3})(?: .*)?", first)
            try:
                value = json.loads(payload)
            except (ValueError, UnicodeDecodeError):
                value = None
            if not separator or match is None or not isinstance(value, dict):
                raise PublicationError(
                    "publish.github_commit_observation", {"reason": "response-shape", "process": process},
                )
            status = int(match[1])
            if result.returncode != (0 if status == 200 else 1):
                raise PublicationError(
                    "publish.github_commit_observation", {"reason": "response-status", "process": process},
                )
            return status, value, process

        def repository_identity() -> dict[str, Any]:
            status, value, process = query(f"repos/{repository_slug}")
            if (
                status != 200 or value.get("full_name") != repository_slug
                or type(value.get("id")) is not int or value["id"] <= 0
                or value.get("permissions", {}).get("pull") is not True
            ):
                raise PublicationError(
                    "publish.github_commit_observation", {"reason": "repository-authority", "process": process},
                )
            return {"id": value["id"], "full_name": value["full_name"], "pull": True}

        before = repository_identity()
        resource = f"repos/{repository_slug}/commits/{commit}"
        status, value, process = query(resource)
        if status == 200 and value.get("sha") == commit:
            state = "known"
        elif status == 422 and value.get("message") == f"No commit found for SHA: {commit}":
            state = "absent"
        else:
            raise PublicationError(
                "publish.github_commit_observation",
                {"reason": "commit-authority", "status": status, "process": process},
            )
        after = repository_identity()
        if before != after:
            raise PublicationError("publish.github_commit_observation", {"reason": "repository-changed"})
        return {
            "state": state, "repository": before, "resource": resource,
            "candidate_commit": commit, "http_status": status,
            "response": {"sha": value["sha"]} if state == "known" else {"message": value["message"]},
        }

    def verify_candidate_bundle(
        self,
        bundle: Path,
        *,
        commit: str,
        tree: str,
        parents: tuple[str, ...],
    ) -> None:
        """Import one sealed candidate bundle into this fresh checkout and verify its exact facts."""

        heads = _text_run(self.path, "bundle", "list-heads", str(bundle)).splitlines()
        if heads != [f"{commit} HEAD"]:
            raise PublicationError("publish.candidate_bundle_head", {"heads": heads})
        _run(self.path, "bundle", "verify", str(bundle))
        _run(
            self.path,
            "fetch",
            "--quiet",
            "--no-tags",
            str(bundle),
            "+HEAD:refs/publish-recovery/head",
        )
        recovered = _text_run(self.path, "rev-parse", "refs/publish-recovery/head")
        facts = _text_run(self.path, "show", "-s", "--format=%T%n%P", recovered).splitlines()
        if recovered != commit or facts != [tree, *parents]:
            raise PublicationError(
                "publish.candidate_bundle_facts",
                {
                    "commit": recovered,
                    "tree": facts[0] if facts else "",
                    "parents": facts[1:] if len(facts) > 1 else [],
                },
            )

    def read_blob(self, *, commit: str, path: str) -> bytes:
        """Read exact file bytes from an already verified candidate commit."""

        return _run(self.path, "cat-file", "blob", f"{commit}:{path}").stdout

    def validate_candidate_objects(self, *, commit: str, expected_base_oid: str) -> None:
        """Reject oversized ordinary Git blobs in the exact candidate upload closure."""

        if _OID.fullmatch(commit) is None or _OID.fullmatch(expected_base_oid) is None:
            raise PublicationError("publish.git_object_inventory", {"reason": "commit-identity"})
        objects = _text_run(
            self.path, "rev-list", "--objects", "--no-object-names", commit, "--not", expected_base_oid,
        ).splitlines()
        if not objects or len(set(objects)) != len(objects) or any(_OID.fullmatch(oid) is None for oid in objects):
            raise PublicationError("publish.git_object_inventory", {"reason": "object-identities"})
        result = subprocess.run(
            ["git", "-C", str(self.path), "cat-file", "--batch-check=%(objectname) %(objecttype) %(objectsize)"],
            input=("\n".join(objects) + "\n").encode("ascii"),
            check=True,
            capture_output=True,
        )
        rows = result.stdout.decode("ascii").splitlines()
        if len(rows) != len(objects):
            raise PublicationError("publish.git_object_inventory", {"reason": "object-count"})
        oversized: dict[str, dict[str, Any]] = {}
        for expected_oid, row in zip(objects, rows, strict=True):
            fields = row.split()
            if (
                len(fields) != 3 or fields[0] != expected_oid
                or fields[1] not in {"blob", "tree", "commit", "tag"} or not fields[2].isdigit()
            ):
                raise PublicationError("publish.git_object_inventory", {"reason": "object-facts", "oid": expected_oid})
            size = int(fields[2])
            if fields[1] == "blob" and size > GITHUB_MAX_BLOB_BYTES:
                oversized[expected_oid] = {"object_id": expected_oid, "bytes": size, "paths": []}
        if not oversized:
            return
        commits = _text_run(self.path, "rev-list", commit, "--not", expected_base_oid).splitlines()
        for reachable_commit in commits:
            for entry in _run(self.path, "ls-tree", "-r", "-z", "--full-tree", reachable_commit).stdout.split(b"\0"):
                if not entry:
                    continue
                metadata, separator, path = entry.partition(b"\t")
                fields = metadata.split()
                if not separator or len(fields) != 3:
                    raise PublicationError("publish.git_object_inventory", {"reason": "tree-entry"})
                oid = fields[2].decode("ascii")
                if oid in oversized:
                    location = {"commit": reachable_commit, "path": path.decode("utf-8", errors="replace")}
                    if location not in oversized[oid]["paths"]:
                        oversized[oid]["paths"].append(location)
            if all(item["paths"] for item in oversized.values()):
                break
        raise PublicationError(
            "publish.github_blob_too_large",
            {
                "provider": "github.com",
                "limit_bytes": GITHUB_MAX_BLOB_BYTES,
                "limit_source": GITHUB_LARGE_FILE_DOCUMENTATION,
                "candidate_commit": commit,
                "expected_base_oid": expected_base_oid,
                "objects": [oversized[oid] for oid in sorted(oversized)],
            },
        )

    def candidate(
        self,
        *,
        publication_root: Path,
        base_ref: str,
        expected_base_oid: str,
        branch_ref: str,
        expected_head_oid: str | None,
        commit_spec: CommitSpec,
        staging_root: Path,
    ) -> PreparedCandidate:
        validate_publication_target(base_ref=base_ref, branch_ref=branch_ref)
        self.validate()
        self.fetch_expected(base_ref, expected_base_oid)
        observed_head = self.observe(branch_ref)
        if observed_head != (expected_head_oid or ""):
            raise PublicationError(
                "publish.head_moved",
                {"expected": expected_head_oid or "", "actual": observed_head},
            )
        start = observed_head or expected_base_oid
        worktree = staging_root / "worktree"
        _run(self.path, "worktree", "add", "--detach", str(worktree), start)
        try:
            if _run(worktree, "merge-base", "--is-ancestor", expected_base_oid, "HEAD", check=False).returncode != 0:
                raise PublicationError("publish.base_not_ancestor")
            self._apply_publication_tree(publication_root, worktree)
            env = {
                **os.environ,
                "GIT_AUTHOR_NAME": commit_spec.author_name,
                "GIT_AUTHOR_EMAIL": commit_spec.author_email,
                "GIT_AUTHOR_DATE": commit_spec.authored_at,
                "GIT_COMMITTER_NAME": commit_spec.committer_name,
                "GIT_COMMITTER_EMAIL": commit_spec.committer_email,
                "GIT_COMMITTER_DATE": commit_spec.committed_at,
                "TZ": "UTC",
                "LC_ALL": "C",
            }
            _run(worktree, "add", "--all", env=env)
            if _run(worktree, "diff", "--cached", "--quiet", check=False, env=env).returncode == 0:
                raise PublicationError("publish.no_change", {"parent": start})
            _run(worktree, "commit", "--no-gpg-sign", "-m", commit_spec.message, env=env)
            if _text_run(worktree, "status", "--porcelain"):
                raise PublicationError("publish.commit_left_dirty")
            commit = _text_run(worktree, "rev-parse", "HEAD")
            tree = _text_run(worktree, "rev-parse", "HEAD^{tree}")
            parents = tuple(_text_run(worktree, "show", "-s", "--format=%P", "HEAD").split())
            expected_parent = expected_head_oid or expected_base_oid
            if parents != (expected_parent,):
                raise PublicationError(
                    "publish.parent_mismatch",
                    {"expected": expected_parent, "actual": list(parents)},
                )
            changed = [
                path.decode("utf-8")
                for path in _run(worktree, "diff", "--name-only", "-z", expected_parent, commit).stdout.split(b"\0")
                if path
            ]
            for path in changed:
                if not any(path == name or path.startswith(name + "/") for name in GOVERNED_PATHS):
                    raise PublicationError("publish.ungoverned_change", {"path": path})
            subject_sha256 = self._verify_commit_tree(publication_root, worktree, commit)
            self._verify_attributes(worktree, publication_root)
            self.validate_candidate_objects(commit=commit, expected_base_oid=expected_base_oid)
            bundle = staging_root / "candidate.bundle"
            _run(worktree, "bundle", "create", str(bundle), "HEAD", "--not", expected_base_oid)
            return PreparedCandidate(
                repository=self.path,
                worktree=worktree,
                staging_root=staging_root,
                commit=commit,
                tree=tree,
                parents=parents,
                subject_sha256=subject_sha256,
                bundle=bundle,
            )
        except BaseException:
            _run(self.path, "worktree", "remove", "--force", str(worktree), check=False)
            _run(self.path, "worktree", "prune", check=False)
            shutil.rmtree(staging_root, ignore_errors=True)
            raise

    @staticmethod
    def _apply_publication_tree(source: Path, worktree: Path) -> None:
        for name in GOVERNED_PATHS:
            if name == ".gitattributes":
                continue
            target = worktree / name
            if target.is_symlink():
                target.unlink()
            elif target.is_dir():
                shutil.rmtree(target)
            elif target.exists():
                target.unlink()
            origin = source / name
            if origin.is_dir():
                shutil.copytree(origin, target)
            elif origin.is_file():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(origin, target)
            else:
                raise PublicationError("publish.tree_incomplete", {"path": name})
        (worktree / ".gitattributes").write_bytes(GIT_ATTRIBUTES)

    def _verify_commit_tree(self, source: Path, worktree: Path, commit: str) -> str:
        expected = inventory(source)
        expected[".gitattributes"] = hashlib.sha256(GIT_ATTRIBUTES).hexdigest()
        listing = _run(worktree, "ls-tree", "-r", "-z", "--name-only", commit).stdout.split(b"\0")
        governed = {
            name.decode("utf-8")
            for name in listing
            if name
            and any(
                name.decode("utf-8") == item or name.decode("utf-8").startswith(item + "/")
                for item in GOVERNED_PATHS
            )
        }
        if governed != set(expected):
            raise PublicationError(
                "publish.tree_inventory",
                {
                    "missing": sorted(set(expected) - governed),
                    "extra": sorted(governed - set(expected)),
                },
            )
        for relative, checksum in sorted(expected.items()):
            if relative == ".gitattributes":
                content = _run(worktree, "show", f"{commit}:{relative}").stdout
                actual = hashlib.sha256(content).hexdigest()
            else:
                content = _run(worktree, "show", f"{commit}:{relative}").stdout
                actual = hashlib.sha256(content).hexdigest()
            if actual != checksum:
                raise PublicationError("publish.blob_mismatch", {"path": relative})
        attributes = _run(worktree, "show", f"{commit}:.gitattributes").stdout
        if attributes != GIT_ATTRIBUTES:
            raise PublicationError("publish.gitattributes")
        subject = subject_digest(source)
        if subject != digest(
            {
                path: checksum
                for path, checksum in expected.items()
                if path in SUBJECT_FILES
                or path.startswith(tuple(name + "/" for name in (*SUBJECT_DIRECTORIES, *PROJECTION_DIRECTORIES)))
            }
        ):
            raise PublicationError("publish.subject")
        return subject

    @staticmethod
    def _verify_attributes(worktree: Path, source: Path) -> None:
        paths = sorted([*inventory(source), ".gitattributes"])
        result = subprocess.run(
            [
                "git",
                "-C",
                str(worktree),
                "check-attr",
                "-z",
                "--stdin",
                "text",
                "filter",
                "working-tree-encoding",
            ],
            input=b"\0".join(path.encode("utf-8") for path in paths) + b"\0",
            check=True,
            capture_output=True,
        )
        fields = result.stdout.split(b"\0")[:-1]
        if len(fields) != len(paths) * 9:
            raise PublicationError("publish.gitattributes_inventory")
        for offset in range(0, len(fields), 3):
            _path, attribute, value = (field.decode("utf-8") for field in fields[offset : offset + 3])
            if value != "unset":
                raise PublicationError(
                    "publish.gitattributes_conflict",
                    {"path": _path, "attribute": attribute, "value": value},
                )

    def push(
        self,
        candidate: PreparedCandidate,
        *,
        base_ref: str,
        branch_ref: str,
        expected_head_oid: str | None,
    ) -> None:
        validate_publication_target(base_ref=base_ref, branch_ref=branch_ref)
        self.validate()
        self._validate_remote(Path(candidate.worktree))
        lease = f"--force-with-lease=refs/heads/{branch_ref}:{expected_head_oid or ''}"
        _run(
            candidate.worktree,
            "push",
            lease,
            "origin",
            f"HEAD:refs/heads/{branch_ref}",
        )
