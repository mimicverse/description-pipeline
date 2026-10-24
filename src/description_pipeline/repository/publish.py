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

from ..delivery import subject_digest
from ..io import PipelineError, digest, inventory, read_data, write_json
from ..schemas import REPOSITORY_SNAPSHOT_SCHEMA

GOVERNED_PATHS = (
    "manifest.json",
    "input",
    "evidence",
    "model",
    "urdf",
    "meshes",
    "tool.json",
    "reports",
    ".gitattributes",
)
GIT_ATTRIBUTES = (
    "# Preserve published delivery bytes and verification receipts.\n"
    + "".join(
        f"/{name}{'/**' if name in {'input', 'evidence', 'model', 'urdf', 'meshes', 'reports'} else ''} "
        "-text -filter -working-tree-encoding\n"
        for name in GOVERNED_PATHS
    )
).encode("ascii")
_OID = re.compile(r"\A[0-9a-f]{40}\Z")
_HEX64 = re.compile(r"\A[0-9a-f]{64}\Z")
REPOSITORY_SNAPSHOT_FILE = "repository.bundle"
REPOSITORY_SNAPSHOT_METADATA = "snapshot.json"


class PublicationError(PipelineError):
    def __init__(self, code: str, detail: object | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


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

    source = Path(repository).resolve()
    actual_origin = _text_run(source, "remote", "get-url", "origin")
    if actual_origin != origin_url:
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
    for oid in (expected.base_oid, expected.head_oid):
        if oid is not None and _run(destination, "cat-file", "-e", f"{oid}^{{commit}}", check=False).returncode != 0:
            raise PublicationError("publish.snapshot_object_missing", {"oid": oid})
    return snapshot


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
        if input_path is not None:
            with input_path.open("rb") as stream:
                result = subprocess.run(
                    command,
                    cwd=self.repository,
                    input=stream.read(),
                    check=True,
                    capture_output=True,
                )
        else:
            result = subprocess.run(command, cwd=self.repository, check=True, capture_output=True)
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
        self.path = Path(path).resolve()
        if not self.path.is_dir() or self.path.is_symlink():
            raise PublicationError("publish.repository_missing", {"path": str(self.path)})
        self.expected_origin = expected_origin

    def validate(self) -> None:
        actual = _text_run(self.path, "remote", "get-url", "origin")
        if actual != self.expected_origin:
            raise PublicationError("publish.origin_mismatch", {"expected": self.expected_origin, "actual": actual})
        if _text_run(self.path, "status", "--porcelain"):
            raise PublicationError("publish.repository_dirty", {"path": str(self.path)})

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
                if path == "tool.json"
                or path.startswith(("input/", "evidence/", "model/", "urdf/", "meshes/"))
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

    def push(self, candidate: PreparedCandidate, *, branch_ref: str, expected_head_oid: str | None) -> None:
        lease = f"--force-with-lease=refs/heads/{branch_ref}:{expected_head_oid or ''}"
        _run(
            candidate.worktree,
            "push",
            lease,
            "origin",
            f"HEAD:refs/heads/{branch_ref}",
        )

