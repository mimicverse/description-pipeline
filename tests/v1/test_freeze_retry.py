"""The freeze-retry resolver: strict source selection and byte-bound evidence reads."""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from description_pipeline.io import digest
from description_pipeline.orchestration.airflow_client import native_run_id
from description_pipeline.orchestration.freeze_retry import (
    FreezeRetryRefusal,
    resolve_freeze_retry_identity,
)
from description_pipeline.sources.solidworks.revision import package_inventory
from description_pipeline.sources.solidworks.selection import validate_file_index

DAG_RUN_ID = "portal-20261016T135105-78ab40ea"
PARENT_RUN_ID = native_run_id(DAG_RUN_ID)


class _Platform:
    """The resolver's registered-fact surface for one terminal freeze failure."""

    def __init__(self, receipt: dict, request: dict) -> None:
        self._receipt = receipt
        self._request = request

    def run_record(self, run_id: str) -> dict | None:
        if run_id != PARENT_RUN_ID:
            return None
        return {
            "run_id": PARENT_RUN_ID,
            "state": "failed",
            "request_id": self._request["request_id"],
            "parent_run_id": None,
            "from_stage": None,
            "tool_identity": None,
        }

    def stage_records(self, run_id: str) -> dict:
        if run_id != PARENT_RUN_ID:
            return {}
        return {"freeze": {"stage": "freeze", "state": "failed", "receipt": dict(self._receipt)}}

    def request_record(self, request_id: str) -> dict | None:
        if request_id != self._request["request_id"]:
            return None
        return dict(self._request)


class FreezeRetryResolverTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="freeze-retry-resolver-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.intake_root = self.root / "cad-handoffs"
        self.intake = self.intake_root / DAG_RUN_ID / "M3.0.3_20261016"
        (self.intake / "parts").mkdir(parents=True)
        (self.intake / "3.0 总装.SLDASM").write_bytes(b"assembly-bytes")
        (self.intake / "parts" / "bracket.SLDPRT").write_bytes(b"part-bytes")
        self.upload_files = validate_file_index(package_inventory(self.intake))
        self.upload_sha = digest(self.upload_files)
        self.log_path = self.root / "freeze-attempt-1.log"
        self.log_path.write_bytes(self._log_bytes())
        self.log_sha = hashlib.sha256(self.log_path.read_bytes()).hexdigest()
        self.receipt = {
            "error": "AirflowFailException: PLATFORM_CONFIG_DIGEST is unavailable",
            "airflow_failure": {"path": str(self.log_path), "sha256": self.log_sha},
        }
        self.request = {
            "request_id": f"portal-{DAG_RUN_ID}",
            "principal": "cli_fixture:tenant:ou_fixture",
            "target_hardware": "m3.0",
            "main_assembly": "3.0 总装.SLDASM",
            "sealed_at": "2026-10-16 21:51:07.308311+08",
            "purpose": "engineering",
            "baseline": None,
        }
        self.platform = _Platform(self.receipt, self.request)

    def _log_bytes(self, *, selections: int = 1, files: int | None = None, handoff: str | None = None) -> bytes:
        lines = []
        for attempt in range(1, selections + 1):
            lines.append(
                {
                    "timestamp": "2026-10-16T13:51:10.237754Z",
                    "level": "info",
                    "event": (
                        f"selection intake={self.intake} files={files or len(self.upload_files)} "
                        f"upload_sha256={handoff or self.upload_sha} main_assembly=3.0 总装.SLDASM "
                        "endpoint_conn=solidworks_windows"
                    ),
                    "logger": "unusual_prefix_solidworks_to_urdf",
                    "try_number": attempt,
                    "run_id": DAG_RUN_ID,
                    "ti_id": "01a144fb-1252-77b2-b363-12c7beb80fad",
                    "task_id": "freeze",
                    "dag_id": "solidworks_to_urdf",
                    "map_index": -1,
                    "filename": "solidworks_to_urdf.py",
                    "lineno": 1505,
                }
            )
        return ("\n".join(json.dumps(line, ensure_ascii=False) for line in lines) + "\n").encode()

    def _resolve(self):
        return resolve_freeze_retry_identity(
            self.platform,
            PARENT_RUN_ID,
            parent_dag_run_id=DAG_RUN_ID,
            expected_dag_id="solidworks_to_urdf",
            intake_path=self.intake,
            intake_root=self.intake_root,
        )

    def test_null_baseline_resolves_from_the_bound_evidence(self) -> None:
        resolved = self._resolve()
        self.assertEqual(resolved.source, "parent_freeze_failure_log")
        self.assertEqual(resolved.handoff_sha256, self.upload_sha)
        self.assertEqual(resolved.files, len(self.upload_files))
        self.assertEqual(resolved.main_assembly, "3.0 总装.SLDASM")
        self.assertEqual(resolved.intake_path, self.intake)
        self.assertEqual(resolved.upload_files, self.upload_files)
        self.assertEqual(
            resolved.provenance,
            {
                "source": "parent_freeze_failure_log",
                "path": str(self.log_path),
                "sha256": self.log_sha,
                "parent_run_id": PARENT_RUN_ID,
            },
        )

    def test_malformed_baseline_never_falls_back_to_the_evidence_path(self) -> None:
        valid = {
            "handoff_sha256": self.upload_sha,
            "files": len(self.upload_files),
            "bytes": 64,
            "folder": "M3.0.3_20261016",
        }
        cases = {
            "not_a_dict": "baseline-text",
            "missing_everything": {},
            "bad_handoff": {**valid, "handoff_sha256": "f" * 63},
            "uppercase_handoff": {**valid, "handoff_sha256": self.upload_sha.upper()},
            "handoff_wrong_type": {**valid, "handoff_sha256": 5},
            "files_zero": {**valid, "files": 0},
            "files_wrong_type": {**valid, "files": str(len(self.upload_files))},
            "files_bool": {**valid, "files": True},
            "files_missing": {key: value for key, value in valid.items() if key != "files"},
            "bytes_negative": {**valid, "bytes": -1},
            "bytes_wrong_type": {**valid, "bytes": "0"},
            "bytes_missing": {key: value for key, value in valid.items() if key != "bytes"},
            "folder_empty": {**valid, "folder": ""},
            "folder_missing": {key: value for key, value in valid.items() if key != "folder"},
            "provenance_present": {**valid, "provenance": {"source": "anything"}},
        }
        for label, baseline in cases.items():
            with self.subTest(case=label):
                self.request["baseline"] = baseline
                with self.assertRaises(FreezeRetryRefusal) as raised:
                    self._resolve()
                self.assertEqual(raised.exception.reason, "originals_identity_missing")
        # A present and strictly faithful baseline still resolves through the baseline source.
        self.request["baseline"] = valid
        resolved = self._resolve()
        self.assertEqual(resolved.source, "baseline")
        self.assertEqual(resolved.handoff_sha256, self.upload_sha)

    def test_evidence_bytes_are_read_once_and_bound_by_those_bytes(self) -> None:
        original = self.log_path.read_bytes()
        swapped = self._log_bytes(files=999, handoff="0" * 64)
        real_read_bytes = Path.read_bytes
        real_read_text = Path.read_text
        reads = {"evidence": 0}

        def counting_read_bytes(path_self):
            if path_self == self.log_path:
                reads["evidence"] += 1
                return original if reads["evidence"] == 1 else swapped
            return real_read_bytes(path_self)

        def guarded_read_text(path_self, *args, **kwargs):
            if path_self == self.log_path:
                raise AssertionError("the bound evidence must not be reopened for parsing")
            return real_read_text(path_self, *args, **kwargs)

        with (
            mock.patch.object(Path, "read_bytes", counting_read_bytes),
            mock.patch.object(Path, "read_text", guarded_read_text),
        ):
            resolved = self._resolve()
        self.assertEqual(reads["evidence"], 1)
        self.assertEqual(resolved.handoff_sha256, self.upload_sha)
        self.assertEqual(resolved.files, len(self.upload_files))
