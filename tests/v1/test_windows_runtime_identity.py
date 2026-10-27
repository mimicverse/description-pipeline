"""The Windows runtime consumes the install identity created by the lifecycle."""

from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from description_pipeline.io import PipelineError
from description_pipeline.orchestration import windows


class WindowsRuntimeIdentityTests(unittest.TestCase):
    def test_lifecycle_install_id_is_read_exactly(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            expected = "win-" + "a" * 32
            (root / "install-id").write_text(expected + "\n", encoding="utf-8")
            self.assertEqual(windows.installed_install_id(root), expected)

    def test_missing_or_malformed_install_id_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaises(PipelineError):
                windows.installed_install_id(root)
            (root / "install-id").write_text("generated-later", encoding="utf-8")
            with self.assertRaises(PipelineError):
                windows.installed_install_id(root)

    def test_build_runtime_passes_the_same_install_id(self) -> None:
        config = {
            "package_root": Path("C:/URDF/packages"),
            "output_root": Path("C:/URDF/results"),
            "state_root": Path("C:/URDF/state"),
            "host": "127.0.0.1",
            "port": 8765,
            "config_digest": "b" * 64,
        }
        with (
            mock.patch.object(
                windows,
                "_cad_ownership_file",
                return_value=Path("C:/URDF/state/cad-ownership.json"),
            ),
            mock.patch.object(windows, "_native_tool_record", return_value={"role": "native"}),
            mock.patch.object(windows, "NativeOperationRuntime") as runtime,
        ):
            windows.build_runtime(config, install_id="win-" + "c" * 32)
        self.assertEqual(runtime.call_args.kwargs["install_id"], "win-" + "c" * 32)

    def test_serve_binds_the_lifecycle_identity_before_startup(self) -> None:
        config = {
            "package_root": Path("C:/URDF/packages"),
            "output_root": Path("C:/URDF/results"),
            "state_root": Path("C:/URDF/state"),
            "host": "127.0.0.1",
            "port": 8765,
            "token": "t" * 64,
        }
        runtime = mock.Mock()
        server = mock.Mock()
        server.serve_forever.side_effect = KeyboardInterrupt
        with (
            mock.patch.object(windows.sys, "platform", "win32"),
            mock.patch.object(windows, "read_config", return_value=config),
            mock.patch.object(
                windows,
                "installed_install_id",
                return_value="win-" + "d" * 32,
            ),
            mock.patch.object(windows, "build_runtime", return_value=runtime) as build,
            mock.patch.object(windows, "handler", return_value=object()),
            mock.patch.object(windows, "ThreadingHTTPServer", return_value=server),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            windows.serve(Path("C:/URDF/state/endpoint.json"))
        build.assert_called_once_with(config, install_id="win-" + "d" * 32)
        runtime.close.assert_called_once_with()
        server.server_close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
