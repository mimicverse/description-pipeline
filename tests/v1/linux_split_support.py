"""Shared split-capture fixtures: one admission chain, one portable twin.

The synthetic native tree carries bare staged names under ``input/cad``; the
sealed transfer identity is the digest of that selected closure, distinct from
the full admitted upload.  The mixin registers freeze and capture through the
platform's own sealed writer, so every split-capture suite feeds and is fed by
the same verified chain instead of a hand-built store view.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import zipfile
from pathlib import Path

from packaging.utils import canonicalize_name

from description_pipeline.delivery import write_delivery_manifest
from description_pipeline.interface_records import CHECK_POLICY_SCHEMA, check_policy_digest
from description_pipeline.io import digest, file_digest, read_data, write_json
from description_pipeline.platform import capture as platform_capture
from description_pipeline.platform import flow
from description_pipeline.platform import storage as platform_storage
from description_pipeline.request_evidence import build_request_evidence, request_evidence_bytes
from description_pipeline.runtime import RUNTIME_VERSIONS, required_packages, tool_record
from description_pipeline.stages import CONFIRMED_STAGE_PASSED, CONTRACT, CONTRACT_SHA256

from .test_stage_transfer import MAIN_ASSEMBLY

SELECTED = {MAIN_ASSEMBLY: hashlib.sha256(b"CAD-BYTES").hexdigest()}
UPLOAD = {**SELECTED, "unrelated.txt": hashlib.sha256(b"not selected").hexdigest()}
HANDOFF = digest(SELECTED)


def capture_job(run_id: str, archive: Path, tool: dict) -> dict:
    """A current server receipt whose upload differs from its selected closure."""
    from tests.sources.selection_support import sealed_selection

    selection = sealed_selection(
        UPLOAD,
        MAIN_ASSEMBLY,
        reader_sha256=tool["source_sha256"],
        stage_root=r"C:\stage",
        edges={MAIN_ASSEMBLY: []},
    )
    with zipfile.ZipFile(archive) as packed:
        manifest_sha256 = hashlib.sha256(packed.read("transfer-manifest.json")).hexdigest()
    return {
        "status": "native_complete",
        "run_id": run_id,
        "main_assembly": MAIN_ASSEMBLY,
        "native_tool": tool,
        "handoff_sha256": HANDOFF,
        "package_files": dict(SELECTED),
        "request": {
            "run_id": run_id,
            "upload_files": dict(UPLOAD),
            "upload_sha256": digest(UPLOAD),
            "main_assembly": MAIN_ASSEMBLY,
        },
        "selection": {
            "state": "sealed",
            "reader_sha256": tool["source_sha256"],
            "upload_sha256": digest(UPLOAD),
            "staged_files": dict(UPLOAD),
            "manifest": selection,
        },
        "result": {
            "native_complete": True,
            "native_tool": tool,
            "capture_archive": {
                "name": "native-evidence.zip",
                "sha256": file_digest(archive),
                "size": archive.stat().st_size,
                "manifest_sha256": manifest_sha256,
            },
        },
    }


def portable_record(native: dict, *, source_sha256: str | None = None, mujoco: str = "3.13.0") -> dict:
    """A portable twin of a fixture native tool record, carrying the pinned closure."""
    record = json.loads(json.dumps(native))
    packages = {canonicalize_name(name): RUNTIME_VERSIONS[name] for name in required_packages("portable")}
    packages["mujoco"] = mujoco
    record["runtime"] = {
        **record.get("runtime", {}),
        "role": "portable",
        "system": "Linux",
        "python": "3.12.14",
        "packages": packages,
    }
    if source_sha256 is not None:
        record["source_sha256"] = source_sha256
    return record


class FakeEndpoint:
    """A deliberately unverifying artifact client; admission must not trust it."""

    def __init__(self, payload: bytes):
        self.payload = payload
        self.calls = 0

    def stream_capture_archive(self, run_id, archive, destination, **kwargs):
        self.calls += 1
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(self.payload)
        return destination


class SplitAdmissionMixin:
    """The freeze/capture admission chain shared by the split-capture suites.

    Subclasses set ``tmp``, ``store``, ``store_root``, ``tool`` and ``archive``
    in ``setUp``; every registration goes through the platform's sealed writer
    and the store's confirmation, so the fixture is verified by the same chain
    it feeds.
    """

    def _paths(self, run_id: str) -> flow.RunPaths:
        return flow.RunPaths(self.store_root, run_id)

    def _frozen_payload(self) -> bytes:
        return request_evidence_bytes(
            build_request_evidence(
                request_id="fixture-frozen-request",
                target_hardware="nd_fixture",
                change_summary="fixture frozen original",
                main_assembly=MAIN_ASSEMBLY,
                handoff_digest=HANDOFF,
                platform_config_digest="c" * 64,
            )
        )

    def _subject_layout(self, tree: Path) -> None:
        """The Linux-side subject layout every sealed stage tree carries (deploy parity)."""
        for name in ("input", "evidence", "model", "urdf", "meshes"):
            (tree / name).mkdir(parents=True, exist_ok=True)
        if not (tree / "tool.json").is_file():
            write_json(tree / "tool.json", tool_record(role="portable"))

    def _seal_stage(self, run_id: str, stage: str, tree: Path, *, state: str, **receipt) -> tuple[str, dict]:
        """Seal one stage tree and build its receipt through the one writer primitive.

        Mirrors the deploy side's receipt builder: the subject digest and the artifact
        inventory are read back from the sealed bytes, and the handoff binding is the
        frozen package digest (freeze) or the sealed native transfer identity.  The
        returned uri and completed receipt are what a caller would confirm with.
        """
        manifest = write_delivery_manifest(
            tree,
            contract={"schema_version": CONTRACT["schema_version"], "digest": CONTRACT_SHA256},
            policy={"schema_version": CHECK_POLICY_SCHEMA, "digest": check_policy_digest()},
        )
        if stage == "freeze":
            handoff = digest(read_data(tree / "input" / "handoff.json")["package_files"])
        else:
            handoff = read_data(tree / "transfer-manifest.json")["handoff_sha256"]
        payload = {
            "schema_version": "solidworks-to-urdf.bundle/v1",
            "pipeline_id": "solidworks-to-urdf",
            "run_id": run_id,
            "stage": stage,
            "state": state,
            "passed": CONFIRMED_STAGE_PASSED[stage],
            "subject_sha256": manifest["subject"]["digest"],
            "artifacts": [dict(record) for record in manifest["subject"]["files"]],
            "handoff_sha256": handoff,
            **receipt,
        }
        return platform_storage.seal_stage_confirmation(self.store_root, run_id, stage, tree, payload)

    def _register_stage(self, run_id: str, stage: str, tree: Path, *, state: str, **receipt) -> dict:
        """Seal one stage tree and register the row through the one writer primitive.

        The registration re-verifies the sealed delivery before the stage row is written.
        """
        uri, completed = self._seal_stage(run_id, stage, tree, state=state, **receipt)
        self.store.confirm_stage(run_id=run_id, stage=stage, state="completed", receipt=completed, checkpoint_uri=uri)
        return completed

    def _register_freeze(self, run_id: str) -> bytes:
        """Seal and register the freeze stage: the full admitted upload and the request original."""
        payload = self._frozen_payload()
        tree = self.tmp / f"fixture-freeze-{run_id}"
        (tree / "input" / "cad").mkdir(parents=True)
        (tree / "input" / "cad" / MAIN_ASSEMBLY).write_bytes(b"CAD-BYTES")
        (tree / "input" / "unrelated.txt").write_bytes(b"not selected")
        (tree / "input" / "request.json").write_bytes(payload)
        package_files = {
            "cad/" + MAIN_ASSEMBLY: file_digest(tree / "input" / "cad" / MAIN_ASSEMBLY),
            "unrelated.txt": file_digest(tree / "input" / "unrelated.txt"),
        }
        write_json(tree / "input" / "handoff.json", {"passed": True, "package_files": package_files})
        self._subject_layout(tree)
        self._register_stage(run_id, "freeze", tree, state="frozen", tool=read_data(tree / "tool.json"))
        return payload

    def admit(self, run_id: str, archive: Path | None = None):
        """Admit the sealed native capture through the platform seam (fixture plumbing)."""
        payload = self._register_freeze(run_id)
        paths = self._paths(run_id)
        archive = Path(archive) if archive is not None else self.archive
        job = capture_job(run_id, archive, self.tool)
        endpoint = FakeEndpoint(archive.read_bytes())
        result = platform_capture.import_native_capture(self.store, paths, run_id, job, endpoint)
        # The DAG materializes both mutable copies of the registered frozen request:
        # the attempt-level original and the copy carried into the capture tree.
        for target in (paths.run_dir / "input" / "request.json", paths.capture_dir / "input" / "request.json"):
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payload)
        # The native registration seals a snapshot: the capture members stay byte-exact
        # in the attempt tree while the snapshot adds the Linux-side subject layout.
        snapshot = self.tmp / f"fixture-native-{run_id}"
        shutil.copytree(paths.capture_dir, snapshot)
        self._subject_layout(snapshot)
        self._register_stage(run_id, "capture", snapshot, state="native_complete", native_tool=self.tool)
        return result
