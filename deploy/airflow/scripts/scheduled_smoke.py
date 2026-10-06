"""Scheduled (not dags-test) smoke run against a neutral mocked endpoint.

Starts the Airflow DAG processor and scheduler, triggers one DAG run and waits for the run to
finish. Requires an installed environment; writes nothing outside AIRFLOW_HOME.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import signal
import subprocess
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

TOKEN = "scheduled-token"
SUBJECT = "a" * 64
COMMIT = "b" * 40
BOUND = ("run_id", "package", "revision_sha256", "target")


def _view(job: dict) -> dict:
    """The job document as served: everything but the test-only poke counter."""
    return {key: value for key, value in job.items() if key != "pokes"}


def _mock_endpoint() -> tuple[ThreadingHTTPServer, dict]:
    state = {"jobs": {}}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args) -> None:
            return

        def _send(self, code: int, payload: dict) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _auth(self) -> bool:
            if self.headers.get("Authorization") != f"Bearer {TOKEN}":
                self._send(401, {"error": "unauthorized"})
                return False
            return True

        def do_GET(self) -> None:
            if not self._auth():
                return
            if self.path == "/health":
                self._send(200, {"pipeline_id": "solidworks-to-urdf", "ready": True})
                return
            run_id = self.path.rsplit("/", 1)[-1]
            job = state["jobs"].get(run_id)
            if job is None:
                self._send(404, {"error": "unknown"})
                return
            job["pokes"] += 1
            if job["pokes"] >= 2:
                job["status"] = "passed"
                job["result"] = {
                    "passed": True, "pipeline_id": "solidworks-to-urdf", "output": "build/out",
                    "subject_sha256": SUBJECT, "repository_slug": "example/m3.0",
                    "quality": {"passed": True, "subject_sha256": SUBJECT, "checks": [{"id": "x"}]},
                    "submission": {"passed": True, "subject_sha256": SUBJECT, "base": "feature/m3.0",
                                   "branch": "work/solidworks/m3.0",
                                   "state": "published", "commit": COMMIT,
                                   "repository_slug": "example/m3.0",
                                   "url": "https://github.com/example/m3.0/pull/1"},
                }
            else:
                job["status"] = "running"
            job["events"].append({"stage": "job", "state": job["status"], "at": "t0"})
            self._send(200, _view(job))

        def do_POST(self) -> None:
            if not self._auth():
                return
            payload = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            run_id = payload["run_id"]
            if run_id in state["jobs"]:
                existing = state["jobs"][run_id]
                if any(existing[field] != payload.get(field) for field in BOUND):
                    self._send(409, {"error": "mismatch"})
                    return
                self._send(200, _view(existing))
                return
            job = {"schema_version": "solidworks-to-urdf.job/v1", "pipeline_id": "solidworks-to-urdf",
                   **{field: payload[field] for field in BOUND},
                   "repository_slug": "example/m3.0",
                   "repository_base": "feature/m3.0", "status": "queued",
                   "events": [{"stage": "submit", "state": "queued", "at": "t0"}],
                   "result": None, "error": None, "pokes": 0}
            state["jobs"][run_id] = job
            self._send(202, _view(job))

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, state


def _run(venv: Path, env: dict, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([str(venv / "bin/airflow"), *args], env=env, capture_output=True,
                          text=True, timeout=120)


def _json_tail(text: str) -> list:
    for line in reversed(text.splitlines()):
        line = line.strip()
        if not line.startswith("["):
            continue
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            continue
    return []


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--venv", type=Path, required=True)
    parser.add_argument("--airflow-home", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=180)
    args = parser.parse_args()
    server, state = _mock_endpoint()
    port = server.server_address[1]
    connection = json.dumps({"conn_type": "http", "host": "127.0.0.1", "port": port, "password": TOKEN})
    env = dict(os.environ, AIRFLOW_HOME=str(args.airflow_home), PYTHONPATH=str(args.root / "src"),
               AIRFLOW_CONN_SOLIDWORKS_WINDOWS=connection, SOLIDWORKS_SENSOR_MODE="poke",
               SOLIDWORKS_POLL_INTERVAL="1", SOLIDWORKS_TIMEOUT="120",
               AIRFLOW__CORE__EXECUTION_API_SERVER_URL="http://127.0.0.1:8791/execution")
    log_dir = args.airflow_home / "logs" / "smoke"
    log_dir.mkdir(parents=True, exist_ok=True)
    logs = {}
    procs = []
    stack = contextlib.ExitStack()
    components = (("dag-processor", []), ("scheduler", []), ("api-server", ["--port", "8791"]))
    for component, extra in components:
        # 句柄生命周期由 finally 里的 stack.close() 管理（进程要先拿到日志 fd 才能启动）。
        handle = stack.enter_context(
            open(log_dir / f"{component}.log", "w", encoding="utf-8")  # noqa: SIM115 - ExitStack 统一关闭
        )
        logs[component] = handle
        procs.append(subprocess.Popen([str(args.venv / "bin/airflow"), component, *extra], env=env,
                                      stdout=handle, stderr=subprocess.STDOUT, start_new_session=True))
    run_id = f"smoke-{uuid.uuid4().hex[:12]}"
    conf = json.dumps({"package": "handoff/m3.0", "revision_sha256": SUBJECT, "target": "local",
                       "repository_slug": "example/m3.0", "base": "feature/m3.0"})
    try:
        time.sleep(15)
        for (component, _), proc in zip(components, procs, strict=True):
            if proc.poll() is not None:
                tail = (log_dir / f"{component}.log").read_text(encoding="utf-8")[-600:]
                print(json.dumps({"ok": False, "stage": f"{component}_exit", "log": tail}))
                return 1
        unpause = _run(args.venv, env, "dags", "unpause", "solidworks_to_urdf")
        if unpause.returncode != 0:
            print(json.dumps({"ok": False, "stage": "unpause", "stderr": unpause.stderr[-500:]}))
            return 1
        trigger = _run(args.venv, env, "dags", "trigger", "solidworks_to_urdf", "-r", run_id, "-c", conf)
        if trigger.returncode != 0:
            print(json.dumps({"ok": False, "stage": "trigger", "stderr": trigger.stderr[-500:]}))
            return 1
        deadline = time.time() + args.timeout
        while time.time() < deadline:
            done = _run(args.venv, env, "dags", "list-runs", "solidworks_to_urdf", "-o", "json")
            if done.returncode == 0:
                runs = _json_tail(done.stdout or "")
                match = [r for r in runs if r.get("run_id") == run_id]
                if match and match[0].get("state") == "success":
                    job = next(iter(state["jobs"].values()), {})
                    print(json.dumps({"ok": job.get("status") == "passed", "run_id": run_id,
                                      "dag_state": "success", "job_status": job.get("status"),
                                      "submission": (job.get("result") or {}).get("submission")}))
                    return 0 if job.get("status") == "passed" else 1
                if match and match[0].get("state") == "failed":
                    print(json.dumps({"ok": False, "run_id": run_id, "dag_state": "failed"}))
                    return 1
            time.sleep(5)
        tails = {c: (log_dir / f"{c}.log").read_text(encoding="utf-8")[-400:] for c in logs}
        print(json.dumps({"ok": False, "stage": "timeout", "run_id": run_id, "logs": tails}))
        return 1
    finally:
        stack.close()
        for proc in procs:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        for proc in procs:
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=20)
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    raise SystemExit(main())
