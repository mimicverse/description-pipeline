"""The authoritative Linux attempt store for the portable half of the split.

One immutable directory per attempt holds the admitted native capture, the staged
portable checkpoints (generate, verify), the published output and the merged event
stream the portal serves.  Attempts are never rewritten; a linked rerun is a new
attempt that reads its parent only as a seed.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path

from ..delivery import (
    BUNDLE_SCHEMA,
    PIPELINE_ID,
    read_delivery_manifest,
    stamped_output,
    subject_digest,
    subject_inventory,
)
from ..interface_records import check_policy_digest
from ..io import PipelineError, confined, inventory, read_data, write_json
from ..runtime import tool_record
from ..stages import (
    CONFIRMED_STAGE_PASSED,
    CONFIRMED_STAGE_STATES,
    CONTRACT_SHA256,
    PORTABLE_STAGES,
    STAGE_IDS,
    stage_view,
)
from ..verification.solidworks_urdf import check_bundle, rebuilt_definition, require_qualified_report
from .airflow_client import JOB_SCHEMA, validate_run_id
from .stage_transfer import CAPTURE_MANIFEST, TRANSFER_SCHEMA, admit_capture

STORE_SCHEMA = "solidworks-to-urdf.linux-run/v1"
#: Bounds on the diagnostic preview payload; totals always report the untruncated count.
MAX_PREVIEW_CHECKS = 60
MAX_PREVIEW_WARNINGS = 40


class LinuxStore:
    """Attempt-scoped, immutable Linux store for the portable engineering stages."""

    def __init__(self, root: Path):
        root = Path(root)
        if not root.is_absolute():
            raise PipelineError("Linux store root must be an absolute path")
        self.root = root
        #: Bound previews per immutable checkpoint, keyed by path identity and mtime.
        self._previews: dict[tuple[str, int, tuple], tuple[Path, dict]] = {}
        #: The last bound preview per attempt that artifact serving may trust.
        self._bound_previews: dict[str, tuple[Path, dict]] = {}

    # ------------------------------------------------------------------ paths
    def run_dir(self, run_id: str) -> Path:
        return self.root / validate_run_id(run_id)

    def capture_dir(self, run_id: str) -> Path:
        return self.run_dir(run_id) / "capture"

    def stage_dir(self, run_id: str, stage: str) -> Path:
        if stage not in STAGE_IDS:
            raise PipelineError(f"Unknown engineering stage: {stage!r}")
        return self.run_dir(run_id) / "stages" / stage

    def output_dir(self, run_id: str) -> Path:
        return self.run_dir(run_id) / "output"

    def receipt_path(self, run_id: str, stage: str) -> Path:
        return self.run_dir(run_id) / "receipts" / f"{stage}.json"

    def events_path(self, run_id: str) -> Path:
        return self.run_dir(run_id) / "events.json"

    def meta_path(self, run_id: str) -> Path:
        return self.run_dir(run_id) / "meta.json"

    def checkpoint_owner(self, run_id: str, stage: str) -> str:
        """Resolve an upstream checkpoint without substituting an older attempt's output.

        A linked attempt inherits only stages before its selected restart boundary.
        Its newly generated or verified files always take precedence thereafter.
        """
        if stage not in {"capture", "generate", "verify"}:
            raise PipelineError(f"Not a reusable checkpoint: {stage!r}")
        current = validate_run_id(run_id)
        seen = set()
        while current not in seen:
            seen.add(current)
            path = self.capture_dir(current) if stage == "capture" else self.stage_dir(current, stage)
            if path.is_dir():
                return current
            meta = self.meta(current) or {}
            parent, boundary = meta.get("source_run_id"), meta.get("resume_from")
            if (
                not parent or parent == current or boundary not in PORTABLE_STAGES
                or STAGE_IDS.index(stage) >= STAGE_IDS.index(boundary)
            ):
                raise PipelineError(f"Attempt {current} has no retained {stage} checkpoint")
            current = validate_run_id(parent)
        raise PipelineError("Linked checkpoint lineage contains a cycle")

    def checkpoint_dir(self, run_id: str, stage: str) -> Path:
        owner = self.checkpoint_owner(run_id, stage)
        return self.capture_dir(owner) if stage == "capture" else self.stage_dir(owner, stage)

    def checkpoint_receipt(self, run_id: str, stage: str) -> Path:
        return self.receipt_path(self.checkpoint_owner(run_id, stage), stage)

    def confirmed_stage_checkpoint(self, run_id: str, stage: str) -> dict | None:
        """The attempt's registered stage receipt, fully rebound - or an explicit refusal.

        Reuse only stands through this query: the receipt must name this run and
        stage with a confirmed state, and the sealed artifacts must still bind
        - the delivery manifest record-for-record with its contract and check
        policy digests, the delivery tool identity, the subject recomputed from
        the checkpoint bytes and the stored handoff.  A receipt that claims
        confirmation without binding artifacts is a damaged store: it refuses
        instead of silently re-running, and no caller keeps its own copy of the
        confirmed-state vocabulary.
        """

        if stage not in CONFIRMED_STAGE_STATES:
            raise PipelineError(f"stage must be one of {tuple(CONFIRMED_STAGE_STATES)}: {stage!r}")
        path = self.receipt_path(run_id, stage)
        if not path.is_file():
            return None
        receipt = read_data(path)
        if not isinstance(receipt, dict) or receipt.get("schema_version") != BUNDLE_SCHEMA:
            raise PipelineError("The recorded stage receipt is malformed")
        if receipt.get("run_id") != run_id or receipt.get("stage") != stage:
            raise PipelineError("The recorded stage receipt names another run or stage")
        if receipt.get("state") not in CONFIRMED_STAGE_STATES[stage]:
            return None
        if receipt.get("passed") is not CONFIRMED_STAGE_PASSED[stage]:
            raise PipelineError(f"The recorded {stage} receipt does not carry its confirmed outcome")
        recorded_handoff = receipt.get("handoff_sha256")
        meta = self.meta(run_id) or {}
        if (
            not isinstance(recorded_handoff, str)
            or not recorded_handoff
            or recorded_handoff != meta.get("handoff_sha256")
        ):
            raise PipelineError("The recorded checkpoint receipt is not bound to this attempt's handoff")
        directory = self.output_dir(run_id) if stage == "publish" else self.stage_dir(run_id, stage)
        if not directory.is_dir():
            raise PipelineError(f"The recorded {stage} checkpoint has no sealed delivery to reuse")
        manifest = read_delivery_manifest(directory)
        if manifest.get("contract", {}).get("digest") != CONTRACT_SHA256:
            raise PipelineError("The recorded checkpoint was produced under another contract")
        if manifest.get("policy", {}).get("digest") != check_policy_digest():
            raise PipelineError("The recorded checkpoint was recorded under another check policy")
        if read_data(directory / "tool.json") != tool_record():
            raise PipelineError("The recorded checkpoint was produced by another tool release")
        subject = receipt.get("subject_sha256")
        if not (
            isinstance(subject, str)
            and len(subject) == 64
            and all(character in "0123456789abcdef" for character in subject)
        ):
            raise PipelineError("The recorded checkpoint receipt lacks its subject digest")
        if subject_digest(directory) != subject:
            raise PipelineError("The recorded checkpoint bytes no longer match its receipt")
        if stage == "verify":
            report = require_qualified_report(
                read_data(directory / "reports/quality.json"), rebuilt_definition(directory)
            )
            if report.get("subject_sha256") != subject:
                raise PipelineError("The verified quality report is bound to another subject")
        if stage == "publish":
            report = require_qualified_report(
                read_data(directory / "reports/quality.json"), rebuilt_definition(directory)
            )
            if report.get("subject_sha256") != subject:
                raise PipelineError("The published quality report is bound to another subject")
            submission = receipt.get("submission")
            if (
                not isinstance(submission, dict)
                or submission.get("passed") is not True
                or submission.get("state") not in {"published", "updated", "noop"}
                or not submission.get("url")
                or submission.get("subject_sha256") not in (None, subject)
            ):
                raise PipelineError("The recorded publication has no confirmed submission")
        return receipt

    def delivery_dir(self, run_id: str) -> Path | None:
        """The newest installed delivery of this attempt (publish, verify or generate)."""
        candidates = (
            self.output_dir(run_id),
            self.stage_dir(run_id, "verify"),
            self.stage_dir(run_id, "generate"),
        )
        for candidate in candidates:
            if (candidate / "input").is_dir() and (candidate / "reports").is_dir():
                return candidate
        return None

    # ------------------------------------------------------------------ state
    def meta(self, run_id: str) -> dict | None:
        path = self.meta_path(run_id)
        return read_data(path) if path.is_file() else None

    def events(self, run_id: str) -> list[dict]:
        path = self.events_path(run_id)
        if not path.is_file():
            return []
        payload = read_data(path)
        if not isinstance(payload, list):
            raise PipelineError("events.json must be a JSON list")
        return [item for item in payload if isinstance(item, dict)]

    def _write(self, path: Path, payload) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = tempfile.NamedTemporaryFile(  # noqa: SIM115 - closed explicitly in the finally path
            prefix=".store-", dir=path.parent, delete=False
        )
        try:
            with handle:
                write_json(Path(handle.name), payload)
            os.replace(handle.name, path)
        except BaseException:
            Path(handle.name).unlink(missing_ok=True)
            raise

    def update_meta(self, run_id: str, **fields) -> dict:
        """Record attempt-level facts (routing, source lineage) without touching checkpoints."""
        meta = self.meta(run_id) or {"schema_version": STORE_SCHEMA, "run_id": run_id}
        meta.update(fields)
        self._write(self.meta_path(run_id), meta)
        return meta

    def append_events(self, run_id: str, events) -> None:
        """Append raw events, preserving timestamps; identical records are never duplicated.

        Deduplication compares the complete event content: distinct checks may share
        one timestamp and must both survive.
        """
        existing = self.events(run_id)
        seen = {json.dumps(item, sort_keys=True, ensure_ascii=False) for item in existing}
        merged = list(existing)
        for item in events or []:
            if not isinstance(item, dict):
                continue
            canonical = json.dumps(item, sort_keys=True, ensure_ascii=False)
            if canonical in seen:
                continue
            seen.add(canonical)
            merged.append(item)
        if len(merged) != len(existing):
            self._write(self.events_path(run_id), merged)

    # ------------------------------------------------------------- lifecycle
    def import_capture(
        self,
        run_id: str,
        archive: Path,
        *,
        expected_handoff_sha256: str,
        expected_main_assembly: str | None,
        expected_native_tool: dict,
    ) -> dict:
        """Admit one sealed native capture and seed the raw native events.

        Retrying the identical fetch is idempotent: a committed admission returns its
        recorded metadata, and a crash between capture install and metadata write is
        recovered from the atomic capture install.  A differing request for the same
        attempt is refused.
        """
        run_id = validate_run_id(run_id)

        def finalize(meta: dict) -> dict:
            capture = self.capture_dir(run_id)
            manifest_path = capture / CAPTURE_MANIFEST
            manifest_payload = meta.get("transfer")
            if not isinstance(manifest_payload, dict):
                if not manifest_path.is_file():
                    raise PipelineError(
                        "The admitted capture has no transfer manifest; review this attempt manually"
                    )
                manifest_payload = read_data(manifest_path)
                if not isinstance(manifest_payload, dict):
                    raise PipelineError("The admitted capture transfer manifest is not an object")
            elif not manifest_path.is_file():
                write_json(manifest_path, manifest_payload)
            native_receipt = capture / "reports/native-stages.json"
            if native_receipt.is_file():
                payload = read_data(native_receipt)
                if isinstance(payload, dict):
                    self.append_events(run_id, payload.get("events"))
            admitted = {**meta, "transfer": manifest_payload, "state": "capture_admitted"}
            self._write(self.meta_path(run_id), admitted)
            return admitted

        existing = self.meta(run_id)
        if isinstance(existing, dict):
            same = (
                existing.get("handoff_sha256") == expected_handoff_sha256
                and existing.get("main_assembly") == expected_main_assembly
                and existing.get("native_tool") == expected_native_tool
            )
            if not same:
                raise PipelineError("This attempt already admitted a different native capture")
            state = existing.get("state")
            if state == "importing":
                if self.capture_dir(run_id).is_dir():
                    return finalize(existing)
                # A crash before the capture install: re-import the identical request below.
            elif state == "capture_admitted":
                if not self.capture_dir(run_id).is_dir():
                    raise PipelineError("The admitted capture is missing; review this attempt manually")
                return finalize(existing)
            else:
                # An advanced attempt (generated/verified/published/failed) is never rolled back.
                return existing
        elif self.capture_dir(run_id).exists():
            raise PipelineError("A capture exists without store metadata; review this attempt manually")
        provisional = {
            "schema_version": STORE_SCHEMA,
            "run_id": run_id,
            "state": "importing",
            "handoff_sha256": expected_handoff_sha256,
            "main_assembly": expected_main_assembly,
            "native_tool": expected_native_tool,
        }
        self._write(self.meta_path(run_id), provisional)
        manifest = admit_capture(
            archive,
            self.capture_dir(run_id),
            expected_run_id=run_id,
            expected_handoff_sha256=expected_handoff_sha256,
            expected_main_assembly=expected_main_assembly,
            expected_native_tool=expected_native_tool,
        )
        native_receipt = read_data(self.capture_dir(run_id) / "reports/native-stages.json")
        if isinstance(native_receipt, dict):
            self.append_events(run_id, native_receipt.get("events"))
        return finalize({**provisional, "transfer": dict(manifest)})

    def native_receipts(self, run_id: str) -> dict:
        """The sealed native receipts, as immutable bytes, from run storage.

        The delivery tree no longer carries ``reports/native-*`` or the transfer
        manifest; the orchestration adapter hands consumers the exact sealed
        bytes (no re-serialization, no state oracle) so verification can validate
        them independently.
        """

        run_id = validate_run_id(run_id)
        capture = self.capture_dir(run_id)
        paths = {
            "native_tool_bytes": capture / "reports/native-tool.json",
            "native_stages_bytes": capture / "reports/native-stages.json",
            "transfer_manifest_bytes": capture / "transfer-manifest.json",
        }
        missing = sorted(name for name, path in paths.items() if not path.is_file())
        if missing:
            raise PipelineError(f"Native receipts are missing from run storage: {missing}")
        return {name: path.read_bytes() for name, path in paths.items()}

    def record_stage(self, run_id: str, stage: str, receipt: dict) -> None:
        """Keep one stage receipt and its raw events; the attempt is otherwise untouched."""
        if stage not in PORTABLE_STAGES:
            raise PipelineError(f"Not a portable stage: {stage!r}")
        self._write(self.receipt_path(run_id, stage), receipt)
        self.append_events(run_id, receipt.get("events"))
        meta = self.meta(run_id) or {"schema_version": STORE_SCHEMA, "run_id": run_id}
        meta.update(
            state=receipt.get("state") or meta.get("state"),
            passed=bool(receipt.get("passed")),
            subject_sha256=receipt.get("subject_sha256") or meta.get("subject_sha256"),
            last_stage=stage,
            error=receipt.get("error"),
        )
        self._write(self.meta_path(run_id), meta)

    # ------------------------------------------------------------- serving
    def merged_job(self, run_id: str, native_job: dict | None = None) -> dict:
        """One job-shaped snapshot the existing stage view and portal can render."""
        run_id = validate_run_id(run_id)
        meta = self.meta(run_id) or {}
        delivery = self.delivery_dir(run_id)
        quality_path = delivery / "reports/quality.json" if delivery else None
        submission_path = delivery / "reports/pr.json" if delivery else None
        quality = read_data(quality_path) if quality_path and quality_path.is_file() else None
        submission = read_data(submission_path) if submission_path and submission_path.is_file() else None
        subject = meta.get("subject_sha256") or (quality or {}).get("subject_sha256")
        events = self.events(run_id)
        for item in (native_job or {}).get("events") or []:
            if item not in events:
                events.append(item)
        submission_state = (submission or {}).get("state")
        submission_bound = (
            submission_state in {"published", "updated", "noop"}
            and (submission or {}).get("passed") is True
            and bool((submission or {}).get("url"))
            and (submission or {}).get("subject_sha256") == subject
        )
        result = {
            "pipeline_id": PIPELINE_ID,
            "passed": bool(
                submission_bound and (quality or {}).get("passed") is True
                and (quality or {}).get("subject_sha256") == subject
            ),
            "quality": quality or {},
            "subject_sha256": subject,
            "handoff_sha256": meta.get("handoff_sha256"),
        }
        try:
            capture = self.checkpoint_dir(run_id, "capture")
        except PipelineError:
            capture = None
        hardware = _capture_value(capture, "input/source.json", ("identity", "hardware_id")) if capture else None
        revision = _capture_value(capture, "input/source.json", ("metadata", "revision")) if capture else None
        if submission is not None:
            result["submission"] = submission
        if (native_job or {}).get("status") == "failed":
            return {**native_job, "run_id": run_id, "events": events}
        status = "passed" if result["passed"] else ("failed" if meta.get("state") == "failed" else "running")
        return {
            "schema_version": JOB_SCHEMA,
            "pipeline_id": PIPELINE_ID,
            "run_id": run_id,
            "status": status,
            "request": {"handoff_sha256": meta.get("handoff_sha256"), "main_assembly": meta.get("main_assembly")},
            "events": events,
            "result": result,
            "error": meta.get("error"),
            "hardware_id": hardware,
            "revision": revision,
            "repository_slug": (native_job or {}).get("repository_slug") or meta.get("repository_slug"),
            "repository_base": (native_job or {}).get("repository_base") or meta.get("repository_base"),
        }

    def view(self, run_id: str, native_job: dict | None = None) -> dict:
        return stage_view(self.merged_job(run_id, native_job))

    def preview(self, run_id: str) -> dict:
        """Digest-bound preview of the newest retained delivery.

        A delivery that passed independent verification is served exactly as before.
        When the attempt recorded a failed verify stage, its retained failed-verify
        checkpoint is served instead as an explicitly unverified diagnostic preview: the
        same binding, confinement and streamed-digest rules, plus the exact failing checks
        and warning findings.  A diagnostic preview never asserts verification and never
        authorizes publication.  The verification runs once per immutable checkpoint;
        serving a member never re-runs it.
        """
        return self._preview_binding(run_id)[1]

    def _preview_binding(self, run_id: str) -> tuple[Path, dict]:
        """Resolve the single preview this attempt may serve, without assuming the outcome."""
        delivery = self.delivery_dir(run_id)
        verified_error: PipelineError | None = None
        if delivery is not None:
            stat = delivery.stat()
            cache_key = (str(delivery), stat.st_ino, _delivery_fingerprint(delivery))
            cached = self._previews.get(cache_key)
            if cached is not None:
                return cached
            try:
                report = require_qualified_report(
                    check_bundle(delivery), rebuilt_definition(delivery), root=delivery
                )
            except PipelineError as error:
                verified_error = error
            else:
                subject = report.get("subject_sha256")
                if not isinstance(subject, str) or subject != subject_digest(delivery):
                    raise PipelineError("The verified report is not bound to the current delivery bytes")
                files = subject_inventory(delivery)
                if "urdf/robot.urdf" not in files:
                    raise PipelineError("The delivery carries no urdf/robot.urdf artifact")
                return self._remember_preview(
                    cache_key,
                    run_id,
                    delivery,
                    {
                        "schema_version": "solidworks-to-urdf.preview/v1",
                        "pipeline_id": PIPELINE_ID,
                        "run_id": run_id,
                        "subject_sha256": subject,
                        "urdf": "urdf/robot.urdf",
                        "files": files,
                        "verified": True,
                        "state": "verified",
                    },
                )
        diagnostic = self._diagnostic_delivery(run_id)
        if diagnostic is not None:
            stat = diagnostic.stat()
            cache_key = (str(diagnostic), stat.st_ino, _delivery_fingerprint(diagnostic))
            cached = self._previews.get(cache_key)
            if cached is not None:
                return cached
            return self._remember_preview(cache_key, run_id, diagnostic, self._diagnostic_preview(run_id, diagnostic))
        if verified_error is not None:
            raise verified_error
        raise PipelineError("This attempt has no delivery to preview yet")

    def _remember_preview(self, cache_key, run_id: str, root: Path, preview: dict) -> tuple[Path, dict]:
        self._bound_previews[run_id] = (root, preview)
        if len(self._previews) >= 8:
            self._previews.pop(next(iter(self._previews)))
        self._previews[cache_key] = (root, preview)
        return root, preview

    def _diagnostic_delivery(self, run_id: str) -> Path | None:
        """The retained failed-verify checkpoint, only when this attempt owns it.

        The stage receipt is the sole authority for the location, and the location has
        to prove one identity from several directions before anything is served: the
        failed verify stage, an in-attempt path, a directory still matching its stamped
        inventory, the embedded receipt's run id, handoff and subject equal to this
        attempt's recorded ones, the transfer provenance (when present) naming the
        native capture owner and the same handoff, and the archived input bytes equal
        to that owner's admitted capture.  The delivery itself is bound by
        recomputing its subject from the retained bytes - the receipt no longer
        travels inside the delivery.  A linked attempt reuses the parent's immutable
        transfer and checkpoints, so its own receipts must name the child while the
        transfer may legitimately name the parent.  Failure at any step is an
        integrity error, never a reason to serve something else, so a foreign
        stamped bundle copied into another attempt cannot be mislabeled as its
        diagnostic preview.
        """
        receipt_path = self.receipt_path(run_id, "verify")
        if not receipt_path.is_file():
            return None
        receipt = read_data(receipt_path)
        if not isinstance(receipt, dict):
            raise PipelineError("The retained verify receipt is malformed")
        recorded = receipt.get("diagnostic_path")
        if recorded is None:
            return None
        if receipt.get("stage") != "verify" or receipt.get("state") != "failed" or receipt.get("passed") is not False:
            return None
        if not isinstance(recorded, str) or not recorded:
            raise PipelineError("The failed verify receipt records no diagnostic location")
        run_root = self.run_dir(run_id).resolve()
        candidate = Path(recorded).resolve()
        if candidate != run_root and run_root not in candidate.parents:
            raise PipelineError("The retained diagnostic location is outside this attempt")
        if not stamped_output(candidate):
            raise PipelineError("The retained diagnostic delivery is not a stamped pipeline output")
        if receipt.get("run_id") != run_id:
            raise PipelineError("The retained verify receipt belongs to another attempt")
        subject = receipt.get("subject_sha256")
        if not isinstance(subject, str):
            raise PipelineError("The failed verify receipt is not bound to a subject")
        try:
            computed_subject = subject_digest(candidate)
        except PipelineError as error:
            raise PipelineError("The retained diagnostic delivery is not a stamped pipeline output") from error
        if computed_subject != subject:
            raise PipelineError("The retained diagnostic delivery does not match its recorded subject")
        meta = self.meta(run_id) or {}
        expected_handoff = meta.get("handoff_sha256") or receipt.get("handoff_sha256")
        if not isinstance(expected_handoff, str) or not expected_handoff:
            raise PipelineError("This attempt records no handoff identity for its diagnostic delivery")
        try:
            capture_owner = self.checkpoint_owner(run_id, "capture")
            capture = self.capture_dir(capture_owner)
        except PipelineError as error:
            raise PipelineError("This attempt has no admitted capture to bind its diagnostic delivery") from error
        manifest_path = candidate / CAPTURE_MANIFEST
        if manifest_path.is_file():
            manifest = read_data(manifest_path)
            if (
                not isinstance(manifest, dict)
                or manifest.get("schema_version") != TRANSFER_SCHEMA
                # A linked attempt inherits the parent's immutable native transfer,
                # so the provenance names the capture checkpoint owner, not the child.
                or manifest.get("run_id") != capture_owner
                or manifest.get("handoff_sha256") != expected_handoff
            ):
                raise PipelineError("The retained diagnostic transfer provenance belongs to another attempt")
        if inventory(candidate / "input") != inventory(capture / "input"):
            raise PipelineError("The retained diagnostic delivery does not carry this attempt's archived input")
        report = _read_optional_json(candidate / "reports/quality.json")
        if (
            not isinstance(report, dict)
            or report.get("passed") is not False
            or report.get("subject_sha256") != subject
            or not isinstance(report.get("checks"), list)
        ):
            raise PipelineError("The retained diagnostic report is not a bound failed report")
        return candidate

    def _diagnostic_preview(self, run_id: str, delivery: Path) -> dict:
        """One unverified diagnostic payload over the retained failed-verify bytes."""
        receipt = read_data(self.receipt_path(run_id, "verify"))
        subject = subject_digest(delivery)
        if not isinstance(receipt, dict) or receipt.get("subject_sha256") != subject:
            raise PipelineError("The retained diagnostic delivery is not bound to its recorded subject")
        report = read_data(confined(delivery, "reports/quality.json"))
        files = subject_inventory(delivery)
        if "urdf/robot.urdf" not in files:
            raise PipelineError("The retained diagnostic delivery carries no urdf/robot.urdf artifact")
        failing = _failing_checks(report)
        warnings = _delivery_warnings(delivery)
        return {
            "schema_version": "solidworks-to-urdf.preview/v1",
            "pipeline_id": PIPELINE_ID,
            "run_id": run_id,
            "subject_sha256": subject,
            "urdf": "urdf/robot.urdf",
            "files": files,
            "verified": False,
            "state": "unverified",
            "reason": "verification_failed",
            "error": str(receipt.get("error") or ""),
            "failing_checks": failing[:MAX_PREVIEW_CHECKS],
            "failing_checks_total": len(failing),
            "warnings": warnings[:MAX_PREVIEW_WARNINGS],
            "warnings_total": len(warnings),
        }

    def _bound_preview(self, run_id: str) -> tuple[Path, dict]:
        """The last bound preview of this attempt; serving never re-runs verification."""
        bound = self._bound_previews.get(run_id)
        return bound if bound is not None else self._preview_binding(run_id)

    def open_artifact(self, run_id: str, name: str, *, sha256: str):
        """Open one previewed artifact; the returned stream proves the previewed digest.

        The caller-supplied digest is checked against the bound preview inventory, never
        trusted, and the opened bytes are hashed on the same stream that is served.  The
        artifact resolves inside the bound checkpoint only, whatever kind of preview it is.
        """
        root, preview = self._bound_preview(run_id)
        expected = preview["files"].get(name)
        if expected is None or expected != sha256:
            raise PipelineError("The requested artifact is not part of the previewed delivery")
        path = confined(root, name)
        size = path.stat().st_size
        handle = path.open("rb")
        return _BoundArtifact(handle, name=name, expected=expected, size=size), size


class _BoundArtifact:
    """Read-only stream that proves the served bytes are exactly the previewed ones."""

    def __init__(self, handle, *, name: str, expected: str, size: int):
        self._handle = handle
        self._name = name
        self._expected = expected
        self._size = size
        self._digest = hashlib.sha256()
        self._total = 0
        self._done = False

    def read(self, size: int = -1) -> bytes:
        chunk = self._handle.read(size)
        if chunk:
            self._total += len(chunk)
            self._digest.update(chunk)
            return chunk
        if not self._done:
            self._done = True
            if self._total != self._size or self._digest.hexdigest() != self._expected:
                raise PipelineError(f"Served artifact bytes do not match the previewed digest: {self._name}")
        return chunk

    def close(self) -> None:
        self._handle.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    def __getattr__(self, item):
        return getattr(self._handle, item)


def _capture_value(capture: Path, relative: str, key) -> str | None:
    """One scalar from the admitted native input, or ``None`` when it is absent."""
    path = Path(capture) / relative
    if not path.is_file():
        return None
    try:
        payload = read_data(path)
    except (PipelineError, OSError, ValueError, TypeError):
        return None
    for part in key if isinstance(key, tuple) else (key,):
        payload = payload.get(part) if isinstance(payload, dict) else None
    value = payload
    return value if isinstance(value, str) and value else None


def _delivery_fingerprint(delivery: Path) -> tuple:
    """Content identity of a delivery checkpoint: every member's name, size and mtime."""
    rows = []
    for path in sorted(Path(delivery).rglob("*")):
        if path.is_file():
            stat = path.stat()
            rows.append((path.relative_to(delivery).as_posix(), stat.st_size, stat.st_mtime_ns))
    return tuple(rows)


def _read_optional_json(path: Path) -> object | None:
    """Best-effort read of a diagnostic input; a missing or unreadable file is not one."""

    try:
        return read_data(path)
    except (PipelineError, OSError, ValueError):
        return None


def _failing_checks(report: dict) -> list[dict]:
    """The exact failed checks as the saved failed-verify report recorded them.

    The one convention both writers share: a row is failed when it carries the
    boolean ``passed`` false OR the recorded ``state`` is ``"failed"`` - a
    check-limit report may record only the state, and the preview must still
    list exactly the failed rows.
    """

    checks: list[dict] = []
    for row in report.get("checks") or []:
        if not isinstance(row, dict) or not (row.get("passed") is False or row.get("state") == "failed"):
            continue
        details = row.get("details") if isinstance(row.get("details"), dict) else {}
        checks.append(
            {
                "id": str(row.get("id") or ""),
                "state": str(row.get("state") or "failed"),
                "message": str(details.get("error") or details.get("reason") or "自动检查未通过"),
            }
        )
    return checks


def _delivery_warnings(delivery: Path) -> list[dict]:
    """Input warnings and non-blocking discovery findings the retained delivery carries."""

    warnings: list[dict] = []
    report = _read_optional_json(delivery / "input/handoff.json")
    if isinstance(report, dict):
        for item in report.get("warnings") or []:
            if isinstance(item, dict) and item.get("code"):
                warnings.append(
                    {
                        "source": "input",
                        "code": str(item["code"]),
                        "object": "",
                        "message": str(item.get("message") or ""),
                        "detail": item.get("detail"),
                    }
                )
    payload = _read_optional_json(delivery / "evidence/discovery.json")
    rows = payload.get("findings") if isinstance(payload, dict) else None
    if isinstance(rows, list):
        for item in rows:
            if isinstance(item, dict) and item.get("blocking") is False:
                warnings.append(
                    {
                        "source": "discovery",
                        "code": str(item.get("code") or ""),
                        "object": str(item.get("object") or ""),
                        "message": str(item.get("message") or ""),
                        "detail": item.get("detail"),
                    }
                )
    return warnings
