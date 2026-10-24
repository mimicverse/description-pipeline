"""Native capture recovery binds the original stage, request and tool facts."""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path

from description_pipeline.delivery import PIPELINE_ID, read_delivery_manifest
from description_pipeline.io import PipelineError, digest, file_digest, write_json
from description_pipeline.platform.native_recovery import build_discovery_tree_from_prepared
from description_pipeline.platform.native_recovery import discovery_receipt
from description_pipeline.platform.store import PlatformStore, PlatformStoreError
from description_pipeline.request_evidence import build_request_evidence, request_evidence_bytes
from description_pipeline.schemas import TOOL_SCHEMA
from description_pipeline.stages import CONTRACT, CONTRACT_SHA256
from tests.sources.selection_support import sealed_selection
from tests.v1.source_record_support import source_record

RUN_ID = "cfa3e725-abc5-5b60-94ad-6ba838110408"
MAIN = "assembly.SLDASM"


def native_tool(role: str) -> dict:
    return {
        "schema_version": TOOL_SCHEMA,
        "pipeline_id": PIPELINE_ID,
        "version": "1.5.0",
        "source_sha256": "a" * 64,
        "runtime": {"role": role},
    }


def stage_view(
    *,
    discover_qc: str = "passed",
    handoff_sha256: str,
    run_id: str = RUN_ID,
    capture_state: str = "failed",
    execution_scope: list[str] | None = None,
) -> dict:
    rows = [{"id": stage["id"]} for stage in CONTRACT["stages"]]
    by_id = {row["id"]: row for row in rows}
    discover_definition = next(stage for stage in CONTRACT["stages"] if stage["id"] == "discover")
    by_id["discover"].update(
        state="completed",
        in_scope=True,
        input_qc=[
            {"id": item["id"], "state": "passed", "details": {"passed": True}}
            for item in discover_definition["input_qc"]
        ],
        output_qc=[
            {"id": item["id"], "state": discover_qc, "details": {"passed": discover_qc == "passed"}}
            for item in discover_definition["output_qc"]
        ],
    )
    by_id["capture"].update(state=capture_state, in_scope=True)
    return {
        "schema_version": "solidworks-to-urdf.stages/v1",
        "pipeline_id": PIPELINE_ID,
        "contract_sha256": CONTRACT_SHA256,
        "run_id": run_id,
        "handoff_sha256": handoff_sha256,
        "execution_scope": execution_scope or ["discover", "capture"],
        "stages": rows,
    }


class NativeRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="native-recovery-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.derived = self.root / "derived"
        self.derived.mkdir()
        (self.derived / MAIN).write_bytes(b"assembly")
        write_json(self.derived / "capture-plan.json", {"plan": "fixture"})
        write_json(self.derived / "source.json", {"source": "fixture"})
        (self.derived / "records").mkdir()
        write_json(self.derived / "records" / "arm.json", {"record": "fixture"})
        self.native_files = {MAIN: file_digest(self.derived / MAIN)}
        self.handoff = digest(self.native_files)
        self.full_upload = {**self.native_files, "excluded.SLDPRT": "d" * 64}
        self.full_upload_sha = digest(self.full_upload)
        write_json(
            self.derived / "source.json",
            source_record(
                hardware_id="m3.0",
                handoff_digest=self.full_upload_sha,
                main_assembly=MAIN,
            ),
        )
        selection = sealed_selection(
            self.full_upload,
            MAIN,
            edges={MAIN: []},
        )
        discovery = {
            "native_files": self.native_files,
            "handoff_sha256": self.handoff,
            "raw": {"files": self.native_files, "source_selection": selection},
        }
        self.archive = self.root / "capture-request.zip"
        with zipfile.ZipFile(self.archive, "w") as archive:
            archive.writestr("discovery/native-discovery.json", json.dumps(discovery))
            archive.writestr("discovery/findings.json", "{}")
            archive.writestr("discovery/identity-resolution.json", "{}")
            archive.writestr("capture-plan.json", (self.derived / "capture-plan.json").read_bytes())
            archive.writestr("source.json", (self.derived / "source.json").read_bytes())
            archive.writestr("manifest.json", "{}")
        self.prepared = {
            MAIN: file_digest(self.derived / MAIN),
            "capture-plan.json": file_digest(self.derived / "capture-plan.json"),
            "source.json": file_digest(self.derived / "source.json"),
            "records/arm.json": file_digest(self.derived / "records" / "arm.json"),
            "discovery/findings.json": "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a",
            "discovery/identity-resolution.json": "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a",
            "discovery/native-discovery.json": hashlib.sha256(json.dumps(discovery).encode()).hexdigest(),
        }
        self.request = request_evidence_bytes(
            build_request_evidence(
                purpose="inspection_preview",
                request_id="portal-recovery-fixture",
                target_hardware="m3.0",
                change_summary="recovery fixture",
                main_assembly=MAIN,
                handoff_digest=self.full_upload_sha,
                platform_config_digest="c" * 64,
            )
        )
        self.native = native_tool("native")
        self.portable = native_tool("portable")

    def _build(self, **overrides):
        tree = overrides.get("tree", self.root / "discover")
        if tree.exists():
            shutil.rmtree(tree)
        values = {
            "tree": tree,
            "derived": self.derived,
            "archive_path": self.archive,
            "prepared": self.prepared,
            "native_tool": self.native,
            "run_id": RUN_ID,
            "main_assembly": MAIN,
            "native_stages": stage_view(handoff_sha256=self.handoff),
            "request_evidence": self.request,
            "portable_tool": self.portable,
            "request_upload_files": self.full_upload,
            "request_upload_sha256": self.full_upload_sha,
        }
        values.update(overrides)
        return build_discovery_tree_from_prepared(**values)

    def test_the_original_stage_request_and_tool_are_bound_into_the_tree(self) -> None:
        built = self._build()
        tree = self.root / "discover"
        self.assertEqual(built["native_stages_sha256"], digest(stage_view(handoff_sha256=self.handoff)))
        self.assertEqual(built["request_evidence_sha256"], hashlib.sha256(self.request).hexdigest())
        self.assertTrue((tree / "input" / "request.json").is_file())
        self.assertTrue((tree / "reports" / "native-stages.json").is_file())
        self.assertTrue((tree / "tool.json").is_file())

    def test_native_success_recovery_uses_the_expected_completed_capture(self) -> None:
        completed = stage_view(
            handoff_sha256=self.handoff,
            capture_state="completed",
        )
        built = self._build(native_stages=completed, capture_state="completed")
        self.assertEqual(built["native_stages_sha256"], digest(completed))
        for stages, capture_state in (
            (completed, "failed"),
            (stage_view(handoff_sha256=self.handoff), "completed"),
        ):
            with self.subTest(capture_state=capture_state), self.assertRaises(PipelineError):
                self._build(native_stages=stages, capture_state=capture_state)

    def test_native_execution_scope_allows_freeze_with_discover_and_capture(self) -> None:
        stages = stage_view(
            handoff_sha256=self.handoff,
            capture_state="completed",
            execution_scope=["freeze", "discover", "capture"],
        )
        self._build(native_stages=stages, capture_state="completed")
        for scope in (
            ["discover"],
            ["capture", "discover"],
            ["discover", "capture", "discover"],
            ["discover", "capture", "unknown"],
            ["freeze", "discover", "capture", "generate"],
            ["verify", "discover", "capture"],
        ):
            with self.subTest(scope=scope), self.assertRaises(PipelineError):
                self._build(
                    native_stages=stage_view(
                        handoff_sha256=self.handoff,
                        capture_state="completed",
                        execution_scope=scope,
                    ),
                    capture_state="completed",
                )

    def test_sealed_receipt_digests_are_recomputed_from_their_files(self) -> None:
        built = self._build()
        tree = self.root / "discover"
        receipt = discovery_receipt(
            run_id=RUN_ID,
            tree=tree,
            native_tool=self.native,
            native_handoff=built["native_handoff"],
            native_stages_sha256=built["native_stages_sha256"],
            request_evidence_sha256=built["request_evidence_sha256"],
            portable_tool_sha256=built["portable_tool_sha256"],
        )
        store = PlatformStore(lambda: None, sealed=lambda _uri: tree)
        checkpoint = f"store://sealed/{RUN_ID}/discover"
        row = ("discover", "completed", receipt, checkpoint, None, None)
        self.assertEqual(
            store._verified_direct_completed_stage(
                None,
                run_id=RUN_ID,
                stage="discover",
                row=row,
            )["native_stages_sha256"],
            built["native_stages_sha256"],
        )
        changed = dict(receipt)
        changed["native_stages_sha256"] = "f" * 64
        with self.assertRaisesRegex(PlatformStoreError, "digests differ"):
            store._verified_direct_completed_stage(
                None,
                run_id=RUN_ID,
                stage="discover",
                row=("discover", "completed", changed, checkpoint, None, None),
            )

    def test_every_recovered_discovery_file_is_bound_by_the_delivery_manifest(self) -> None:
        self._build()
        tree = self.root / "discover"
        self.assertFalse((tree / "discovery").exists(), "recovered evidence has no unregistered root directory")
        manifest = read_delivery_manifest(tree)
        paths = {record["path"] for record in manifest["subject"]["files"]}
        self.assertIn("evidence/discovery.json", paths)
        self.assertIn("evidence/discovery/findings.json", paths)
        self.assertIn("evidence/discovery/identity-resolution.json", paths)

        changed = tree / "evidence" / "discovery" / "findings.json"
        changed.write_text('{"changed":true}\n', encoding="utf-8")
        with self.assertRaisesRegex(PipelineError, "subject files differ"):
            read_delivery_manifest(tree)

    def test_recovery_refuses_missing_or_contradictory_original_evidence(self) -> None:
        cases = (
            ({"native_stages": None}, "carries no stage view"),
            (
                {"native_stages": stage_view(discover_qc="failed", handoff_sha256=self.handoff)},
                "output_qc is not completely passed",
            ),
            ({"request_evidence": None}, "no frozen request evidence"),
            ({"portable_tool": None}, "no matching portable tool identity"),
            ({"request_upload_sha256": self.handoff}, "full request upload inventory"),
            (
                {"request_evidence": self.request.replace(self.full_upload_sha.encode(), self.handoff.encode())},
                "admitted upload and main assembly",
            ),
        )
        for overrides, expected in cases:
            with self.subTest(expected=expected), self.assertRaisesRegex(PipelineError, expected):
                self._build(**overrides)


if __name__ == "__main__":
    unittest.main()
