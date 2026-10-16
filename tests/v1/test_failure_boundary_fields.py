"""Owned boundary checks: an error's stable code/detail survive the DAG seams.

The native poke and the portable summary both hand their failure to the one
convergence path; the registered receipt and snapshot must carry the same
fields, and a string-only failure must never fabricate them.
"""

from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from description_pipeline.io import digest
from description_pipeline.platform import flow
from tests.v1._airflow_env import pinned_airflow_home
from tests.v1.portal_read_support import RegisteredPlatform
from tests.v1.protocol_support import protocol_events

RUN = "943b0065-f4e5-4dc9-8f6a-a2be32f7889a"
CODE = "selection_reference_missing"
DETAIL = {"document": "cad/robot.SLDASM", "reference": "cad/missing.SLDPRT"}


def _module():
    pinned_airflow_home()
    spec = importlib.util.spec_from_file_location(
        "owned_failure_boundary_dag", Path("deploy/airflow/dags/solidworks_to_urdf.py")
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class FailureBoundaryFieldTests(unittest.TestCase):
    def _native_seam(self, receipt_fields: dict) -> dict:
        module = _module()
        with tempfile.TemporaryDirectory(prefix="owned-boundary-") as temp:
            platform = RegisteredPlatform(Path(temp))
            platform.admit(RUN)
            receipt = {
                "run_id": RUN,
                "state": "failed",
                "passed": False,
                "error": "Selected assembly references unavailable CAD",
                "events": protocol_events(failed_stage="capture"),
                **receipt_fields,
            }
            files = {"cad/robot.SLDASM": "a" * 64}
            request = {
                "run_id": RUN,
                "upload_files": files,
                "upload_sha256": digest(files),
                "main_assembly": "cad/robot.SLDASM",
                "conn_id": "owned-boundary",
            }
            binding = {key: request[key] for key in ("run_id", "upload_files", "upload_sha256", "main_assembly")}
            job = {**receipt, "status": "failed", "request": binding}
            endpoint = SimpleNamespace(get_job=lambda _: job)
            with (
                patch.object(module, "_platform_binding", return_value=(platform.store, platform.root)),
                patch.object(module, "_endpoint", return_value=endpoint),
                self.assertRaises(module.AirflowFailException),
            ):
                module._poke(request)
            row = platform.store.stage_records(RUN).get("capture") or {}
            snapshot = flow.registered_delivery_snapshot(platform.store, RUN)
            return {"receipt": row.get("receipt") or {}, "snapshot": snapshot}

    def test_native_boundary_preserves_code_and_detail(self) -> None:
        observed = self._native_seam({"error_code": CODE, "detail": DETAIL})
        for view in ("receipt", "snapshot"):
            self.assertEqual(observed[view].get("error_code"), CODE, view)
            self.assertEqual(observed[view].get("detail"), DETAIL, view)

    def test_shape_only_failure_does_not_fabricate_fields(self) -> None:
        observed = self._native_seam({})
        for view in ("receipt", "snapshot"):
            self.assertIsNone(observed[view].get("error_code"), view)
            self.assertIsNone(observed[view].get("detail"), view)

    def test_portable_summary_preserves_code_and_detail(self) -> None:
        module = _module()
        receipt = {"state": "failed", "passed": False, "error": "boom", "error_code": CODE, "detail": DETAIL}
        with self.assertRaises(module.AirflowFailException) as caught:
            module._portable_summary(receipt, stage="generate")
        self.assertEqual(getattr(caught.exception, "code", None), CODE)
        self.assertEqual(getattr(caught.exception, "detail", None), DETAIL)
