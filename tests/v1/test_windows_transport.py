"""Windows PowerShell transport tests."""

from __future__ import annotations

import base64
import contextlib
import gzip
import hashlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from deploy.operator import windows_transport


class WindowsTransportTests(unittest.TestCase):
    def test_long_unicode_script_never_enters_argv(self) -> None:
        script = "$value='中文\\nline-two';\n" + ("Write-Output $value;\n" * 10_000)
        argv = windows_transport.invocation(
            ssh_host="windows-fixture",
            timeout=60,
        )
        self.assertLess(sum(len(item) for item in argv), 8_192)
        self.assertNotIn(script, argv)
        self.assertNotIn("\n", argv[-1])
        encoded = windows_transport.encode_script(script)
        self.assertNotIn(b"\n", encoded)
        restored = gzip.decompress(base64.b64decode(encoded)).decode("utf-8")
        self.assertIn(script, restored)

    def test_run_powershell_sends_only_fixed_launcher_and_stdin_payload(self) -> None:
        script = "$value='中文\\nline-two';\n" + ("Write-Output $value;\n" * 20_000)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            capture = root / "capture.json"
            payload_file = root / "payload.b64"
            fake_ssh = root / "ssh"
            fake_ssh.write_text(
                "#!/usr/bin/env python3\n"
                "import json,sys\n"
                "from pathlib import Path\n"
                f"payload=sys.stdin.buffer.read()\n"
                f"Path({str(payload_file)!r}).write_bytes(payload)\n"
                f"Path({str(capture)!r}).write_text(json.dumps(sys.argv[1:]))\n"
                "sys.stdout.buffer.write('stdout-ok'.encode())\n"
                "sys.stderr.buffer.write('stderr-ok'.encode())\n",
                encoding="utf-8",
            )
            fake_ssh.chmod(0o700)
            completed = windows_transport.run_powershell(
                ssh_host="windows-fixture",
                script=script,
                timeout=60,
                ssh_bin=str(fake_ssh),
            )
            self.assertEqual(completed.returncode, 0)
            self.assertEqual(windows_transport.decode_output(completed.stdout), "stdout-ok")
            self.assertEqual(windows_transport.decode_output(completed.stderr), "stderr-ok")
            self.assertEqual(completed.transport["encoding"], "base64(gzip(utf-8))")
            self.assertEqual(completed.transport["framing"], "single-line-lf")
            self.assertGreater(completed.transport["script_bytes"], 32_767)
            self.assertGreater(completed.transport["payload_bytes"], 0)
            self.assertEqual(len(completed.transport["script_sha256"]), 64)
            self.assertEqual(len(completed.transport["payload_sha256"]), 64)
            self.assertGreater(completed.transport["launcher_bytes"], 0)
            arguments = json.loads(capture.read_text(encoding="utf-8"))
            self.assertEqual(
                arguments[arguments.index("-ExecutionPolicy") + 1],
                "Bypass",
            )
            launcher = base64.b64decode(arguments[-1]).decode("utf-16le")
            self.assertIn("System.Byte[] 8192", launcher)
            self.assertIn(".Read($buffer,0,$buffer.Length)", launcher)
            self.assertIn("$chunk.IndexOf([char]10)", launcher)
            self.assertIn("forbidden CR framing", launcher)
            self.assertIn("previousWasCR", launcher)
            self.assertIn("payload has trailing bytes", launcher)
            self.assertIn("script is too large", launcher)
            self.assertNotIn("ReadLine()", launcher)
            self.assertNotIn("StringBuilder", launcher)
            self.assertIn("stdin payload is too large", launcher)
            self.assertNotIn("FromBase64String('", launcher)
            self.assertLess(sum(len(item) for item in arguments), 8_192)
            encoded_payload = payload_file.read_bytes()
            self.assertTrue(encoded_payload.endswith(b"\n"))
            self.assertEqual(encoded_payload.count(b"\n"), 1)
            restored = gzip.decompress(
                base64.b64decode(encoded_payload)
            ).decode("utf-8")
            self.assertIn(script, restored)

    def test_nonzero_remote_exit_and_streams_are_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fake_ssh = Path(temporary) / "ssh"
            fake_ssh.write_text(
                "#!/usr/bin/env python3\n"
                "import sys\n"
                "sys.stdin.buffer.read()\n"
                "sys.stdout.buffer.write(b'partial-out')\n"
                "sys.stderr.buffer.write(b'transport-error')\n"
                "raise SystemExit(7)\n",
                encoding="utf-8",
            )
            fake_ssh.chmod(0o700)
            completed = windows_transport.run_powershell(
                ssh_host="windows-fixture",
                script="Write-Output 'never'",
                timeout=60,
                ssh_bin=str(fake_ssh),
            )
            self.assertEqual(completed.returncode, 7)
            self.assertEqual(windows_transport.decode_output(completed.stdout), "partial-out")
            self.assertEqual(windows_transport.decode_output(completed.stderr), "transport-error")
            self.assertEqual(len(completed.transport["payload_sha256"]), 64)

    def test_launcher_rejects_missing_or_truncated_payload(self) -> None:
        self.assertIn("windows transport stdin payload is missing", windows_transport.LAUNCHER)
        self.assertIn("windows transport stdin payload is too large", windows_transport.LAUNCHER)
        self.assertIn("windows transport stdin payload is not LF terminated", windows_transport.LAUNCHER)
        self.assertIn("windows transport stdin payload uses forbidden CR framing", windows_transport.LAUNCHER)
        self.assertIn("windows transport stdin payload has trailing bytes", windows_transport.LAUNCHER)
        self.assertIn("windows transport script is too large", windows_transport.LAUNCHER)
        self.assertIn("windows transport failed:", windows_transport.LAUNCHER)
        self.assertIn("exit 1", windows_transport.LAUNCHER)

    def test_payload_limit_uses_single_line_framing(self) -> None:
        self.assertEqual(windows_transport.MAX_PAYLOAD_BYTES, 4 * 1024 * 1024)
        payload = windows_transport.encode_script("Write-Output 'fixture'")
        self.assertNotIn(b"\n", payload)

    def test_payload_over_limit_fails_before_starting_ssh(self) -> None:
        script = base64.b64encode(os.urandom(3_120_000)).decode("ascii")
        with mock.patch.object(windows_transport.subprocess, "run") as run:
            with self.assertRaisesRegex(ValueError, "payload exceeds"):
                windows_transport.run_powershell(
                    ssh_host="windows-fixture",
                    script=script,
                    timeout=60,
                )
            run.assert_not_called()

    def test_decompressed_script_over_limit_fails_before_starting_ssh(self) -> None:
        script = "A" * (windows_transport.MAX_SCRIPT_BYTES + 1)
        with mock.patch.object(windows_transport.subprocess, "run") as run:
            with self.assertRaisesRegex(ValueError, "script exceeds"):
                windows_transport.run_powershell(
                    ssh_host="windows-fixture",
                    script=script,
                    timeout=60,
                )
            run.assert_not_called()

    def test_cli_reports_transport_value_error_without_traceback(self) -> None:
        stderr = io.StringIO()
        with tempfile.TemporaryDirectory() as temporary:
            metadata_path = Path(temporary) / "transport.json"
            with (
                mock.patch.object(
                    windows_transport.sys,
                    "stdin",
                    io.StringIO("Write-Output 'fixture'"),
                ),
                mock.patch.object(
                    windows_transport,
                    "run_powershell",
                    side_effect=ValueError(
                        f"Windows transport payload exceeds {windows_transport.MAX_PAYLOAD_BYTES} bytes"
                    ),
                ),
                contextlib.redirect_stderr(stderr),
            ):
                result = windows_transport.main(
                    [
                        "--host",
                        "windows-fixture",
                        "--metadata-file",
                        str(metadata_path),
                    ]
                )
            document = json.loads(metadata_path.read_text(encoding="utf-8"))
            self.assertEqual(result, 1)
            self.assertIn("windows transport failed: Windows transport payload exceeds", stderr.getvalue())
            self.assertNotIn("Traceback", stderr.getvalue())
            self.assertIsNone(document["returncode"])
            self.assertEqual(document["stdout"]["bytes"], 0)
            self.assertEqual(document["stderr"]["bytes"], 0)
            for key in ("script_sha256", "payload_sha256", "launcher_sha256"):
                self.assertEqual(len(document["transport"][key]), 64)

    def test_cli_empty_stdin_writes_stable_error_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            metadata_path = Path(temporary) / "transport.json"
            stderr = io.StringIO()
            with (
                mock.patch.object(windows_transport.sys, "stdin", io.StringIO("")),
                mock.patch.object(windows_transport.subprocess, "run") as run,
                contextlib.redirect_stderr(stderr),
            ):
                result = windows_transport.main(
                    [
                        "--host",
                        "windows-fixture",
                        "--metadata-file",
                        str(metadata_path),
                    ]
                )
            document = json.loads(metadata_path.read_text(encoding="utf-8"))
            self.assertEqual(result, 1)
            run.assert_not_called()
            self.assertEqual(document["status"], "error")
            self.assertEqual(document["error_type"], "EmptyInput")
            self.assertIsNone(document["returncode"])
            self.assertEqual(document["stdout"]["bytes"], 0)
            self.assertEqual(
                document["stdout"]["sha256"],
                hashlib.sha256(b"").hexdigest(),
            )
            self.assertIn("script is empty", stderr.getvalue())

    def test_cli_writes_hash_bound_metadata_sidecar(self) -> None:
        completed = subprocess.CompletedProcess(
            ["ssh", "windows-fixture"],
            7,
            b"partial-out",
            b"transport-error",
        )
        completed.transport = {
            "encoding": "base64(gzip(utf-8))",
            "framing": "single-line-lf",
            "script_sha256": "a" * 64,
            "payload_sha256": "b" * 64,
        }
        with tempfile.TemporaryDirectory() as temporary:
            metadata = Path(temporary) / "transport.json"
            stdout = io.StringIO()
            stderr = io.StringIO()
            with (
                mock.patch.object(windows_transport.sys, "stdin", io.StringIO("Write-Output 'fixture'")),
                mock.patch.object(windows_transport, "run_powershell", return_value=completed),
                contextlib.redirect_stdout(stdout),
                contextlib.redirect_stderr(stderr),
            ):
                result = windows_transport.main(
                    ["--host", "windows-fixture", "--metadata-file", str(metadata)]
                )
            document = json.loads(metadata.read_text(encoding="utf-8"))
            self.assertEqual(result, 7)
            self.assertEqual(
                document["schema_version"],
                "solidworks-to-urdf.windows-transport/v1",
            )
            self.assertEqual(stdout.getvalue(), "partial-out")
            self.assertEqual(stderr.getvalue(), "transport-error")
            self.assertEqual(document["status"], "completed")
            self.assertEqual(document["returncode"], 7)
            self.assertEqual(document["timeout_seconds"], 60)
            self.assertEqual(
                document["stdout"]["sha256"],
                hashlib.sha256(b"partial-out").hexdigest(),
            )
            self.assertEqual(
                document["stderr"]["sha256"],
                hashlib.sha256(b"transport-error").hexdigest(),
            )
            self.assertEqual(document["transport"]["framing"], "single-line-lf")

    def test_output_decodes_utf16_bom_utf8_and_gb18030(self) -> None:
        text = "中文 transport"
        self.assertEqual(
            windows_transport.decode_output(b"\xff\xfe" + text.encode("utf-16le")),
            text,
        )
        truncated = b"\xff\xfe" + text.encode("utf-16le")[:-1]
        with self.assertRaises(windows_transport.TransportDecodeError) as raised:
            windows_transport.decode_output(truncated, stream="stdout")
        self.assertEqual(raised.exception.stream, "stdout")
        self.assertEqual(raised.exception.payload_bytes, len(truncated))
        self.assertEqual(
            raised.exception.payload_sha256,
            hashlib.sha256(truncated).hexdigest(),
        )
        self.assertEqual(
            windows_transport.decode_output(text.encode("utf-8")),
            text,
        )
        self.assertEqual(
            windows_transport.decode_output(text.encode("gb18030")),
            text,
        )

    def test_cli_rejects_truncated_utf16_stdout_and_stderr_with_metadata(self) -> None:
        truncated = b"\xff\xfe\x41"
        for stream in ("stdout", "stderr"):
            with self.subTest(stream=stream), tempfile.TemporaryDirectory() as temporary:
                completed = subprocess.CompletedProcess(
                    ["ssh", "windows-fixture"],
                    0,
                    truncated if stream == "stdout" else b"valid",
                    truncated if stream == "stderr" else b"valid",
                )
                completed.transport = windows_transport.request_metadata("Write-Output 'fixture'")
                metadata_path = Path(temporary) / "transport.json"
                stdout_output = io.StringIO()
                stderr_output = io.StringIO()
                with (
                    mock.patch.object(
                        windows_transport.sys,
                        "stdin",
                        io.StringIO("Write-Output 'fixture'"),
                    ),
                    mock.patch.object(windows_transport, "run_powershell", return_value=completed),
                    contextlib.redirect_stdout(stdout_output),
                    contextlib.redirect_stderr(stderr_output),
                ):
                    result = windows_transport.main(
                        [
                            "--host",
                            "windows-fixture",
                            "--metadata-file",
                            str(metadata_path),
                        ]
                    )
                document = json.loads(metadata_path.read_text(encoding="utf-8"))
                self.assertEqual(result, windows_transport.DECODE_ERROR_EXIT)
                self.assertEqual(document["status"], "decode_error")
                self.assertEqual(document["decode_error"]["stream"], stream)
                self.assertEqual(
                    document["decode_error"]["sha256"],
                    hashlib.sha256(truncated).hexdigest(),
                )
                if stream == "stdout":
                    self.assertIn("�", stdout_output.getvalue())
                    self.assertIn("valid", stderr_output.getvalue())
                else:
                    self.assertEqual(stdout_output.getvalue(), "valid")
                    self.assertIn("�", stderr_output.getvalue())
                self.assertIn("not valid UTF-16", stderr_output.getvalue())

    def test_cli_timeout_metadata_keeps_request_and_partial_output_hashes(self) -> None:
        timeout = subprocess.TimeoutExpired(
            ["ssh", "windows-fixture"],
            1,
            output=b"partial-out",
            stderr=b"partial-error",
        )
        with tempfile.TemporaryDirectory() as temporary:
            metadata_path = Path(temporary) / "transport.json"
            stdout = io.StringIO()
            stderr = io.StringIO()
            with (
                mock.patch.object(
                    windows_transport.sys,
                    "stdin",
                    io.StringIO("Write-Output 'fixture'"),
                ),
                mock.patch.object(windows_transport, "run_powershell", side_effect=timeout),
                contextlib.redirect_stdout(stdout),
                contextlib.redirect_stderr(stderr),
            ):
                result = windows_transport.main(
                    [
                        "--host",
                        "windows-fixture",
                        "--metadata-file",
                        str(metadata_path),
                    ]
                )
            document = json.loads(metadata_path.read_text(encoding="utf-8"))
            self.assertEqual(result, 1)
            self.assertEqual(document["status"], "error")
            self.assertEqual(document["error_type"], "TimeoutExpired")
            self.assertEqual(stdout.getvalue(), "partial-out")
            self.assertIn("partial-error", stderr.getvalue())
            self.assertEqual(
                document["stdout"]["sha256"],
                hashlib.sha256(b"partial-out").hexdigest(),
            )
            self.assertEqual(
                document["stderr"]["sha256"],
                hashlib.sha256(b"partial-error").hexdigest(),
            )
            for key in ("script_sha256", "payload_sha256", "launcher_sha256"):
                self.assertEqual(len(document["transport"][key]), 64)


if __name__ == "__main__":
    unittest.main()
