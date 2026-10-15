"""IF-10 delivery side: the live-health binding and the frozen release request builders.

These tests never touch CAD, Airflow or a Windows host: the endpoint is a stub
and the payloads are built from a plain directory layout.  The registration,
claim and settlement wiring lives with the DAG tests.
"""

from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from description_pipeline.io import PipelineError
from description_pipeline.interface_records import validate_cache_release_request
from description_pipeline.orchestration.airflow_client import (
    EndpointConfig,
    WindowsEndpoint,
    cache_release_manifest,
    cache_release_request,
)
from description_pipeline.orchestration.stage_transfer import CAPTURE_MANIFEST
from description_pipeline.stages import CONTRACT, CONTRACT_SHA256

RUN = "5a73ccbc-0508-5ad4-a811-f5296333d660"
RELEASE = {"version": "1.4.0", "source_commit": "ab" * 20, "source_sha256": "cd" * 32}
HEALTH = {
    "release": {**RELEASE, "role": "native"},
    "contract": {"schema_version": CONTRACT["schema_version"], "digest": CONTRACT_SHA256},
}


class CacheReleaseDeliveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.intake = self.root / "intake"
        (self.intake / "cad").mkdir(parents=True)
        (self.intake / "cad" / "robot.SLDASM").write_bytes(b"assembly bytes")
        self.upload = {"cad/robot.SLDASM": hashlib.sha256(b"assembly bytes").hexdigest()}
        capture = self.root / RUN / "capture"
        capture.mkdir(parents=True)
        (capture / CAPTURE_MANIFEST).write_bytes(b"manifest bytes")
        self.sealed = hashlib.sha256(b"manifest bytes").hexdigest()

    def test_request_binds_live_health_and_validates_frozen(self):
        manifest = cache_release_manifest(self.intake, self.upload)
        self.assertEqual(
            [
                {
                    "path": "cad/robot.SLDASM",
                    "bytes": len(b"assembly bytes"),
                    "sha256": self.upload["cad/robot.SLDASM"],
                }
            ],
            manifest,
        )
        request = cache_release_request(
            run_id=RUN,
            linux_run_id=RUN,
            production_operation_id=f"{RUN}:native",
            operation_id=f"cache-release:{RUN}",
            health=HEALTH,
            imported=manifest,
            sealed_receipt_sha256=self.sealed,
        )
        validate_cache_release_request(request)
        self.assertEqual({key: RELEASE[key] for key in RELEASE}, request["binding"]["release"])
        self.assertEqual(HEALTH["contract"], request["binding"]["contract"])
        self.assertEqual(self.sealed, request["import"]["confirmation"]["sealed"]["receipt_sha256"])

    def test_a_pinned_import_block_is_reused_verbatim(self):
        manifest = cache_release_manifest(self.intake, self.upload)
        frozen = cache_release_request(
            run_id=RUN,
            linux_run_id=RUN,
            production_operation_id=f"{RUN}:native",
            operation_id="op-1",
            health=HEALTH,
            imported=manifest,
            sealed_receipt_sha256=self.sealed,
        )["import"]
        rebuilt = cache_release_request(
            run_id=RUN,
            linux_run_id=RUN,
            production_operation_id=f"{RUN}:native",
            operation_id="op-1",
            health=HEALTH,
            imported=manifest,
            sealed_receipt_sha256=self.sealed,
            import_block=frozen,
        )
        self.assertEqual(rebuilt["import"], frozen)

    def test_manifest_refuses_changed_or_missing_bytes(self):
        changed = dict(self.upload)
        changed["cad/robot.SLDASM"] = "00" * 32
        with self.assertRaises(PipelineError):
            cache_release_manifest(self.intake, changed)
        with self.assertRaises(PipelineError):
            cache_release_manifest(self.intake, {"cad/missing.SLDPRT": "00" * 32})

    def test_client_posts_the_frozen_record_path(self):
        endpoint = WindowsEndpoint(EndpointConfig(base_url="http://127.0.0.1:9", token="t" * 32))
        with patch.object(endpoint, "_request", return_value={"status": "released"}) as request:
            result = endpoint.release_cache(RUN, {"operation_id": "op"})
        self.assertEqual({"status": "released"}, result)
        request.assert_called_once_with("POST", f"/v1/jobs/{RUN}/cache/release", {"operation_id": "op"})


if __name__ == "__main__":
    unittest.main()
