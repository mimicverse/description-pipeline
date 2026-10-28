#!/usr/bin/env python3
"""Verify the operator HTTPS endpoint with the configured origin and trust anchor."""

from __future__ import annotations

import argparse
import http.client
import json
import socket
import ssl
import sys
from pathlib import Path
from urllib.parse import urlsplit


class _LoopbackHTTPSConnection(http.client.HTTPSConnection):
    def __init__(
        self,
        host: str,
        port: int,
        connect_host: str,
        *,
        context: ssl.SSLContext,
        timeout: float,
    ) -> None:
        self._connect_host = connect_host
        super().__init__(host, port, context=context, timeout=timeout)
        self._create_connection = self._connect_loopback

    def _connect_loopback(self, address, timeout, source_address):
        return socket.create_connection(
            (self._connect_host, address[1]),
            timeout,
            source_address,
        )


def _parts(url: str) -> tuple[str, int, str]:
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise ValueError("URL must be an absolute HTTPS URL without credentials or fragment")
    port = parsed.port or 443
    if not 0 < port < 65536:
        raise ValueError("URL port is invalid")
    target = parsed.path or "/"
    if parsed.query:
        target += "?" + parsed.query
    return parsed.hostname, port, target


def probe(
    url: str,
    cafile: Path,
    *,
    connect_host: str = "127.0.0.1",
    timeout: float = 10,
) -> tuple[int, bytes]:
    host, port, target = _parts(url)
    cafile = Path(cafile)
    if not cafile.is_file() or cafile.is_symlink():
        raise ValueError(f"TLS trust anchor is missing or linked: {cafile}")
    context = ssl.create_default_context(cafile=str(cafile))
    context.verify_flags |= ssl.VERIFY_X509_PARTIAL_CHAIN
    connection = _LoopbackHTTPSConnection(
        host,
        port,
        connect_host,
        context=context,
        timeout=timeout,
    )
    try:
        connection.request("GET", target)
        response = connection.getresponse()
        return response.status, response.read(1024 * 1024)
    finally:
        connection.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument("--cafile", type=Path, required=True)
    parser.add_argument("--connect-host", default="127.0.0.1")
    parser.add_argument("--timeout", type=float, default=10)
    parser.add_argument("--mode", choices=("status", "feishu-health"), default="status")
    parser.add_argument("--expect-status", type=int)
    args = parser.parse_args(argv)
    try:
        status, payload = probe(
            args.url,
            args.cafile,
            connect_host=args.connect_host,
            timeout=args.timeout,
        )
    except (OSError, ValueError, http.client.HTTPException) as error:
        print(f"TLS probe failed: {error}")
        return 1
    if args.mode == "status":
        print(f"HTTP {status}")
        if args.expect_status is not None:
            return 0 if status == args.expect_status else 1
        return 0 if 200 <= status < 400 else 1
    try:
        document = json.loads(payload or b"{}")
    except ValueError:
        print(f"HTTP {status} non-JSON response")
        return 1
    if not isinstance(document, dict):
        print(f"HTTP {status} JSON response is not an object")
        return 1
    if status == 200 and document.get("configured") is True:
        print(f"HTTP {status} configured=true")
        return 0
    if status == 503 and document.get("configured") is False:
        print(f"HTTP {status} configured=false")
        return 2
    print(f"HTTP {status} " + json.dumps(document, sort_keys=True))
    return 1


if __name__ == "__main__":
    sys.exit(main())
