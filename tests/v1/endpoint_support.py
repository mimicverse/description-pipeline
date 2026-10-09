"""Synthetic native-boundary controls; these helpers cannot qualify a CAD model."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path
from types import SimpleNamespace

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
    write_json(discovery, {"synthetic_control": True})
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
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "engineering"
        self.source.mkdir()
        (self.source / "总装.SLDASM").write_bytes(b"Synthetic native-boundary control; not actual CAD")
        self.packages = self.root / "packages"
        self.packages.mkdir()
        repository = self.root / "repository"
        repository.mkdir()
        subprocess.run(["git", "init", "-q", str(repository)], check=True)
        subprocess.run(
            ["git", "-C", str(repository), "remote", "add", "origin", "https://github.com/a/b.git"], check=True
        )
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
            "targets": {"arm": {"repository": str(repository), "base": "feature/arm"}},
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
            runner=runner or (lambda *args, **kwargs: self.passing_result(on_event=kwargs["on_event"])),
        )
        self.addCleanup(jobs.close)
        return jobs

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
