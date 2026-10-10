"""Authenticated, persistent, serial Windows execution endpoint for Airflow.

The API freezes native engineering folders and routes their discovered hardware
identity through platform configuration. It cannot execute shell commands or
accept arbitrary output/repository paths. Job identity is
stable across network retries; an interrupted native run fails explicitly and
is never silently replayed after a process restart.

One run may name its delivered assembly explicitly (``main_assembly``); the
endpoint verifies the value against the frozen upload and binds it to the run's
checkpoints, so a linked attempt can never reuse a different selection.
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
from ..sources.snapshot import verify_snapshot
from ..sources.solidworks.revision import package_inventory
from ..runtime import tool_record
from ..stages import ACTIVITY_HISTORY_LIMIT, STAGE_IDS, merge_activity, normalize_activity, stage_view
from .stage_transfer import CAPTURE_MANIFEST as CAPTURE_MANIFEST_NAME
from .recovery import stage_reruns, start_plan
from ..sources.solidworks.handoff import HANDOFF_SCHEMA, MAX_HANDOFF_BYTES, freeze_handoff, import_archive

_SHA = re.compile(r"^[0-9a-f]{64}$")
_HARDWARE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,63}$")
JOB_SCHEMA = "solidworks-to-urdf.job/v1"
CONFIG_SCHEMA = "solidworks-to-urdf.endpoint/v1"

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
            "handoff_roots",
            "output_root",
            "state_root",
            "targets",
            "token_file",
            "host",
            "port",
            "discovery",
        },
        "Unknown endpoint configuration key",
    )
    for key in ("package_root", "output_root", "state_root"):
        config[key] = _root(config[key])
    _require(config["package_root"].is_dir(), "Package root does not exist")
    from ..sources.solidworks.handoff import validate_handoff_roots

    config["handoff_roots"] = validate_handoff_roots(config.get("handoff_roots"))
    settings = config.get("discovery", {})
    _require(
        isinstance(settings, dict) and set(settings) <= {"record_roots", "frozen_names_file"},
        "Unknown native discovery setting",
    )
    records = settings.get("record_roots", [])
    _require(isinstance(records, list), "Native record_roots must be a list")
    settings["record_roots"] = [_root(value) for value in records]
    _require(all(path.is_dir() for path in settings["record_roots"]), "Native record root does not exist")
    if "frozen_names_file" in settings:
        settings["frozen_names_file"] = _root(settings["frozen_names_file"])
        _require(settings["frozen_names_file"].is_file(), "Missing frozen-name registry")
    config["discovery"] = settings
    targets = config["targets"]
    _require(isinstance(targets, dict) and bool(targets), "Configure at least one model repository target")
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
            isinstance(target["base"], str) and target["base"].startswith("feature/"),
            "Model PR base must be feature/<hardware>",
        )
    for index, first in enumerate(roots):
        for second in roots[index + 1 :]:
            _require(
                not first.is_relative_to(second) and not second.is_relative_to(first),
                "Endpoint package, output, state and repository roots must be separate",
            )
    for source in config["handoff_roots"]:
        for managed in roots:
            _require(
                not source.is_relative_to(managed) and not managed.is_relative_to(source),
                "Engineering source roots must be separate from managed inputs, state, outputs and repositories",
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
    config.setdefault("host", "127.0.0.1")
    config.setdefault("port", 8765)
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


class Jobs:
    """One persistent queue, one CAD runner, one endpoint process owner."""

    #: Linked-run plans hash retained checkpoints; the detail route may poll, so
    #: finished plans are memoized briefly. ``create`` always probes afresh.
    PLAN_TTL = 30.0

    def __init__(self, config, *, runner=None, native_preparer=None):
        from ..solidworks import run

        self.config = config
        self.runner = runner or run
        self.native_preparer = native_preparer
        self._plans = {}
        self.directory = config["state_root"] / "jobs"
        self.directory.mkdir(parents=True, exist_ok=True)
        inventory(self.directory)
        self.lock_path = config["state_root"] / ".endpoint.lock"
        self.handle = _owner_lock(self.lock_path)
        self._closed = False
        self.mutex = threading.RLock()
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
                if job["status"] == "running":
                    job.update(
                        status="failed",
                        error="Endpoint restarted during native execution; inspect diagnostics and use a new run_id",
                    )
                    self._clear_activity(job)
                    self._save(job)
                elif job["status"] == "queued":
                    self.queue.put(identifier)
            self.thread = threading.Thread(target=self._work, name="description-cad-queue", daemon=True)
            self.thread.start()
        except BaseException:
            os.close(self.handle)
            raise

    def _save(self, job):
        write_json(self.directory / (job["run_id"] + ".json"), job)

    def _handoff(self, package, identity):
        return {
            "schema_version": HANDOFF_SCHEMA,
            "pipeline_id": PIPELINE_ID,
            "package": package.relative_to(self.config["package_root"]).as_posix(),
            "handoff_sha256": identity["handoff_sha256"],
        }

    def resolve_handoff(self, request):
        _require(isinstance(request, dict) and set(request) == {"handoff_path"}, "Expected a mechanical handoff folder")
        path = request["handoff_path"]
        _require(
            isinstance(path, str) and bool(path.strip()) and not any(ord(c) < 32 for c in path),
            "Mechanical handoff folder is required",
        )
        source = Path(path)
        if not source.is_absolute():
            _require(
                len(self.config["handoff_roots"]) == 1, "Use an absolute engineering path with multiple source roots"
            )
            source = confined(
                self.config["handoff_roots"][0], path.rstrip("/") + "/.handoff-folder", exists=False
            ).parent
        from ..sources.solidworks.handoff import authorize_handoff

        source = authorize_handoff(source, self.config["handoff_roots"])
        package, identity = freeze_handoff(source, self.config["package_root"] / "imports")
        return self._handoff(package, identity)

    def import_handoff(self, archive):
        package, identity = import_archive(archive, self.config["package_root"] / "imports")
        return self._handoff(package, identity)

    def validate(self, request):
        _require(
            isinstance(request, dict)
            and set(request)
            in (
                {"run_id", "package", "handoff_sha256"},
                {"run_id", "package", "handoff_sha256", "resume"},
                {"run_id", "package", "handoff_sha256", "main_assembly"},
                {"run_id", "package", "handoff_sha256", "resume", "main_assembly"},
            ),
            "Expected run_id, package, handoff_sha256, an optional resume and an optional "
            "main_assembly",
        )
        _job_id(request["run_id"])
        _resume_spec(request)
        _main_assembly_spec(request.get("main_assembly"))
        _require(
            isinstance(request["package"], str) and bool(artifact_path_parts(request["package"])),
            "Invalid native package path",
        )
        _require(
            isinstance(request["handoff_sha256"], str) and _SHA.fullmatch(request["handoff_sha256"]) is not None,
            "Invalid native handoff digest",
        )
        package = confined(self.config["package_root"], request["package"] + "/.handoff-folder", exists=False).parent
        from ..sources.solidworks.handoff import describe_handoff

        identity = describe_handoff(package)
        _require(
            identity["handoff_sha256"] == request["handoff_sha256"],
            "Native engineering files changed after selection; start a new run",
        )
        return package

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
    def _recorded_frozen_names(job):
        for event in job.get("events") or []:
            check = event.get("check") if isinstance(event, dict) else None
            if not isinstance(check, dict) or check.get("id") != "discovery.inputs":
                continue
            details = check.get("details") or {}
            value = details.get("frozen_names_sha256")
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

    def _dependency_identity(self):
        """The dependency inputs a discovery run binds: registry snapshot and record roots."""
        settings = self.config.get("discovery", {})
        names = None
        try:
            names = read_data(settings["frozen_names_file"]) if settings.get("frozen_names_file") else {}
        except (PipelineError, OSError, ValueError):
            names = None
        roots = None
        try:
            roots = sorted(str(Path(value).resolve()) for value in settings.get("record_roots", []))
        except (TypeError, ValueError, OSError):
            roots = None
        return {
            "frozen_names_sha256": digest(names) if names is not None else None,
            "record_roots": roots,
        }

    @staticmethod
    def _native_stage_rows(rows):
        """The native endpoint owns freeze/discover/capture; the rest runs on Linux."""
        for row in rows:
            if row.get("stage") not in NATIVE_STAGES:
                row["eligible"] = False
                row["reason"] = "linux_owned"
                row["reason_zh"] = "该阶段已移至 Linux 执行；请通过交付流程重新运行"
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
            package = confined(
                self.config["package_root"], str(request.get("package", "")) + "/.handoff-folder", exists=False
            ).parent
            if package.is_dir():
                from ..sources.solidworks.handoff import describe_handoff

                identity = describe_handoff(package)
                files = package_inventory(package)
                probe["source"] = (
                    "ok"
                    if identity.get("handoff_sha256") == request.get("handoff_sha256")
                    and files == job.get("package_files")
                    else "changed"
                )
        except (PipelineError, OSError, ValueError, TypeError):
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
        recorded = job.get("dependency") if isinstance(job.get("dependency"), dict) else None
        current = self._dependency_identity()
        if recorded is None:
            # Legacy jobs predate the recorded identity: only the registry snapshot can be
            # compared; the records their discovery embedded stay pinned by the prepared package.
            recorded = {"frozen_names_sha256": self._recorded_frozen_names(job), "record_roots": None}
        if recorded.get("frozen_names_sha256") is None or current.get("frozen_names_sha256") is None:
            probe["dependency"] = "unverifiable"
        elif recorded["frozen_names_sha256"] != current["frozen_names_sha256"] or (
            recorded.get("record_roots") is not None and recorded["record_roots"] != current.get("record_roots")
        ):
            probe["dependency"] = "changed"
        elif recorded.get("record_roots") is None and current.get("record_roots"):
            probe["dependency"] = "unverifiable"
        else:
            probe["dependency"] = "ok"
        target = self.config["targets"].get(job.get("hardware_id"))
        if (
            isinstance(target, dict)
            and isinstance(job.get("repository_slug"), str)
            and isinstance(job.get("repository_base"), str)
        ):
            probe["target"] = (
                "ok"
                if target.get("repository_slug") == job["repository_slug"]
                and target["base"] == job["repository_base"]
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
                and other.get("status") in {"queued", "running"}
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
                stage_reruns(job, probe=self._probe(job), tool=self._tool_state(job))
            ),
        }
        with self.mutex:
            self._plans[identifier] = (key, time.monotonic(), payload)
        return payload

    def preview(self, identifier):
        """Expose only artifact bytes bound to a successful independently verified run."""
        job = self.snapshot(identifier)
        _require(job["status"] in {"passed", "failed"}, "URDF preview is available after verification")
        output = self.config["output_root"] / identifier
        files = subject_inventory(output)
        result = job.get("result") or {}
        quality = result.get("quality") or {}
        _require(
            quality.get("passed") is True and quality.get("subject_sha256") == result.get("subject_sha256"),
            "Unverified models cannot be previewed",
        )
        _require(
            any(
                check.get("id") == "source.native_discovery" and check.get("passed") is True
                for check in quality.get("checks", [])
            ),
            "Native-derived models require independent discovery verification before preview",
        )
        _require(digest(files) == result.get("subject_sha256"), "Delivered model differs from its verified subject")
        assets = {name: checksum for name, checksum in files.items() if name.startswith(("urdf/", "meshes/"))}
        _require("urdf/robot.urdf" in assets, "Verified delivery has no URDF preview")
        return {
            "pipeline_id": PIPELINE_ID,
            "run_id": identifier,
            "subject_sha256": result["subject_sha256"],
            "urdf": "urdf/robot.urdf",
            "files": assets,
        }

    def artifact(self, identifier, name):
        job = self.snapshot(identifier)
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
                return stream, path.stat().st_size
            except BaseException:
                stream.close()
                raise
        preview = self.preview(identifier)
        _require(name in preview["files"], "Only verified URDF and mesh assets are available")
        path = confined(self.config["output_root"] / identifier, name)
        stream = path.open("rb")
        try:
            _require(
                hashlib.file_digest(stream, "sha256").hexdigest() == preview["files"][name],
                "Preview artifact differs from its verified bytes",
            )
            stream.seek(0)
            return stream, path.stat().st_size
        except BaseException:
            stream.close()
            raise

    def create(self, request):
        _require(isinstance(request, dict), "Expected a JSON object")
        identifier = _job_id(request.get("run_id"))
        with self.mutex:
            if identifier in self.jobs:
                job = self.jobs[identifier]
                if job["request"] != request:
                    raise RequestError("run_id is already bound to a different request", 409)
                return self.snapshot(identifier), False
            package = self.validate(request)
            spec = _resume_spec(request)
            if spec is not None and spec["from_stage"] not in NATIVE_STAGES:
                raise _refusal(
                    "linux_owned",
                    "该阶段已移至 Linux 执行；请通过交付流程重新运行",
                    from_stage=spec["from_stage"],
                )
            events = []
            inherited = {}
            if spec is not None:
                _require(spec["parent_run"] != identifier, "A linked run cannot reuse itself")
                parent = self.jobs.get(spec["parent_run"])
                if parent is None:
                    raise RequestError("The selected run is unknown to this endpoint", 409)
                plan = start_plan(parent, spec["from_stage"], probe=self._probe(parent), tool=self._tool_state(parent))
                if not plan["accepted"]:
                    raise _refusal(plan["reason"], plan["reason_zh"], earliest=plan["earliest_required"])
                active = self._active_attempt(spec["parent_run"])
                if active is not None:
                    raise _refusal(
                        "already_active",
                        "此运行已有正在执行的重新运行，请等待完成后再试",
                        active_run_id=active,
                    )
                parent_request = parent.get("request") if isinstance(parent.get("request"), dict) else {}
                if (
                    parent_request.get("package") != request["package"]
                    or parent_request.get("handoff_sha256") != request["handoff_sha256"]
                ):
                    raise _refusal(
                        "source_changed",
                        "原始上传不可复用，请重新上传后开始新运行",
                        earliest=None,
                    )
                if request.get("main_assembly") is not None and request["main_assembly"] != parent.get("main_assembly"):
                    raise _refusal(
                        "selection_changed",
                        "所选主装配与原运行不一致，无法复用其检查点；请保持同一主装配或重新开始新运行",
                        earliest=None,
                    )
                if spec["from_stage"] in {"verify", "publish"} and self._recorded_subject(parent) is None:
                    raise _refusal(
                        "prerequisite_invalid",
                        "原运行未记录可验证的模型主体，最早可从“生成 URDF”重新开始",
                        earliest="generate",
                    )
                events = self._reuse_events(parent, spec["from_stage"])
                inherited = self._inherited_metadata(parent, spec["from_stage"])
            selection = request.get("main_assembly")
            if selection is None and spec is not None:
                parent_job = self.jobs.get(spec["parent_run"])
                selection = parent_job.get("main_assembly") if isinstance(parent_job, dict) else None
            job = {
                "schema_version": JOB_SCHEMA,
                "pipeline_id": PIPELINE_ID,
                "run_id": identifier,
                "request": dict(request),
                "main_assembly": selection,
                "status": "queued",
                "events": events,
                "result": None,
                "error": None,
                "created_at": datetime.now(UTC).isoformat(),
            }
            job.update(inherited)
            job["package_files"] = package_inventory(package)
            job["tool"] = self._tool_identity()
            job["dependency"] = self._dependency_identity()
            self._save(job)
            self.jobs[identifier] = job
            self.queue.put(identifier)
            return self.snapshot(identifier), True

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

        job = self.jobs[identifier]
        package, target, prepared, files = discover_structure(
            frozen,
            self.config["state_root"] / "prepared" / identifier,
            identifier,
            expected_digest=job["request"]["handoff_sha256"],
            expected_files=job["package_files"],
            main_assembly=selection,
            configuration=self.config.get("discovery", {}),
            targets=self.config["targets"],
            preparer=self.native_preparer,
            on_event=lambda item: self._event(identifier, item),
            on_activity=lambda item: self._activity(identifier, item),
        )
        with self.mutex:
            job.update(
                hardware_id=prepared.hardware_id,
                revision=prepared.revision,
                repository_slug=target["repository_slug"],
                repository_base=target["base"],
                prepared_files=files,
                prepared_dir=str(package),
            )
            if job.get("main_assembly") is None:
                # A request may omit the selection; native discovery resolves the delivered
                # assembly into its bound identity record.  Persist that resolved value
                # (the operator's request payload stays untouched) so the runner and the
                # capture seal bind the same assembly.
                loaded = read_data(prepared.discovery_path)
                identity = loaded.get("identity") if isinstance(loaded, dict) else None
                assembly = identity.get("main_assembly") if isinstance(identity, dict) else None
                if isinstance(assembly, str) and assembly.strip():
                    job["main_assembly"] = assembly
            self._save(job)
        return package, target

    def _work(self):
        while True:
            identifier = self.queue.get()
            if identifier is None:
                self.queue.task_done()
                return
            job = self.jobs[identifier]
            spec = None
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
                with self.mutex:
                    job.update(status="running", started_at=datetime.now(UTC).isoformat())
                    self._save(job)
                from ..steps import freeze_inputs

                package = confined(
                    self.config["package_root"], job["request"]["package"] + "/.handoff-folder", exists=False
                ).parent
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
                        job["request"]["handoff_sha256"],
                        job["package_files"],
                        main_assembly=selection,
                        on_event=lambda item, identifier=identifier: self._event(identifier, item),
                    )
                if from_stage in {None, "freeze", "discover"}:
                    package, target = self._prepare_native(identifier, package, selection)
                else:
                    # capture..publish reuse the parent's retained freeze+discover
                    # checkpoints; the plan already proved them intact.
                    parent = self.jobs[spec["parent_run"]]
                    package = self._prepared_dir(job)
                    _require(package is not None, "The retained discovery checkpoint is no longer available")
                    target = self.config["targets"].get(job.get("hardware_id"))
                    _require(isinstance(target, dict), "The selected run has no configured publication target")
                    seed = self._delivery_dir(parent) if from_stage in {"generate", "verify", "publish"} else None
                    _require(
                        from_stage == "capture" or seed is not None,
                        "The retained delivery of the selected run is no longer available",
                    )
                    resume_kwargs.update(
                        resume_from=from_stage,
                        seed_dir=seed,
                        expected_subject=self._recorded_subject(parent),
                    )
                # Re-read after preparation: a request without an explicit selection now
                # carries the discovery-resolved assembly, and linked native reruns
                # inherit the parent's effective selection.
                selection = job.get("main_assembly")
                _require(
                    isinstance(job.get("repository_slug"), str)
                    and bool(job["repository_slug"])
                    and job["repository_slug"] == target["repository_slug"]
                    and job.get("repository_base") == target["base"],
                    "Persisted job lacks matching repository metadata; review it and use a new run_id",
                )
                result = self.runner(
                    package,
                    self.config["output_root"] / identifier,
                    run_id=identifier,
                    stop_after="capture",
                    main_assembly=selection,
                    on_event=lambda event, identifier=identifier: self._event(identifier, event),
                    prior_events=list(job["events"]),
                    expected_inputs=job["prepared_files"],
                    handoff_sha256=job["request"]["handoff_sha256"],
                    **resume_kwargs,
                )
                with self.mutex:
                    job["result"] = result
                    archive = result.get("capture_archive") if isinstance(result.get("capture_archive"), dict) else None
                    if result.get("native_complete") is True and result.get("passed") is False and archive:
                        verified = self._verified_capture_archive(identifier, archive)
                        job.update(status="native_complete", error=None, capture_archive=verified)
                        self._clear_activity(job)
                    elif result.get("native_complete") is True or result.get("passed") is True:
                        job.update(
                            status="failed",
                            error="Native result shape is not a sealed capture transfer; "
                            "the run cannot be qualified",
                        )
                        self._clear_activity(job)
                        if result.get("error_code"):
                            job["error_code"] = str(result["error_code"])
                    else:
                        # A genuinely failed native run keeps its own diagnostics.
                        job.update(status="failed", error=result.get("error") or "Native capture failed")
                        self._clear_activity(job)
                        if result.get("error_code"):
                            job["error_code"] = str(result["error_code"])
                        for key in ("detail", "diagnostic_path"):
                            if result.get(key) is not None:
                                job[key] = result[key]
            except Exception as error:
                with self.mutex:
                    job.update(status="failed", error=f"{type(error).__name__}: {error}")
                    self._clear_activity(job)
                    code = getattr(error, "code", None)
                    if code:
                        job["error_code"] = str(code)
                    if getattr(error, "detail", None) is not None:
                        job["detail"] = error.detail
                    elif isinstance(getattr(error, "details", None), dict):
                        job["detail"] = error.details
            finally:
                with self.mutex:
                    if job["status"] == "failed" and (not job["events"] or job["events"][-1]["state"] != "failed"):
                        phase = (job["events"][-1] if job["events"] else {}).get("stage", "freeze")
                        self._event(identifier, {"stage": phase, "state": "failed", "error": job.get("error")})
                    job["completed_at"] = datetime.now(UTC).isoformat()
                    self._save(job)
                self.queue.task_done()

    def close(self):
        if self._closed:
            return
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
            self._reply(getattr(error, "status", 400), body)

        def do_GET(self):
            if not self._authorized():
                return
            try:
                if self.path == "/health":
                    self._reply(
                        200,
                        {
                            "pipeline_id": PIPELINE_ID,
                            "ready": jobs.thread.is_alive(),
                            "native_platform": sys.platform == "win32",
                        },
                    )
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
                    elif suffix.endswith("/preview"):
                        self._reply(200, jobs.preview(suffix[: -len("/preview")]))
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
            if self.path not in {"/v1/jobs", "/v1/handoffs/resolve", "/v1/handoffs/import"}:
                self._reply(404, {"error": "Unknown API route"})
                return
            try:
                if self.path == "/v1/handoffs/import":
                    _require(
                        self.headers.get("Content-Type", "").split(";")[0] == "application/zip",
                        "Content-Type must be application/zip",
                    )
                    size = int(self.headers.get("Content-Length", "0"))
                    _require(
                        0 < size <= MAX_HANDOFF_BYTES and not self.headers.get("Transfer-Encoding"),
                        "Invalid handoff size",
                    )
                    self.connection.settimeout(60)
                    with tempfile.TemporaryDirectory(prefix=".transport-", dir=jobs.config["state_root"]) as temporary:
                        archive = Path(temporary) / "handoff.zip"
                        with archive.open("xb") as output:
                            remaining = size
                            while remaining:
                                chunk = self.rfile.read(min(remaining, 1024 * 1024))
                                _require(bool(chunk), "Incomplete handoff transport; no job was started")
                                output.write(chunk)
                                remaining -= len(chunk)
                        self._reply(200, jobs.import_handoff(archive))
                    return
                _require(
                    self.headers.get("Content-Type", "").split(";")[0] == "application/json",
                    "Content-Type must be application/json",
                )
                size = int(self.headers.get("Content-Length", "0"))
                _require(0 < size <= 16384 and not self.headers.get("Transfer-Encoding"), "Invalid request size")
                self.connection.settimeout(10)
                request = json.loads(self.rfile.read(size))
                if self.path == "/v1/handoffs/resolve":
                    self._reply(200, jobs.resolve_handoff(request))
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
