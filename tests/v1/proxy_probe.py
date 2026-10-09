"""Child-process probe: internal openers must use direct connections (test support).

Started under a hostile environment where HTTP(S)_PROXY points at a local fake proxy that
answers 502 and counts hits. A direct connection returns 200 with zero proxy hits; the
module openers are imported AFTER the environment is set, so preserving ambient proxies
(the pre-fix behaviour) routes the request into the fake proxy and fails this probe.
"""

from __future__ import annotations

import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib import request as urlrequest


def main(argv: list[str]) -> int:
    target = argv[1] if len(argv) > 1 else "airflow_client"

    class ProxyHandler(BaseHTTPRequestHandler):
        hits = 0

        def log_message(self, *args) -> None:
            return

        def _fail(self) -> None:
            type(self).hits += 1
            body = b'{"error": "fake proxy"}'
            self.send_response(502)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            self._fail()

        def do_POST(self) -> None:
            self._fail()

    proxy = ThreadingHTTPServer(("127.0.0.1", 0), ProxyHandler)
    threading.Thread(target=proxy.serve_forever, daemon=True).start()
    proxy_url = f"http://127.0.0.1:{proxy.server_address[1]}"
    os.environ["HTTP_PROXY"] = os.environ["http_proxy"] = proxy_url
    os.environ["HTTPS_PROXY"] = os.environ["https_proxy"] = proxy_url
    os.environ["NO_PROXY"] = os.environ["no_proxy"] = ""

    class Target(BaseHTTPRequestHandler):
        def log_message(self, *args) -> None:
            return

        def do_GET(self) -> None:
            body = json.dumps({"ok": True}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Target)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    if target == "airflow_client":
        from description_pipeline.orchestration import airflow_client as module

        def perform(request):
            return module._opener(request, timeout=5)
    else:
        from description_pipeline.orchestration import portal as module

        def perform(request):
            return module._AIRFLOW_OPENER.open(request, timeout=5)

    url = f"http://127.0.0.1:{server.server_address[1]}/probe"
    request = urlrequest.Request(url, headers={"Authorization": "Bearer probe-token"})
    try:
        with perform(request) as response:
            status = response.status
            response.read()
    except Exception as error:  # noqa: BLE001 - probe reports any failure
        print(f"opener-failed {type(error).__name__}: {error}")
        return 1
    print(f"status {status} proxy_hits {ProxyHandler.hits}")
    return 0 if status == 200 and ProxyHandler.hits == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
