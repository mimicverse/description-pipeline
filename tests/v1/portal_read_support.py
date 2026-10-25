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
import hashlib
import shutil
import tempfile
from pathlib import Path
from unittest import mock

from description_pipeline.delivery import read_delivery_manifest
from description_pipeline.io import digest, read_data, write_json
from description_pipeline.pipeline.contracts import ArtifactRef
from description_pipeline.platform.storage import seal_stage_confirmation
from description_pipeline.platform.store import PlatformStore
from description_pipeline.schemas import VERIFICATION_CERTIFICATE_SCHEMA
from description_pipeline.verification import solidworks_urdf
from description_pipeline.verification.solidworks_urdf import required_checks

from .test_platform_store import FakeWorld, build_sealed_delivery

#: The definition the synthetic sealed publication fixture binds its quality to.
DEFINITION = {"source": {"bodies": [{"name": "base_link"}]}, "checks": {}}
STAGES = ("freeze", "discover", "capture", "generate", "verify", "publish")


class RegisteredProjectionArtifacts:
    """Materialize the immutable members registered by one test-side projection."""

    def __init__(self) -> None:
        self.objects: dict[str, dict[str, bytes]] = {}

    def register(
        self,
        *,
        run_id: str,
        stage: str,
        files: dict[str, bytes],
        kind: str | None = None,
    ) -> dict:
        ordered = dict(sorted(files.items()))
        set_sha256 = digest(
            {
                "members": [
                    {
                        "path": path,
                        "bytes": len(data),
                        "sha256": hashlib.sha256(data).hexdigest(),
                    }
                    for path, data in ordered.items()
                ]
            }
        )
        artifact_id = "fixture:" + digest({"run_id": run_id, "stage": stage, "set": set_sha256})
        record = {
            "artifact_id": artifact_id,
            "kind": kind or f"stage_output:{stage}",
            "locator": f"cas://artifact/{artifact_id}",
            "manifest_sha256": digest({"manifest": artifact_id}),
            "producer_run_id": run_id,
            "producer_stage": stage,
            "producer_tool_sha256": digest({"tool": stage}),
            "set_sha256": set_sha256,
        }
        ArtifactRef.from_record(record)
        self.objects[artifact_id] = ordered
        return record

    def materialize(self, artifact: ArtifactRef, destination: str | Path) -> Path:
        files = self.objects.get(artifact.artifact_id)
        if files is None:
            raise AssertionError(f"Unknown projection artifact {artifact.artifact_id}")
        root = Path(destination)
        root.mkdir(parents=True)
        for path, data in files.items():
            target = root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        return root


class RegisteredRunControl:
    """Expose the canonical portal control surface over the registered fixture."""

    def __init__(self, store: PlatformStore) -> None:
        self.store = store
        self.fact_projection = store.fact_projection
        self.projection_artifacts = store.projection_artifacts

    def admission(self):
        return self.store.admission()

    def list_run_display_metadata(self):
        return self.store.run_displays()

    def get_run_display_metadata(self, run_id: str):
        return self.store.run_display(run_id)

    def rename_run(self, run_id: str, display_name: str | None):
        return self.store.set_run_display(run_id, display_name)

    def hide_run(self, run_id: str, *, actor: str):
        return self.store.hide_run(run_id, actor=actor)

    def restore_run(self, run_id: str):
        return self.store.restore_run(run_id)


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
        self.projection_artifacts = RegisteredProjectionArtifacts()
        self.store.fact_projection = self.fact_projection
        self.store.projection_artifacts = self.projection_artifacts
        self.run_control = RegisteredRunControl(self.store)

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

    def fact_projection(self, run_id: str) -> dict | None:
        """Project registered legacy fixture rows into the canonical read facts."""

        record = self.store.run_record(run_id)
        if not isinstance(record, dict):
            return None
        registered = self.store.stage_records(run_id)
        stage_rows: list[dict] = []
        by_stage: dict[str, dict[str, dict]] = {}
        members: dict[str, dict] = {}
        for stage in STAGES:
            row = registered.get(stage)
            if not isinstance(row, dict):
                stage_rows.append(
                    {
                        "stage": stage,
                        "state": "waiting",
                        "command": None,
                        "decision": None,
                        "receipt": {"artifacts": []},
                        "checkpoint_uri": None,
                        "reused_from_run": None,
                        "original_executed_at": None,
                        "updated_at": None,
                        "scope_kind": "formal",
                        "formal_eligible": False,
                        "qualification": None,
                        "in_scope": True,
                    }
                )
                by_stage[stage] = {}
                continue
            receipt = row.get("receipt") if isinstance(row.get("receipt"), dict) else {}
            tree = self._stage_tree(run_id, stage, row.get("checkpoint_uri"))
            selected = (
                {
                    path.relative_to(tree).as_posix(): path.read_bytes()
                    for path in sorted(tree.rglob("*"))
                    if path.is_file() and path.name != "manifest.json"
                }
                if tree is not None and tree.is_dir()
                else {}
            )
            if stage == "verify" and selected:
                selected = self._bind_projection_verify(
                    run_id,
                    selected,
                    receipt,
                )
            artifact = self.projection_artifacts.register(
                run_id=run_id,
                stage=stage,
                files=selected,
            )
            stage_members = {
                path: {
                    "bytes": len(data),
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "artifact": artifact,
                }
                for path, data in selected.items()
            }
            by_stage[stage] = stage_members
            members.update(stage_members)
            outcome = row.get("state")
            command = None
            if stage == "verify" and "verification-certificate.json" in selected:
                pair = self._verify_pair_refs(run_id, selected)
                command = {
                    "command_id": f"fixture:{run_id}:verify",
                    "input_artifacts": [pair[0], pair[1]],
                }
            qualification = {
                "outcome": outcome,
                "scope_kind": "formal",
                "formal_eligible": outcome == "completed",
                "qualification_digest": digest(
                    {
                        "run_id": run_id,
                        "stage": stage,
                        "outcome": outcome,
                        "scope_kind": "formal",
                    }
                ),
            }
            stage_rows.append(
                {
                    **row,
                    "stage": stage,
                    "command": command,
                    "decision": {
                        "command_id": (command or {}).get(
                            "command_id",
                            f"fixture:{run_id}:{stage}",
                        ),
                        "outcome": outcome,
                        "reason_code": receipt.get("error_code"),
                    },
                    "scope_kind": "formal",
                    "formal_eligible": outcome == "completed",
                    "qualification": qualification,
                    "in_scope": True,
                }
            )
        run_state = {
            "completed": "running",
            "succeeded": "succeeded",
            "failed": "failed",
            "cancelled": "cancelled",
            "running": "running",
        }.get(str(record.get("state")), str(record.get("state")))
        events = []
        for event in sorted(
            (row for row in self.world.events if row[0] == run_id),
            key=lambda row: row[1],
        ):
            try:
                payload = json.loads(event[5])
            except (TypeError, ValueError):
                payload = {}
            if isinstance(payload, dict):
                events.append(payload)
        return {
            "run_id": run_id,
            "request_id": record.get("request_id"),
            "parent_run_id": record.get("parent_run_id"),
            "from_stage": record.get("from_stage"),
            "request": record.get("request") or {},
            "purpose": (
                (record.get("request") or {}).get("purpose")
                if isinstance(record.get("request"), dict)
                else None
            ),
            "scope": {
                "kind": "formal",
                "target_stage": None,
                "formal_eligible": run_state == "succeeded",
            },
            "formal_eligible": run_state == "succeeded",
            "state": run_state,
            "events": events,
            "stages": stage_rows,
            "by_stage": by_stage,
            "members": members,
            "technical": {
                "commands": [],
                "claims": [],
                "effects": [],
            },
        }

    def _stage_tree(self, run_id: str, stage: str, checkpoint_uri: object) -> Path | None:
        if not isinstance(checkpoint_uri, str) or not checkpoint_uri.startswith("store://sealed/"):
            return None
        relative = checkpoint_uri.removeprefix("store://sealed/")
        if relative != f"{run_id}/{stage}":
            return None
        return self.sealed(checkpoint_uri)

    def _bind_projection_verify(
        self,
        run_id: str,
        files: dict[str, bytes],
        receipt: dict,
    ) -> dict[str, bytes]:
        generate_files = {
            path: data
            for path, data in files.items()
            if path.startswith(("model/", "urdf/", "meshes/")) or path == "tool.json"
        }
        if "urdf/robot.urdf" not in generate_files:
            generate_files["urdf/robot.urdf"] = b"<robot name='fixture'/>\n"
        capture_files = {
            path: data
            for path, data in files.items()
            if path.startswith(("input/", "evidence/"))
        }
        if not capture_files:
            capture_files["input/handoff.json"] = b"{}\n"
        capture = self.projection_artifacts.register(
            run_id=run_id,
            stage="capture",
            files=capture_files,
        )
        config_snapshot_sha256 = "e" * 64
        contract_sha256 = "f" * 64
        generate_executor_sha256 = digest({"tool": "generate"})
        generate_receipt = {
            "capture_artifact": capture,
            "config_snapshot_sha256": config_snapshot_sha256,
            "contract_sha256": contract_sha256,
            "actual_executor_sha256": generate_executor_sha256,
            "required_executor_sha256": generate_executor_sha256,
        }
        generate_receipt_bytes = (
            json.dumps(generate_receipt, sort_keys=True).encode("utf-8")
            + b"\n"
        )
        generate_files["reports/generate.json"] = (
            generate_receipt_bytes
        )
        files["reports/generate.json"] = generate_receipt_bytes
        generate = self.projection_artifacts.register(
            run_id=run_id,
            stage="generate",
            files=generate_files,
        )
        quality = {}
        try:
            parsed = json.loads(files.get("reports/quality.json", b"{}"))
            quality = parsed if isinstance(parsed, dict) else {}
        except ValueError:
            quality = {}
        verified = quality.get("passed") is True
        del receipt
        subject = generate["set_sha256"]
        request = {
            "capture_artifact": capture,
            "generate_artifact": generate,
        }
        certificate = {
            "actual_verifier": {"executor": "fixture"},
            "actual_verifier_sha256": digest({"tool": "verify"}),
            "capture_artifact": capture,
            "checks": quality.get("checks") or [],
            "config_snapshot_sha256": config_snapshot_sha256,
            "contract_sha256": contract_sha256,
            "decision": "PASS" if verified else "FAIL",
            "diagnostics": quality.get("diagnostics") or [],
            "evaluation_subject_sha256": digest(
                {
                    **{
                        "input/" + path.removeprefix("input/"): hashlib.sha256(data).hexdigest()
                        for path, data in capture_files.items()
                        if path.startswith("input/")
                    },
                    **{
                        "evidence/" + path.removeprefix("evidence/"): hashlib.sha256(data).hexdigest()
                        for path, data in capture_files.items()
                        if path.startswith("evidence/")
                    },
                    **{
                        path: hashlib.sha256(data).hexdigest()
                        for path, data in generate_files.items()
                        if path == "tool.json"
                        or path.startswith(
                            ("model/", "urdf/", "meshes/")
                        )
                    },
                }
            ),
            "generate_artifact": generate,
            "generate_receipt_sha256": hashlib.sha256(
                generate_receipt_bytes
            ).hexdigest(),
            "passed": verified,
            "policy_sha256": "1" * 64,
            "purpose": "engineering",
            "request": request,
            "request_sha256": digest(request),
            "schema_version": VERIFICATION_CERTIFICATE_SCHEMA,
            "subject_sha256": subject,
        }
        certificate["verification_key"] = digest(certificate)
        files["verification-certificate.json"] = (
            json.dumps(certificate, sort_keys=True).encode("utf-8") + b"\n"
        )
        files["verification-execution.json"] = b"{}\n"
        return files

    def _verify_pair_refs(self, run_id: str, files: dict[str, bytes]) -> tuple[dict, dict]:
        certificate = json.loads(files["verification-certificate.json"])
        del run_id
        return certificate["generate_artifact"], certificate["capture_artifact"]
