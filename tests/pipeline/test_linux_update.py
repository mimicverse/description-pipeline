"""Linux capture/reuse semantics and ownership of the transient worker tunnel."""

from __future__ import annotations

import contextlib
import io
import json
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from description_pipeline import cli
from description_pipeline.build import freeze
from description_pipeline.io import PipelineError, read_data, write_json
from description_pipeline.repository import update
from description_pipeline.repository.tunnel import _forward, _idle, worker_tunnel
from description_pipeline.sources.snapshot import write_manifest
from tests.pipeline import test_model_update
from tests.pipeline.test_pipeline import scene

HEALTH = {
    "status": "ok",
    "runner": {"alive": True, "queued": 0, "current": None},
    "jobs": {"queued": 0, "running": 0},
    "worker_version": "0.3.5",
}
SOURCE = {"provider": "solidworks", "worker_url": "http://127.0.0.1:8765"}


class LinuxUpdateTests(test_model_update.ModelUpdateFixture):
    def test_default_message_is_english_and_does_not_read_stdin(self):
        self._prepare()
        with patch("description_pipeline.repository._submit", return_value={"passed": True}) as submit:
            result = update(self.root, "kinematics")
        self.assertEqual(submit.call_args.args[2], "Update microban model")
        self.assertFalse(submit.call_args.kwargs["ci"])
        self.assertEqual(result["preflight"]["message"], "Update microban model")
        self.assertEqual(result["source_action"], "captured")

    def test_managed_tunnel_is_only_held_during_capture(self):
        order = self._prepare()

        @contextlib.contextmanager
        def tunnel(*args):
            self.assertTrue((self.root / ".git/description-update.lock").exists())
            order.append("connect")
            try:
                yield {"host": "windows"}
            finally:
                order.append("disconnect")

        with patch("description_pipeline.repository.worker_tunnel", side_effect=tunnel):
            result = update(self.root, "kinematics", worker_host="windows")
        self.assertEqual(order, ["connect", "freeze", "disconnect", "build", "submit"])
        self.assertEqual(result["connection"], {"host": "windows"})

    def test_conflicting_options_are_rejected_before_any_stage(self):
        order = self._prepare()
        for options in (
            {"reuse_source": True, "worker_host": "windows"},
            {"reuse_source": True, "worker_port": 8765},
            {"reuse_source": True, "expect_worker_url": SOURCE["worker_url"]},
            {"worker_port": 8765},
            {"worker_host": "windows", "expect_worker_url": SOURCE["worker_url"]},
        ):
            with self.subTest(options=options), self.assertRaises(PipelineError):
                update(self.root, "kinematics", **options)
        self.assertEqual(order, [])

    def test_invalid_port_is_not_replaced_by_default(self):
        self._prepare()
        with (
            patch("description_pipeline.repository.worker_tunnel", side_effect=PipelineError("bad port")) as tunnel,
            self.assertRaises(PipelineError),
        ):
            update(self.root, "kinematics", worker_host="windows", worker_port=0)
        self.assertEqual(tunnel.call_args.args[-1], 0)

    def _frozen(self):
        original = Path(self.temp.name) / "fixture"
        original.mkdir()
        write_json(original / "scene.json", scene())
        write_manifest(original, kind="fixture", identity={"case": "reuse"}, evidence_class="fixture")
        write_json(
            self.root / "config/robot.yaml",
            {
                "schema_version": "description.definition/v1",
                "hardware_id": "microban",
                "source": {"provider": "fixture", "path": str(original)},
                "overrides": [],
            },
        )
        return freeze(self.root)

    def test_reuse_verifies_the_snapshot_without_contacting_cad(self):
        locked = self._frozen()
        order = self._prepare()
        with patch("description_pipeline.repository.worker_tunnel") as tunnel:
            result = update(self.root, "kinematics", reuse_source=True)
        self.assertEqual(order, ["build", "submit"])
        self.assertEqual(result["freeze"], locked)
        self.assertEqual(result["source_action"], "reused")
        tunnel.assert_not_called()

    def test_reuse_refuses_an_edited_source_and_corrupt_snapshot(self):
        locked = self._frozen()
        order = self._prepare()
        path = self.root / "config/robot.yaml"
        definition = read_data(path)
        original = json.loads(json.dumps(definition))
        definition["source"]["path"] += "-changed"
        write_json(path, definition)
        with self.assertRaisesRegex(PipelineError, "Source definition changed"):
            update(self.root, "kinematics", reuse_source=True)
        write_json(path, original)
        (self.root / locked["snapshot"] / "scene.json").write_text("{}")
        with self.assertRaises(PipelineError):
            update(self.root, "kinematics", reuse_source=True)
        self.assertEqual(order, [])

    def test_a_failed_capture_never_degrades_into_snapshot_reuse(self):
        """The runbook's promise, pinned: a capture that fails fails the update.

        A reusable snapshot is present in the workspace, so a fallback would succeed and publish the
        old bytes as if CAD had been captured.  Only `--reuse-source` may rebuild a frozen source.
        """

        self._frozen()
        order = self._prepare()
        with (
            patch("description_pipeline.repository.freeze", side_effect=PipelineError("capture failed")) as capture,
            self.assertRaisesRegex(PipelineError, "capture failed"),
        ):
            update(self.root, "kinematics")
        self.assertEqual(order, [])
        capture.assert_called_once()

    def test_cli_defaults_to_current_directory_and_allows_explicit_overrides(self):
        with patch.object(cli, "update", return_value={"ok": True}) as call, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["model", "update", "--worker-host", "windows"]), 0)
        self.assertEqual(call.call_args.args, (Path.cwd(), "kinematics", None))
        self.assertEqual(call.call_args.kwargs["worker_host"], "windows")
        with patch.object(cli, "update", return_value={"ok": True}) as call, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(
                cli.main(
                    ["model", "update", "--root", str(self.root), "--reuse-source", "--message", "Adjust dynamics"]
                ),
                0,
            )
        self.assertEqual(call.call_args.args, (self.root, "kinematics", "Adjust dynamics"))
        self.assertTrue(call.call_args.kwargs["reuse_source"])

    def test_explicit_empty_message_is_refused_before_capture(self):
        order = self._prepare()
        with self.assertRaises(PipelineError):
            update(self.root, "kinematics", " ")
        self.assertEqual(order, [])


@unittest.skipUnless(sys.platform == "linux", "Linux SSH entry")
class WorkerTunnelTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.process = MagicMock()
        self.process.pid = 12345
        self.process.poll.return_value = None
        self.commands = []
        self.forward_returncode = 0
        self.worker = MagicMock()
        self.worker.health.return_value = HEALTH

    def _start(self, command, **kwargs):
        self.master = command
        self.socket = Path(command[command.index("-S") + 1])
        self.socket.touch()
        return self.process

    def _run(self, command, **kwargs):
        self.commands.append(command)
        return subprocess.CompletedProcess(
            command,
            self.forward_returncode if "forward" in command else 0,
            b"",
            b"bind: Address already in use" if self.forward_returncode else b"",
        )

    @contextlib.contextmanager
    def _doubles(self):
        with (
            patch("description_pipeline.repository.tunnel.subprocess.Popen", side_effect=self._start),
            patch("description_pipeline.repository.tunnel.subprocess.run", side_effect=self._run),
            patch("description_pipeline.repository.tunnel.WorkerClient", return_value=self.worker),
        ):
            yield

    def test_forward_acknowledgement_precedes_http_and_child_is_reaped(self):
        def health():
            self.assertIn("forward", self.commands[-1])
            return HEALTH

        self.worker.health.side_effect = health
        with self._doubles(), worker_tunnel(self.root, SOURCE, "windows", 9876) as connection:
            self.assertEqual(connection["host"], "windows")
            self.assertEqual(self.socket.parent.stat().st_mode & 0o777, 0o700)
            self.assertLess(len(str(self.socket)), 104)
        self.assertFalse(self.socket.parent.exists())
        self.process.terminate.assert_called_once()
        self.process.wait.assert_called_once_with(timeout=5)
        self.assertIn("127.0.0.1:8765:127.0.0.1:9876", self.commands[-1])
        self.assertIn("StrictHostKeyChecking=yes", self.master)
        self.assertIn("BatchMode=yes", self.master)
        self.assertNotIn("-f", self.master)

    def test_occupied_port_never_probes_some_other_listener(self):
        self.forward_returncode = 1
        with self._doubles(), self.assertRaises(PipelineError) as caught, worker_tunnel(self.root, SOURCE, "windows"):
            self.fail("must not capture")
        self.worker.health.assert_not_called()
        self.process.terminate.assert_called_once()
        assert caught.exception.diagnostic_path is not None
        self.assertIn(b"Address already in use", (Path(caught.exception.diagnostic_path) / "ssh.log").read_bytes())
        self.assertIn("Address already in use", cli._failure_message(caught.exception))

    def test_capture_failure_keeps_its_original_diagnostic(self):
        diagnostic = self.root / "build/source-failed"
        diagnostic.mkdir(parents=True)
        failure = PipelineError("capture failed")
        failure.diagnostic_path = str(diagnostic)
        with self._doubles(), self.assertRaises(PipelineError) as caught, worker_tunnel(self.root, SOURCE, "windows"):
            raise failure
        self.assertIs(caught.exception, failure)
        self.assertEqual(caught.exception.diagnostic_path, str(diagnostic))
        self.assertTrue((diagnostic / "ssh.log").exists())
        self.process.terminate.assert_called_once()

    def test_sigterm_cleans_the_child_and_restores_signal_handler(self):
        previous = signal.getsignal(signal.SIGTERM)
        with self._doubles(), self.assertRaises(KeyboardInterrupt), worker_tunnel(self.root, SOURCE, "windows"):
            handler = signal.getsignal(signal.SIGTERM)
            assert callable(handler)
            handler(signal.SIGTERM, None)
        self.assertEqual(signal.getsignal(signal.SIGTERM), previous)
        self.process.terminate.assert_called_once()
        self.assertFalse(self.socket.parent.exists())

    def test_repeated_cancellation_during_cleanup_cannot_skip_kill(self):
        def wait(*, timeout):
            self.assertEqual(signal.getsignal(signal.SIGTERM), signal.SIG_IGN)
            self.assertEqual(signal.getsignal(signal.SIGINT), signal.SIG_IGN)
            if self.process.wait.call_count == 1:
                raise subprocess.TimeoutExpired("ssh", timeout)

        previous = signal.getsignal(signal.SIGINT)
        self.process.wait.side_effect = wait
        with self._doubles(), worker_tunnel(self.root, SOURCE, "windows"):
            pass
        self.process.kill.assert_called_once()
        self.assertEqual(self.process.wait.call_count, 2)
        self.assertEqual(signal.getsignal(signal.SIGINT), previous)

    def test_dead_ssh_fails_without_http_or_capture(self):
        self.process.poll.return_value = 255
        with (
            self._doubles(),
            self.assertRaisesRegex(PipelineError, "SSH connection failed"),
            worker_tunnel(self.root, SOURCE, "windows"),
        ):
            self.fail("must not capture")
        self.worker.health.assert_not_called()

    def test_busy_worker_fails_and_disconnects(self):
        self.worker.health.return_value = {**HEALTH, "cad_operation_active": True}
        with (
            self._doubles(),
            self.assertRaisesRegex(PipelineError, "busy"),
            worker_tunnel(self.root, SOURCE, "windows"),
        ):
            self.fail("must not capture")
        self.process.terminate.assert_called_once()

    def test_alias_url_and_port_are_validated_before_ssh(self):
        for source, host, port in (
            (SOURCE, "-oProxyCommand=anything", 8765),
            ({**SOURCE, "provider": "onshape"}, "windows", 8765),
            ({**SOURCE, "worker_url": "http://localhost:8765"}, "windows", 8765),
            ({**SOURCE, "worker_url": "http://127.0.0.1:8765/other"}, "windows", 8765),
            (SOURCE, "windows", 0),
            (SOURCE, "windows", 65536),
            (SOURCE, "windows", True),
        ):
            with self.subTest(source=source, host=host, port=port), self.assertRaises(PipelineError):
                _forward(source, host, port)

    def test_all_worker_busy_states_are_refused(self):
        for changed in (
            {"maintenance": True},
            {"cad_recovery_required": True},
            {"runner": {"alive": False}},
            {"runner": {"alive": True, "queued": 1}},
            {"jobs": {"running": 1}},
        ):
            with self.subTest(changed=changed), self.assertRaises(PipelineError):
                _idle({**HEALTH, **changed})


if __name__ == "__main__":
    unittest.main()
