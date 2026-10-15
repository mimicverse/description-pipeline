"""Authenticated, persistent, serial Windows execution endpoint for Airflow.

The API freezes native engineering folders and routes their discovered hardware
identity through platform configuration. It cannot execute shell commands or
accept arbitrary output/repository paths. Job identity is
stable across network retries; an interrupted native run fails explicitly and
is never silently replayed after a process restart.

One run may name its delivered assembly explicitly (``main_assembly``); the
endpoint verifies the value against the frozen upload and binds it to the run's
checkpoints, so a linked attempt can never reuse a different selection.

The only deletion path is the sanctioned release of one terminal run's staged
imported cache (IF-10): the caller binds an operation identity and its closed
imported inventory, the endpoint verifies both against the staged bytes, and
answers an immutable receipt.  Refusals and interrupted calls never partially
delete, and replaying one operation returns its original receipt.
"""

from __future__ import annotations

import contextlib
import copy
import hmac
import hashlib
import json
import os
import queue
import re
import shutil
import sys
import threading
import time
import tempfile
import unicodedata
import uuid
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote

from ..delivery import PIPELINE_ID, subject_inventory
from ..io import (
    PipelineError,
    acquire_process_lock,
    artifact_path_parts,
    confined,
    digest,
    file_digest,
    inventory,
    read_data,
    write_json,
)
from ..ports import SOLIDWORKS_ENDPOINT_PORT
from ..sources.snapshot import verify_snapshot
from ..sources.solidworks.revision import package_inventory
from ..sources.solidworks.selection import SelectionError
from ..runtime import tool_record
from ..interface_records import (
    HEALTH_SCHEMA,
    RESOURCE_POLICY_SCHEMA,
    cache_release_params_digest,
    cache_release_receipt_digest,
    resource_policy_v1_digest,
    validate_cache_release_receipt,
    validate_cache_release_request,
)
from ..schemas import CACHE_RELEASE_RECEIPT_SCHEMA
from ..stages import (
    ACTIVITY_HISTORY_LIMIT,
    CONTRACT,
    CONTRACT_SHA256,
    STAGE_IDS,
    merge_activity,
    normalize_activity,
    stage_view,
)
from .stage_transfer import CAPTURE_MANIFEST as CAPTURE_MANIFEST_NAME
from .selection_transport import StagedSelection
from ..sources.solidworks.selection import validate_file_index, verify_selection
from .recovery import stage_reruns, start_plan
from ..sources.solidworks.handoff import MAX_HANDOFF_BYTES

_SHA = re.compile(r"^[0-9a-f]{64}$")
_HARDWARE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,63}$")
# A publication base may be any well-formed git branch name (the release pins
# `main`; feature branches stay valid).  The pattern keeps the documented
# check-ref-format shape and the extra checks below reject `..`, trailing dots
# and `.lock` components, which the pattern alone would let through.
_BASE_BRANCH = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*(/[A-Za-z0-9][A-Za-z0-9._-]*)*")


#: Distinguishes "the runner already returned" from "the runner has not run yet"
#: when a terminal publication is deferred to the one atomic critical section.
_UNSET = object()


def _valid_base_branch(value: object) -> bool:
    if not isinstance(value, str) or value != value.strip() or not value:
        return False
    if _BASE_BRANCH.fullmatch(value) is None or ".." in value or value.endswith("."):
        return False
    return all(not part.lower().endswith(".lock") for part in value.split("/"))


JOB_SCHEMA = "solidworks-to-urdf.job/v1"
CONFIG_SCHEMA = "solidworks-to-urdf.endpoint/v1"

#: The staged per-run cache root and its release journal: releasing one terminal
#: run's imported cache is the endpoint's only deletion path (IF-10).
CACHE_RELEASE_SELECTED = "selected"
CACHE_RELEASE_JOURNAL_SCHEMA = "solidworks-to-urdf.cache-release-journal/v1"

#: Stages the Windows endpoint owns; generation, verification and publication run on Linux.
NATIVE_STAGES = tuple(STAGE_IDS[:3])


def _native_tool_record() -> dict:
    """The native-role tool identity for the sealed capture provenance."""

    return tool_record(role="native")


class RequestError(PipelineError):
    def __init__(self, message, status=400, payload=None):
        super().__init__(message)
        self.status = status
        self.payload = dict(payload or {})


def _require(value, message):
    if not value:
        raise RequestError(message)


def _resume_spec(request):
    """The validated linked-run binding, or None for an ordinary fresh job."""
    resume = request.get("resume") if isinstance(request, dict) else None
    if resume is None:
        return None
    if not isinstance(resume, dict) or set(resume) != {"parent_run", "from_stage"}:
        raise RequestError("resume must carry exactly parent_run and from_stage")
    parent = _job_id(resume["parent_run"])
    stage = resume["from_stage"]
    if stage not in STAGE_IDS:
        raise RequestError("from_stage must be a canonical engineering stage")
    return {"parent_run": parent, "from_stage": stage}


#: One explicit delivered-assembly selection shares the member-path bound.
MAX_MAIN_ASSEMBLY = 1024


def _main_assembly_spec(value):
    """Optional explicit delivered assembly: canonical POSIX path inside the frozen root."""

    if value is None:
        return None
    if not isinstance(value, str) or not value or len(value) > MAX_MAIN_ASSEMBLY:
        raise RequestError("主装配必须是工程文件夹内的一个 .SLDASM 相对路径")
    if value != value.strip():
        raise RequestError("主装配路径不能包含首尾空白")
    if unicodedata.normalize("NFC", value) != value:
        raise RequestError("主装配路径必须使用 NFC 规范形式")
    try:
        parts = artifact_path_parts(value)
    except PipelineError as error:
        raise RequestError("主装配路径必须是可移植的相对路径") from error
    if (
        not parts
        or "/".join(parts) != value
        or "\\" in value
        or value.startswith("/")
        or any(segment in {"", ".", ".."} for segment in value.split("/"))
    ):
        raise RequestError("主装配必须是工程文件夹内的一个 .SLDASM 相对路径")
    if not value.casefold().endswith(".sldasm"):
        raise RequestError("主装配必须指向一个已保存的 SolidWorks 装配（.SLDASM）")
    return value


def _refusal(reason, message, *, earliest=None, **extra):
    payload = {"reason": reason, "reason_zh": message, "earliest_required": earliest}
    payload.update(extra)
    return RequestError(message, 409, payload=payload)


def _job_id(value):
    try:
        parsed = str(uuid.UUID(value))
    except (ValueError, AttributeError, TypeError) as error:
        raise RequestError("run_id must be a canonical UUID") from error
    _require(parsed == value, "run_id must be a canonical UUID")
    return parsed


def _root(value):
    path = Path(value)
    _require(
        path.is_absolute() and not path.is_symlink() and not path.is_junction(),
        "Configured roots must be absolute real directories",
    )
    return path.resolve()


def read_config(path):
    config = read_data(Path(path))
    _require(
        isinstance(config, dict) and config.get("schema_version") == CONFIG_SCHEMA, "Unknown endpoint configuration"
    )
    _require(
        set(config)
        <= {
            "schema_version",
            "package_root",
            "output_root",
            "state_root",
            "targets",
            "token_file",
            "host",
            "port",
        },
        "Unknown endpoint configuration key",
    )
    #: The install identity digests the document itself, before any derived key:
    #: the lifecycle stage step computes the same value with the same owner.
    config_digest = digest(config)
    for key in ("package_root", "output_root", "state_root"):
        config[key] = _root(config[key])
    _require(config["package_root"].is_dir(), "Package root does not exist")
    #: Model-repository routing belongs to the Linux side; the endpoint accepts
    #: the documented minimal configuration without targets and never requires
    #: one to exist.
    targets = config.get("targets") or {}
    _require(isinstance(targets, dict), "Endpoint targets must be a mapping")
    roots = [config[key] for key in ("package_root", "output_root", "state_root")]
    for hardware, target in targets.items():
        _require(
            isinstance(hardware, str)
            and _HARDWARE.fullmatch(hardware) is not None
            and isinstance(target, dict)
            and set(target) == {"repository_slug", "base"},
            "Each hardware identity needs a repository slug and base",
        )
        _require(
            isinstance(target["repository_slug"], str)
            and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*", target["repository_slug"])
            is not None,
            "Target repository slug must be owner/name",
        )
        _require(
            _valid_base_branch(target["base"]),
            "Model PR base must be a well-formed git branch name",
        )
    for index, first in enumerate(roots):
        for second in roots[index + 1 :]:
            _require(
                not first.is_relative_to(second) and not second.is_relative_to(first),
                "Endpoint package, output, state and repository roots must be separate",
            )
    token_file = _root(config["token_file"])
    _require(token_file.is_file(), "Missing endpoint token file")
    token = token_file.read_text(encoding="utf-8").strip()
    _require(
        len(token) >= 32 and token.isascii() and not any(character.isspace() for character in token),
        "Endpoint token must contain at least 32 non-whitespace ASCII characters",
    )
    config["token"] = token
    config.pop("token_file")
    config["targets"] = targets
    config["config_digest"] = config_digest
    config.setdefault("host", "127.0.0.1")
    config.setdefault("port", SOLIDWORKS_ENDPOINT_PORT)
    _require(
        isinstance(config["port"], int) and not isinstance(config["port"], bool) and 1 <= config["port"] <= 65535,
        "Invalid endpoint port",
    )
    _require(
        config["host"] in {"127.0.0.1", "::1", "localhost"},
        "The Windows endpoint must bind to loopback and use the authenticated SSH tunnel",
    )
    return config


def _owner_lock(path):
    """Process-held ownership survives neither a crash nor an OS restart."""
    try:
        return acquire_process_lock(path)
    except OSError as error:
        raise PipelineError("Another endpoint owns the state root or its ownership lock is unavailable") from error


def provision_install_id(state_root: Path) -> str:
    """The endpoint's own install identity: written once, stable across restarts.

    The operator derives install records from the running endpoint's ``/health``,
    so the endpoint itself must mint and keep its identity; a write-once marker
    in the instance state root survives config switches and crash restarts.
    """

    marker = Path(state_root) / "install-id"
    try:
        existing = marker.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        existing = ""
    if existing:
        return existing
    fresh = "win-" + uuid.uuid4().hex
    marker.parent.mkdir(parents=True, exist_ok=True)
    try:
        handle = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return marker.read_text(encoding="utf-8").strip()
    with os.fdopen(handle, "w", encoding="utf-8") as stream:
        stream.write(fresh + "\n")
    return fresh


#: Endpoint job states, partitioned for the health identity: anything outside the
#: known sets is unresolved (a fail-closed bucket, never silently healthy).
_HEALTH_ACTIVE_JOB_STATES = frozenset({"running", "selecting", "awaiting_definition"})
_HEALTH_QUEUED_JOB_STATES = frozenset({"queued"})
_HEALTH_TERMINAL_JOB_STATES = frozenset({"passed", "failed", "native_complete"})


def _cad_health(jobs) -> tuple[dict, list[str]]:
    """CAD session facts via the isolation owner; observe-only, never opens or terminates CAD."""

    if sys.platform != "win32":
        return {"external_session_detected": False, "owned_sessions": 0, "lease_held": False}, ["unsupported_platform"]
    try:
        from ..sources.solidworks import isolation

        facts = isolation.cad_session_facts(
            isolation.registered_executable(), ownership_file=jobs.cad_ownership_file
        )
    except Exception:  # noqa: BLE001 - an unprovable CAD state is a readiness problem, never a pass
        return {"external_session_detected": False, "owned_sessions": 0, "lease_held": False}, ["cad_state_unavailable"]
    return (
        {
            "external_session_detected": facts["external_session_detected"],
            "owned_sessions": facts["owned_sessions"],
            "lease_held": facts["lease_held"],
        },
        list(facts["problems"]),
    )


def health_payload(jobs) -> dict:
    """The IF-09 endpoint identity from its own install record, config snapshot and live state."""

    config = jobs.config
    record = tool_record()
    embedded = record.get("release") if isinstance(record.get("release"), dict) else None
    if embedded is None:
        # An unreadable own identity is never a green health document: the
        # handler turns this into the stable 500 identity-unavailable code.
        raise PipelineError("The endpoint has no installed release identity; /health cannot report an IF-09 identity")
    release = {
        "version": str(record.get("version") or ""),
        "source_commit": str((embedded or {}).get("source_commit") or ""),
        "source_sha256": str(record.get("source_sha256") or ""),
        "role": "native",
    }
    problems: list[str] = []
    native_ok = sys.platform == "win32"
    if not native_ok:
        problems.append("unsupported_platform")
    if not getattr(jobs, "thread", None) or not jobs.thread.is_alive():
        problems.append("queue_thread_unavailable")
    if native_ok:
        try:
            from ..runtime import native_readiness

            native_readiness()
        except Exception:  # noqa: BLE001 - an unprovable native state is a readiness problem, never a pass
            problems.append("native_readiness_failed")
    for key in ("package_root", "output_root"):
        if not Path(config[key]).is_dir():
            problems.append(f"missing_{key}")
    with jobs.mutex:
        states = [str(job.get("status") or "") for job in jobs.jobs.values()]
    active = sum(1 for state in states if state in _HEALTH_ACTIVE_JOB_STATES)
    queued = sum(1 for state in states if state in _HEALTH_QUEUED_JOB_STATES)
    unresolved = sum(
        1
        for state in states
        if state not in (_HEALTH_ACTIVE_JOB_STATES | _HEALTH_QUEUED_JOB_STATES | _HEALTH_TERMINAL_JOB_STATES)
    )
    cad, cad_problems = _cad_health(jobs)
    problems.extend(cad_problems)
    ready = native_ok and not problems and not cad["external_session_detected"] and unresolved == 0
    if ready:
        reason_code = None
    elif problems:
        reason_code = problems[0]
    elif cad["external_session_detected"]:
        reason_code = "external_cad_session"
    else:
        reason_code = "unresolved_operations"
    usage = shutil.disk_usage(Path(config["output_root"]))
    payload = {
        "schema_version": HEALTH_SCHEMA,
        "pipeline_id": PIPELINE_ID,
        "native_platform": native_ok,
        "ready": ready,
        "release": release,
        "contract": {"schema_version": CONTRACT["schema_version"], "digest": CONTRACT_SHA256},
        "resource_policy": {"schema_version": RESOURCE_POLICY_SCHEMA, "digest": resource_policy_v1_digest()},
        "install": {"config_digest": config["config_digest"], "install_id": jobs.install_id},
        "endpoint": {"port": config["port"]},
        "cad": cad,
        "jobs": {"active": active, "queued": queued, "unresolved": unresolved},
        "capacity": {"free_bytes": int(usage.free), "reserved_bytes": 0},
        "readiness": {
            "reason_code": reason_code,
            "native": {"ok": native_ok and not problems, "problems": problems},
        },
    }
    from ..interface_records import InterfaceRecordError, validate_health

    try:
        validate_health(payload)
    except InterfaceRecordError as error:
        # An incomplete or invalid identity is never a green document: the
        # same schema owner refuses it, and the handler answers the stable 500.
        raise PipelineError(f"The endpoint cannot read its complete IF-09 identity: {error}") from error
    return payload


def _native_release_identity() -> dict:
    """The endpoint's installed identity as a native-role release triple."""

    record = tool_record()
    embedded = record.get("release") if isinstance(record.get("release"), dict) else None
    if embedded is None:
        raise PipelineError("The endpoint has no installed release identity; the cache release cannot bind one")
    return {
        "version": str(record.get("version") or ""),
        "source_commit": str(embedded.get("source_commit") or ""),
        "source_sha256": str(record.get("source_sha256") or ""),
        "role": "native",
    }


def _staged_manifest(root: Path) -> list[dict]:
    """The closed staged-cache inventory (path, bytes, SHA-256); native locks excluded."""

    locked = package_inventory(root)
    return [
        {"path": relative, "bytes": (root / relative).stat().st_size, "sha256": locked[relative]}
        for relative in sorted(locked)
    ]


def _default_runner(*args, **kwargs):
    """The native capture entry, imported only when a run actually needs it.

    ``orchestration.windows`` must stay inside the Windows capture closure: the
    endpoint supplies an explicit runner in tests that exercise the capture
    boundary, so the generation/verification domain is never pulled in by
    importing or constructing the endpoint.
    """

    from ..solidworks import capture

    return capture(*args, **kwargs)


class Jobs:
    """One persistent queue, one CAD runner, one endpoint process owner."""

    #: Linked-run plans hash retained checkpoints; the detail route may poll, so
    #: finished plans are memoized briefly. ``create`` always probes afresh.
    PLAN_TTL = 30.0

    def __init__(self, config, *, runner=None, selection_reader_factory=None, native_reader=None, backend_factory=None):
        self.config = config
        self.runner = runner or _default_runner
        self.selection_reader_factory = selection_reader_factory
        self.native_reader = native_reader
        self.backend_factory = backend_factory or self._sealed_backend
        #: Every endpoint-side owned launch (selection and capture) records
        #: into this one ledger.
        self.cad_ownership_file = config["state_root"] / "cad-ownership.json"
        self._plans = {}
        self.directory = config["state_root"] / "jobs"
        self.directory.mkdir(parents=True, exist_ok=True)
        inventory(self.directory)
        self.lock_path = config["state_root"] / ".endpoint.lock"
        self.handle = _owner_lock(self.lock_path)
        self.install_id = provision_install_id(config["state_root"])
        self._closed = False
        self.mutex = threading.RLock()
        self.condition = threading.Condition(self.mutex)
        self._stopping = False
        self.selections = {}
        self.selection_locks = {}
        self.prepared_packages = {}
        self.queue = queue.Queue()
        self.jobs = {}
        try:
            for path in sorted(self.directory.glob("*.json")):
                identifier = _job_id(path.stem)
                job = read_data(path)
                _require(
                    job.get("schema_version") == JOB_SCHEMA and job.get("run_id") == identifier, "Invalid persisted job"
                )
                self.jobs[identifier] = job
                if job["status"] in {"running", "selecting", "awaiting_definition"}:
                    # Restart repair publishes this terminal record inside one
                    # single-threaded construction block (flip -> clear -> seal
                    # -> save) and deliberately carries no completed_at: the job
                    # never ran to completion, and the interrupted-record shape
                    # is pinned by
                    # test_process_exit_releases_ownership_and_fails_interrupted_job.
                    job.update(
                        status="failed",
                        error="Endpoint restarted during native execution; inspect diagnostics and use a new run_id",
                    )
                    self._clear_activity(job)
                    self._seal_failure_diagnostics(job)
                    self._save(job)
                elif job["status"] == "queued":
                    self.selection_locks[identifier] = threading.RLock()
                    self.queue.put(identifier)
            self.thread = threading.Thread(target=self._work, name="description-cad-queue", daemon=True)
            self.thread.start()
        except BaseException:
            os.close(self.handle)
            raise

    def _save(self, job):
        write_json(self.directory / (job["run_id"] + ".json"), job)

    def _seal_failure_diagnostics(self, job):
        """Seal the raw failure diagnostics; the Linux side renders the report from these bytes."""

        from ..structural_report import DIAGNOSTIC_SCHEMA, diagnostic_file_sources

        run_id = job["run_id"]
        root = self.config["state_root"] / "diagnostics" / run_id
        if root.exists():
            shutil.rmtree(root)
        root.mkdir(parents=True)
        receipt = root / "receipt.json"
        staged = copy.deepcopy(job)
        staged.pop("failure_diagnostics", None)
        write_json(receipt, staged)
        evidence = self._delivery_dir(job)
        partial_roots: tuple[str, ...] = ()
        if evidence is not None:
            # Gated to the immediate owned failure receipt directories; no
            # recursive discovery, so an unexpected layout can never be guessed at.
            partial_roots = tuple(
                child.name
                for child in sorted(Path(evidence).iterdir())
                if child.is_dir() and re.fullmatch(r"evidence\.failed-\d{3}", child.name)
            )
        files: dict[str, str] = {}
        partial_names: list[str] = []
        for name, relative in diagnostic_file_sources(evidence, failure_roots=partial_roots).items():
            source = confined(Path(evidence), relative)
            if relative != name:
                partial_names.append(name)
            target = confined(root / "evidence", name, exists=False)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            with target.open("rb") as stream:
                files[name] = hashlib.file_digest(stream, "sha256").hexdigest()
        with receipt.open("rb") as stream:
            receipt_digest = hashlib.file_digest(stream, "sha256").hexdigest()
        manifest = {
            "schema": DIAGNOSTIC_SCHEMA,
            "run_id": run_id,
            "receipt": {"path": "receipt.json", "sha256": receipt_digest},
            "files": files,
        }
        if partial_names:
            # Honest provenance: some members were recovered from the partial tree,
            # so this bundle is explicitly marked as not a complete capture.
            manifest["partial"] = True
            manifest["partial_files"] = sorted(partial_names)
        write_json(root / "diagnostic.json", manifest)
        with (root / "diagnostic.json").open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        job["failure_diagnostics"] = {"path": "diagnostic.json", "sha256": digest}

    def validate(self, request):
        expected = {"run_id", "upload_files", "upload_sha256", "main_assembly"}
        _require(
            isinstance(request, dict) and set(request) in (expected, expected | {"resume"}),
            "Expected run_id, upload_files, upload_sha256, main_assembly and optional resume",
        )
        _job_id(request["run_id"])
        _resume_spec(request)
        _require(
            _main_assembly_spec(request["main_assembly"]) is not None, "Select the main assembly before native work"
        )
        files = validate_file_index(request["upload_files"])
        _require(digest(files) == request["upload_sha256"], "Upload metadata differs from its bound identity")
        if request["main_assembly"] not in files:
            raise SelectionError(
                "selection_main_invalid",
                "Choose a main assembly present in the uploaded folder",
                path=request["main_assembly"],
            )
        return files

    def _source_dir(self, job):
        owner = _job_id(job.get("source_run_id") or job["run_id"])
        return confined(self.config["package_root"], "selected/" + owner + "/.source", exists=False).parent

    def _delivery_dir(self, job):
        """Retained delivery or diagnostic directory of one finished native job."""
        output = self.config["output_root"] / job["run_id"]
        if output.is_dir():
            return output
        diagnostic = (job.get("result") or {}).get("diagnostic_path")
        if isinstance(diagnostic, str) and Path(diagnostic).is_dir():
            return Path(diagnostic)
        failed = output.with_name(output.name + ".failed")
        return failed if failed.is_dir() else None

    def _verified_capture_archive(self, identifier, archive):
        """Re-hash the sealed transfer; native_complete is refused on any mismatch."""
        output = self.config["output_root"] / identifier
        name = archive.get("name")
        _require(isinstance(name, str) and bool(name), "Capture archive name is missing")
        for key in ("sha256", "manifest_sha256"):
            _require(
                isinstance(archive.get(key), str) and _SHA.fullmatch(archive[key]) is not None,
                f"Capture archive {key} is missing",
            )
        archive_path = confined(output, name)
        manifest_path = confined(output, CAPTURE_MANIFEST_NAME)
        _require(
            archive_path.is_file() and manifest_path.is_file(),
            "Sealed capture transfer files are missing from the run output",
        )
        _require(
            file_digest(archive_path) == archive["sha256"],
            "Capture archive differs from its recorded digest",
        )
        _require(
            file_digest(manifest_path) == archive["manifest_sha256"],
            "Transfer manifest differs from its recorded digest",
        )
        return {
            "name": name,
            "sha256": archive["sha256"],
            "size": archive_path.stat().st_size,
            "manifest_sha256": archive["manifest_sha256"],
        }

    @staticmethod
    def _recorded_subject(job):
        result = job.get("result") if isinstance(job.get("result"), dict) else {}
        subject = result.get("subject_sha256")
        if isinstance(subject, str) and _SHA.fullmatch(subject):
            return subject
        for event in reversed(job.get("events") or []):
            check = event.get("check") if isinstance(event, dict) else None
            details = (check or {}).get("details") if isinstance(check, dict) else None
            value = (details or {}).get("subject_sha256")
            if isinstance(value, str) and _SHA.fullmatch(value):
                return value
        return None

    @staticmethod
    def _check_details(job, check_id):
        for event in reversed(job.get("events") or []):
            check = event.get("check") if isinstance(event, dict) else None
            if isinstance(check, dict) and check.get("id") == check_id:
                details = check.get("details")
                return details if isinstance(details, dict) else None
        return None

    @staticmethod
    def _native_stage_rows(rows, *, reader_matches=True):
        """The native endpoint owns freeze/discover/capture; the rest runs on Linux."""
        for row in rows:
            if row.get("stage") not in NATIVE_STAGES:
                row["eligible"] = False
                row["reason"] = "linux_owned"
                row["reason_zh"] = "该阶段已移至 Linux 执行；请通过交付流程重新运行"
                row["recomputes"] = []
            elif not reader_matches:
                row["eligible"] = False
                row["reason"] = "tool_changed"
                row["reason_zh"] = "原生依赖读取器已变化；请重新提交工程并完成文件选择"
                row["recomputes"] = []
        return rows

    @staticmethod
    def _tool_identity():
        """The full installed tool record; identity is compared as a whole."""
        try:
            return _native_tool_record()
        except (PipelineError, OSError, ValueError):
            return None

    def _prepared_dir(self, job):
        """The validated prepared package reused by linked runs (may belong to an ancestor)."""
        root = (self.config["state_root"] / "prepared").resolve()
        recorded = job.get("prepared_dir")
        if isinstance(recorded, str):
            path = Path(recorded)
            if (
                path.is_dir()
                and not path.is_symlink()
                and not path.is_junction()
                and path.resolve().is_relative_to(root)
            ):
                return path
        fallback = root / job["run_id"]
        return fallback if fallback.is_dir() else None

    def _capture_checkpoint(self, seed, job):
        """Capture outputs only count when the recorded binding still matches their bytes."""
        try:
            if not all((seed / part).exists() for part in ("reports/input.json", "input", "evidence")):
                return "absent"
            if not any(
                isinstance(event, dict) and event.get("stage") == "capture" and event.get("state") == "completed"
                for event in job.get("events") or []
            ):
                return "changed"
            input_details = self._check_details(job, "input.valid")
            integrity_details = self._check_details(job, "capture.integrity")
            if not isinstance(input_details, dict) or not isinstance(integrity_details, dict):
                return "changed"
            input_files = inventory(seed / "input")
            if {"input/" + name: checksum for name, checksum in input_files.items()} != input_details.get("files"):
                return "changed"
            if digest(input_files) != input_details.get("files_sha256"):
                return "changed"
            report = read_data(seed / "reports" / "input.json")
            if not isinstance(report, dict) or report.get("package_files") != input_files:
                return "changed"
            manifest = verify_snapshot(seed / "evidence")
            manifest_hash = file_digest(seed / "evidence" / "manifest.json")
            if manifest_hash != integrity_details.get("manifest_sha256"):
                return "changed"
            evidence_files = {"evidence/manifest.json": manifest_hash}
            evidence_files.update({"evidence/" + name: checksum for name, checksum in manifest["files"].items()})
            if evidence_files != integrity_details.get("files"):
                return "changed"
            return "ok"
        except (PipelineError, OSError, ValueError, TypeError, KeyError):
            return "changed"

    def _receipt_checkpoint(self, seed, job):
        try:
            if not (seed / "reports/quality.json").is_file():
                return "absent"
            quality = read_data(seed / "reports" / "quality.json")
            subject = self._recorded_subject(job)
        except (PipelineError, OSError, ValueError):
            return "changed"
        if not isinstance(quality, dict):
            return "changed"
        return "ok" if quality.get("passed") is True and quality.get("subject_sha256") == subject else "changed"

    def _probe(self, job):
        """Checkpoint, dependency and target availability for one native job."""
        probe = {
            "source": "absent",
            "discover": "absent",
            "capture": "absent",
            "generate": "absent",
            "receipt": "absent",
            "dependency": "unverifiable",
            "target": "unverifiable",
        }
        request = job.get("request") if isinstance(job.get("request"), dict) else {}
        try:
            package = self._source_dir(job)
            if package.is_dir():
                files = package_inventory(package)
                manifest = (job.get("selection") or {}).get("manifest")
                verify_selection(
                    manifest,
                    request["upload_files"],
                    files,
                    reader_sha256=job["native_tool"]["source_sha256"],
                )
                probe["source"] = (
                    "ok"
                    if digest(files) == job.get("handoff_sha256") and files == job.get("package_files")
                    else "changed"
                )
        except (PipelineError, OSError, ValueError, TypeError, KeyError):
            probe["source"] = "changed"
        prepared = self._prepared_dir(job)
        if prepared is not None and isinstance(job.get("prepared_files"), dict):
            try:
                probe["discover"] = "ok" if package_inventory(prepared) == job["prepared_files"] else "changed"
            except (PipelineError, OSError, ValueError):
                probe["discover"] = "changed"
        seed = self._delivery_dir(job)
        if seed is not None:
            probe["capture"] = self._capture_checkpoint(seed, job)
            subject = self._recorded_subject(job)
            generated = (
                "README.md",
                "input",
                "evidence",
                "model",
                "urdf",
                "meshes",
                "reports/input.json",
                "reports/tool.json",
            )
            if not all((seed / part).exists() for part in generated):
                probe["generate"] = "absent"
            else:
                try:
                    probe["generate"] = "ok" if subject and digest(subject_inventory(seed)) == subject else "changed"
                except (PipelineError, OSError, ValueError):
                    probe["generate"] = "changed"
            probe["receipt"] = self._receipt_checkpoint(seed, job)
        # Linux owns derivation dependencies. Native reuse depends on the sealed
        # source graph, installed reader, and exact prepared checkpoint bytes.
        probe["dependency"] = "ok" if probe["source"] == "ok" and self._tool_state(job) == "ok" else "changed"
        target = self.config["targets"].get(job.get("hardware_id"))
        if (
            isinstance(target, dict)
            and isinstance(job.get("repository_slug"), str)
            and isinstance(job.get("repository_base"), str)
        ):
            probe["target"] = (
                "ok"
                if target.get("repository_slug") == job["repository_slug"] and target["base"] == job["repository_base"]
                else "changed"
            )
        return probe

    def _tool_state(self, job):
        """Whether the installed tool still matches the tool recorded for this job."""
        try:
            current = _native_tool_record()
        except (PipelineError, OSError, ValueError):
            return "unknown"
        recorded = job.get("tool")
        if not isinstance(recorded, dict):
            seed = self._delivery_dir(job)
            if seed is not None and (seed / "reports/tool.json").is_file():
                try:
                    recorded = read_data(seed / "reports/tool.json")
                except (PipelineError, OSError, ValueError):
                    return "unknown"
        if not isinstance(recorded, dict):
            # No historical identity: reuse cannot be proven, only a freeze restart may proceed.
            return "unknown"
        return "ok" if recorded and recorded == current else "changed"

    @staticmethod
    def _reuse_events(parent, from_stage):
        upstream = set(STAGE_IDS[: STAGE_IDS.index(from_stage)])
        reused = []
        for event in parent.get("events") or []:
            if not isinstance(event, dict) or event.get("stage") not in upstream:
                continue
            copied = copy.deepcopy(event)
            previous = copied.get("reuse") if isinstance(copied.get("reuse"), dict) else {}
            producer = previous.get("source_run") or previous.get("parent_run") or parent["run_id"]
            copied["reuse"] = {"parent_run": parent["run_id"], "reused": True, "source_run": producer}
            reused.append(copied)
        return reused

    @staticmethod
    def _inherited_metadata(parent, from_stage):
        if from_stage not in {"capture", "generate", "verify", "publish"}:
            return {}
        inherited = {
            key: copy.deepcopy(parent[key])
            for key in (
                "hardware_id",
                "revision",
                "main_assembly",
                "repository_slug",
                "repository_base",
                "prepared_files",
                "prepared_dir",
            )
            if key in parent
        }
        if isinstance(parent.get("discovery"), dict):
            inherited["discovery"] = copy.deepcopy(parent["discovery"])
        return inherited

    def _active_attempt(self, parent_run):
        for other in self.jobs.values():
            request = other.get("request")
            resume = request.get("resume") if isinstance(request, dict) else None
            if (
                isinstance(resume, dict)
                and resume.get("parent_run") == parent_run
                and other.get("status") in {"queued", "running", "selecting", "awaiting_definition"}
            ):
                return other["run_id"]
        return None

    def plan(self, identifier):
        """Memoized linked-run plan for the run detail route; create() probes afresh."""
        job = self.snapshot(identifier)
        if job.get("status") not in {"passed", "failed", "native_complete"}:
            rows = self._native_stage_rows(stage_reruns(job, probe={}, tool="ok"))
            return {"run_id": identifier, "status": job.get("status"), "stage_reruns": rows}
        key = (job.get("status"), job.get("completed_at"))
        with self.mutex:
            cached = self._plans.get(identifier)
            if cached and cached[0] == key and time.monotonic() - cached[1] < self.PLAN_TTL:
                return cached[2]
        payload = {
            "run_id": identifier,
            "status": job.get("status"),
            "stage_reruns": self._native_stage_rows(
                stage_reruns(job, probe=self._probe(job), tool=self._tool_state(job)),
                reader_matches=job.get("native_tool") == self._tool_identity(),
            ),
        }
        with self.mutex:
            self._plans[identifier] = (key, time.monotonic(), payload)
        return payload

    def artifact(self, identifier, name):
        job = self.snapshot(identifier)
        diagnostics = job.get("failure_diagnostics")
        if isinstance(diagnostics, dict) and diagnostics.get("path") == "diagnostic.json":
            from ..structural_report import is_sealed_diagnostic_member

            root = self.config["state_root"] / "diagnostics" / identifier
            manifest = json.loads(confined(root, "diagnostic.json").read_text(encoding="utf-8"))
            allowed: dict[str, str] = {"diagnostic.json": diagnostics.get("sha256")}
            receipt_entry = manifest.get("receipt") if isinstance(manifest, dict) else None
            if isinstance(receipt_entry, dict) and receipt_entry.get("path") == "receipt.json":
                allowed["receipt.json"] = receipt_entry.get("sha256")
            files = manifest.get("files") if isinstance(manifest, dict) else None
            if isinstance(files, dict):
                for relative, checksum in files.items():
                    if (
                        isinstance(relative, str)
                        and isinstance(checksum, str)
                        and is_sealed_diagnostic_member(relative)
                    ):
                        allowed[f"evidence/{relative}"] = checksum
            expected = allowed.get(name)
            if isinstance(expected, str):
                path = confined(root, name)
                stream = path.open("rb")
                try:
                    if hashlib.file_digest(stream, "sha256").hexdigest() != expected:
                        raise RequestError("Failure diagnostics differ from their recorded digest", 409)
                    stream.seek(0)
                    return stream, os.fstat(stream.fileno()).st_size
                except BaseException:
                    stream.close()
                    raise
        record = job.get("native_record")
        if isinstance(record, dict) and name == record.get("name"):
            path = confined(self.config["state_root"] / "native_records" / identifier, name)
            stream = path.open("rb")
            try:
                if hashlib.file_digest(stream, "sha256").hexdigest() != record.get("sha256"):
                    raise RequestError("Raw record artifact differs from its bound digest", 409)
                stream.seek(0)
                return stream, os.fstat(stream.fileno()).st_size
            except BaseException:
                stream.close()
                raise
        archive = job.get("capture_archive") if isinstance(job.get("capture_archive"), dict) else None
        if job.get("status") == "native_complete" and archive:
            if name == archive.get("name"):
                expected = archive.get("sha256")
            elif name == CAPTURE_MANIFEST_NAME:
                expected = archive.get("manifest_sha256")
            else:
                raise RequestError("Only the sealed native capture transfer is available for this run", 404)
            path = confined(self.config["output_root"] / identifier, name)
            stream = path.open("rb")
            try:
                if hashlib.file_digest(stream, "sha256").hexdigest() != expected:
                    raise RequestError("Transfer artifact differs from its bound digest", 409)
                stream.seek(0)
                return stream, os.fstat(stream.fileno()).st_size
            except BaseException:
                stream.close()
                raise
        raise RequestError("Only sealed native records and capture transfers are available", 404)

    def create(self, request):
        _require(isinstance(request, dict), "Expected a JSON object")
        identifier = _job_id(request.get("run_id"))
        with self.mutex:
            if identifier in self.jobs:
                if self.jobs[identifier]["request"] != request:
                    raise RequestError("run_id is already bound to a different request", 409)
                return self.snapshot(identifier), False
            files = self.validate(request)
            spec = _resume_spec(request)
            tool = self._tool_identity()
            _require(
                isinstance(tool, dict) and _SHA.fullmatch(str(tool.get("source_sha256", ""))),
                "Native selection requires a pinned source identity",
            )
            job = {
                "schema_version": JOB_SCHEMA,
                "pipeline_id": PIPELINE_ID,
                "run_id": identifier,
                "request": copy.deepcopy(request),
                "main_assembly": request["main_assembly"],
                "status": "selecting",
                "events": [],
                "result": None,
                "error": None,
                "native_tool": tool,
                "tool": tool,
                "created_at": datetime.now(UTC).isoformat(),
            }
            if spec is not None:
                if spec["from_stage"] not in NATIVE_STAGES:
                    raise _refusal("linux_owned", "Portable reruns belong to the Linux host")
                parent = self.jobs.get(spec["parent_run"])
                _require(parent is not None and parent["run_id"] != identifier, "The parent native run is unknown")
                _require(parent.get("native_tool") == tool, "Native reader changed; start a fresh source selection")
                active = self._active_attempt(spec["parent_run"])
                if active is not None:
                    raise _refusal(
                        "already_active", "The selected run already has an active linked attempt", active_run_id=active
                    )
                plan = start_plan(parent, spec["from_stage"], probe=self._probe(parent), tool=self._tool_state(parent))
                if not plan["accepted"]:
                    raise _refusal(plan["reason"], plan["reason_zh"], earliest=plan["earliest_required"])
                _require(
                    all(
                        parent["request"].get(key) == request[key]
                        for key in ("upload_files", "upload_sha256", "main_assembly")
                    ),
                    "A linked attempt must retain its parent's exact upload and selected assembly",
                )
                _require((parent.get("selection") or {}).get("state") == "sealed", "Parent selection is incomplete")
                _require(
                    parent.get("main_assembly") == request["main_assembly"]
                    and parent["selection"]["manifest"]["main_assembly"] == request["main_assembly"],
                    "The parent's effective main assembly differs from its sealed selection",
                )
                job.update(self._inherited_metadata(parent, spec["from_stage"]))
                job.update(
                    source_run_id=parent.get("source_run_id") or parent["run_id"],
                    selection=copy.deepcopy(parent["selection"]),
                    handoff_sha256=parent["handoff_sha256"],
                    package_files=copy.deepcopy(parent["package_files"]),
                    events=self._reuse_events(parent, spec["from_stage"]),
                    status="queued",
                )
            else:
                session = StagedSelection(
                    self._source_dir(job), files, job["main_assembly"], reader_sha256=tool["source_sha256"]
                )
                self.selections[identifier] = session
                job["selection"] = session.snapshot()
            self.selection_locks[identifier] = threading.RLock()
            self._save(job)
            self.jobs[identifier] = job
            if spec is not None:
                self.queue.put(identifier)
            return self.snapshot(identifier), True

    def _selection(self, identifier):
        identifier = _job_id(identifier)
        self.snapshot(identifier)
        session = self.selections.get(identifier)
        if session is None:
            raise RequestError("This run has no active source selection", 409)
        return session

    def submit_files(self, identifier, archive):
        session = self._selection(identifier)
        with self.selection_locks[identifier]:
            with self.mutex:
                _require(
                    self.jobs[identifier]["status"] not in {"failed", "native_complete", "passed"},
                    "The native run is already terminal",
                )
            accepted = session.accept_batch(archive)
            with self.condition:
                job = self.jobs[identifier]
                job["selection"] = session.snapshot()
                if accepted and not job.get("staging_worker_started"):
                    job["staging_worker_started"] = True
                    self.queue.put(identifier)
                self._save(job)
                self.condition.notify_all()
        return self.snapshot(identifier)

    def _await_client(self, identifier, predicate, *, timeout=600):
        deadline = time.monotonic() + timeout
        with self.condition:
            while True:
                if self._stopping:
                    raise PipelineError("Endpoint closed while waiting for the Linux continuation")
                if self.jobs[identifier]["status"] == "failed":
                    raise PipelineError(self.jobs[identifier]["error"])
                if predicate(self.jobs[identifier]):
                    return
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise PipelineError("Timed out waiting for the bound Linux continuation")
                self.condition.wait(timeout=min(remaining, 10))

    def _default_selection_reader(self, identifier):
        """The selection reader shares the sealed backend's ownership ledger.

        Selection (closure admission) and capture both launch SolidWorks on
        this endpoint, so both must record into one ledger: otherwise a
        corpse left by one path is unrecorded for the other path's sweep,
        and the liveness semantics of the two paths diverge.
        """

        from ..sources.solidworks.native_selection import NativeSelectionReader

        return NativeSelectionReader(
            reader_sha256=self.jobs[identifier]["native_tool"]["source_sha256"],
            cad_ownership_file=self.cad_ownership_file,
        )

    def _select_native(self, identifier):
        """Resolve the fully staged folder once, then retain its proven native closure."""
        session = self.selections[identifier]
        factory = self.selection_reader_factory
        if factory is None:
            def factory():
                return self._default_selection_reader(identifier)

        _require(session.state == "reading", "The complete uploaded folder must be staged before native resolution")
        _require(
            self._tool_identity() == self.jobs[identifier]["native_tool"],
            "Native reader identity changed before closure resolution",
        )
        reader = factory()
        evidence = reader.observe(session.stage, session.main_assembly, session.upload_files)
        # Close the native session before pruning excluded files. The admitted
        # source keeps the same absolute paths the resolver actually reported.
        with self.selection_locks[identifier], self.condition:
            job = self.jobs[identifier]
            _require(not self._stopping and job["status"] == "selecting", "Native staging is no longer active")
            _require(self._tool_identity() == job["native_tool"], "Native reader identity changed during resolution")
            manifest = session.seal_closure(evidence)
            job.update(
                selection=session.snapshot(),
                handoff_sha256=manifest["handoff_sha256"],
                package_files=manifest["files"],
                status="running",
            )
            self._save(job)
            self.condition.notify_all()
        return session.stage

    def _record_and_await_definition(self, identifier, record):
        """Seal native observations, then wait for Linux's derived capture inputs."""
        from .discovery_transfer import RAW_RECORD_ARCHIVE, seal_raw_record_bundle

        job = self.jobs[identifier]
        _require(
            package_inventory(self._source_dir(job)) == job["package_files"], "Native reading changed selected CAD"
        )
        _require(self._tool_identity() == job["native_tool"], "Native tool identity changed during discovery")
        directory = self.config["state_root"] / "native_records" / identifier
        directory.mkdir(parents=True, exist_ok=False)
        sealed = seal_raw_record_bundle(
            record,
            directory / RAW_RECORD_ARCHIVE,
            run_id=identifier,
            selection=job["selection"]["manifest"],
            native_tool=job["native_tool"],
        )
        artifact = sealed["artifact"]
        with self.condition:
            _require(not self._stopping, "Endpoint is closing")
            job.update(
                status="awaiting_definition",
                native_record={
                    "name": artifact["name"],
                    "sha256": artifact["sha256"],
                    "size": artifact["bytes"],
                    "manifest_sha256": digest(sealed["manifest"]),
                    "manifest": sealed["manifest"],
                },
            )
            self._clear_activity(job)
            self._save(job)
            self.condition.notify_all()
        self._await_client(identifier, lambda current: identifier in self.prepared_packages)
        with self.mutex:
            return self.prepared_packages.pop(identifier)

    def submit_definition(self, identifier, archive, prepared_sha256):
        """Admit generated text beside the selected CAD without deriving on Windows."""
        from .discovery_transfer import admit_raw_record_bundle, stage_capture_request

        self.snapshot(identifier)
        _require(
            isinstance(prepared_sha256, str) and _SHA.fullmatch(prepared_sha256),
            "X-Prepared-SHA256 must bind the Linux prepared inventory",
        )
        archive_sha256 = file_digest(archive)
        requested = {"sha256": archive_sha256, "prepared_sha256": prepared_sha256}
        lock = self.selection_locks.get(identifier)
        _require(lock is not None, "This run has no active definition exchange")
        with lock:
            job = self.snapshot(identifier)
            previous = job.get("capture_request")
            if previous is not None:
                if previous != requested:
                    raise RequestError("This run already accepted a different capture definition", 409)
                return job
            if job["status"] != "awaiting_definition":
                raise RequestError("The native run is not awaiting a Linux definition", 409)
            source = self._source_dir(job)
            files = package_inventory(source)
            selection = job["selection"]["manifest"]
            verify_selection(
                selection, job["request"]["upload_files"], files, reader_sha256=job["native_tool"]["source_sha256"]
            )
            _require(files == job["package_files"], "Selected CAD changed before definition admission")
            metadata = job["native_record"]
            admission = admit_raw_record_bundle(
                confined(self.config["state_root"] / "native_records" / identifier, metadata["name"]),
                expected_run_id=identifier,
                expected_selection=selection,
                expected_native_tool=job["native_tool"],
                expected_archive_sha256=metadata["sha256"],
            )
            destination = self.config["state_root"] / "prepared" / identifier
            destination.mkdir(parents=True, exist_ok=False)
            try:
                for name in files:
                    target = confined(destination, name, exists=False)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(confined(source, name), target)
                _require(package_inventory(destination) == files, "Selected CAD changed while preparing capture")
                prepared = stage_capture_request(
                    archive,
                    destination,
                    expected_run_id=identifier,
                    expected_selection=selection,
                    expected_record=admission.record,
                    expected_native_tool=job["native_tool"],
                    expected_prepared_sha256=prepared_sha256,
                    expected_archive_sha256=archive_sha256,
                )
                with self.condition:
                    current = self.jobs[identifier]
                    _require(
                        not self._stopping and current["status"] == "awaiting_definition",
                        "The definition exchange is no longer active",
                    )
                    current.update(capture_request=requested, status="running")
                    self._save(current)
                    self.prepared_packages[identifier] = prepared
                    self.condition.notify_all()
            except BaseException:
                shutil.rmtree(destination)
                raise
        return self.snapshot(identifier)

    def abort(self, identifier, request):
        _require(
            isinstance(request, dict) and set(request) == {"error", "error_code", "detail"},
            "Expected the Linux derivation error, error_code and detail",
        )
        _require(isinstance(request["error"], str) and 0 < len(request["error"]) <= 4096, "Invalid derivation error")
        _require(isinstance(request["error_code"], str) and 0 < len(request["error_code"]) <= 128, "Invalid error_code")
        _require(isinstance(request["detail"], dict), "Derivation detail must be an object")
        self.snapshot(identifier)
        with self.condition:
            job = self.jobs[identifier]
            if job.get("client_failure") == request:
                return self.snapshot(identifier)
            if job["status"] != "awaiting_definition":
                raise RequestError("Only a pending Linux derivation can be aborted", 409)
            # Already atomic (one condition block: flip -> clear -> seal -> save);
            # deliberately no completed_at - the derivation aborted before any
            # native completion (definition-endpoint tests pin this shape).
            job.update(copy.deepcopy(request), status="failed", client_failure=copy.deepcopy(request))
            discovery = request["detail"].get("discovery")
            if isinstance(discovery, dict) and discovery.get("passed") is False:
                job["discovery"] = copy.deepcopy(discovery)
            self._clear_activity(job)
            self._seal_failure_diagnostics(job)
            self._save(job)
            self.condition.notify_all()
        return self.snapshot(identifier)

    def _release_refusal(self, reason, message, **detail):
        """One uniform precondition refusal: 409 plus the stable code and reason."""

        return RequestError(
            message,
            status=409,
            payload={"error_code": "cache_release_refused", "reason": reason, "detail": detail},
        )

    def _release_receipt(self, *, status, operation_id, params_sha256, import_block, released, released_at):
        """The immutable release receipt, validated by its frozen record owner."""

        receipt = {
            "schema_version": CACHE_RELEASE_RECEIPT_SCHEMA,
            "status": status,
            "operation_id": operation_id,
            "params_sha256": params_sha256,
            "import": import_block,
            "released": dict(released),
            "retained": ["terminal_state", "import_confirmation"],
            "released_at": released_at,
            "identity": _native_release_identity(),
            "receipt_sha256": "",
        }
        receipt["receipt_sha256"] = cache_release_receipt_digest(receipt)
        validate_cache_release_receipt(receipt)
        return receipt

    def release_cache(self, identifier, request):
        """Release one terminal run's staged imported cache (IF-10).

        Every precondition is checked before anything is deleted: the run is
        terminal, no active job references the cache, the caller's closed
        imported inventory equals the staged bytes exactly, and the cache is
        confined to the package root.  The release journal makes replays and
        interrupted calls safe: one operation id with the same parameters
        returns its original receipt, an interrupted release resumes on the
        next call, and a refusal never partially deletes.
        """

        identifier = _job_id(identifier)
        validate_cache_release_request(request)
        import_block = request["import"]
        if import_block["production"]["run_id"] != identifier:
            raise self._release_refusal(
                "production_run_mismatch",
                "The import block names another production run",
                run_id=import_block["production"]["run_id"],
            )
        own = _native_release_identity()
        if request["binding"]["release"] != {key: own[key] for key in ("version", "source_commit", "source_sha256")}:
            raise self._release_refusal(
                "release_identity_mismatch",
                "The binding release differs from the installed endpoint identity",
            )
        if request["binding"]["contract"] != {"schema_version": CONTRACT["schema_version"], "digest": CONTRACT_SHA256}:
            raise self._release_refusal("contract_mismatch", "The binding contract differs from the staged contract")
        params_sha256 = cache_release_params_digest(import_block)
        with self.mutex:
            job = self.jobs.get(identifier)
            if job is None:
                raise RequestError("Unknown run_id", 404)
            if job.get("status") not in _HEALTH_TERMINAL_JOB_STATES:
                raise self._release_refusal(
                    "job_not_terminal",
                    "The native run is not terminal; its imports may still be pending",
                    status=job.get("status"),
                )
            owner = _job_id(job.get("source_run_id") or identifier)
            for other in self.jobs.values():
                if other.get("run_id") == identifier:
                    continue
                if (
                    _job_id(other.get("source_run_id") or other.get("run_id")) == owner
                    and other.get("status") not in _HEALTH_TERMINAL_JOB_STATES
                ):
                    raise self._release_refusal(
                        "cache_in_use",
                        "Another active run references this staged cache",
                        run_id=other.get("run_id"),
                    )
            if job.get("activity") is not None:
                raise self._release_refusal("resources_not_released", "The run still reports live native activity")
            try:
                cache_dir = confined(self.config["package_root"], CACHE_RELEASE_SELECTED + "/" + owner, exists=False)
            except PipelineError as error:
                raise self._release_refusal("path_outside_cache_root", str(error)) from error
            if cache_dir.is_symlink() or cache_dir.is_junction():
                raise self._release_refusal("path_outside_cache_root", "The staged cache is a link")
            journal_path = self.config["state_root"] / "releases" / (owner + ".json")
            journal = read_data(journal_path) if journal_path.is_file() else None
            if journal is not None:
                if journal.get("operation_id") != request["operation_id"]:
                    if journal.get("state") != "done":
                        raise self._release_refusal(
                            "release_in_progress",
                            "Another release operation is unfinished for this cache",
                            operation_id=journal.get("operation_id"),
                        )
                    return self._release_receipt(
                        status="already_released",
                        operation_id=request["operation_id"],
                        params_sha256=params_sha256,
                        import_block=import_block,
                        released={"files": 0, "bytes": 0},
                        released_at=journal["started_at"],
                    )
                if journal.get("params_sha256") != params_sha256:
                    raise RequestError(
                        "operation_id is already bound to a different request",
                        409,
                        payload={"error_code": "cache_release_conflict"},
                    )
                if journal.get("state") == "done":
                    return journal["receipt"]
            if journal is None:
                if not cache_dir.is_dir():
                    raise self._release_refusal(
                        "cache_absent", "The staged cache is absent and no release is on record"
                    )
                try:
                    staged = _staged_manifest(cache_dir)
                except PipelineError as error:
                    raise self._release_refusal("path_outside_cache_root", str(error)) from error
                if not staged:
                    raise self._release_refusal("cache_empty", "The staged cache carries no releasable files")
                if staged != import_block["confirmation"]["imported"]:
                    raise self._release_refusal(
                        "imported_manifest_mismatch",
                        "The imported inventory differs from the staged cache bytes",
                        staged=[entry["path"] for entry in staged][:20],
                        imported=[entry["path"] for entry in import_block["confirmation"]["imported"]][:20],
                        staged_sha256=digest(staged),
                        imported_sha256=digest(import_block["confirmation"]["imported"]),
                    )
                journal = {
                    "schema_version": CACHE_RELEASE_JOURNAL_SCHEMA,
                    "run_id": identifier,
                    "owner": owner,
                    "operation_id": request["operation_id"],
                    "params_sha256": params_sha256,
                    "state": "started",
                    "started_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                    "released": {"files": len(staged), "bytes": sum(entry["bytes"] for entry in staged)},
                    "manifest": staged,
                    "import": import_block,
                    "receipt": None,
                }
                journal_path.parent.mkdir(parents=True, exist_ok=True)
                write_json(journal_path, journal)
            else:
                # Resumed after an interrupted call: every remaining file must
                # still be a recorded member, byte for byte.
                recorded = {entry["path"]: entry for entry in journal.get("manifest") or []}
                if cache_dir.is_dir():
                    try:
                        remaining = _staged_manifest(cache_dir)
                    except PipelineError as error:
                        raise self._release_refusal("path_outside_cache_root", str(error)) from error
                    if any(recorded.get(entry["path"]) != entry for entry in remaining):
                        raise self._release_refusal(
                            "cache_changed_since_release",
                            "The staged cache changed after the release began",
                        )
        # The single deletion: only the confined staged root, only after every
        # precondition passed; a failure here leaves the journal started for a
        # safe resume and never reports a partial release as complete.
        if cache_dir.exists():
            try:
                shutil.rmtree(cache_dir)
            except OSError as error:
                raise RequestError(
                    "The cache release could not complete; retry the same operation",
                    500,
                    payload={
                        "error_code": "cache_release_incomplete",
                        "detail": {"error": f"{type(error).__name__}: {error}"[:300]},
                    },
                ) from error
        if cache_dir.exists():
            raise RequestError(
                "The cache release could not complete; retry the same operation",
                500,
                payload={"error_code": "cache_release_incomplete"},
            )
        with self.mutex:
            receipt = self._release_receipt(
                status="released",
                operation_id=journal["operation_id"],
                params_sha256=journal["params_sha256"],
                import_block=journal["import"],
                released=journal["released"],
                released_at=journal["started_at"],
            )
            journal.update(state="done", receipt=receipt)
            write_json(journal_path, journal)
            return receipt

    def snapshot(self, identifier):
        identifier = _job_id(identifier)
        with self.mutex:
            if identifier not in self.jobs:
                raise RequestError("Unknown run_id", 404)
            job = json.loads(json.dumps(self.jobs[identifier]))
            job["stages"] = stage_view(job)
            return job

    def _event(self, identifier, event):
        with self.mutex:
            job = self.jobs[identifier]
            entry = dict(event)
            entry.setdefault("at", datetime.now(UTC).isoformat())
            job["events"].append(entry)
            if isinstance(entry.get("discovery"), dict):
                job["discovery"] = entry["discovery"]
            self._retire_activity(job, entry)
            self._save(job)

    @staticmethod
    def _retire_activity(job, event) -> None:
        """A stage transition retires telemetry that belongs to another or an ended stage.

        Without this, a stale discovery action would keep rendering as busy while
        capture (which reports nothing) runs, until the job turns terminal.  Check
        records describe a check rather than a transition and never retire.
        """

        if not isinstance(event, dict) or "check" in event:
            return
        activity = job.get("activity")
        if not isinstance(activity, dict):
            return
        stage, state = event.get("stage"), event.get("state")
        if not isinstance(stage, str) or state not in {"running", "completed", "failed"}:
            return
        if stage == activity.get("phase") and state == "running":
            return
        job["activity_final"] = activity
        job["activity"] = None

    def _activity(self, identifier, record):
        """Persist one live-activity update; malformed or stale records are ignored.

        Activity is observation only: it never touches events, checks, results
        or the job status, and a broken emitter can never fail a run.  Every
        accepted update is saved immediately so the poll API sees it while the
        native job is still blocking.
        """

        normalized = normalize_activity(record)
        if normalized is None:
            return
        with self.mutex:
            job = self.jobs.get(identifier)
            if job is None:
                return
            result = merge_activity(job.get("activity"), normalized, at=datetime.now(UTC).isoformat())
            if result is None:
                return
            activity, history_entry = result
            job["activity"] = activity
            if history_entry is not None:
                history = list(job.get("activity_history") or [])
                history.append(history_entry)
                job["activity_history"] = history[-ACTIVITY_HISTORY_LIMIT:]
            self._save(job)

    @staticmethod
    def _clear_activity(job) -> None:
        """Terminal statuses retire the live record but keep the final observation."""

        if not isinstance(job, dict):
            return
        activity = job.get("activity")
        if isinstance(activity, dict):
            job["activity_final"] = activity
        job["activity"] = None

    def _prepare_native(self, identifier, frozen, selection=None):
        from ..steps import discover_structure
        from ..sources.solidworks.discovery import DiscoverySettings, read_native_record

        job = self.jobs[identifier]
        self._event(identifier, {"stage": "discover", "state": "running"})
        settings = DiscoverySettings(main_assembly=selection)

        def activity(item):
            self._activity(identifier, item)

        if self.native_reader is None:
            backend = self.backend_factory(frozen, job["package_files"])
            record = read_native_record(frozen, settings, backend=backend, on_activity=activity)
        else:
            record = self.native_reader(frozen, settings, on_activity=activity)
        record = {**record, "source_selection": copy.deepcopy(job["selection"]["manifest"])}
        prepared = self._record_and_await_definition(identifier, record)
        package, target, prepared, files = discover_structure(
            frozen,
            self.config["state_root"] / "prepared" / identifier,
            identifier,
            expected_digest=job["handoff_sha256"],
            expected_files=job["package_files"],
            main_assembly=selection,
            configuration={},
            targets=self.config["targets"],
            prepared=prepared,
            on_event=lambda item: self._event(identifier, item),
            on_activity=lambda item: self._activity(identifier, item),
        )
        with self.mutex:
            job.update(
                hardware_id=prepared.hardware_id,
                revision=prepared.revision,
                repository_slug=(target or {}).get("repository_slug"),
                repository_base=(target or {}).get("base"),
                prepared_files=files,
                prepared_dir=str(package),
            )
            self._save(job)
        return package, target

    def _sealed_backend(self, package, files):
        from ..runtime import native_readiness
        from ..sources.solidworks.native import SolidWorksBackend

        native_readiness()
        backend = SolidWorksBackend(cad_ownership_file=self.cad_ownership_file)
        backend.configure_staged_search(package)
        backend.seal_documents([confined(package, name) for name in files])
        return backend

    def _work(self):
        while True:
            identifier = self.queue.get()
            if identifier is None:
                self.queue.task_done()
                return
            job = self.jobs[identifier]
            spec = None
            result = _UNSET
            terminal = None
            terminal_guarded = False
            try:
                spec = _resume_spec(job["request"])
                if spec is not None and spec["from_stage"] not in NATIVE_STAGES:
                    raise RequestError(
                        "The requested stage is owned by the Linux delivery flow",
                        409,
                        payload={
                            "reason": "linux_owned",
                            "reason_zh": "该阶段已移至 Linux 执行；请通过交付流程重新运行",
                        },
                    )
                # A replayed linked run must name a retained parent before any deeper
                # validator runs: a removed parent record must not surface as a bare
                # KeyError from the checkpoint lookup below.
                _require(spec is None or spec["parent_run"] in self.jobs, "The parent native run is unknown")
                with self.mutex:
                    _require(not self._stopping, "Endpoint is closing")
                    job["started_at"] = datetime.now(UTC).isoformat()
                    self._save(job)
                if spec is None:
                    self._select_native(identifier)
                with self.mutex:
                    job["status"] = "running"
                    self._save(job)
                from ..steps import freeze_inputs

                package = self._source_dir(job)
                # The retained selected tree must still exist; without it the inventory
                # validators would report an empty selection instead of the real cause.
                _require(
                    package.is_dir(),
                    "The retained selected source is no longer available; start a new run",
                )
                _require(
                    job.get("main_assembly") == job["request"]["main_assembly"]
                    and job["selection"]["manifest"]["main_assembly"] == job["request"]["main_assembly"],
                    "The queued run's effective main assembly differs from its sealed selection",
                )
                verify_selection(
                    job["selection"]["manifest"],
                    job["request"]["upload_files"],
                    package_inventory(package),
                    reader_sha256=job["native_tool"]["source_sha256"],
                )
                _require(self._tool_identity() == job["native_tool"], "Native tool changed after source selection")
                from_stage = spec["from_stage"] if spec is not None else None
                selection = job.get("main_assembly")
                if selection is None and spec is not None:
                    parent = self.jobs.get(spec["parent_run"]) or {}
                    selection = parent.get("main_assembly") if isinstance(parent, dict) else None
                resume_kwargs = {"resume": spec} if spec is not None else {}
                if spec is not None and from_stage != "freeze":
                    # Revalidate the retained checkpoints at execution time; enqueue-time
                    # checks are advisory because registries and pulls may change meanwhile.
                    parent = self.jobs[spec["parent_run"]]
                    plan = start_plan(parent, from_stage, probe=self._probe(parent), tool=self._tool_state(parent))
                    if not plan["accepted"]:
                        failure = RequestError(
                            plan["reason_zh"],
                            409,
                            payload={
                                "reason": plan["reason"],
                                "reason_zh": plan["reason_zh"],
                                "earliest_required": plan["earliest_required"],
                            },
                        )
                        failure.code = plan["reason"]
                        raise failure
                if from_stage not in {"discover", "capture", "generate", "verify", "publish"}:
                    # A fresh job or a freeze restart re-admits the retained upload.
                    freeze_inputs(
                        package,
                        job["handoff_sha256"],
                        job["package_files"],
                        main_assembly=selection,
                        on_event=lambda item, identifier=identifier: self._event(identifier, item),
                    )
                if from_stage in {None, "freeze", "discover"}:
                    package, target = self._prepare_native(identifier, package, selection)
                else:
                    # A capture rerun reuses the parent's retained freeze+discover
                    # checkpoints; the linked-run gate already refused the
                    # Linux-owned stages before enqueue.
                    package = self._prepared_dir(job)
                    _require(package is not None, "The retained discovery checkpoint is no longer available")
                    target = self.config["targets"].get(job.get("hardware_id"))
                selection = job.get("main_assembly")
                _require(
                    job.get("repository_slug") == (target or {}).get("repository_slug")
                    and job.get("repository_base") == (target or {}).get("base"),
                    "Persisted publication routing changed; review it and use a new run_id",
                )
                result = self.runner(
                    package,
                    self.config["output_root"] / identifier,
                    backend=self.backend_factory(package, job["package_files"]),
                    run_id=identifier,
                    main_assembly=selection,
                    on_event=lambda event, identifier=identifier: self._event(identifier, event),
                    on_activity=lambda item, identifier=identifier: self._activity(identifier, item),
                    prior_events=list(job["events"]),
                    expected_inputs=job["prepared_files"],
                    handoff_sha256=job["handoff_sha256"],
                    **resume_kwargs,
                )
                archive = result.get("capture_archive") if isinstance(result.get("capture_archive"), dict) else None
                if result.get("native_complete") is True and result.get("passed") is False and archive:
                    verified = self._verified_capture_archive(identifier, archive)
                    terminal = {
                        "result": result,
                        "status": "native_complete",
                        "error": None,
                        "capture_archive": verified,
                    }
                elif result.get("native_complete") is True or result.get("passed") is True:
                    terminal = {
                        "result": result,
                        "status": "failed",
                        "error": "Native result shape is not a sealed capture transfer; the run cannot be qualified",
                    }
                    if result.get("error_code"):
                        terminal["error_code"] = str(result["error_code"])
                else:
                    # A genuinely failed native run keeps its own diagnostics.
                    terminal = {
                        "result": result,
                        "status": "failed",
                        "error": result.get("error") or "Native capture failed",
                    }
                    if result.get("error_code"):
                        terminal["error_code"] = str(result["error_code"])
                    for key in ("detail", "diagnostic_path"):
                        if result.get(key) is not None:
                            terminal[key] = result[key]
            except Exception as error:
                # Record the failure without publishing it: the terminal record
                # (status, error, completion timestamp, terminal event, sealed
                # diagnostics, journal save) is published in ONE critical
                # section below, so a snapshot can never observe a terminal
                # status that is missing its completion record.
                terminal = {"status": "failed", "error": f"{type(error).__name__}: {error}"}
                if result is not _UNSET:
                    terminal["result"] = result
                code = getattr(error, "code", None)
                if code:
                    terminal["error_code"] = str(code)
                if getattr(error, "detail", None) is not None:
                    terminal["detail"] = error.detail
                elif isinstance(getattr(error, "details", None), dict):
                    terminal["detail"] = error.details
                terminal_guarded = True
            finally:
                with self.mutex:
                    # The terminal publication is atomic for observers: flip,
                    # clear, event, timestamp, seal and save under one lock.
                    if terminal is not None and (not terminal_guarded or job["status"] != "failed"):
                        job.update(terminal)
                        self._clear_activity(job)
                    if job["status"] == "failed" and (not job["events"] or job["events"][-1]["state"] != "failed"):
                        phase = (job["events"][-1] if job["events"] else {}).get("stage", "freeze")
                        self._event(identifier, {"stage": phase, "state": "failed", "error": job.get("error")})
                    job["completed_at"] = datetime.now(UTC).isoformat()
                    if job["status"] == "failed":
                        # Seal exactly once, after the final failure event and the completion
                        # timestamp, so the sealed receipt includes both.  A successful job
                        # seals nothing; restart and abort keep their own lifecycle seals.
                        self._seal_failure_diagnostics(job)
                    self._save(job)
                self.queue.task_done()

    def close(self):
        if self._closed:
            return
        with self.condition:
            self._stopping = True
            self.condition.notify_all()
        self.queue.put(None)
        self.thread.join(timeout=30)
        if self.thread.is_alive():
            raise PipelineError("Native job still runs; endpoint ownership lock retained")
        self._closed = True
        os.close(self.handle)


def handler(jobs, token):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format, *args):
            # HTTP headers and user requests never enter logs, especially tokens.
            return

        def _reply(self, status, data):
            payload = (json.dumps(data, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(payload)
            self.close_connection = True

        def _authorized(self):
            received = self.headers.get("Authorization", "")
            if not hmac.compare_digest(received.encode("utf-8"), ("Bearer " + token).encode("ascii")):
                self._reply(401, {"error": "Unauthorized"})
                return False
            return True

        def _failure(self, error):
            body = {"error": str(error)}
            body.update(getattr(error, "payload", {}) or {})
            code = getattr(error, "code", None)
            if code:
                body.setdefault("error_code", str(code))
            detail = getattr(error, "detail", None)
            if isinstance(detail, dict):
                body.setdefault("detail", detail)
            self._reply(getattr(error, "status", 400), body)

        def do_GET(self):
            if not self._authorized():
                return
            try:
                if self.path == "/health":
                    try:
                        payload = health_payload(jobs)
                    except PipelineError as error:
                        self._reply(500, {"error": str(error), "error_code": "health_identity_unavailable"})
                    else:
                        self._reply(200, payload)
                elif self.path.startswith("/v1/jobs/"):
                    suffix = self.path[len("/v1/jobs/") :]
                    if "/artifacts/" in suffix:
                        identifier, name = suffix.split("/artifacts/", 1)
                        stream, size = jobs.artifact(identifier, unquote(name))
                        with stream:
                            self.send_response(200)
                            self.send_header(
                                "Content-Type",
                                "application/xml" if name.endswith(".urdf") else "application/octet-stream",
                            )
                            self.send_header("Content-Length", str(size))
                            self.send_header("Cache-Control", "private, no-store")
                            self.send_header("X-Content-Type-Options", "nosniff")
                            self.send_header("Connection", "close")
                            self.end_headers()
                            import shutil

                            shutil.copyfileobj(stream, self.wfile, length=1024 * 1024)
                            self.close_connection = True
                    elif suffix.endswith("/reruns"):
                        self._reply(200, jobs.plan(suffix[: -len("/reruns")]))
                    else:
                        self._reply(200, jobs.snapshot(suffix))
                else:
                    self._reply(404, {"error": "Unknown API route"})
            except (PipelineError, OSError, ValueError, TypeError) as error:
                self._failure(error)

        def do_POST(self):
            if not self._authorized():
                return
            match = re.fullmatch(r"/v1/jobs/([0-9a-f-]+)/(files|definition|abort|cache-release)", self.path)
            if self.path != "/v1/jobs" and match is None:
                self._reply(404, {"error": "Unknown API route"})
                return
            try:
                identifier, operation = match.groups() if match else (None, None)
                if identifier is not None:
                    _job_id(identifier)
                if operation in {"files", "definition"}:
                    from .discovery_transfer import MAX_REQUEST_BYTES

                    _require(
                        self.headers.get("Content-Type", "").split(";")[0] == "application/zip",
                        "Content-Type must be application/zip",
                    )
                    size = int(self.headers.get("Content-Length", "0"))
                    limit = MAX_HANDOFF_BYTES if operation == "files" else MAX_REQUEST_BYTES
                    _require(0 < size <= limit and not self.headers.get("Transfer-Encoding"), "Invalid archive size")
                    self.connection.settimeout(60)
                    with tempfile.TemporaryDirectory(prefix=".transport-", dir=jobs.config["state_root"]) as temporary:
                        archive = Path(temporary) / "request.zip"
                        with archive.open("xb") as output:
                            remaining = size
                            while remaining:
                                chunk = self.rfile.read(min(remaining, 1024 * 1024))
                                _require(bool(chunk), "Incomplete archive transport")
                                output.write(chunk)
                                remaining -= len(chunk)
                        if operation == "files":
                            result = jobs.submit_files(identifier, archive)
                        else:
                            result = jobs.submit_definition(identifier, archive, self.headers.get("X-Prepared-SHA256"))
                        self._reply(202, result)
                    return
                _require(
                    self.headers.get("Content-Type", "").split(";")[0] == "application/json",
                    "Content-Type must be application/json",
                )
                size = int(self.headers.get("Content-Length", "0"))
                _require(0 < size <= 8 * 1024**2 and not self.headers.get("Transfer-Encoding"), "Invalid request size")
                self.connection.settimeout(10)
                request = json.loads(self.rfile.read(size))
                if operation == "cache-release":
                    self._reply(200, jobs.release_cache(identifier, request))
                elif operation == "abort":
                    self._reply(200, jobs.abort(identifier, request))
                else:
                    result, created = jobs.create(request)
                    self._reply(202 if created else 200, result)
            except (PipelineError, ValueError, OSError, TypeError) as error:
                self._failure(error)

    return Handler


def serve(config_path):
    _require(sys.platform == "win32", "The native execution endpoint requires Windows with licensed SolidWorks")
    config = read_config(config_path)
    with contextlib.ExitStack() as stack:
        jobs = Jobs(config)
        stack.callback(jobs.close)
        server = ThreadingHTTPServer((config["host"], config["port"]), handler(jobs, config["token"]))
        stack.callback(server.server_close)
        print(
            json.dumps({"pipeline_id": PIPELINE_ID, "endpoint": f"{config['host']}:{config['port']}", "ready": True}),
            flush=True,
        )
        try:
            server.serve_forever(poll_interval=0.5)
        except KeyboardInterrupt:
            return
