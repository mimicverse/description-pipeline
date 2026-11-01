"""Run the registered optional attempt through Teleop, retaining original evidence."""

from __future__ import annotations

import hashlib
import os
import secrets
import threading
from collections import Counter
from contextlib import contextmanager
from pathlib import Path

from ..artifacts.store import path_has_link_ancestor
from ..io import canonical, safe_process_output
from .contracts import (
    RESULT_SCHEMA,
    TERMINAL_STATES,
    AssessmentConflict,
    AssessmentRequest,
    MemberRef,
    dispatch_conf,
    parse_json,
    require,
)
from .bundle import validate_bundle
from .runtime import TeleopProcess, process_identity, process_state, resource_state, terminate_owned
from .service import AssessmentService


class AssessmentExecutor:
    """One fenced attempt; all numeric model/trace judgments stay in Teleop."""

    def __init__(self, service: AssessmentService, process: TeleopProcess):
        service._enabled()
        require(service.runtime.sha256 == process.runtime.sha256, "Executor and admitted runtime differ")
        require(not path_has_link_ancestor(service.work_root), "Assessment work root traverses a link")
        self.service = service
        self.store = service.store
        self.process = process

    def _work(self, attempt_id: str) -> Path:
        return self.service.work_root / attempt_id

    def _bundle(self, request: AssessmentRequest, destination: Path) -> tuple[Path, dict]:
        self.store.artifacts.materialize(request.bundle, destination)
        return validate_bundle(request.source, destination)

    @contextmanager
    def _renew(self, attempt_id: str, owner: str, phase: str):
        stopped = threading.Event()
        failures = []

        def renew():
            while not stopped.wait(5):
                try:
                    self.store.heartbeat(attempt_id, owner, phase=phase, process=None)
                except Exception as error:
                    failures.append(error)
                    return

        thread = threading.Thread(target=renew, name="assessment-lease-renewal", daemon=True)
        thread.start()
        try:
            yield
            if failures:
                raise AssessmentConflict("Assessment lease renewal failed during artifact IO") from failures[0]
        finally:
            stopped.set()
            thread.join(timeout=6)

    def execute(self, attempt_id: str, conf: dict) -> dict:
        self.service._enabled()
        attempt = self.store.attempt(attempt_id)
        request = self.store.request(attempt["request_id"])
        require(
            conf == dispatch_conf(attempt["request_id"], attempt_id, request),
            "Auxiliary DagRun conf differs from the registered assessment request",
        )
        require(
            request.runtime.sha256 == self.process.runtime.sha256
            and request.record["executor_sha256"] == self.service.executor_sha256,
            "Assessment executor differs from the admitted runtime identity",
        )
        if attempt["state"] in TERMINAL_STATES:
            return {"attempt_id": attempt_id, "state": attempt["state"]}
        owner = secrets.token_hex(32)
        self.store.claim(attempt_id, owner, controller=process_identity(os.getpid(), owner))
        work = self._work(attempt_id)
        try:
            work.mkdir(mode=0o700)
        except FileExistsError as error:
            self.store.unknown(
                attempt_id, {"phase": "prepare", "error": "Attempt directory already exists"}, owner=owner
            )
            raise AssessmentConflict(
                "Assessment attempt directory already exists; reconcile its original evidence"
            ) from error
        output = work / "result"
        output.mkdir()
        (output / "request.json").write_bytes(canonical(request.to_record()) + b"\n")
        state = "failed"
        detail = {}
        operations = {}
        derivation = None
        last_process = None

        def cancelled():
            return self.store.attempt(attempt_id)["cancel_requested"]

        def checkpoint(phase):
            def save(identity):
                nonlocal last_process
                last_process = identity
                self.store.heartbeat(attempt_id, owner, phase=phase, process=identity)

            return save

        try:
            with self._renew(attempt_id, owner, "running"):
                request.source.verify(self.store.artifacts)
                for role, member_record in {
                    "urdf": request.source.record["urdf"],
                    "mjcf": request.source.record["mjcf"],
                    **{name + "-certificate": item for name, item in request.source.record["certificates"].items()},
                }.items():
                    member = MemberRef.from_record(member_record)
                    destination = output / "engineering" / role / member.path
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_bytes(member.read(self.store.artifacts))
                if request.bundle is None:
                    state, detail = "unsupported", {"missing": ["sealed_execution_bundle"]}
                elif cancelled():
                    state, detail = "cancelled", {"phase": "before_execution"}
                else:
                    plan, derivation = self._bundle(request, work / "bundle")
            if request.bundle is not None and state != "cancelled":
                operations["run"] = self.process.run(
                    operation="run",
                    root=output,
                    plan=plan,
                    output=output / "run",
                    nonce=secrets.token_hex(32),
                    checkpoint=checkpoint("running"),
                    should_cancel=cancelled,
                )
                execution = operations["run"]
                if execution["reason"]:
                    state = (
                        "cancelled"
                        if execution["reason"] == "cancelled"
                        else ("timeout" if execution["reason"] == "timeout" else "failed")
                    )
                    detail = {"phase": "run", "reason": execution["reason"]}
                elif execution["exit_code"] == 65:
                    state, detail = "unsupported", {"phase": "teleop_input_qualification"}
                elif (output / "run/manifest.json").is_file():
                    operations["verify"] = self.process.run(
                        operation="verify",
                        root=output,
                        plan=plan,
                        output=output / "run",
                        nonce=secrets.token_hex(32),
                        checkpoint=checkpoint("verifying"),
                        should_cancel=cancelled,
                    )
                    verification = operations["verify"]
                    if verification["reason"]:
                        state = (
                            "cancelled"
                            if verification["reason"] == "cancelled"
                            else ("timeout" if verification["reason"] == "timeout" else "failed")
                        )
                    else:
                        receipt = parse_json((output / "verify.stdout.json").read_bytes())
                        manifest = parse_json((output / "run/manifest.json").read_bytes())
                        state = (
                            "completed"
                            if (
                                execution["exit_code"] == 0
                                and verification["exit_code"] == 0
                                and manifest.get("run_status") == "completed"
                                and receipt.get("pass") is True
                                and receipt.get("coverage_complete") is True
                                and type(receipt.get("records_checked")) is int
                                and receipt["records_checked"] > 0
                                and receipt["records_checked"]
                                == receipt.get("expected_records")
                                == manifest.get("record_count")
                            )
                            else "failed"
                        )
                        if manifest.get("run_status") == "timeout":
                            state = "timeout"
                    detail = {"phase": "verification"}
                else:
                    state, detail = "failed", {"phase": "run", "exit_code": execution["exit_code"]}
        except Exception as error:
            if last_process is not None and not terminate_owned(last_process):
                self.store.unknown(
                    attempt_id, {"phase": "cleanup", "error": "Owned subprocess is unresolved"}, owner=owner
                )
                return {"attempt_id": attempt_id, "state": "unknown"}
            state, detail = "failed", {"phase": "execution", "error": safe_process_output(str(error))["text"]}
        if cancelled():
            state = "cancelled"
        try:
            return self._seal(attempt, request, owner, output, state, detail, operations, derivation)
        except AssessmentConflict:
            current = self.store.attempt(attempt_id)
            if current["owner_token"] == owner and current["cancel_requested"] and current["state"] != "unknown":
                return self._seal(
                    attempt,
                    request,
                    owner,
                    output,
                    "cancelled",
                    {"phase": "before_result_commit"},
                    operations,
                    derivation,
                )
            self.store.unknown(
                attempt_id, {"phase": "result_commit", "error": "Execution ownership is unresolved"}, owner=owner
            )
            return {"attempt_id": attempt_id, "state": "unknown"}
        except Exception as error:
            self.store.unknown(
                attempt_id, {"phase": "result_sealing", "error": safe_process_output(str(error))["text"]}, owner=owner
            )
            raise

    def _seal(self, attempt, request, owner, output, state, detail, operations, derivation):
        attempt_id = attempt["attempt_id"]
        observed = self.store.attempt(attempt_id)["process"]
        if observed is not None and resource_state(observed) != "dead":
            raise AssessmentConflict("Assessment process group is unresolved before result sealing")
        self.store.heartbeat(attempt_id, owner, phase="sealing", process=None)
        limitations = [
            "Assessment is limited to the supplied model and declared parameter provenance.",
            "A completed assessment is not hardware, thermal or sim-to-real qualification.",
            "Original execution/feasibility/verification axes remain separate from the receipt.",
            "Engineering warnings remain in the bound source certificates and the source run warning view.",
        ]
        provider = None
        verification = None
        metric_rows = {}
        canonical_axes = None
        provider_report = output / "run/report.json"
        if provider_report.is_file():
            try:
                raw_report = parse_json(provider_report.read_bytes())
                provider = {
                    key: raw_report.get(key)
                    for key in ("records", "run_status", "qualification_scope", "production_engineering_qualification")
                }
                reported = raw_report.get("limitations")
                require(
                    isinstance(reported, list) and all(isinstance(item, str) for item in reported),
                    "Teleop report limitations are invalid",
                )
                limitations = list(dict.fromkeys([*limitations, *reported]))
            except (OSError, ValueError):
                limitations.append("Provider report limitations could not be parsed; original bytes are retained.")
        if (output / "verify.stdout.json").is_file():
            try:
                raw_verification = parse_json((output / "verify.stdout.json").read_bytes())
                verification = {
                    key: raw_verification.get(key)
                    for key in ("pass", "records_checked", "expected_records", "coverage_complete")
                }
                verification["failure_count"] = len(raw_verification.get("failures", []))
            except (OSError, ValueError, TypeError):
                limitations.append("Independent receipt summary is unavailable; original bytes are retained.")
        if (output / "run/metrics.json").is_file():
            try:
                raw_metrics = parse_json((output / "run/metrics.json").read_bytes())
                fields = (
                    "joint_tracking_max_normalized",
                    "joint_tracking_rmse_rad",
                    "joint_tracking_rmse_m",
                    "tcp_position_error_m",
                    "tcp_orientation_error_rad",
                    "ctrl_saturation_fraction",
                    "actuator_force_limit_fraction",
                    "native_warning_count",
                )
                metric_rows = {
                    name: {key: row.get(key) for key in fields} for name, row in raw_metrics["tasks"].items()
                }
            except (OSError, ValueError, KeyError, TypeError, AttributeError):
                limitations.append("Provider metric summary is unavailable; original bytes are retained.")
        if (output / "run/records.jsonl").is_file():
            try:
                counts = {axis: Counter() for axis in ("execution", "feasibility", "verification")}
                with (output / "run/records.jsonl").open() as records:
                    for line in records:
                        record = parse_json(line.encode())
                        for axis in counts:
                            require(isinstance(record.get(axis), str), "Canonical record axis is invalid")
                            counts[axis][record[axis]] += 1
                canonical_axes = {axis: dict(values) for axis, values in counts.items()}
            except (OSError, ValueError):
                limitations.append("Canonical record counts are unavailable; partial original records are retained.")
        summary = {
            "schema": RESULT_SCHEMA,
            "request_id": attempt["request_id"],
            "attempt_id": attempt_id,
            "state": state,
            "resources_settled": True,
            "request_sha256": request.sha256,
            "runtime_sha256": request.runtime.sha256,
            "source_sha256": request.source.sha256,
            "source_certificate_members": {
                role: "engineering/" + role + "-certificate/" + member["path"]
                for role, member in request.source.record["certificates"].items()
            },
            "execution_model": derivation,
            "operations": operations,
            "detail": detail,
            "canonical_axes_modified": False,
            "publish_dependency": False,
            "dynamic_recording": provider,
            "independent_verification": verification,
            "reported_metrics": metric_rows,
            "canonical_axes": canonical_axes,
            "limitations": limitations,
            "teleop_report": "run/report.json" if provider_report.is_file() else None,
            "verification_receipt": "verify.stdout.json" if (output / "verify.stdout.json").is_file() else None,
        }
        summary_path = output / "assessment_result.json"
        summary_bytes = canonical(summary) + b"\n"
        if summary_path.exists() and summary_path.read_bytes() != summary_bytes:
            original = summary_path.read_bytes()
            retained = output / "interrupted" / (hashlib.sha256(original).hexdigest() + ".json")
            retained.parent.mkdir(exist_ok=True)
            if retained.exists():
                require(retained.read_bytes() == original, "Retained interrupted summary differs")
            else:
                retained.write_bytes(original)
        summary_path.write_bytes(summary_bytes)
        with self._renew(attempt_id, owner, "sealing"):
            artifact = self.store.artifacts.seal_tree(
                output,
                kind="dynamics_result",
                producer_run_id=attempt_id,
                producer_stage=None,
                producer_tool_sha256=request.record["executor_sha256"],
            )
        result = self.store.finish(attempt_id, owner, state=state, artifact=artifact, detail=detail)
        return {"attempt_id": attempt_id, "state": result["state"], "result_artifact": artifact.to_record()}

    def cancel_owned(self, attempt_id: str) -> bool:
        """A kill hook requests cancellation; it never kills by an unbound PID."""
        attempt = self.store.cancel(attempt_id)
        identity = attempt["process"]
        return terminate_owned(identity) if identity is not None else attempt["resources_settled"]

    def reconcile(self, attempt_id: str) -> dict:
        """An orphan remains UNKNOWN until its exact controller and child are settled."""
        attempt = self.store.attempt(attempt_id)
        if attempt["state"] in TERMINAL_STATES:
            return {"attempt_id": attempt_id, "state": attempt["state"]}
        controller = attempt["owner_process"]
        if controller is None or process_state(controller) != "dead":
            return {"attempt_id": attempt_id, "state": "unknown"}
        if attempt["process"] is not None and not terminate_owned(attempt["process"]):
            return {"attempt_id": attempt_id, "state": "unknown"}
        owner = secrets.token_hex(32)
        self.store.claim_reconciliation(
            attempt_id, attempt["owner_token"], owner, controller=process_identity(os.getpid(), owner)
        )
        request = self.store.request(attempt["request_id"])
        output = self._work(attempt_id) / "result"
        if not output.is_dir() or path_has_link_ancestor(output):
            self.store.unknown(
                attempt_id, {"phase": "reconciliation", "error": "Original evidence is unavailable"}, owner=owner
            )
            return {"attempt_id": attempt_id, "state": "unknown"}
        state = "cancelled" if attempt["cancel_requested"] else "failed"
        return self._seal(
            attempt,
            request,
            owner,
            output,
            state,
            {
                "phase": "orphan_reconciliation",
                "error": "Interrupted attempt had no committed result",
                "previous_owner": attempt["owner_token"],
                "previous_controller": controller,
                "previous_process": attempt["process"],
            },
            {},
            None,
        )
