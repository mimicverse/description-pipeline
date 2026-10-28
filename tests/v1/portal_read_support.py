"""The registered fact projection the portal suites read through."""

from __future__ import annotations

import json
import hashlib
import shutil
from pathlib import Path
from types import SimpleNamespace

from description_pipeline.delivery import (
    PIPELINE_ID,
    read_delivery_manifest,
    write_delivery_manifest,
)
from description_pipeline.io import digest, file_digest, read_data, write_json
from description_pipeline.interface_records import (
    CHECK_POLICY_SCHEMA,
    check_policy_digest,
)
from description_pipeline.pipeline.contracts import ArtifactRef
from description_pipeline.request_evidence import (
    build_request_evidence,
    write_request_evidence,
)
from description_pipeline.runtime import tool_record
from description_pipeline.schemas import (
    TOOL_SCHEMA,
    VERIFICATION_CERTIFICATE_SCHEMA,
)
from description_pipeline.stages import CONTRACT, CONTRACT_SHA256, STAGE_IDS, stage_view

from .protocol_support import protocol_events
from .source_record_support import source_record


def build_sealed_delivery(
    root: Path,
    *,
    hardware_id: str = "nd",
    revision: str | None = None,
    run_id: str = "fixture-sealed-delivery",
) -> tuple[str, list[dict], str, str]:
    """Build one complete delivery tree for portal fixture projections."""

    for name in ("input", "evidence", "model", "urdf", "meshes", "reports"):
        (root / name).mkdir(parents=True)
    (root / "input" / "records").mkdir()
    (root / "input" / "discovery").mkdir()
    (root / "evidence" / "raw").mkdir()
    (root / "native-observation").mkdir()
    (root / "prepared").mkdir()
    write_json(root / "stage-request.json", {"fixture": "freeze-stage-request"})
    write_json(root / "request.json", {"fixture": "stage-request"})
    write_json(root / "inputs.json", {"fixture": "freeze-inputs"})
    write_json(root / "ruleset.json", {"fixture": "freeze-ruleset"})
    write_json(root / "configuration.json", {"fixture": "freeze-configuration"})
    write_json(root / "checks.json", {"fixture": "stage-checks"})
    write_json(root / "result.json", {"fixture": "discover-result"})
    write_json(
        root / "native-observation" / "observation.json",
        {"fixture": "native-observation"},
    )
    write_json(root / "prepared" / "request.json", {"fixture": "prepared-request"})
    write_json(root / "input" / "records" / "arm.json", {"record": "fixture"})
    (root / "input" / "cad" / "cad").mkdir(parents=True)
    (root / "input" / "cad" / "cad" / "robot.SLDASM").write_bytes(
        b"fixture-native"
    )
    native_inventory = {
        "cad/robot.SLDASM": file_digest(
            root / "input" / "cad" / "cad" / "robot.SLDASM"
        )
    }
    write_json(
        root / "input" / "source.json",
        source_record(
            hardware_id=hardware_id,
            handoff_digest=digest(native_inventory),
            main_assembly="cad/robot.SLDASM",
            revision=revision,
        ),
    )
    package_files = {
        "source.json": file_digest(root / "input" / "source.json"),
        "records/arm.json": file_digest(root / "input" / "records" / "arm.json"),
        "cad/cad/robot.SLDASM": file_digest(
            root / "input" / "cad" / "cad" / "robot.SLDASM"
        ),
    }
    write_json(
        root / "input" / "handoff.json",
        {
            "passed": True,
            "package_files": package_files,
            "input_receipt": {"inventory": native_inventory},
        },
    )
    write_json(
        root / "input" / "discovery" / "native-discovery.json",
        {
            "discovery": "fixture",
            "handoff_sha256": digest(native_inventory),
            "native_files": native_inventory,
            "identity": {
                "main_assembly": "cad/robot.SLDASM",
                "delivery_configuration": "Default",
                "hardware_id": hardware_id,
                "revision": revision,
            },
            "records": [],
        },
    )
    write_json(root / "input" / "capture-plan.json", {"plan": "fixture"})
    write_json(
        root / "evidence" / "native-runtime.json",
        {"runtime": "fixture"},
    )
    write_json(
        root / "evidence" / "raw" / "native_capture_facts.json",
        {},
    )
    write_json(root / "model" / "robot.json", {"model": "fixture"})
    (root / "urdf" / "robot.urdf").write_text("<robot/>\n", encoding="utf-8")
    (root / "meshes" / "part.stl").write_bytes(b"mesh-stl")
    write_json(root / "tool.json", tool_record())
    write_json(root / "reports" / "quality.json", {"quality": "fixture"})
    write_json(
        root / "reports" / "generate.json",
        {"fixture": "generate-receipt"},
    )
    write_json(
        root / "reports" / "native-tool.json",
        {
            "schema_version": TOOL_SCHEMA,
            "pipeline_id": PIPELINE_ID,
            "version": "1.4.0",
            "source_sha256": "a" * 64,
            "runtime": {"role": "native"},
        },
    )
    native_events = protocol_events(stages=("freeze", "discover", "capture"))
    native_stages = stage_view(
        {
            "run_id": run_id,
            "request": {"handoff_sha256": digest(native_inventory)},
            "result": {
                "execution_scope": ["freeze", "discover", "capture"]
            },
            "events": native_events,
        }
    )
    native_stages["events"] = native_events
    write_json(root / "reports" / "native-stages.json", native_stages)
    write_json(
        root / "transfer-manifest.json",
        {"handoff_sha256": digest(native_inventory)},
    )
    write_json(
        root / "verification-certificate.json",
        {"fixture": "verification-certificate"},
    )
    write_json(
        root / "verification-execution.json",
        {"fixture": "verification-execution"},
    )
    write_json(
        root / "publication-receipt.json",
        {"fixture": "publication-receipt"},
    )
    write_json(
        root / "publication-plan.json",
        {"fixture": "publication-plan"},
    )
    write_json(
        root / "observation.json",
        {"fixture": "publication-observation"},
    )
    (root / "candidate.bundle").write_bytes(b"fixture-candidate\n")
    write_request_evidence(
        root / "input" / "request.json",
        build_request_evidence(
            purpose="engineering",
            request_id="fixture-sealed-delivery",
            target_hardware=hardware_id,
            change_summary="fixture",
            main_assembly="cad/robot.SLDASM",
            handoff_digest=digest(package_files),
            platform_config_digest="c" * 64,
        ),
    )
    write_delivery_manifest(
        root,
        contract={
            "schema_version": CONTRACT["schema_version"],
            "digest": CONTRACT_SHA256,
        },
        policy={
            "schema_version": CHECK_POLICY_SCHEMA,
            "digest": check_policy_digest(),
        },
    )
    manifest = read_delivery_manifest(root)
    return (
        manifest["subject"]["digest"],
        [dict(record) for record in manifest["subject"]["files"]],
        digest(package_files),
        digest(native_inventory),
    )


class RegisteredProjectionArtifacts:
    """Materialize the immutable members registered by one test-side projection."""

    def __init__(self) -> None:
        self.objects: dict[str, dict[str, bytes]] = {}

    def register(
        self,
        *,
        run_id: str,
        producer_run_id: str | None = None,
        stage: str,
        files: dict[str, bytes],
        kind: str | None = None,
    ) -> dict:
        producer_run_id = producer_run_id or run_id
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
        artifact_id = "fixture:" + digest(
            {
                "run_id": producer_run_id,
                "stage": stage,
                "set": set_sha256,
            }
        )
        record = {
            "artifact_id": artifact_id,
            "kind": kind or f"stage_output:{stage}",
            "locator": f"cas://artifact/{artifact_id}",
            "manifest_sha256": digest({"manifest": artifact_id}),
            "producer_run_id": producer_run_id,
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


class _RegisteredWorld:
    def __init__(self) -> None:
        self.runs: dict[str, dict] = {}
        self.run_states: dict[str, str] = {}
        self.stages: dict[tuple[str, str], dict] = {}
        self.events: dict[str, list[dict]] = {}
        self.display_metadata: dict[str, dict] = {}


class RegisteredFactPort:
    """Minimal registered-facts double used by the portal read tests."""

    def __init__(self, platform: RegisteredPlatform) -> None:
        self.platform = platform
        self.fact_projection = platform.fact_projection
        self.projection_artifacts = platform.projection_artifacts

    def run_record(self, run_id: str) -> dict | None:
        record = self.platform.world.runs.get(run_id)
        if record is None:
            return None
        return {
            **record,
            "state": self.platform.world.run_states.get(
                run_id,
                record.get("state"),
            ),
        }

    def stage_records(self, run_id: str) -> dict[str, dict]:
        return {
            stage: dict(row)
            for (candidate_run, stage), row in self.platform.world.stages.items()
            if candidate_run == run_id
        }

    def confirm_stage(
        self,
        *,
        run_id: str,
        stage: str,
        state: str,
        receipt: dict | None = None,
    ) -> None:
        source_run = run_id
        self.platform.world.stages[(run_id, stage)] = {
            "stage": stage,
            "state": state,
            "receipt": dict(receipt or {}),
            "source_run": source_run,
            "original_executed_at": None,
            "updated_at": None,
        }

    def admission(self):
        return SimpleNamespace(state="open")

    def run_displays(self) -> list[dict]:
        return list(self.platform.world.display_metadata.values())

    def run_display(self, run_id: str) -> dict | None:
        row = self.platform.world.display_metadata.get(run_id)
        return dict(row) if row is not None else None

    def set_run_display(self, run_id: str, display_name: str | None) -> dict:
        row = self.platform.world.display_metadata.setdefault(
            run_id,
            {
                "run_id": run_id,
                "title": None,
                "deleted": False,
                "deleted_at": None,
                "deleted_by": None,
            },
        )
        row["title"] = display_name
        return dict(row)

    def hide_run(self, run_id: str, *, actor: str) -> dict:
        row = self.set_run_display(run_id, None)
        row.update({"deleted": True, "deleted_by": actor})
        self.platform.world.display_metadata[run_id].update(row)
        return row

    def restore_run(self, run_id: str) -> dict:
        row = self.run_display(run_id)
        if row is None:
            raise AssertionError("Run display metadata is absent")
        row.update({"deleted": False, "deleted_at": None, "deleted_by": None})
        self.platform.world.display_metadata[run_id].update(row)
        return row


class RegisteredRunControl:
    """Expose the canonical portal control surface over the fact fixture."""

    def __init__(self, facts: RegisteredFactPort) -> None:
        self.facts = facts
        self.fact_projection = facts.fact_projection
        self.projection_artifacts = facts.projection_artifacts

    def admission(self):
        return self.facts.admission()

    def list_run_display_metadata(self):
        return self.facts.run_displays()

    def get_run_display_metadata(self, run_id: str):
        return self.facts.run_display(run_id)

    def rename_run(self, run_id: str, display_name: str | None):
        return self.facts.set_run_display(run_id, display_name)

    def hide_run(self, run_id: str, *, actor: str):
        return self.facts.hide_run(run_id, actor=actor)

    def restore_run(self, run_id: str):
        return self.facts.restore_run(run_id)


class RegisteredPlatform:
    """One in-memory run/stage fixture plus immutable artifact registrations."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.world = _RegisteredWorld()
        self.projection_artifacts = RegisteredProjectionArtifacts()
        self.facts = RegisteredFactPort(self)
        self.run_control = RegisteredRunControl(self.facts)

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
        self.world.runs[run_id] = {
            "run_id": run_id,
            "request_id": f"q-{run_id}",
            "parent_run_id": parent_run_id,
            "from_stage": from_stage,
            "request": {
                "target_hardware": target_hardware,
                "main_assembly": main_assembly,
                "purpose": "engineering",
            },
        }
        self.world.run_states[run_id] = state

    def conclude(self, run_id: str, state: str) -> None:
        self.world.run_states[run_id] = state

    def stage(
        self,
        run_id: str,
        stage: str,
        *,
        state: str = "completed",
        receipt: dict | None = None,
        reuse_from: tuple[str, str] | None = None,
    ) -> None:
        source_run, source_stage = reuse_from or (run_id, stage)
        self.world.stages[(run_id, stage)] = {
            "stage": stage,
            "state": state,
            "receipt": dict(receipt or {}),
            "source_run": source_run,
            "source_stage": source_stage,
            "original_executed_at": None,
            "updated_at": None,
        }

    def tree(self, run_id: str, stage: str) -> Path:
        tree = self.sealed(f"store://sealed/{run_id}/{stage}")
        tree.mkdir(parents=True, exist_ok=True)
        return tree

    def register_capture(
        self,
        run_id: str,
        *,
        hardware_id: str = "robot",
        revision: str = "r1",
    ) -> Path:
        capture = self.tree(run_id, "capture")
        registered = self.facts.stage_records(run_id).get("capture") or {}
        if registered.get("state") == "completed":
            return capture
        if capture.exists():
            shutil.rmtree(capture)
        capture.mkdir(parents=True)
        _subject, artifacts, _handoff_full, handoff_native = (
            build_sealed_delivery(
                capture,
                hardware_id=hardware_id,
                revision=revision,
                run_id=run_id,
            )
        )
        source = read_data(capture / "input/source.json")
        self.stage(
            run_id,
            "capture",
            receipt={
                "schema_version": "solidworks-to-urdf.bundle/v1",
                "pipeline_id": PIPELINE_ID,
                "run_id": run_id,
                "stage": "capture",
                "state": "native_complete",
                "passed": False,
                "handoff_sha256": handoff_native,
                "upload_sha256": source["identity"]["handoff_digest"],
                "artifacts": artifacts,
                "native_tool": read_data(
                    capture / "reports/native-tool.json"
                ),
                "native_prepared_dir": "C:\\fixture\\prepared",
            },
        )
        return capture

    def events(self, run_id: str, events: list[dict]) -> None:
        self.world.events[run_id] = [dict(event) for event in events]

    def fact_projection(self, run_id: str) -> dict | None:
        """Project registered legacy fixture rows into the canonical read facts."""

        record = self.facts.run_record(run_id)
        if not isinstance(record, dict):
            return None
        registered = self.facts.stage_records(run_id)
        stage_rows: list[dict] = []
        by_stage: dict[str, dict[str, dict]] = {}
        members: dict[str, dict] = {}
        for stage in STAGE_IDS:
            row = registered.get(stage)
            if not isinstance(row, dict):
                stage_rows.append(
                    {
                        "stage": stage,
                        "state": "waiting",
                        "command": None,
                        "decision": None,
                        "receipt": {"artifacts": []},
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
            tree = self._stage_tree(run_id, stage, row)
            source_run = row.get("source_run")
            if not isinstance(source_run, str) or not source_run:
                raise AssertionError(f"{stage} fixture has no producer run")
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
                    by_stage.get("capture"),
                )
            artifact = self.projection_artifacts.register(
                run_id=run_id,
                producer_run_id=source_run,
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
                    **{
                        key: value
                        for key, value in row.items()
                        if key not in {"source_run", "source_stage"}
                    },
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
        recorded_state = str(record.get("state"))
        stage_states = {
            row["state"]
            for row in stage_rows
            if row.get("state") != "waiting"
        }
        if recorded_state not in {
            "completed",
            "succeeded",
            "failed",
            "cancelled",
            "unresolved",
            "running",
        }:
            run_state = recorded_state
        elif recorded_state == "unresolved":
            run_state = "unresolved"
        elif recorded_state == "cancelled":
            run_state = "cancelled"
        elif recorded_state == "failed":
            run_state = "failed"
        elif "unresolved" in stage_states:
            run_state = "unresolved"
        elif "cancelled" in stage_states or recorded_state == "cancelled":
            run_state = "cancelled"
        elif "failed" in stage_states or recorded_state == "failed":
            run_state = "failed"
        elif recorded_state == "succeeded":
            run_state = "succeeded"
        else:
            run_state = "running"
        events = []
        for event in self.world.events.get(run_id, ()):
            if isinstance(event, dict):
                events.append(dict(event))
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

    def _stage_tree(
        self,
        run_id: str,
        stage: str,
        row: dict,
    ) -> Path | None:
        source_run = row.get("source_run")
        source_stage = row.get("source_stage", stage)
        if not isinstance(source_run, str) or not isinstance(source_stage, str):
            return None
        del run_id, stage
        return self.sealed(
            f"store://sealed/{source_run}/{source_stage}"
        )

    def _bind_projection_verify(
        self,
        run_id: str,
        files: dict[str, bytes],
        receipt: dict,
        capture_members: dict | None,
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
        capture = None
        capture_object_id = None
        for member in (capture_members or {}).values():
            if not isinstance(member, dict) or not isinstance(
                member.get("artifact"),
                dict,
            ):
                continue
            candidate = ArtifactRef.from_record(
                member["artifact"]
            ).to_record()
            if capture is not None and candidate != capture:
                raise AssertionError(
                    "capture fixture spans multiple artifacts"
                )
            capture = candidate
            capture_object_id = capture["artifact_id"]
        if capture_object_id is not None:
            registered_capture = self.projection_artifacts.objects[
                capture_object_id
            ]
            capture_files = {
                path: data
                for path, data in registered_capture.items()
                if path.startswith(("input/", "evidence/"))
            }
        if not capture_files:
            capture_files["input/handoff.json"] = b"{}\n"
        if capture is None:
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
