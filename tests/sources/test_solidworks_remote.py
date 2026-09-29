"""Freeze through the worker HTTP API: submit, follow, download, verify."""

from __future__ import annotations

import io
import json
import os
import tarfile
import tempfile
import threading
import unittest
from unittest.mock import patch
from pathlib import Path

from . import _paths  # noqa: F401  (import side effect: sys.path)

from description_pipeline.sources.solidworks.errors import BridgeError  # noqa: E402
from description_pipeline.sources.solidworks.freeze import freeze  # noqa: E402
from description_pipeline.sources.solidworks.remote import (  # noqa: E402
    WorkerClient,
    _extract_tar,
    _snapshot_root,
)
from description_pipeline.sources.solidworks.scene import load_scene  # noqa: E402
from description_pipeline.sources.solidworks.worker import Worker, serve  # noqa: E402

from . import support  # noqa: E402


class RemoteFreezeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="sw-remote-"))
        self.assembly = support.make_cad_tree(self.tmp / "cad")
        self.backend = support.FixtureCadBackend(
            self.assembly,
            [
                {"name": "base-1", "transform": support.placement(), "mass": support.mass_payload(1.0)},
                {
                    "name": "arm-1",
                    "transform": support.placement((0.0, 0.0, 0.2)),
                    "mass": support.mass_payload(0.5, (0.0, 0.0, 0.05)),
                },
            ],
            dependencies=[self.tmp / "cad" / "base.SLDPRT", self.tmp / "cad" / "arm.SLDPRT"],
        )
        self.config = {
            "provider": "solidworks",
            "assembly": str(self.assembly),
            "configuration": "Default",
            # this worker runs the fixture backend, so the capture is declared as
            # fixture evidence - exactly the weaker class the adapter allows
            "evidence_class": "fixture",
            "allowed_roots": [str(self.tmp / "cad")],
            "geometry": {"enabled": True},
            "bodies": [
                {"id": "base", "name": "base_link", "components": ["base-1"]},
                {"id": "arm", "name": "arm_link", "components": ["arm-1"]},
            ],
            "joints": [],
        }
        self.worker = Worker(
            jobs_root=self.tmp / "jobs",
            backend_factory=lambda: self.backend,
            freeze_fn=lambda config, destination: freeze(config, destination, backend=self.backend),
            watchdog_seconds=60.0,
        )
        self.server, self.server_thread = serve(self.worker, port=0)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.server_thread.join(timeout=5)
        self.worker.close()
        support.cleanup(self.tmp)

    def _source(self, **extra):
        config: dict[str, object] = {"worker_url": self.base}
        config.update(dict(self.config))
        config.update(extra)
        return config

    def test_freeze_through_the_worker_downloads_a_verified_snapshot(self) -> None:
        destination = self.tmp / "snapshot"
        manifest = freeze(self._source(), destination)

        self.assertEqual(manifest["kind"], "solidworks")
        # the fixture backend must never be published as native CAD evidence
        self.assertEqual(manifest["evidence_class"], "fixture")
        scene = load_scene(destination)
        self.assertEqual({link["name"] for link in scene["links"]}, {"base_link", "arm_link"})
        # the client verified the downloaded package, not just the worker's word
        self.assertIn("scene.json", manifest["files"])

    def test_loopback_tunnel_ignores_unrelated_environment_proxy(self) -> None:
        with patch.dict(
            os.environ,
            {
                "http_proxy": "http://127.0.0.1:1",
                "HTTP_PROXY": "http://127.0.0.1:1",
                "no_proxy": "",
                "NO_PROXY": "",
            },
        ):
            self.assertEqual(WorkerClient(self.base).health()["status"], "ok")

    def test_job_id_resumes_instead_of_capturing_twice(self) -> None:
        client = WorkerClient(self.base)
        job = client.submit({"kind": "freeze", "config": self.config})
        client.wait(job["job_id"], timeout=30)
        before = client.job(job["job_id"])["attempt"]

        manifest = freeze(self._source(job_id=job["job_id"]), self.tmp / "snapshot")

        self.assertEqual(manifest["kind"], "solidworks")
        self.assertEqual(client.job(job["job_id"])["attempt"], before)
        self.assertEqual(self._job_ids(), [job["job_id"]])

    def _job_ids(self) -> list[str]:
        jobs_root = self.tmp / "jobs"
        return sorted(path.name for path in jobs_root.iterdir() if (path / "job.json").is_file())

    def test_the_same_source_captured_twice_is_two_submissions(self) -> None:
        freeze(self._source(), self.tmp / "snapshot-a")
        # the CAD behind that path may have changed since: reusing the previous
        # job because the config is identical would hand back a stale snapshot
        freeze(self._source(), self.tmp / "snapshot-b")

        self.assertEqual(len(self._job_ids()), 2)
        self.assertTrue((self.tmp / "snapshot-b" / "manifest.json").is_file())

    def test_worker_must_echo_the_complete_submitted_request(self) -> None:
        for changed in (False, True):

            def wrong_receipt(submitted, changed=changed):
                return {
                    "job_id": "other-job",
                    "request": {**submitted, "config": {}} if changed else None,
                }

            with (
                self.subTest(changed=changed),
                patch.object(WorkerClient, "submit", side_effect=wrong_receipt),
                self.assertRaises(BridgeError) as raised,
            ):
                freeze(self._source(), self.tmp / "unverified")
            self.assertEqual(raised.exception.code, "worker_job_identity_mismatch")
        self.assertFalse((self.tmp / "unverified").exists())

    def test_timeout_is_diagnosable_and_leaves_the_job_running(self) -> None:
        client = WorkerClient(self.base, poll_seconds=0.05)
        release = threading.Event()

        def slow_freeze(config, destination):
            release.wait(timeout=10)
            return freeze(config, destination, backend=self.backend)

        self.worker._freeze_fn = slow_freeze  # noqa: SLF001 - test injection
        job = client.submit({"kind": "freeze", "config": self.config})
        with self.assertRaises(BridgeError) as raised:
            client.wait(job["job_id"], timeout=0.2)
        release.set()

        self.assertEqual(raised.exception.code, "worker_job_timeout")
        detail = raised.exception.detail
        assert isinstance(detail, dict)
        self.assertEqual(detail["job_id"], job["job_id"])
        self.assertIn(detail["state"], ("queued", "running"))
        self.assertEqual(detail["resume"]["worker_url"], self.base)

    def test_non_loopback_worker_url_needs_explicit_opt_in(self) -> None:
        with self.assertRaises(BridgeError) as raised:
            WorkerClient("http://192.0.2.10:8765")
        self.assertEqual(raised.exception.code, "worker_url_not_loopback")

    def test_packages_with_escaped_paths_are_rejected(self) -> None:
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w") as archive:
            info = tarfile.TarInfo("../escape.txt")
            info.size = 3
            archive.addfile(info, io.BytesIO(b"bad"))
        with self.assertRaises(BridgeError) as raised:
            _extract_tar(buffer.getvalue(), self.tmp / "extract", timeout=5.0)
        self.assertEqual(raised.exception.code, "snapshot_package_unsafe")

    def test_existing_empty_destination_is_used(self) -> None:
        destination = self.tmp / "snapshot"
        destination.mkdir()
        manifest = freeze(self._source(), destination)
        self.assertEqual(manifest["kind"], "solidworks")

    def test_resuming_with_a_different_source_config_is_refused(self) -> None:
        client = WorkerClient(self.base)
        job = client.submit({"kind": "freeze", "config": self.config})
        client.wait(job["job_id"], timeout=30)
        other = dict(self.config)
        other["configuration"] = "Other"

        with self.assertRaises(BridgeError) as raised:
            freeze(self._source(job_id=job["job_id"], configuration="Other"), self.tmp / "snapshot")

        self.assertEqual(raised.exception.code, "worker_job_config_mismatch")
        detail = raised.exception.detail
        assert isinstance(detail, dict)
        self.assertIn("requested", detail)
        self.assertIn("remote", detail)

    def test_snapshot_root_is_found_by_manifest_not_by_directory_count(self) -> None:
        staging = self.tmp / "staging"
        (staging / "wrapped").mkdir(parents=True)
        (staging / "wrapped" / "manifest.json").write_text("{}", encoding="utf-8")
        (staging / "noise").mkdir()
        self.assertEqual(_snapshot_root(staging), staging / "wrapped")

        (staging / "wrapped2").mkdir()
        (staging / "wrapped2" / "manifest.json").write_text("{}", encoding="utf-8")
        with self.assertRaises(BridgeError) as raised:
            _snapshot_root(staging)
        self.assertEqual(raised.exception.code, "snapshot_package_layout")

    def test_tar_rejects_devices_duplicates_and_windows_paths(self) -> None:
        def unsafe(members):
            buffer = io.BytesIO()
            with tarfile.open(fileobj=buffer, mode="w") as archive:
                for member, payload in members:
                    archive.addfile(member, io.BytesIO(payload) if payload is not None else None)
            return buffer.getvalue()

        fifo = tarfile.TarInfo("pipe")
        fifo.type = tarfile.FIFOTYPE
        duplicate = tarfile.TarInfo("same.txt")
        duplicate.size = 1
        drive = tarfile.TarInfo("C:/windows/system32/evil.txt")
        drive.size = 1
        unc = tarfile.TarInfo("\\?\\C:\\evil.txt")
        unc.size = 1

        aliases = []
        for name in ("file.txt:ads", "..:ads", "con", "aux.txt", "x.", "x "):
            member = tarfile.TarInfo(name)
            member.size = 1
            aliases.append((name, unsafe([(member, b"a")])))
        upper = tarfile.TarInfo("Scene.json")
        lower = tarfile.TarInfo("scene.json")
        upper.size = lower.size = 1
        aliases.append(("case_collision", unsafe([(upper, b"a"), (lower, b"b")])))

        for name, payload in (
            ("fifo", unsafe([(fifo, b"")])),
            ("duplicate", unsafe([(duplicate, b"a"), (duplicate, b"b")])),
            ("drive", unsafe([(drive, b"a")])),
            ("unc", unsafe([(unc, b"a")])),
            *aliases,
        ):
            with self.subTest(name=name):
                with self.assertRaises(BridgeError) as raised:
                    _extract_tar(payload, self.tmp / "unsafe-extract", timeout=5.0)
                self.assertEqual(raised.exception.code, "snapshot_package_unsafe")

    def test_fetch_verifies_file_digests(self) -> None:
        client = WorkerClient(self.base)
        job = client.submit({"kind": "freeze", "config": self.config})
        client.wait(job["job_id"], timeout=30)
        destination = self.tmp / "snapshot"
        client.fetch_snapshot(job["job_id"], destination)

        scene_path = destination / "scene.json"
        payload = json.loads(scene_path.read_text(encoding="utf-8"))
        payload["units"] = "MM"
        scene_path.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaises(ValueError):
            load_scene(destination)

    def _finished_job(self) -> tuple[WorkerClient, dict]:
        client = WorkerClient(self.base)
        job = client.submit({"kind": "freeze", "config": self.config})
        client.wait(job["job_id"], timeout=30)
        return client, job

    def test_snapshot_must_declare_the_source_that_was_requested(self) -> None:
        client, job = self._finished_job()
        other = dict(self.config)
        other["assembly"] = str(self.tmp / "cad" / "other.SLDASM")
        destination = self.tmp / "snapshot"

        with self.assertRaises(BridgeError) as raised:
            client.fetch_snapshot(job["job_id"], destination, expected=other, evidence_class="fixture")

        self.assertEqual(raised.exception.code, "snapshot_source_mismatch")
        detail = raised.exception.detail
        self.assertIsInstance(detail, dict)
        self.assertEqual(detail["fields"][0]["field"], "assembly")  # type: ignore[index]
        self.assertFalse(destination.exists())

    def test_assembly_paths_are_compared_the_way_windows_names_files(self) -> None:
        client, job = self._finished_job()
        same = dict(self.config)
        # separators and case do not name a different document
        same["assembly"] = str(self.assembly).replace("\\", "/").upper()
        destination = self.tmp / "snapshot"

        manifest = client.fetch_snapshot(job["job_id"], destination, expected=same, evidence_class="fixture")

        self.assertEqual(manifest["kind"], "solidworks")
        self.assertTrue((destination / "manifest.json").is_file())

    def test_configuration_names_are_compared_exactly(self) -> None:
        client, job = self._finished_job()
        other = dict(self.config)
        # 'default' and 'Default' are two different configurations in SolidWorks,
        # so the name is not normalised the way the path is
        other["configuration"] = "default"
        destination = self.tmp / "snapshot"

        with self.assertRaises(BridgeError) as raised:
            client.fetch_snapshot(job["job_id"], destination, expected=other, evidence_class="fixture")

        self.assertEqual(raised.exception.code, "snapshot_source_mismatch")
        detail = raised.exception.detail
        self.assertIsInstance(detail, dict)
        self.assertEqual(detail["fields"][0]["field"], "configuration")  # type: ignore[index]
        self.assertFalse(destination.exists())

    def test_snapshot_must_carry_the_claimed_evidence_class(self) -> None:
        client, job = self._finished_job()
        destination = self.tmp / "snapshot"

        # the worker answered with fixture evidence; a capture that claims native
        # CAD must not accept it
        with self.assertRaises(BridgeError) as raised:
            client.fetch_snapshot(job["job_id"], destination, expected=self.config, evidence_class="cad")

        self.assertEqual(raised.exception.code, "snapshot_evidence_class_mismatch")
        detail = raised.exception.detail
        self.assertIsInstance(detail, dict)
        self.assertEqual(detail["actual"], "fixture")  # type: ignore[index]
        self.assertFalse(destination.exists())

    def test_refused_snapshot_keeps_the_package_and_its_diagnostics(self) -> None:
        client, job = self._finished_job()
        other = dict(self.config)
        other["configuration"] = "Other"
        destination = self.tmp / "snapshot"

        with self.assertRaises(BridgeError) as raised:
            client.fetch_snapshot(job["job_id"], destination, expected=other, evidence_class="fixture")

        failure = destination.with_name(destination.name + ".failed-001")
        self.assertFalse(destination.exists())
        # the caller is told where the evidence went, and the error stays the same
        detail = raised.exception.detail
        self.assertIsInstance(detail, dict)
        self.assertEqual(detail["diagnostic_path"], str(failure))  # type: ignore[index]
        self.assertEqual(vars(raised.exception).get("diagnostic_path"), str(failure))
        record = json.loads((failure / "failure.json").read_text(encoding="utf-8"))
        self.assertEqual(record["code"], "snapshot_source_mismatch")
        self.assertEqual(record["stage"], "verify")
        self.assertEqual(record["job_id"], job["job_id"])
        self.assertEqual(record["package_bytes"], (failure / "package.tar").stat().st_size)
        # the unverified tree stays beside the package instead of in the snapshot
        self.assertTrue((failure / "partial").is_dir())


if __name__ == "__main__":
    unittest.main()
