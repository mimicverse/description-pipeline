"""The registered platform fixture the portal suites read through.

The portal suites own real platform stores over the in-memory database double:
every portal read goes through the locked PlatformStore surface - run records,
stage records, the sealed resolver and the recorded event stream - exactly as
production reads it.  Stage rows are seeded as the registered records they
are; the sealed write path keeps its own canonical coverage.
"""

from __future__ import annotations

import contextlib
import json
import shutil
import tempfile
from pathlib import Path
from unittest import mock

from description_pipeline.delivery import read_delivery_manifest
from description_pipeline.io import read_data, write_json
from description_pipeline.platform.storage import seal_stage_confirmation
from description_pipeline.platform.store import PlatformStore
from description_pipeline.verification import solidworks_urdf
from description_pipeline.verification.solidworks_urdf import required_checks

from .test_platform_store import FakeWorld, build_sealed_delivery

#: The definition the synthetic sealed publication fixture binds its quality to.
DEFINITION = {"source": {"bodies": [{"name": "base_link"}]}, "checks": {}}


@contextlib.contextmanager
def stub_definition_rebuild():
    """The rebuilt definition of the synthetic sealed fixture's tree."""
    with mock.patch.object(solidworks_urdf, "rebuilt_definition", return_value=DEFINITION):
        yield


class RegisteredPlatform:
    """One platform fixture: a real store over the in-memory world and sealed root."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.world = FakeWorld()
        self.store = PlatformStore(self.world.connect, sealed=self.sealed)

    def sealed(self, uri: str) -> Path:
        return self.root / "sealed" / uri.removeprefix("store://sealed/")

    def admit(
        self,
        run_id: str,
        *,
        state: str = "running",
        from_stage: str | None = None,
        parent_run_id: str | None = None,
        target_hardware: str = "nd",
        main_assembly: str = "cad/robot.SLDASM",
    ) -> None:
        self.store.admit_run(
            request_id=f"q-{run_id}",
            run_id=run_id,
            principal="portal",
            target_hardware=target_hardware,
            main_assembly=main_assembly,
            dispatch={
                "stage": from_stage or "capture",
                "operation_key": f"{run_id}/dispatch:0",
                "params_digest": "a" * 64,
            },
            from_stage=from_stage,
            parent_run_id=parent_run_id,
        )
        self.world.run_states[run_id] = state

    def conclude(self, run_id: str, state: str) -> None:
        """The recorded run outcome; readers consume the registered state only."""
        self.world.run_states[run_id] = state

    def stage(
        self,
        run_id: str,
        stage: str,
        *,
        state: str = "completed",
        receipt: dict | None = None,
        checkpoint_uri: str | None = None,
        reused_from_run: str | None = None,
    ) -> None:
        """One registered stage row; the default checkpoint keeps the stage readable."""
        self.world.stages[(run_id, stage)] = (
            state,
            dict(receipt or {}),
            checkpoint_uri if checkpoint_uri is not None else f"store://sealed/{run_id}/{stage}",
            reused_from_run,
            None,
        )

    def tree(self, run_id: str, stage: str) -> Path:
        """The sealed tree directory of one registered checkpoint, created on demand."""
        tree = self.sealed(f"store://sealed/{run_id}/{stage}")
        tree.mkdir(parents=True, exist_ok=True)
        return tree

    def register_capture(self, run_id: str, *, hardware_id: str = "robot", revision: str = "r1") -> Path:
        """One genuinely registered capture: a real sealed tree through the real writer.

        The tree carries the frozen source identity the capture stage binds; the
        row is written through ``confirm_stage``, so the sealed rebinding and
        its refusals behave exactly as production's do.
        """
        capture = self.sealed(f"store://sealed/{run_id}/capture")
        registered = self.store.stage_records(run_id).get("capture") or {}
        if registered.get("state") == "completed":
            # One registration per stage: a redo is a linked attempt, not a rewrite.
            return capture
        if capture.exists():
            shutil.rmtree(capture)
        capture.mkdir(parents=True)
        _subject, _artifacts, _handoff_full, handoff_native = build_sealed_delivery(
            capture,
            hardware_id=hardware_id,
            revision=revision,
            run_id=run_id,
        )
        manifest = read_delivery_manifest(capture)
        handoff = read_data(capture / "input/handoff.json")
        source = read_data(capture / "input/source.json")
        receipt = {
            "schema_version": "solidworks-to-urdf.bundle/v1",
            "pipeline_id": "solidworks-to-urdf",
            "run_id": run_id,
            "stage": "capture",
            "state": "native_complete",
            "passed": False,
            "handoff_sha256": handoff_native,
            "upload_sha256": source["identity"]["handoff_digest"],
            "artifacts": [dict(record) for record in manifest["subject"]["files"]],
            "native_tool": read_data(capture / "reports/native-tool.json"),
            "native_prepared_dir": "C:\\fixture\\prepared",
            "native_prepared_files": dict(handoff["package_files"]),
        }
        self.world.stages.pop((run_id, "capture"), None)
        self.store.confirm_stage(
            run_id=run_id,
            stage="capture",
            state="completed",
            checkpoint_uri=f"store://sealed/{run_id}/capture",
            receipt=receipt,
        )
        return capture

    def register_publish(self, run_id: str, *, submission_state: str = "published") -> str:
        """One genuinely sealed publication, registered through the production writer.

        The tree is the same real sealed delivery the capture fixture uses; the writer
        seals it and completes the receipt with the report members bound to the sealed
        bytes, and ``confirm_stage`` re-verifies everything exactly as production does.
        Returns the sealed subject digest every read-back binds.
        """
        registered = self.store.stage_records(run_id).get("publish") or {}
        if registered.get("state") == "completed":
            return str((registered.get("receipt") or {}).get("subject_sha256") or "")
        source = Path(tempfile.mkdtemp(prefix="publish-seal-", dir=self.root))
        try:
            subject, artifacts, _handoff_full, handoff_native = build_sealed_delivery(
                source,
                run_id=run_id,
            )
            checks = [
                {"id": name, "state": "passed", "passed": True, "details": {}} for name in required_checks(DEFINITION)
            ]
            quality = {
                "passed": True,
                "subject_sha256": subject,
                "checks": checks,
                "required_checks": required_checks(DEFINITION),
                "qualifications": solidworks_urdf.qualifications(source, DEFINITION, checks),
            }
            write_json(source / "reports/quality.json", quality)
            receipt = {
                "schema_version": "solidworks-to-urdf.bundle/v1",
                "pipeline_id": "solidworks-to-urdf",
                "run_id": run_id,
                "stage": "publish",
                "state": submission_state,
                "passed": True,
                "handoff_sha256": handoff_native,
                "subject_sha256": subject,
                "artifacts": [dict(record) for record in artifacts],
                "quality": quality,
                "submission": {
                    "passed": True,
                    "state": submission_state,
                    "subject_sha256": subject,
                    "commit": "c" * 40,
                    "base": "feature/robot",
                    "repository_slug": "example/robot",
                    "branch": "work/description/robot/0123456789abcdef01234567",
                    "url": "https://github.com/example/robot/pull/1",
                },
            }
            with stub_definition_rebuild():
                uri, sealed_receipt = seal_stage_confirmation(self.root, run_id, "publish", source, receipt)
                self.world.stages.pop((run_id, "publish"), None)
                self.store.confirm_stage(
                    run_id=run_id,
                    stage="publish",
                    state="completed",
                    checkpoint_uri=uri,
                    receipt=sealed_receipt,
                )
            return subject
        finally:
            shutil.rmtree(source, ignore_errors=True)

    def events(self, run_id: str, events: list[dict]) -> None:
        """Record one attempt's display events as the platform event rows they become.

        Sequence numbers continue past anything the registration already wrote
        (``register_capture`` appends its own confirmation event), so the world
        keeps the database's per-run uniqueness the paging cursor relies on.
        """
        start = max((row[1] for row in self.world.events if row[0] == run_id), default=0)
        for offset, event in enumerate(events, start=1):
            self.world.events.append(
                (run_id, start + offset, "stage", str(event.get("stage") or ""), None, json.dumps(event))
            )
