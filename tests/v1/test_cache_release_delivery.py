"""IF-10 delivery side: live-health binding, one ledger per run, reconcile resends.

These tests never touch CAD, Airflow or a Windows host: the endpoint is a
stub and the store is a plain run-directory layout.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from description_pipeline.io import PipelineError, write_json
from description_pipeline.interface_records import validate_cache_release_request
from description_pipeline.orchestration.airflow_client import (
    CAPTURE_MANIFEST,
    EndpointConfig,
    WindowsEndpoint,
    cache_release_manifest,
    cache_release_request,
    reconcile_queued_cache_releases,
    release_run_cache,
)
from description_pipeline.stages import CONTRACT, CONTRACT_SHA256

RUN = "5a73ccbc-0508-5ad4-a811-f5296333d660"
OTHER = "7fa33d95-cd82-4bf5-5f34-3e2e2c5c6eb5"
RELEASE = {"version": "1.4.0", "source_commit": "ab" * 20, "source_sha256": "cd" * 32}
HEALTH = {
    "release": {**RELEASE, "role": "native"},
    "contract": {"schema_version": CONTRACT["schema_version"], "digest": CONTRACT_SHA256},
}


class Refused(Exception):
    def __init__(self, message: str):
        super().__init__(message)
        self.payload = {"error_code": "cache_release_refused", "reason": "job_not_terminal"}


class StubEndpoint:
    def __init__(self, *, error: Exception | None = None):
        self.calls: list[tuple[str, dict]] = []
        self.error = error
        self.health_calls = 0

    def health(self) -> dict:
        self.health_calls += 1
        return json.loads(json.dumps(HEALTH))

    def release_cache(self, run_id: str, request: dict) -> dict:
        self.calls.append((run_id, request))
        if self.error is not None:
            raise self.error
        return {"schema_version": "solidworks-to-urdf.cache-release-receipt/v1", "status": "released", "run_id": run_id}


class Store:
    """The slice of LinuxStore the release ledger uses."""

    def __init__(self, root: Path):
        self.root = root

    def run_dir(self, run_id: str) -> Path:
        return self.root / run_id

    def capture_dir(self, run_id: str) -> Path:
        return self.run_dir(run_id) / "capture"


class CacheReleaseDeliveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = Store(self.root)
        self.intake = self.root / "intake"
        (self.intake / "cad").mkdir(parents=True)
        (self.intake / "cad" / "robot.SLDASM").write_bytes(b"assembly bytes")
        self.upload = {"cad/robot.SLDASM": hashlib.sha256(b"assembly bytes").hexdigest()}
        capture = self.store.capture_dir(RUN)
        capture.mkdir(parents=True)
        (capture / CAPTURE_MANIFEST).write_bytes(b"manifest bytes")
        self.sealed = hashlib.sha256(b"manifest bytes").hexdigest()

    # -- builders ------------------------------------------------------------

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

    # -- the best-effort trigger and the reconcile pass ----------------------

    def test_release_records_a_refusal_and_never_raises(self):
        endpoint = StubEndpoint(error=Refused("refused"))
        ledger = release_run_cache(self.store, endpoint, run_id=RUN, intake=self.intake, upload_files=self.upload)
        self.assertEqual("pending", ledger["state"])
        self.assertEqual("cache_release_refused", ledger["error"]["error_code"])
        self.assertEqual("job_not_terminal", ledger["error"]["reason"])
        ledger = release_run_cache(self.store, endpoint, run_id=RUN, intake=self.intake, upload_files=self.upload)
        self.assertEqual("pending", ledger["state"])
        self.assertEqual(2, len(endpoint.calls))
        self.assertEqual(endpoint.calls[0], endpoint.calls[1])
        self.assertEqual(1, endpoint.health_calls)

    def test_release_succeeds_once_and_replays_without_calling(self):
        endpoint = StubEndpoint()
        ledger = release_run_cache(self.store, endpoint, run_id=RUN, intake=self.intake, upload_files=self.upload)
        self.assertEqual("released", ledger["state"])
        self.assertEqual("released", ledger["receipt"]["status"])
        again = release_run_cache(self.store, endpoint, run_id=RUN, intake=self.intake, upload_files=self.upload)
        self.assertEqual(ledger, again)
        self.assertEqual(1, len(endpoint.calls))

    def test_reconcile_only_resends_recorded_requests(self):
        endpoint = StubEndpoint()
        release_run_cache(self.store, endpoint, run_id=RUN, intake=self.intake, upload_files=self.upload)
        other = self.store.run_dir(OTHER)
        other.mkdir()
        pending_request = {"operation_id": "cache-release:other", "binding": {}, "import": {}}
        write_json(
            other / "cache-release.json",
            {
                "schema_version": "solidworks-to-urdf.cache-release-ledger/v1",
                "kind": "cache_release",
                "run_id": OTHER,
                "operation_id": pending_request["operation_id"],
                "params_sha256": "ab" * 32,
                "request": pending_request,
                "state": "pending",
                "receipt": None,
                "error": None,
            },
        )
        endpoint.calls.clear()
        resent = reconcile_queued_cache_releases(self.store, endpoint)
        self.assertEqual(1, resent)
        self.assertEqual([(OTHER, pending_request)], endpoint.calls)
        self.assertEqual("released", json.loads((other / "cache-release.json").read_text())["state"])
