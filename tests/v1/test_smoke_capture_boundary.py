"""The scheduled control endpoint consumes the real Linux definition and capture."""

from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from description_pipeline import solidworks
from description_pipeline.io import PipelineError, digest, inventory
from description_pipeline.orchestration import stage_transfer
from description_pipeline.orchestration.airflow_client import (
    EndpointConfig, EndpointConflict, WindowsEndpoint, drive_staging,
)
from description_pipeline.platform import capture, flow
from description_pipeline.platform.store import PlatformStore
from description_pipeline.platform.storage import sealed_resolver
from description_pipeline.request_evidence import freeze_request_evidence

from .split_capture_fixture import build_fixture_capture
from . import test_airflow_dag
from .test_platform_store import FakeWorld
from .test_smoke_settlement import _load_smoke


class SmokeCaptureBoundaryTests(unittest.TestCase):
    def test_real_definition_capture_and_admission_share_one_freeze_and_prepared_package(self):
        smoke = _load_smoke()
        run_id = "0b1f2c33-1f1e-4b0e-9d6a-2f9a5c7e1d20"
        with tempfile.TemporaryDirectory(prefix="smoke-capture-boundary-") as temporary:
            root = Path(temporary)
            fixture = build_fixture_capture(root / "fixture", run_id=run_id)
            intake = root / "fixture/native"
            upload = inventory(intake)
            paths = flow.RunPaths(root / "store", run_id)
            world = FakeWorld()
            store = PlatformStore(world.connect, sealed=sealed_resolver(paths.root))
            module = test_airflow_dag.DagTests()._module()
            store.admit_run(
                request_id="smoke-boundary", run_id=run_id, principal="fixture",
                target_hardware=fixture["hardware_id"], main_assembly=fixture["main_assembly"],
                configuration={"purpose": "engineering"},
                dispatch={
                    "stage": "freeze", "operation_key": "portal-smoke-boundary/freeze:0",
                    "params_digest": "a" * 64, "dag_id": "solidworks_to_urdf", "dag_run_id": "smoke-boundary",
                },
                baseline={
                    "platform_config_digest": "c" * 64, "handoff_sha256": digest(upload),
                    "files": len(upload), "bytes": sum((intake / name).stat().st_size for name in upload),
                    "folder": intake.name,
                },
            )
            freeze_request_evidence(
                paths.run_dir, purpose="engineering", request_id="smoke-boundary",
                target_hardware=fixture["hardware_id"], change_summary="Control capture boundary",
                main_assembly=fixture["main_assembly"], handoff_digest=digest(upload),
                platform_config_digest="c" * 64,
            )
            with patch.object(module, "_platform_binding", return_value=(store, paths.root)):
                module._bind_initial_dispatch(store, run_id=run_id, context={
                    "dag_run": SimpleNamespace(run_id="smoke-boundary"),
                    "ti": SimpleNamespace(task_id="freeze", map_index=-1),
                })
                events, freeze_receipt = module._register_freeze_stage(
                    paths, run_id, request_id="smoke-boundary", intake=intake,
                    upload_files=upload, main_assembly=fixture["main_assembly"], config_digest="c" * 64,
                )
            bundle = root / "definition"
            bundle.mkdir()
            server, state = smoke._mock_endpoint(
                fixture, run_id=run_id, upload_files=upload,
                record=smoke._native_record(root / "fixture"), bundle_dir=bundle,
            )
            try:
                endpoint = WindowsEndpoint(EndpointConfig(
                    base_url=f"http://127.0.0.1:{server.server_address[1]}", token=smoke.TOKEN,
                    discovery={"record_roots": (root / "fixture/records",)},
                ))
                endpoint.start_staging_job(
                    run_id=run_id, upload_files=upload, main_assembly=fixture["main_assembly"],
                    freeze_events=events, freeze_receipt_sha256=freeze_receipt,
                )
                changed = copy.deepcopy(events)
                changed[1]["check"]["details"]["package"] += "-changed"
                with self.assertRaises(EndpointConflict):
                    endpoint.start_staging_job(
                        run_id=run_id, upload_files=upload, main_assembly=fixture["main_assembly"],
                        freeze_events=changed, freeze_receipt_sha256=freeze_receipt,
                    )
                drive_staging(
                    endpoint, run_id=run_id, upload_files=upload,
                    main_assembly=fixture["main_assembly"], intake_dir=intake, batch_dir=root / "batch",
                )
                request = {
                        "run_id": run_id, "upload_files": upload, "upload_sha256": digest(upload),
                        "main_assembly": fixture["main_assembly"], "intake_dir": str(intake),
                        "purpose": "engineering", "conn_id": "mock", "platform_config_digest": "c" * 64,
                }
                original_capture = solidworks.capture
                capture_results = []

                def observed_capture(*args, **kwargs):
                    result = original_capture(*args, **kwargs)
                    capture_results.append(result)
                    return result

                with (
                    patch.object(module, "_work_paths", return_value=paths),
                    patch.object(solidworks, "capture", side_effect=observed_capture),
                ):
                    with (
                        patch.object(
                            stage_transfer, "seal_capture", side_effect=PipelineError("control sealing failure"),
                        ),
                        self.assertRaisesRegex(EndpointConflict, "control sealing failure"),
                    ):
                        module._provide_definition(endpoint, request)
                    self.assertIs(capture_results[-1]["native_complete"], False)
                    self.assertTrue(Path(capture_results[-1]["diagnostic_path"]).is_dir())
                    self.assertEqual(endpoint.get_job(run_id)["status"], "awaiting_definition")
                    module._provide_definition(endpoint, request)
                    self.assertEqual(len(capture_results), 2)
                job = endpoint.get_job(run_id)
                self.assertEqual(job["status"], "native_complete")
                actual_prepared = digest(inventory(Path(job["prepared_dir"])))
                self.assertEqual(actual_prepared, state["definition_posts"][0]["prepared_sha256"])
                self.assertEqual(digest(job["prepared_files"]), actual_prepared)
                self.assertNotEqual(actual_prepared, digest(fixture["prepared_files"]))
                definition = next(bundle.glob("attempt-*/capture-request.zip"))
                endpoint.post_definition_archive(run_id, definition, prepared_sha256=actual_prepared)
                with self.assertRaises(EndpointConflict):
                    endpoint.post_definition_archive(run_id, definition, prepared_sha256="0" * 64)
                changed_definition = root / "changed-definition.zip"
                changed_definition.write_bytes(b"another definition")
                with self.assertRaises(EndpointConflict):
                    endpoint.post_definition_archive(run_id, changed_definition, prepared_sha256=actual_prepared)
                self.assertEqual(len(state["definition_posts"]), 1)

                admitted = capture.import_native_capture(store, paths, run_id, job, endpoint)
                self.assertEqual(admitted["state"], "capture_admitted")
                self.assertEqual(capture.registered_freeze_events(store, run_id), events)
                before = len(flow.recorded_events(store, run_id))
                capture.import_native_capture(store, paths, run_id, job, endpoint)
                self.assertEqual(len(flow.recorded_events(store, run_id)), before)
            finally:
                server.shutdown()
                server.server_close()
