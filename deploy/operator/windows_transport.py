#!/usr/bin/env python3
"""Run Windows PowerShell through a fixed EncodedCommand launcher and stdin payload."""

from __future__ import annotations

import argparse
import base64
import gzip
import hashlib
import subprocess
import sys
from collections.abc import Sequence


PRELUDE = (
    "$ProgressPreference='SilentlyContinue';"
    "$utf8=New-Object System.Text.UTF8Encoding($false);"
    "$OutputEncoding=$utf8;"
    "[Console]::OutputEncoding=$utf8;"
)

MAX_PAYLOAD_BYTES = 4 * 1024 * 1024

# The launcher is intentionally constant. Product scripts and arguments must never enter argv.
LAUNCHER = (
    "$ErrorActionPreference='Stop';"
    "$p=[Console]::In.ReadLine();"
    "if([string]::IsNullOrWhiteSpace($p)){"
    "[Console]::Error.WriteLine('windows transport stdin payload is missing');exit 1};"
    f"if($p.Length -gt {MAX_PAYLOAD_BYTES}){{"
    "[Console]::Error.WriteLine('windows transport stdin payload is too large');exit 1};"
    "try{"
    "$b=[Convert]::FromBase64String($p);"
    "$m=New-Object System.IO.MemoryStream(,$b);"
    "$g=New-Object System.IO.Compression.GZipStream($m,"
    "[System.IO.Compression.CompressionMode]::Decompress);"
    "$r=New-Object System.IO.StreamReader($g,[System.Text.Encoding]::UTF8);"
    "$s=$r.ReadToEnd();$r.Dispose();$g.Dispose();$m.Dispose();"
    "Invoke-Expression $s"
    "}catch{"
    "[Console]::Error.WriteLine('windows transport failed: '+$_.Exception.Message);exit 1"
    "};"
)
ENCODED_LAUNCHER = base64.b64encode(LAUNCHER.encode("utf-16le")).decode("ascii")


def encode_script(script: str) -> bytes:
    compressed = gzip.compress((PRELUDE + script).encode("utf-8"), mtime=0)
    return base64.b64encode(compressed)


def invocation(
    *,
    ssh_host: str,
    timeout: int,
    ssh_bin: str = "ssh",
) -> list[str]:
    return [
        ssh_bin,
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        f"ConnectTimeout={min(timeout, 30)}",
        ssh_host,
        "powershell.exe",
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
        "-EncodedCommand",
        ENCODED_LAUNCHER,
    ]


def run_powershell(
    *,
    ssh_host: str,
    script: str,
    timeout: int,
    ssh_bin: str = "ssh",
) -> subprocess.CompletedProcess[bytes]:
    argv = invocation(ssh_host=ssh_host, timeout=timeout, ssh_bin=ssh_bin)
    payload = encode_script(script) + b"\n"
    if len(payload) > MAX_PAYLOAD_BYTES:
        raise ValueError(f"Windows transport payload exceeds {MAX_PAYLOAD_BYTES} bytes")
    if payload.count(b"\n") != 1 or not payload.endswith(b"\n"):
        raise ValueError("Windows transport payload must be exactly one LF-terminated line")
    completed = subprocess.run(
        argv,
        input=payload,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    completed.transport = {
        "encoding": "base64(gzip(utf-8))",
        "framing": "single-line-lf",
        "script_sha256": hashlib.sha256((PRELUDE + script).encode("utf-8")).hexdigest(),
        "script_bytes": len((PRELUDE + script).encode("utf-8")),
        "payload_sha256": hashlib.sha256(payload).hexdigest(),
        "payload_bytes": len(payload),
        "launcher_sha256": hashlib.sha256(
            ENCODED_LAUNCHER.encode("ascii")
        ).hexdigest(),
        "launcher_bytes": len(ENCODED_LAUNCHER),
    }
    return completed


def decode_output(payload: bytes) -> str:
    if payload.startswith((b"\xff\xfe", b"\xfe\xff")):
        return payload.decode("utf-16")
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            return payload.decode(encoding)
        except UnicodeDecodeError:
            continue
    return payload.decode("utf-8", errors="replace")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", required=True)
    parser.add_argument("--timeout", type=int, default=60)
    arguments = parser.parse_args(argv)
    script = sys.stdin.read()
    if not script:
        raise SystemExit("windows transport script is empty")
    try:
        completed = run_powershell(
            ssh_host=arguments.host,
            script=script,
            timeout=arguments.timeout,
        )
    except (OSError, subprocess.SubprocessError, ValueError) as error:
        print(f"windows transport failed: {error}", file=sys.stderr)
        return 1
    sys.stdout.write(decode_output(completed.stdout))
    sys.stderr.write(decode_output(completed.stderr))
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
