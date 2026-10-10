"""Synthetic native-boundary controls; these helpers cannot qualify a CAD model."""

from __future__ import annotations

import shutil
import tempfile
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from description_pipeline.io import digest, file_digest, write_json
from description_pipeline.sources.solidworks.handoff import freeze_handoff
from description_pipeline.orchestration.windows import Jobs, read_config
from description_pipeline.sources.solidworks.revision import package_inventory, seal_revision
from .protocol_support import protocol_events


def prepare_control(source, output, run_id, **kwargs):
    shutil.copytree(source, output)
    (output / "robot.yaml").write_text("Generated queue control; not a qualified robot definition")
    revision = seal_revision(
        output,
        hardware_id="arm",
        revision="r1",
        owner="mechanical",
        system="handoff",
        reference="arm/r1",
        summary="Synthetic queue control",
    )
    discovery = output / "discovery/discovery.json"
    write_json(discovery, {"synthetic_control": True, "identity": {"main_assembly": "总装.SLDASM"}})
    return SimpleNamespace(
        passed=True,
        findings=(),
        package=output,
        hardware_id=revision["hardware_id"],
        revision=revision["revision"],
        discovery_path=discovery,
        discovery_sha256=file_digest(discovery),
        handoff_sha256=digest(package_inventory(source)),
        prepared_sha256=digest(package_inventory(output)),
    )


class EndpointFixture:
    def setUp(self):
        # The endpoint only runs on the native host; simulate its role identity here.
        identity = patch(
            "description_pipeline.orchestration.windows._native_tool_record",
            return_value={
                "name": "solidworks-native-reader",
                "version": "1.3.1",
                "runtime": {"role": "native", "python": "3.12.10", "packages": {"numpy": "2.5.3"}},
            },
        )
        identity.start()
        self.addCleanup(identity.stop)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "engineering"
        self.source.mkdir()
        (self.source / "总装.SLDASM").write_bytes(b"Synthetic native-boundary control; not actual CAD")
        self.packages = self.root / "packages"
        self.packages.mkdir()
        token = self.root / "token"
        token.write_text("t" * 64)
        self.path = self.root / "endpoint.json"
        self.config_data = {
            "schema_version": "solidworks-to-urdf.endpoint/v1",
            "package_root": str(self.packages),
            "handoff_roots": [str(self.source)],
            "output_root": str(self.root / "outputs"),
            "state_root": str(self.root / "state"),
            "token_file": str(token),
            "targets": {"arm": {"repository_slug": "a/b", "base": "feature/arm"}},
        }
        write_json(self.path, self.config_data)
        self.config = read_config(self.path)

    prepare = staticmethod(prepare_control)

    def request(self, jobs=None):
        if jobs is None:
            package, identity = freeze_handoff(self.source, self.packages / "imports")
            handoff = {**identity, "package": package.relative_to(self.packages).as_posix()}
        else:
            handoff = jobs.resolve_handoff({"handoff_path": str(self.source)})
            self.assertEqual({"schema_version", "pipeline_id", "package", "handoff_sha256"}, set(handoff))
        return {"run_id": str(uuid.uuid4()), "package": handoff["package"], "handoff_sha256": handoff["handoff_sha256"]}

    def jobs(self, runner=None, *, preparer=None):
        jobs = Jobs(
            self.config,
            native_preparer=preparer or self.prepare,
            runner=runner
            or (
                lambda package, output, **kwargs: self.capture_transfer_result(
                    output, run_id=kwargs["run_id"], on_event=kwargs["on_event"]
                )
            ),
        )
        self.addCleanup(jobs.close)
        return jobs

    def capture_transfer_result(self, output, *, run_id, on_event=None):
        """A sealed capture-transfer result: native_complete, never qualified success."""

        output = Path(output)
        output.mkdir(parents=True, exist_ok=True)
        archive = output / "native-evidence.zip"
        manifest = output / "transfer-manifest.json"
        archive.write_bytes(b"PK\x03\x04synthetic-transfer")
        write_json(manifest, {"schema_version": "solidworks-to-urdf.transfer/v1", "run_id": run_id})
        if on_event is not None:
            for event in protocol_events(stages=("freeze", "discover", "capture")):
                on_event(event)
        return {
            "native_complete": True,
            "passed": False,
            "state": "native_complete",
            "run_id": run_id,
            "capture_archive": {
                "name": "native-evidence.zip",
                "sha256": file_digest(archive),
                "size": archive.stat().st_size,
                "manifest_sha256": file_digest(manifest),
            },
        }

    def passing_result(self, *, on_event=None):
        subject = "a" * 64
        if on_event is not None:
            for event in protocol_events(stages=("capture", "generate", "verify", "publish"), subject=subject):
                on_event(event)
        return {
            "passed": True,
            "subject_sha256": subject,
            "quality": {
                "passed": True,
                "subject_sha256": subject,
                "checks": [{"id": "source.native_discovery", "passed": True}],
            },
            "submission": {
                "passed": True,
                "subject_sha256": subject,
                "url": "https://github.com/a/b/pull/1",
                "repository_slug": "a/b",
                "base": "feature/arm",
                "branch": "work/solidworks/arm",
                "state": "published",
                "commit": "b" * 40,
            },
        }
