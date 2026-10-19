"""The owned CI scenario: real producer -> platform -> actual generator/verifier.

Fixture CAD data and a native-role stamp; a byte-copy endpoint and FakeWorld SQL.
No producer/verifier/qualification gate is stubbed and no native artifact is
added or rewritten after capture.  The scenario asserts the exact control-failure
set, the required control gates, the subject binding and the registered sealed
failed-quality bytes - the behaviour the retired LinuxStore roundtrip test used
to cover.  This is not real PG/scheduler/Windows/UI evidence.
"""

from __future__ import annotations

import copy
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from description_pipeline.io import digest, file_digest, inventory, read_data
from description_pipeline.orchestration.airflow_client import native_run_id
from description_pipeline.platform import capture, flow
from description_pipeline.platform.storage import sealed_resolver
from description_pipeline.platform.store import PlatformStore
from description_pipeline.request_evidence import carry_request_evidence, freeze_request_evidence
from description_pipeline.sources.solidworks.selection import seal_closure_selection

from tests.v1 import split_capture_fixture as producer
from tests.v1.test_airflow_dag import DagBag, DAG_DIR
from tests.v1.test_platform_store import FakeWorld

DAG_RUN = "portal-canonical-roundtrip"
RUN = native_run_id(DAG_RUN)
MAIN = "cad/robot.SLDASM"
REQUEST_ID = f"portal-{DAG_RUN}"


class ByteEndpoint:
    def __init__(self, payload):
        self.payload = payload
        self.calls = 0

    def stream_capture_archive(self, run_id, archive, destination, **kwargs):
        self.calls += 1
        target = Path(destination)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(self.payload)
        return target


def _run_scenario(root: Path) -> dict:
    report: dict = {"calls": [], "ok": False}
    store_root = root / "store"
    store_root.mkdir()
    bag = DagBag(str(DAG_DIR))
    module = sys.modules[bag.dags["solidworks_to_urdf"].task_dict["freeze"].python_callable.__module__]
    world = FakeWorld()
    store = PlatformStore(world.connect, sealed=sealed_resolver(store_root))
    paths = flow.RunPaths(store_root, RUN)
    native_root = root / "producer/native"
    for name in ("cad/robot.SLDASM", "cad/base.SLDPRT", "cad/arm.SLDPRT"):
        path = native_root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(f"placeholder {name}\n".encode())
    upload = inventory(native_root)
    handoff = digest(upload)
    store.admit_run(
        request_id=REQUEST_ID,
        run_id=RUN,
        principal="portal",
        target_hardware="nd_fixture",
        main_assembly=MAIN,
        configuration={"purpose": "engineering"},
        dispatch={
            "stage": "freeze",
            "operation_key": f"portal-{DAG_RUN}/freeze:0",
            "params_digest": "a" * 64,
            "dag_id": "solidworks_to_urdf",
            "dag_run_id": DAG_RUN,
        },
        baseline={
            "platform_config_digest": "b" * 64,
            "handoff_sha256": handoff,
            "files": len(upload),
            "bytes": sum((native_root / name).stat().st_size for name in upload),
            "folder": native_root.name,
        },
    )
    freeze_request_evidence(
        paths.run_dir,
        purpose="engineering",
        request_id=REQUEST_ID,
        target_hardware="nd_fixture",
        change_summary="Independent control roundtrip",
        main_assembly=MAIN,
        handoff_digest=handoff,
        platform_config_digest="b" * 64,
    )
    tool = producer.native_twin()
    raw = copy.deepcopy(producer.capture_record())
    selection = raw["source_selection"]
    closure = copy.deepcopy(selection["closure"])
    closure["reader_sha256"] = tool["source_sha256"]
    raw["source_selection"] = seal_closure_selection(upload, MAIN, closure, reader_sha256=tool["source_sha256"])
    phase = "freeze"
    with patch.object(module, "_platform_binding", return_value=(store, store_root)):
        try:
            module._bind_initial_dispatch(
                store,
                run_id=RUN,
                context={
                    "dag_run": SimpleNamespace(run_id=DAG_RUN),
                    "ti": SimpleNamespace(task_id="freeze", map_index=-1),
                },
            )
            report["initial_dispatch"] = world.operation_bindings[
                world.operations[f"portal-{DAG_RUN}/freeze:0"][0]
            ]
            module._register_freeze_stage(
                paths,
                RUN,
                request_id=REQUEST_ID,
                intake=native_root,
                upload_files=upload,
                main_assembly=MAIN,
                config_digest="b" * 64,
            )
            report["calls"].append("DAG._register_freeze_stage")
            phase = "producer"
            with patch.object(producer, "capture_record", return_value=raw):
                native = producer.build_fixture_capture(root / "producer", run_id=RUN, purpose="engineering")
            assert inventory(native_root) == upload
            report["calls"].append("shared.build_fixture_capture: real freeze/discover/capture")
            phase = "import"
            endpoint = ByteEndpoint(native["archive"].read_bytes())
            selection = raw["source_selection"]
            job = {
                "status": "native_complete",
                "run_id": RUN,
                "main_assembly": MAIN,
                "native_tool": native["native_tool"],
                "handoff_sha256": native["handoff_sha256"],
                "package_files": selection["files"],
                "request": {
                    "run_id": RUN,
                    "upload_files": upload,
                    "upload_sha256": digest(upload),
                    "main_assembly": MAIN,
                },
                "selection": {
                    "state": "sealed",
                    "reader_sha256": tool["source_sha256"],
                    "upload_sha256": digest(upload),
                    "staged_files": upload,
                    "manifest": selection,
                },
                "result": {
                    "native_complete": True,
                    "native_tool": native["native_tool"],
                    "capture_archive": native["receipt"],
                },
            }
            admitted = capture.import_native_capture(store, paths, RUN, job, endpoint)
            report["calls"].append("platform.capture.import_native_capture")
            report["admitted_state"] = admitted["state"]
            report["downloads"] = endpoint.calls
            carry_request_evidence(paths.run_dir, paths.capture_dir)
            before = inventory(paths.capture_dir)
            phase = "native_registration"
            module._register_native_stages(store, store_root, RUN, paths.capture_dir, paths.run_dir)
            report["calls"].append("DAG._register_native_stages")
            assert inventory(paths.capture_dir) == before
            phase = "generate"
            generated = module._portable_registered(RUN, "generate", tree=paths.stage_output("generate"))
            report["calls"].append("DAG._portable_registered(generate): actual flow/solidworks.generate")
            phase = "verify"
            try:
                module._portable_registered(
                    RUN, "verify", tree=paths.stage_output("verify"), expected_subject=generated["subject_sha256"]
                )
                report["unexpected_verification_acceptance"] = True
            except Exception as exc:  # noqa: BLE001 - the control fixture must fail its release gates
                report["verification_exception"] = {"type": type(exc).__name__, "message": str(exc)}
            report["calls"].append("DAG._portable_registered(verify): actual flow/independent verifier")
            verify_output = paths.stage_output("verify")
            quality_path = verify_output / "reports/quality.json"
            if not quality_path.is_file():
                quality_path = verify_output.with_name(verify_output.name + ".failed") / "reports/quality.json"
            quality = read_data(quality_path)
            failed = sorted(
                row["id"]
                for row in quality.get("checks", [])
                if row.get("passed") is False or row.get("state") == "failed"
            )
            rows = {row["id"]: row for row in quality.get("checks", [])}
            report["quality_failed_checks"] = failed
            report["allowed_control_failures"] = sorted(producer.CONTROL_LIMITS)
            report["required_control_gates_pass"] = all(
                rows.get(key, {}).get("passed") is True for key in producer.REQUIRED_CONTROL_GATES
            )
            report["quality_subject_matches_generated"] = (
                quality.get("subject_sha256") == generated["subject_sha256"]
                and quality.get("subject_sha256") == rows.get("bundle.subject", {}).get("details", {}).get("sha256")
            )
            stage = store.stage_records(RUN).get("verify") or {}
            report["failed_registration_has_checkpoint"] = bool(stage.get("checkpoint_uri"))
            report["failed_quality_bytes_bound"] = False
            if stage.get("checkpoint_uri"):
                sealed_quality = store.sealed_path(stage["checkpoint_uri"]) / "reports/quality.json"
                registered_quality = next(
                    (
                        record
                        for record in (stage.get("receipt") or {}).get("artifacts", [])
                        if record.get("path") == "reports/quality.json"
                    ),
                    None,
                )
                report["failed_quality_bytes_bound"] = (
                    sealed_quality.is_file()
                    and file_digest(sealed_quality) == file_digest(quality_path)
                    and registered_quality is not None
                    and registered_quality.get("sha256") == file_digest(quality_path)
                )
            report["ok"] = (
                report.get("initial_dispatch") == ("solidworks_to_urdf", DAG_RUN, "freeze", -1)
                and bool(failed)
                and set(failed) <= producer.CONTROL_LIMITS
                and not report.get("unexpected_verification_acceptance")
                and report["required_control_gates_pass"]
                and report["quality_subject_matches_generated"]
                and stage.get("state") == "failed"
                and report["failed_quality_bytes_bound"]
            )
        except Exception as exc:  # noqa: BLE001 - surfaced as the blocked phase in the report
            report["blocked_phase"] = phase
            report["error"] = {"type": type(exc).__name__, "message": str(exc)}
        report["stage_states"] = {key: value.get("state") for key, value in store.stage_records(RUN).items()}
    return report


class CanonicalPlatformRoundtripTests(unittest.TestCase):
    def test_the_real_producer_registers_and_the_actual_tail_verifies(self) -> None:
        with tempfile.TemporaryDirectory(prefix="canonical-platform-roundtrip-") as temporary:
            report = _run_scenario(Path(temporary))
        self.assertEqual(report.get("initial_dispatch"), ("solidworks_to_urdf", DAG_RUN, "freeze", -1), report)
        self.assertEqual(report.get("admitted_state"), "capture_admitted", report)
        self.assertEqual(report.get("downloads"), 1, report)
        self.assertFalse(report.get("unexpected_verification_acceptance"), report)
        self.assertTrue(set(report["quality_failed_checks"]) <= producer.CONTROL_LIMITS, report)
        self.assertTrue(report["required_control_gates_pass"], report)
        self.assertTrue(report["quality_subject_matches_generated"], report)
        self.assertTrue(report["failed_registration_has_checkpoint"], report)
        self.assertTrue(report["failed_quality_bytes_bound"], report)
        self.assertTrue(report["ok"], report)


if __name__ == "__main__":
    unittest.main()
