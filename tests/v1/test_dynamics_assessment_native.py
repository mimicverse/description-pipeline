"""Real fixed-wheel dynamics behind private PostgreSQL and immutable artifacts.

The dispatcher and authenticated source callback are isolated test adapters.
These tests do not claim formal Airflow, SSO, Publish or robot qualification.
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import multiprocessing
import os
import shutil
import signal
import subprocess
import threading
import time
import unittest
from contextlib import suppress
from pathlib import Path
from unittest import mock

from description_pipeline.dynamics_assessment.contracts import (
    BUNDLE_SCHEMA,
    SOURCE_SCHEMA,
    MemberRef,
    RuntimePin,
    SourceBinding,
    dispatch_conf,
)
from description_pipeline.dynamics_assessment.executor import AssessmentExecutor
from description_pipeline.owned_process import process_state, terminate_owned
from description_pipeline.dynamics_assessment.runtime import (
    ProcessLimits,
    TeleopProcess,
)
from description_pipeline.dynamics_assessment.service import AssessmentAccess, AssessmentService
from description_pipeline.io import canonical, digest, file_digest
from tests.v1.test_dynamics_assessment_postgres import PostgresCase


class Dispatch:
    def __init__(self):
        self.runs = {}
        self.sends = 0

    def trigger(self, dag_id, dag_run_id, conf):
        self.sends += 1
        record = {"dag_id": dag_id, "dag_run_id": dag_run_id, "conf": conf, "state": "queued"}
        self.runs[dag_run_id] = record
        return record

    def observe(self, _dag_id, dag_run_id):
        return self.runs.get(dag_run_id)


def build_native_case(database, modes=None):
    fixture = os.environ.get("ASSESSMENT_TEST_TELEOP_PLAN")
    interpreter = os.environ.get("ASSESSMENT_TEST_TELEOP_PYTHON")
    pin_path = os.environ.get("ASSESSMENT_TEST_RUNTIME_PIN")
    if not all((fixture, interpreter, pin_path)):
        raise unittest.SkipTest("A fixed installed Teleop runtime and independent software input are required")
    store, _old_request, root = database
    modes = {modes} if isinstance(modes, str) else set(modes or ())
    plan_path = Path(fixture)
    plan = json.loads(plan_path.read_bytes())
    plan.pop("_plan_path", None)
    copied = root / "software-fixture"
    copied.mkdir()
    shutil.copytree(plan_path.parent / plan["input_closure"]["root"], copied / "inputs")
    plan["input_closure"]["root"] = "inputs"
    if "long_run" in modes:
        task = copied / "inputs" / plan["tasks"]["hold"]["path"]
        specification = json.loads(task.read_bytes())
        specification["duration_s"] = 2.0
        specification["schedule"][-1]["time_s"] = 2.0
        specification["hold_window_s"] = 0.2
        task.write_bytes(canonical(specification) + b"\n")
        for member in plan["input_closure"]["files"]:
            if member["path"] == plan["tasks"]["hold"]["path"]:
                member.update(bytes=task.stat().st_size, sha256=file_digest(task))
        plan["tasks"]["hold"]["sha256"] = file_digest(task)
    (copied / "plan.json").write_bytes(canonical(plan) + b"\n")
    original = root / "software-engineering-source"
    original.mkdir()
    for role in ("urdf", "mjcf"):
        path = plan["input_closure"][role]["path"]
        target = original / path
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(copied / "inputs" / path, target)
        warnings = [{"code": "SOFTWARE_BENCHMARK_ONLY", "scope": "software_regression_only"}]
        if "large_certificates" in modes:
            warnings = [
                {"code": "SOFTWARE_SIZE_BOUNDARY", "locator": str(index), "detail": "x" * 400} for index in range(7000)
            ]
        (original / (role + "-source.json")).write_bytes(
            canonical(
                {
                    "schema": "isolated-software-source/v1",
                    "scope": "software_regression_only",
                    "path": path,
                    "sha256": file_digest(target),
                    "formal_engineering_certificate": False,
                    "warnings": warnings,
                }
            )
            + b"\n"
        )
    artifact = store.artifacts.seal_tree(
        original,
        kind="software_benchmark_source",
        producer_run_id="source-run",
        producer_stage=None,
        producer_tool_sha256=digest({"fixture": "source"}),
    )
    store.platform.register_artifact(artifact)

    def member(name):
        content = (original / name).read_bytes()
        return MemberRef(artifact, name, len(content), hashlib.sha256(content).hexdigest()).to_record()

    source = SourceBinding(
        {
            "schema": SOURCE_SCHEMA,
            "run_id": "source-run",
            "dag_run_id": "source-dag-run",
            "subject_sha256": plan["input_closure"]["mjcf"]["sha256"],
            "urdf": member(plan["input_closure"]["urdf"]["path"]),
            "mjcf": member(plan["input_closure"]["mjcf"]["path"]),
            "certificates": {"urdf": member("urdf-source.json"), "mjcf": member("mjcf-source.json")},
        }
    )
    envelope = {
        "schema": BUNDLE_SCHEMA,
        "source_sha256": source.sha256,
        "plan": "plan.json",
        "model_derivation": {
            "kind": "source_exact",
            "source_mjcf_sha256": source.record["mjcf"]["sha256"],
            "execution_mjcf_sha256": plan["input_closure"]["mjcf"]["sha256"],
            "provenance": {"scope": "software_regression_only"},
        },
    }
    (copied / "assessment.json").write_bytes(canonical(envelope) + b"\n")
    bundle = store.artifacts.seal_tree(
        copied,
        kind="dynamics_execution_bundle",
        producer_run_id=None,
        producer_stage=None,
        producer_tool_sha256=digest({"fixture": "bundle"}),
    )
    runtime = RuntimePin(json.loads(Path(pin_path).read_bytes()))
    work = root / "work"
    work.mkdir()
    module = Path(__file__).resolve().parents[2] / "src/description_pipeline/dynamics_assessment"
    executor_sha = digest({p.name: file_digest(p) for p in sorted(module.glob("*.py"))})
    service = AssessmentService(store, enabled=True, runtime=runtime, executor_sha256=executor_sha, work_root=work)
    process = TeleopProcess(Path(interpreter), runtime, ProcessLimits(120, 2 * 1024**3, 60, 256 * 1024**2))

    def authorized(dag, _control):
        assert dag == source.record["dag_run_id"]
        return source

    access = AssessmentAccess("tenant:owner", False, authorized)
    return (service, AssessmentExecutor(service, process), access, bundle, copied, original)


def execute_case(native_case, bundle_override=False):
    service, executor, access, bundle, *_ = native_case
    transport = Dispatch()
    public = service.create(access, "source-dag-run", "native-case", None if bundle_override else bundle, transport)
    attempt = public["attempt"]
    request = service.store.request(public["request_id"])
    result = executor.execute(
        attempt["attempt_id"], dispatch_conf(public["request_id"], attempt["attempt_id"], request)
    )
    return (public, result)


def start_controller(native_case):
    service, executor, access, bundle, *_ = native_case
    public = service.create(access, "source-dag-run", "native-interruption", bundle, Dispatch())
    attempt_id = public["attempt"]["attempt_id"]
    request = service.store.request(public["request_id"])
    controller = multiprocessing.get_context("fork").Process(
        target=executor.execute, args=(attempt_id, dispatch_conf(public["request_id"], attempt_id, request))
    )
    libc = ctypes.CDLL(None, use_errno=True)
    previous = ctypes.c_int()
    assert libc.prctl(37, ctypes.byref(previous), 0, 0, 0) == 0
    assert libc.prctl(36, 1, 0, 0, 0) == 0
    controller._assessment_previous_subreaper = previous.value
    try:
        controller.start()
    except BaseException:
        libc.prctl(36, previous.value, 0, 0, 0)
        raise
    return (public, attempt_id, controller)


def wait_for_attempt(service, attempt_id, controller, predicate):
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        attempt = service.store.attempt(attempt_id)
        if predicate(attempt):
            return attempt
        assert controller.is_alive(), attempt
        time.sleep(0.005)
    unittest.TestCase().fail("The native controller did not reach the expected interruption point")


def cleanup_controller(service, attempt_id, controller):
    try:
        if controller.is_alive():
            controller.kill()
        controller.join(timeout=5)
        identity = service.store.attempt(attempt_id)["process"]
        if identity is not None:

            def reap():
                with suppress(ChildProcessError):
                    os.waitpid(identity["pid"], os.WNOHANG)

            assert terminate_owned(identity, reap=reap), identity
    finally:
        reaper = getattr(controller, "_assessment_reaper", None)
        if reaper is not None:
            reaper[0].set()
            reaper[1].join(timeout=5)
        ctypes.CDLL(None).prctl(36, controller._assessment_previous_subreaper, 0, 0, 0)


def reap_orphan(controller, identity):
    """Perform real parent reaping so the test can require kernel group absence."""
    stopped = threading.Event()

    def reap():
        while not stopped.wait(0.01):
            with suppress(ChildProcessError):
                if os.waitpid(identity["pid"], os.WNOHANG)[0] == identity["pid"]:
                    return

    thread = threading.Thread(target=reap)
    controller._assessment_reaper = (stopped, thread)
    thread.start()


class NativeDatabaseTests(PostgresCase):
    def _check_actual_native_execution_verification_and_detached_archive(self, case_modes):
        tmp_path = self.root
        native_case = build_native_case(self.database, case_modes)
        service, _executor, access, _bundle, copied, original = native_case
        public, result = execute_case(native_case)
        assert result["state"] == "completed"
        summary_bytes, _member = service.read_result(access, result["attempt_id"])
        summary = json.loads(summary_bytes)
        assert summary["operations"]["run"]["exit_code"] == summary["operations"]["verify"]["exit_code"] == 0
        assert summary["canonical_axes_modified"] is False and summary["publish_dependency"] is False
        receipt_bytes, _member = service.read_result(access, result["attempt_id"], "verify.stdout.json")
        receipt = json.loads(receipt_bytes)
        assert receipt["pass"] and receipt["coverage_complete"] and (receipt["records_checked"] > 0)
        reference = service.store.result_artifact(result["attempt_id"])
        archive = tmp_path / "detached"
        service.store.artifacts.materialize(reference, archive)
        shutil.rmtree(copied)
        shutil.rmtree(original)
        records = [json.loads(line) for line in (archive / "run/records.jsonl").read_text().splitlines()]
        assert len(records) == receipt["records_checked"]
        assert {row["verification"] for row in records} == {"unchecked"}
        request_bytes = (archive / "request.json").read_bytes()
        assert len(request_bytes) < 1024 * 1024
        assert "warnings" not in json.loads(request_bytes)["source"]
        for role, path in summary["source_certificate_members"].items():
            declared = json.loads(request_bytes)["source"]["certificates"][role]
            data = (archive / path).read_bytes()
            assert len(data) == declared["bytes"] and hashlib.sha256(data).hexdigest() == declared["sha256"]
            assert json.loads(data)["warnings"]
        command = [
            os.environ["ASSESSMENT_TEST_TELEOP_PYTHON"],
            "-I",
            "-B",
            "-m",
            "teleop.evaluation",
            "verify",
            "--run",
            str(archive / "run"),
        ]
        verified = subprocess.run(command, cwd=archive, capture_output=True, timeout=120, check=False)
        assert verified.returncode == 0, verified.stderr.decode()
        assert json.loads(verified.stdout)["pass"] is True
        assert service.get(access, public["request_id"])["attempt"]["state"] == "completed"

    def test_actual_native_execution_verification_and_detached_archive_case_1(self):
        self._check_actual_native_execution_verification_and_detached_archive(None)

    def test_actual_native_execution_verification_and_detached_archive_case_2(self):
        self._check_actual_native_execution_verification_and_detached_archive("large_certificates")

    def test_real_source_without_execution_bundle_is_explicitly_unsupported(self):
        native_case = build_native_case(self.database, None)
        service, _executor, access, *_ = native_case
        _public, result = execute_case(native_case, bundle_override=True)
        assert result["state"] == "unsupported"
        data, _member = service.read_result(access, result["attempt_id"])
        summary = json.loads(data)
        assert summary["detail"]["missing"] == ["sealed_execution_bundle"]
        assert summary["operations"] == {}
        assert summary["verification_receipt"] is None

    def test_wrong_installed_runtime_refuses_before_model_execution(self):
        native_case = build_native_case(self.database, None)
        service, executor, access, _bundle, *_ = native_case
        record = service.runtime.to_record()
        name = next(iter(record["files"]))
        record["files"][name] = "0" * 64
        service.runtime = RuntimePin(record)
        executor.process = TeleopProcess(executor.process.python, service.runtime, executor.process.limits)
        public, result = execute_case(native_case)
        assert result["state"] == "failed"
        data, _member = service.read_result(access, result["attempt_id"], "run.stdout.json")
        assert json.loads(data)["assessment_error"] == "runtime_identity"
        assert service.get(access, public["request_id"])["attempt"]["resources_settled"] is True

    def test_result_reads_reauthorize_and_reject_cross_operator(self):
        native_case = build_native_case(self.database, None)
        service, _executor, access, *_ = native_case
        _public, result = execute_case(native_case, bundle_override=True)
        other = AssessmentAccess("tenant:other", False, access.source)
        with self.assertRaisesRegex(PermissionError, "another operator"):
            service.read_result(other, result["attempt_id"])

        def revoked(_dag, _control):
            raise PermissionError("Source access was revoked")

        with self.assertRaisesRegex(PermissionError, "revoked"):
            service.read_result(AssessmentAccess(access.principal, False, revoked), result["attempt_id"])

    def _check_owned_native_cancel_preserves_partial_evidence(self, case_modes, phase):
        native_case = build_native_case(self.database, case_modes)
        service, _executor, access, *_ = native_case
        _public, attempt_id, controller = start_controller(native_case)
        records = service.work_root / attempt_id / "result/run/records.jsonl"
        try:

            def executing(attempt):
                return (
                    attempt["state"] == phase
                    and attempt["process"] is not None
                    and records.is_file()
                    and (records.stat().st_size > 0)
                )

            attempt = wait_for_attempt(service, attempt_id, controller, executing)
            identity = attempt["process"]
            service.cancel(access, attempt_id)
            controller.join(timeout=20)
            assert not controller.is_alive() and controller.exitcode == 0
            assert process_state(identity) == "dead"
            settled = service.store.attempt(attempt_id)
            assert settled["state"] == "cancelled" and settled["resources_settled"]
            data, _member = service.read_result(access, attempt_id)
            summary = json.loads(data)
            operation = "run" if phase == "running" else "verify"
            assert summary["operations"][operation]["reason"] == "cancelled"
            assert summary["canonical_axes_modified"] is False
            archive = service.work_root / (attempt_id + "-detached")
            service.store.artifacts.materialize(service.store.result_artifact(attempt_id), archive)
            assert (archive / "run/records.jsonl").read_bytes() == records.read_bytes()
        finally:
            cleanup_controller(service, attempt_id, controller)

    def test_owned_native_cancel_preserves_partial_evidence_case_1(self):
        self._check_owned_native_cancel_preserves_partial_evidence("long_run", "running")

    def test_owned_native_cancel_preserves_partial_evidence_case_2(self):
        self._check_owned_native_cancel_preserves_partial_evidence("long_run", "verifying")

    def _check_native_controller_sigkill_requires_original_orphan_reconciliation(self, case_modes):
        native_case = build_native_case(self.database, case_modes)
        service, executor, access, *_ = native_case
        public, attempt_id, controller = start_controller(native_case)
        records = service.work_root / attempt_id / "result/run/records.jsonl"
        try:
            attempt = wait_for_attempt(
                service,
                attempt_id,
                controller,
                lambda row: (
                    row["state"] == "running"
                    and row["process"] is not None
                    and records.is_file()
                    and (records.stat().st_size > 0)
                ),
            )
            child = attempt["process"]
            assert process_state(child) == "alive"
            os.kill(child["pid"], signal.SIGSTOP)
            controller.kill()
            controller.join(timeout=5)
            assert controller.exitcode == -signal.SIGKILL
            reap_orphan(controller, child)
            original = records.read_bytes()
            with service.store.transaction() as cursor:
                cursor.execute(
                    "UPDATE assessment_attempt SET lease_until=now()-interval '1 second' WHERE attempt_id=%s",
                    (attempt_id,),
                )
            assert service.get(access, public["request_id"])["attempt"]["state"] == "unknown"
            with self.assertRaisesRegex(ValueError, "settled"):
                service.retry(access, public["request_id"], Dispatch())
            result = executor.reconcile(attempt_id)
            assert result["state"] == "failed"
            assert process_state(child) == "dead"
            assert records.read_bytes() == original
            summary = json.loads(service.read_result(access, attempt_id)[0])
            assert summary["detail"]["phase"] == "orphan_reconciliation"
            assert summary["detail"]["previous_process"] == child
            assert summary["operations"] == {}
            for role, path in summary["source_certificate_members"].items():
                original_certificate = native_case[-1] / (role + "-source.json")
                assert service.read_result(access, attempt_id, path)[0] == original_certificate.read_bytes()
            assert service.store.attempt(attempt_id)["resources_settled"]
            assert executor.reconcile(attempt_id)["state"] == "failed"
        finally:
            cleanup_controller(service, attempt_id, controller)

    def test_native_controller_sigkill_requires_original_orphan_reconciliation_case_1(self):
        self._check_native_controller_sigkill_requires_original_orphan_reconciliation("long_run")

    def test_native_controller_sigkill_requires_original_orphan_reconciliation_case_2(self):
        self._check_native_controller_sigkill_requires_original_orphan_reconciliation(
            ("long_run", "large_certificates")
        )

    def _check_cancel_during_real_sealing_retains_interrupted_summary(self, case_modes):
        native_case = build_native_case(self.database, case_modes)
        service, _executor, access, *_ = native_case
        seal = service.store.artifacts.seal_tree
        interrupted = []

        def cancel_at_seal(root, **kwargs):
            if kwargs["kind"] == "dynamics_result" and (not interrupted):
                interrupted.append((root / "assessment_result.json").read_bytes())
                attempt_id = json.loads(interrupted[0])["attempt_id"]
                service.cancel(access, attempt_id)
            return seal(root, **kwargs)

        self.enterContext(mock.patch.object(service.store.artifacts, "seal_tree", cancel_at_seal))
        _public, result = execute_case(native_case)
        assert result["state"] == "cancelled"
        summary = json.loads(service.read_result(access, result["attempt_id"])[0])
        assert summary["detail"]["phase"] == "before_result_commit"
        retained = "interrupted/" + hashlib.sha256(interrupted[0]).hexdigest() + ".json"
        assert service.read_result(access, result["attempt_id"], retained)[0] == interrupted[0]
        assert json.loads(interrupted[0])["state"] == "completed"
        for role, path in summary["source_certificate_members"].items():
            assert (
                service.read_result(access, result["attempt_id"], path)[0]
                == (native_case[-1] / (role + "-source.json")).read_bytes()
            )

    def test_cancel_during_real_sealing_retains_interrupted_summary_case_1(self):
        self._check_cancel_during_real_sealing_retains_interrupted_summary(None)

    def test_cancel_during_real_sealing_retains_interrupted_summary_case_2(self):
        self._check_cancel_during_real_sealing_retains_interrupted_summary("large_certificates")

    def test_native_wall_budget_cannot_report_completion(self):
        native_case = build_native_case(self.database, None)
        _service, executor, _access, *_ = native_case
        executor.process = TeleopProcess(
            executor.process.python, executor.process.runtime, ProcessLimits(0.01, 2 * 1024**3, 60, 256 * 1024**2)
        )
        _public, result = execute_case(native_case)
        assert result["state"] == "timeout"

    def _check_native_output_budget_cannot_report_completion(self, case_modes):
        native_case = build_native_case(self.database, case_modes)
        service, executor, access, *_ = native_case
        executor.process = TeleopProcess(
            executor.process.python, executor.process.runtime, ProcessLimits(120, 2 * 1024**3, 60, 128 * 1024)
        )
        _public, result = execute_case(native_case)
        assert result["state"] == "failed"
        summary = json.loads(service.read_result(access, result["attempt_id"])[0])
        assert summary["operations"]["run"]["exit_code"] != 0
        assert summary["independent_verification"] is None

    def test_native_output_budget_cannot_report_completion_case_1(self):
        self._check_native_output_budget_cannot_report_completion("long_run")
