"""Scheduled (not dags-test) smoke run against a neutral mocked endpoint.

Starts the Airflow DAG processor, scheduler and api-server with the current Feishu auth manager,
then triggers one DAG run the way the operator page does: through the portal's Airflow client with
a real authorized operator JWT.  Requires an installed environment; writes nothing outside
AIRFLOW_HOME.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib import error as urlerror
from urllib import request as urlrequest

TOKEN = "scheduled-token"
SUBJECT = "a" * 64
COMMIT = "b" * 40
#: One smoke enterprise app; the api-server and the minted operator token must agree on it.
FEISHU_APP_ID = "cli_smoke"
FEISHU_TENANT = "smoke-tenant"
FEISHU_OPERATOR = "ou_smoke"
FEISHU_SECRET = "smoke-app-secret"
JWT_SECRET = "scheduled-smoke-" + "0" * 48
#: The strict native resolution: the endpoint names only the package and its digest.
HANDOFF = {
    "schema_version": "solidworks-to-urdf.handoff/v1",
    "pipeline_id": "solidworks-to-urdf",
    "package": "handoff/m3.0",
    "handoff_sha256": "c" * 64,
}


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
                    "passed": True,
                    "pipeline_id": "solidworks-to-urdf",
                    "output": "build/out",
                    "subject_sha256": SUBJECT,
                    "quality": {"passed": True, "subject_sha256": SUBJECT, "checks": [{"id": "x"}]},
                    "submission": {
                        "passed": True,
                        "subject_sha256": SUBJECT,
                        "base": "feature/m3.0",
                        "branch": "work/solidworks/m3.0",
                        "state": "published",
                        "commit": COMMIT,
                        "repository_slug": "example/m3.0",
                        "url": "https://github.com/example/m3.0/pull/1",
                    },
                }
            else:
                job["status"] = "running"
            job["events"].append({"stage": "job", "state": job["status"], "at": "t0"})
            self._send(200, _view(job))

        def do_POST(self) -> None:
            if not self._auth():
                return
            payload = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            if self.path == "/v1/handoffs/resolve":
                state["resolved_paths"].append(payload.get("handoff_path"))
                self._send(200, HANDOFF)
                return
            run_id = payload["run_id"]
            if run_id in state["jobs"]:
                existing = state["jobs"][run_id]
                if existing["request"] != payload:
                    self._send(409, {"error": "mismatch"})
                    return
                self._send(200, _view(existing))
                return
            job = {
                "schema_version": "solidworks-to-urdf.job/v1",
                "pipeline_id": "solidworks-to-urdf",
                "run_id": run_id,
                # Routing resolved inside the serialized Windows job after CAD discovery.
                "hardware_id": "m3.0",
                "revision": "r1",
                "repository_slug": "example/m3.0",
                "repository_base": "feature/m3.0",
                "status": "queued",
                "events": [{"stage": "submit", "state": "queued", "at": "t0"}],
                "result": None,
                "error": None,
                "request": dict(payload),
                "pokes": 0,
            }
            state["jobs"][run_id] = job
            self._send(202, _view(job))

    state["resolved_paths"] = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, state


def _run(venv: Path, env: dict, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([str(venv / "bin/airflow"), *args], env=env, capture_output=True, text=True, timeout=120)


def _free_port() -> int:
    """A free loopback port, so an isolated smoke never collides with a running deployment."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _auth_health(port: int) -> dict:
    """The current auth manager's public health contract, as the deployment probe reads it."""
    url = f"http://127.0.0.1:{port}/auth/feishu/health"
    try:
        with urlrequest.urlopen(url, timeout=10) as response:
            payload = json.loads(response.read() or b"{}")
            return {"status": response.status, "configured": payload.get("configured")}
    except urlerror.HTTPError as error:
        with contextlib.suppress(ValueError):
            payload = json.loads(error.read() or b"{}")
            return {"status": error.code, "configured": payload.get("configured")}
        return {"status": error.code}
    except (urlerror.URLError, TimeoutError, OSError) as error:
        return {"status": None, "error": str(error)}


def _feishu_env(home: Path) -> dict[str, str]:
    """One throwaway 0600 enterprise app, so the smoke exercises the real Feishu manager."""
    secret = home / "feishu.json"
    secret.write_text(json.dumps({"app_id": FEISHU_APP_ID, "app_secret": FEISHU_SECRET}), encoding="utf-8")
    os.chmod(secret, 0o600)
    return {
        "FEISHU_APP_SECRET_FILE": str(secret),
        "FEISHU_TENANT_KEYS": FEISHU_TENANT,
        "FEISHU_REDIRECT_URI": "https://127.0.0.1:8443/auth/feishu/callback",
    }


def _operator_jwt(venv: Path, env: dict[str, str]) -> str:
    """The token /auth/feishu/callback would mint for one approved-tenant operator."""
    code = (
        "from description_pipeline.orchestration import feishu_auth as a;"
        f"u=a.FeishuUser(app_id='{FEISHU_APP_ID}',open_id='{FEISHU_OPERATOR}',name='smoke',"
        f"avatar_url='',tenant_key='{FEISHU_TENANT}');"
        "print(a.FeishuAuthManager().generate_jwt(u))"
    )
    minted = subprocess.run(
        [str(venv / "bin/python"), "-c", code], env=env, capture_output=True, text=True, timeout=120
    )
    if minted.returncode != 0:
        raise RuntimeError(f"cannot mint the smoke operator token: {minted.stderr[-500:]}")
    return minted.stdout.strip().splitlines()[-1]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--venv", type=Path, required=True)
    parser.add_argument("--airflow-home", type=Path, required=True)
    parser.add_argument("--api-port", type=int, default=0, help="loopback api-server port (0 picks a free one)")
    parser.add_argument("--timeout", type=float, default=180)
    args = parser.parse_args()
    api_port = args.api_port or _free_port()
    server, state = _mock_endpoint()
    port = server.server_address[1]
    connection = json.dumps({"conn_type": "http", "host": "127.0.0.1", "port": port, "password": TOKEN})
    env = dict(
        os.environ,
        AIRFLOW_HOME=str(args.airflow_home),
        PYTHONPATH=str(args.root / "src"),
        AIRFLOW_CONN_SOLIDWORKS_WINDOWS=connection,
        AIRFLOW__CORE__AUTH_MANAGER="description_pipeline.orchestration.feishu_auth.FeishuAuthManager",
        AIRFLOW__API_AUTH__JWT_SECRET=JWT_SECRET,
        **_feishu_env(args.airflow_home),
        SOLIDWORKS_SENSOR_MODE="poke",
        SOLIDWORKS_POLL_INTERVAL="1",
        SOLIDWORKS_TIMEOUT="120",
        # An isolated home has no airflow.cfg pointing at the repository DAG folder.
        AIRFLOW__CORE__DAGS_FOLDER=str(args.root / "deploy/airflow/dags"),
        AIRFLOW__CORE__EXECUTION_API_SERVER_URL=f"http://127.0.0.1:{api_port}/execution",
    )
    log_dir = args.airflow_home / "logs" / "smoke"
    log_dir.mkdir(parents=True, exist_ok=True)
    logs = {}
    procs = []
    stack = contextlib.ExitStack()
    components = (
        ("dag-processor", []),
        ("scheduler", []),
        ("api-server", ["--host", "127.0.0.1", "--port", str(api_port)]),
    )
    for component, extra in components:
        # Handles live in an ExitStack closed below in ``finally``; the child process needs the fd first.
        handle = stack.enter_context(
            open(log_dir / f"{component}.log", "w", encoding="utf-8")  # noqa: SIM115 - closed via ExitStack
        )
        logs[component] = handle
        procs.append(
            subprocess.Popen(
                [str(args.venv / "bin/airflow"), component, *extra],
                env=env,
                stdout=handle,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        )
    run_id = f"smoke-{uuid.uuid4().hex[:12]}"
    try:
        time.sleep(15)
        for (component, _), proc in zip(components, procs, strict=True):
            if proc.poll() is not None:
                tail = (log_dir / f"{component}.log").read_text(encoding="utf-8")[-600:]
                print(json.dumps({"ok": False, "stage": f"{component}_exit", "log": tail}))
                return 1
        auth_health = _auth_health(api_port)
        if auth_health.get("status") is None:
            print(json.dumps({"ok": False, "stage": "auth_health", "auth_health": auth_health}))
            return 1
        unpause = _run(args.venv, env, "dags", "unpause", "solidworks_to_urdf")
        if unpause.returncode != 0:
            print(json.dumps({"ok": False, "stage": "unpause", "stderr": unpause.stderr[-500:]}))
            return 1
        # Trigger exactly as the operator page does: the portal's client, a real operator JWT and
        # the pinned strict request model.  A CLI trigger could not catch a body regression.
        sys.path.insert(0, str(args.root / "src"))
        from description_pipeline.orchestration.portal import AirflowApi  # noqa: PLC0415

        airflow = AirflowApi(f"http://127.0.0.1:{api_port}")
        token = _operator_jwt(args.venv, env)
        try:
            airflow.trigger_dag_run(token, "solidworks_to_urdf", run_id, {"handoff_path": "handoff/m3.0"})
        except Exception as error:  # noqa: BLE001 - the smoke reports the exact stage
            print(json.dumps({"ok": False, "stage": "portal_trigger", "error": str(error)}))
            return 1
        deadline = time.time() + args.timeout
        triggering_user_name = None
        while time.time() < deadline:
            try:
                run = airflow.dag_run(token, "solidworks_to_urdf", run_id)
            except Exception:  # noqa: BLE001 - the run may not be visible for a moment
                time.sleep(5)
                continue
            triggering_user_name = run.get("triggering_user_name")
            if run.get("state") == "success":
                job = next(iter(state["jobs"].values()), {})
                request = job.get("request") or {}
                print(
                    json.dumps(
                        {
                            "ok": job.get("status") == "passed",
                            "run_id": run_id,
                            "api_port": api_port,
                            "auth_health": auth_health,
                            "trigger": "portal-airflow-client",
                            "triggering_user_name": triggering_user_name,
                            "dag_state": "success",
                            "job_status": job.get("status"),
                            "resolved_paths": state["resolved_paths"],
                            "handoff_sha256": request.get("handoff_sha256"),
                            "submission": (job.get("result") or {}).get("submission"),
                        }
                    )
                )
                return 0 if job.get("status") == "passed" else 1
            if run.get("state") == "failed":
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
