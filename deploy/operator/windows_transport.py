#!/usr/bin/env python3
"""Run Windows PowerShell through a fixed EncodedCommand launcher and stdin payload."""

from __future__ import annotations

import argparse
import base64
import gzip
import hashlib
import json
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path


PRELUDE = (
    "$ProgressPreference='SilentlyContinue';"
    "$utf8=New-Object System.Text.UTF8Encoding($false);"
    "$OutputEncoding=$utf8;"
    "[Console]::OutputEncoding=$utf8;"
)

MAX_PAYLOAD_BYTES = 4 * 1024 * 1024
MAX_SCRIPT_BYTES = 4 * 1024 * 1024
DECODE_ERROR_EXIT = 2
METADATA_ERROR_EXIT = 3

# The launcher is intentionally constant. Product scripts and arguments must never enter argv.
LAUNCHER = (
    "$ErrorActionPreference='Stop';"
    "$stream=[Console]::OpenStandardInput();"
    f"$limit={MAX_PAYLOAD_BYTES};"
    "$buffer=New-Object System.Byte[] 8192;"
    "$memory=New-Object System.IO.MemoryStream;"
    "$complete=$false;"
    "$previousWasCR=$false;"
    "while($true){"
    "$count=0;"
    "try{$count=$stream.Read($buffer,0,$buffer.Length)}"
    "catch{if($complete){$count=0}else{throw}};"
    "if($count -le 0){break};"
    "if($complete){"
    "[Console]::Error.WriteLine('windows transport stdin payload has trailing bytes');exit 1};"
    "$chunk=[System.Text.Encoding]::ASCII.GetString($buffer,0,$count);"
    "$lineEnd=$chunk.IndexOf([char]10);"
    "if(($lineEnd -eq 0 -and $previousWasCR) -or "
    "($lineEnd -gt 0 -and $buffer[$lineEnd-1] -eq 13)){"
    "[Console]::Error.WriteLine('windows transport stdin payload uses forbidden CR framing');exit 1};"
    "if($lineEnd -ge 0 -and $lineEnd -lt ($count-1)){"
    "[Console]::Error.WriteLine('windows transport stdin payload has trailing bytes');exit 1};"
    "$take=$(if($lineEnd -ge 0){$lineEnd}else{$count});"
    "if(($memory.Length + $take) -gt $limit){"
    "[Console]::Error.WriteLine('windows transport stdin payload is too large');exit 1}"
    "if($take -gt 0){$memory.Write($buffer,0,$take)};"
    "if($lineEnd -ge 0){$complete=$true}"
    "elseif($count -gt 0){$previousWasCR=($buffer[$count-1] -eq 13)}"
    "};"
    "if($memory.Length -eq 0){"
    "[Console]::Error.WriteLine('windows transport stdin payload is missing');exit 1};"
    "if(-not $complete){"
    "[Console]::Error.WriteLine('windows transport stdin payload is not LF terminated');exit 1};"
    "$p=[System.Text.Encoding]::ASCII.GetString($memory.ToArray()).TrimEnd([char]13);"
    "$memory.Dispose();$stream.Dispose();"
    "try{"
    "$b=[Convert]::FromBase64String($p);"
    "$m=New-Object System.IO.MemoryStream(,$b);"
    "$g=New-Object System.IO.Compression.GZipStream($m,"
    "[System.IO.Compression.CompressionMode]::Decompress);"
    "$r=New-Object System.IO.StreamReader($g,[System.Text.Encoding]::UTF8);"
    "$s=$r.ReadToEnd();$r.Dispose();$g.Dispose();$m.Dispose();"
    f"if([System.Text.Encoding]::UTF8.GetByteCount($s) -gt {MAX_SCRIPT_BYTES}){{"
    "[Console]::Error.WriteLine('windows transport script is too large');exit 1};"
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
        "-ExecutionPolicy",
        "Bypass",
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
    transport = request_metadata(script)
    if len((PRELUDE + script).encode("utf-8")) > MAX_SCRIPT_BYTES:
        raise ValueError(f"Windows transport script exceeds {MAX_SCRIPT_BYTES} bytes")
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
    completed.transport = transport
    return completed


def request_metadata(script: str) -> dict[str, int | str]:
    script_bytes = (PRELUDE + script).encode("utf-8")
    payload = encode_script(script) + b"\n"
    return {
        "encoding": "base64(gzip(utf-8))",
        "framing": "single-line-lf",
        "script_sha256": hashlib.sha256(script_bytes).hexdigest(),
        "script_bytes": len(script_bytes),
        "payload_sha256": hashlib.sha256(payload).hexdigest(),
        "payload_bytes": len(payload),
        "launcher_sha256": hashlib.sha256(
            ENCODED_LAUNCHER.encode("ascii")
        ).hexdigest(),
        "launcher_bytes": len(ENCODED_LAUNCHER),
    }


class TransportDecodeError(ValueError):
    def __init__(self, stream: str, payload: bytes, detail: str) -> None:
        self.stream = stream
        self.payload = payload
        self.payload_sha256 = hashlib.sha256(payload).hexdigest()
        self.payload_bytes = len(payload)
        super().__init__(f"{stream} is not valid UTF-16: {detail}")


def decode_output(payload: bytes, *, stream: str = "output") -> str:
    if payload.startswith((b"\xff\xfe", b"\xfe\xff")):
        try:
            return payload.decode("utf-16")
        except UnicodeDecodeError as error:
            raise TransportDecodeError(stream, payload, str(error)) from error
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            return payload.decode(encoding)
        except UnicodeDecodeError:
            continue
    return payload.decode("utf-8", errors="replace")


def write_metadata(path: Path, document: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def completed_metadata(completed: subprocess.CompletedProcess[bytes]) -> dict:
    return {
        "schema_version": "solidworks-to-urdf.windows-transport/v1",
        "status": "completed",
        "returncode": completed.returncode,
        "stdout": raw_stream_metadata(completed.stdout),
        "stderr": raw_stream_metadata(completed.stderr),
        "transport": dict(getattr(completed, "transport", {})),
    }


def raw_stream_metadata(payload: bytes) -> dict[str, int | str]:
    return {
        "bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }


def error_streams(error: BaseException) -> tuple[bytes, bytes]:
    stdout = getattr(error, "stdout", b"") or b""
    stderr = getattr(error, "stderr", b"") or b""
    if isinstance(stdout, str):
        stdout = stdout.encode("utf-8", errors="replace")
    if isinstance(stderr, str):
        stderr = stderr.encode("utf-8", errors="replace")
    return stdout, stderr


def write_raw_stream(stream, payload: bytes) -> None:
    buffer = getattr(stream, "buffer", None)
    if buffer is not None:
        buffer.write(payload)
    else:
        stream.write(payload.decode("utf-8", errors="replace"))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", required=True)
    parser.add_argument("--timeout", type=int, default=60)
    parser.add_argument("--metadata-file")
    arguments = parser.parse_args(argv)
    script = sys.stdin.read()
    request = request_metadata(script)
    base_metadata = {
        "schema_version": "solidworks-to-urdf.windows-transport/v1",
        "host": arguments.host,
        "timeout_seconds": arguments.timeout,
        "returncode": None,
        "stdout": raw_stream_metadata(b""),
        "stderr": raw_stream_metadata(b""),
        "transport": request,
    }
    if not script:
        message = "windows transport script is empty"
        if arguments.metadata_file:
            metadata = {
                **base_metadata,
                "status": "error",
                "error_type": "EmptyInput",
                "error": message,
            }
            try:
                write_metadata(Path(arguments.metadata_file), metadata)
            except OSError as error:
                print(f"windows transport metadata failed: {error}", file=sys.stderr)
                return METADATA_ERROR_EXIT
        print(message, file=sys.stderr)
        return 1
    try:
        completed = run_powershell(
            ssh_host=arguments.host,
            script=script,
            timeout=arguments.timeout,
        )
    except (OSError, subprocess.SubprocessError, ValueError) as error:
        stdout, stderr = error_streams(error)
        if arguments.metadata_file:
            metadata = {
                **base_metadata,
                "status": "error",
                "error_type": type(error).__name__,
                "error": str(error),
                "stdout": raw_stream_metadata(stdout),
                "stderr": raw_stream_metadata(stderr),
            }
            try:
                write_metadata(Path(arguments.metadata_file), metadata)
            except OSError as metadata_error:
                print(f"windows transport metadata failed: {metadata_error}", file=sys.stderr)
        write_raw_stream(sys.stdout, stdout)
        write_raw_stream(sys.stderr, stderr)
        print(f"windows transport failed: {error}", file=sys.stderr)
        return 1
    metadata = completed_metadata(completed)
    metadata.update(
        {
            "host": arguments.host,
            "timeout_seconds": arguments.timeout,
        }
    )
    try:
        stdout = decode_output(completed.stdout, stream="stdout")
        stderr = decode_output(completed.stderr, stream="stderr")
    except TransportDecodeError as error:
        if arguments.metadata_file:
            metadata["status"] = "decode_error"
            metadata["error_type"] = type(error).__name__
            metadata["error"] = str(error)
            metadata["decode_error"] = {
                "stream": error.stream,
                "bytes": error.payload_bytes,
                "sha256": error.payload_sha256,
            }
            try:
                write_metadata(Path(arguments.metadata_file), metadata)
            except OSError as metadata_error:
                print(f"windows transport metadata failed: {metadata_error}", file=sys.stderr)
                return METADATA_ERROR_EXIT
        write_raw_stream(sys.stdout, completed.stdout)
        write_raw_stream(sys.stderr, completed.stderr)
        print(f"windows transport failed: {error}", file=sys.stderr)
        return DECODE_ERROR_EXIT
    if arguments.metadata_file:
        try:
            write_metadata(Path(arguments.metadata_file), metadata)
        except OSError as error:
            print(f"windows transport metadata failed: {error}", file=sys.stderr)
            return METADATA_ERROR_EXIT
    sys.stdout.write(stdout)
    sys.stderr.write(stderr)
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
