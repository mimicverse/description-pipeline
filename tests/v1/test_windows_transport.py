"""Windows PowerShell transport tests."""

from __future__ import annotations

import base64
import contextlib
import gzip
import io
import json
import os
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
            launcher = base64.b64decode(arguments[-1]).decode("utf-16le")
            self.assertIn("[Console]::In.ReadLine()", launcher)
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
        self.assertIn("windows transport failed:", windows_transport.LAUNCHER)
        self.assertIn("exit 1", windows_transport.LAUNCHER)

    def test_payload_limit_uses_single_line_framing(self) -> None:
        self.assertEqual(windows_transport.MAX_PAYLOAD_BYTES, 4 * 1024 * 1024)
        payload = windows_transport.encode_script("Write-Output 'fixture'")
        self.assertNotIn(b"\n", payload)

    def test_payload_over_limit_fails_before_starting_ssh(self) -> None:
        script = base64.b64encode(os.urandom(3_200_000)).decode("ascii")
        with mock.patch.object(windows_transport.subprocess, "run") as run:
            with self.assertRaisesRegex(ValueError, "payload exceeds"):
                windows_transport.run_powershell(
                    ssh_host="windows-fixture",
                    script=script,
                    timeout=60,
                )
            run.assert_not_called()

    def test_cli_reports_transport_value_error_without_traceback(self) -> None:
        stderr = io.StringIO()
        with (
            mock.patch.object(windows_transport.sys, "stdin", io.StringIO("Write-Output 'fixture'")),
            mock.patch.object(
                windows_transport,
                "run_powershell",
                side_effect=ValueError(
                    f"Windows transport payload exceeds {windows_transport.MAX_PAYLOAD_BYTES} bytes"
                ),
            ),
            contextlib.redirect_stderr(stderr),
        ):
            result = windows_transport.main(["--host", "windows-fixture"])
        self.assertEqual(result, 1)
        self.assertIn("windows transport failed: Windows transport payload exceeds", stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())

    def test_output_decodes_utf16_bom_utf8_and_gb18030(self) -> None:
        text = "中文 transport"
        self.assertEqual(
            windows_transport.decode_output(b"\xff\xfe" + text.encode("utf-16le")),
            text,
        )
        self.assertEqual(
            windows_transport.decode_output(text.encode("utf-8")),
            text,
        )
        self.assertEqual(
            windows_transport.decode_output(text.encode("gb18030")),
            text,
        )


if __name__ == "__main__":
    unittest.main()
