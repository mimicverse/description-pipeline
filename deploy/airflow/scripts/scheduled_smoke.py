"""Scheduled (not dags-test) split-boundary smoke run against a neutral mocked endpoint.

Starts the Airflow DAG processor, scheduler and api-server with the current Feishu auth manager,
then triggers one DAG run the way the operator page does: through the portal's Airflow client with
a real authorized operator JWT.  Requires an installed environment; writes nothing outside
AIRFLOW_HOME.

The mocked Windows delivery is the sealed control fixture: the real freeze/discover/capture
producer runs on the fixture CAD backend and the mocked endpoint serves that exact transfer at the
``native_complete`` boundary.  The Linux half then executes the real generate stage and the real
verify stage, which must reject the control fixture on its documented limits while publication
never executes.  This is an expected rejection proof of the scheduled transport, never native CAD
qualification, and the report states that class explicitly.

When the venv is wired to the checkout by PYTHONPATH alone its metadata carries no
``airflow.plugins`` entry point, so the isolated home receives a plugin-folder shim for the same
class; the middleware itself is proven by the auth health contract.
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
from urllib import parse as urlparse
from urllib import request as urlrequest

TOKEN = "scheduled-token"
#: One smoke enterprise app; the api-server and the minted operator token must agree on it.
FEISHU_APP_ID = "cli_smoke"
FEISHU_TENANT = "smoke-tenant"
FEISHU_OPERATOR = "ou_smoke"
FEISHU_SECRET = "smoke-app-secret"
JWT_SECRET = "scheduled-smoke-" + "0" * 48
DAG_ID = "solidworks_to_urdf"
PIPELINE_ID = "solidworks-to-urdf"
#: The strict native resolution: the endpoint names only the managed package and its digest.
RESOLVED_PACKAGE = "handoff/m3.0"
HANDOFF_PATH = "handoff/m3.0"
REPOSITORY_SLUG = "example/m3.0"
REPOSITORY_BASE = "feature/m3.0"


def _read_json(path: Path) -> dict:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _view(job: dict) -> dict:
    """The job document as served: everything but the test-only poke counter."""
    return {key: value for key, value in job.items() if key != "pokes"}


def _native_complete_job(fixture: dict, request: dict) -> dict:
    """The endpoint job snapshot of the sealed fixture: native_complete, nothing more."""
    return {
        "schema_version": "solidworks-to-urdf.job/v1",
        "pipeline_id": PIPELINE_ID,
        "run_id": request["run_id"],
        "request": dict(request),
        "main_assembly": request.get("main_assembly") or fixture["main_assembly"],
        "hardware_id": fixture["hardware_id"],
        "revision": fixture["revision"],
        "repository_slug": REPOSITORY_SLUG,
        "repository_base": REPOSITORY_BASE,
        "status": "native_complete",
        "events": [
            *fixture["native_stages"]["events"],
            {"stage": "job", "state": "native_complete", "at": "t0"},
        ],
        "result": {
            "native_complete": True,
            "native_tool": fixture["native_tool"],
            "capture_archive": dict(fixture["receipt"]),
        },
        "error": None,
    }


def _mock_endpoint(fixture: dict) -> tuple[ThreadingHTTPServer, dict]:
    """The Windows endpoint contract, serving exactly one sealed control-fixture delivery."""
    state = {"jobs": {}, "resolved_paths": [], "artifact_paths": [], "expected": None}
    archive = Path(fixture["archive"]).read_bytes()

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
            if not self.path.startswith("/v1/jobs/"):
                self._send(404, {"error": "unknown"})
                return
            segments = self.path[len("/v1/jobs/") :].split("/")
            job = state["jobs"].get(segments[0])
            if job is None:
                self._send(404, {"error": "unknown"})
                return
            if len(segments) >= 2 and segments[1] == "artifacts":
                if urlparse.unquote("/".join(segments[2:])) != fixture["receipt"]["name"]:
                    self._send(404, {"error": "only the sealed native capture transfer is available"})
                    return
                state["artifact_paths"].append(self.path)
                self.send_response(200)
                self.send_header("Content-Type", "application/zip")
                self.send_header("Content-Length", str(len(archive)))
                self.end_headers()
                self.wfile.write(archive)
                return
            job["pokes"] += 1
            if job["status"] == "queued":
                job["status"] = "running"
            if job["pokes"] >= 2 and job["status"] == "running":
                sealed = _native_complete_job(fixture, job["request"])
                sealed["pokes"] = job["pokes"]
                job.update(sealed)
            self._send(200, _view(job))

        def do_POST(self) -> None:
            if not self._auth():
                return
            payload = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            if self.path == "/v1/handoffs/resolve":
                path = payload.get("handoff_path")
                if not isinstance(path, str) or not path.strip():
                    self._send(400, {"error": "handoff_path is required"})
                    return
                state["resolved_paths"].append(path)
                state["expected"] = {"package": RESOLVED_PACKAGE, "handoff_sha256": fixture["handoff_sha256"]}
                self._send(
                    200,
                    {
                        "schema_version": "solidworks-to-urdf.handoff/v1",
                        "pipeline_id": PIPELINE_ID,
                        "package": RESOLVED_PACKAGE,
                        "handoff_sha256": fixture["handoff_sha256"],
                    },
                )
                return
            if self.path != "/v1/jobs":
                self._send(404, {"error": "not found"})
                return
            expected = state["expected"] or {}
            wanted = {
                "run_id": fixture["run_id"],
                "package": expected.get("package"),
                "handoff_sha256": expected.get("handoff_sha256"),
            }
            if (
                set(payload) - {"run_id", "package", "handoff_sha256", "main_assembly"}
                or {key: payload.get(key) for key in wanted} != wanted
            ):
                self._send(409, {"error": "job request differs from the resolved handoff"})
                return
            existing = state["jobs"].get(wanted["run_id"])
            if existing is not None:
                if existing["request"] != payload:
                    self._send(409, {"error": "run_id is bound to a different request"})
                    return
                self._send(200, _view(existing))
                return
            job = {
                "schema_version": "solidworks-to-urdf.job/v1",
                "pipeline_id": PIPELINE_ID,
                "run_id": wanted["run_id"],
                "request": dict(payload),
                "status": "queued",
                "events": [{"stage": "job", "state": "queued", "at": "t0"}],
                "result": None,
                "error": None,
                "pokes": 0,
            }
            state["jobs"][wanted["run_id"]] = job
            self._send(202, _view(job))

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
            return {
                "status": response.status,
                "configured": payload.get("configured"),
                "request_context": payload.get("request_context"),
            }
    except urlerror.HTTPError as error:
        with contextlib.suppress(ValueError):
            payload = json.loads(error.read() or b"{}")
            return {
                "status": error.code,
                "configured": payload.get("configured"),
                "request_context": payload.get("request_context"),
            }
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


def _plugin_preflight(venv: Path, env: dict[str, str], home: Path) -> str | None:
    """Ensure the plugin loads from the installed entry point or from this home's plugins folder."""
    code = (
        "import importlib.metadata as m;"
        "print(any(e.group == 'airflow.plugins' and e.name == 'description_pipeline' for e in m.entry_points()))"
    )
    try:
        probe = subprocess.run(
            [str(venv / "bin/python"), "-c", code], env=env, capture_output=True, text=True, timeout=60
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return f"cannot probe the Airflow environment: {error}"
    if probe.returncode == 0 and probe.stdout.strip() == "True":
        return None
    # PYTHONPATH-only wiring has no distribution metadata; the isolated home loads the same class
    # from its plugins folder instead.  The middleware itself is proven by the auth health probe.
    try:
        plugins = home / "plugins"
        plugins.mkdir(parents=True, exist_ok=True)
        (plugins / "description_pipeline_plugin.py").write_text(
            '"""Isolated smoke home: re-export the checkout\'s Airflow plugin class."""\n'
            "\n"
            "from description_pipeline.orchestration.airflow_plugin import DescriptionPipelinePlugin  # noqa: F401\n",
            encoding="utf-8",
        )
    except OSError as error:
        return f"the Airflow environment provides no plugin and the isolated home cannot add one: {error}"
    return None


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
    parser.add_argument("--timeout", type=float, default=420)
    args = parser.parse_args()
    sys.path.insert(0, str(args.root / "src"))
    sys.path.insert(0, str(args.root))
    from description_pipeline.orchestration.airflow_client import native_run_id  # noqa: PLC0415
    from description_pipeline.orchestration.linux_store import LinuxStore  # noqa: PLC0415
    from description_pipeline.orchestration.portal import AirflowApi  # noqa: PLC0415
    from tests.v1.split_capture_fixture import (  # noqa: PLC0415
        CONTROL_LIMITS,
        QUALIFICATION,
        REQUIRED_CONTROL_GATES,
        build_fixture_capture,
    )

    run_id = f"smoke-{uuid.uuid4().hex[:12]}"
    native_id = native_run_id(run_id)
    fixture_root = args.airflow_home / "smoke" / f"fixture-{native_id}"
    try:
        fixture = build_fixture_capture(fixture_root, run_id=native_id)
    except Exception as error:  # noqa: BLE001 - the smoke reports the exact stage
        print(json.dumps({"ok": False, "stage": "control_fixture", "error": f"{type(error).__name__}: {error}"}))
        return 1

    api_port = args.api_port or _free_port()
    server, state = _mock_endpoint(fixture)
    port = server.server_address[1]
    store_root = args.airflow_home / "smoke-store"
    connection = json.dumps(
        {
            "conn_type": "http",
            "host": "127.0.0.1",
            "port": port,
            "password": TOKEN,
            "extra": {
                "store_root": str(store_root),
                "repositories": {REPOSITORY_SLUG: str(args.airflow_home / "smoke-repository")},
            },
        }
    )
    env = dict(
        os.environ,
        AIRFLOW_HOME=str(args.airflow_home),
        PYTHONPATH=os.pathsep.join(filter(None, [str(args.root / "src"), os.environ.get("PYTHONPATH")])),
        AIRFLOW_CONN_SOLIDWORKS_WINDOWS=connection,
        AIRFLOW__CORE__AUTH_MANAGER="description_pipeline.orchestration.feishu_auth.FeishuAuthManager",
        AIRFLOW__CORE__LOAD_EXAMPLES="false",
        AIRFLOW__API_AUTH__JWT_SECRET=JWT_SECRET,
        **_feishu_env(args.airflow_home),
        SOLIDWORKS_SENSOR_MODE="poke",
        SOLIDWORKS_POLL_INTERVAL="1",
        SOLIDWORKS_TIMEOUT="120",
        # An isolated home has no airflow.cfg pointing at the repository DAG folder.
        AIRFLOW__CORE__DAGS_FOLDER=str(args.root / "deploy/airflow/dags"),
        AIRFLOW__CORE__EXECUTION_API_SERVER_URL=f"http://127.0.0.1:{api_port}/execution",
    )
    problem = _plugin_preflight(args.venv, env, args.airflow_home)
    if problem is not None:
        print(json.dumps({"ok": False, "stage": "plugin_missing", "error": problem}))
        server.close()
        return 1
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
        if auth_health.get("status") == 200 and auth_health.get("request_context") is not True:
            print(
                json.dumps(
                    {
                        "ok": False,
                        "stage": "auth_middleware_missing",
                        "auth_health": auth_health,
                        "error": "the request-context middleware is not bound; the isolated home must load the "
                        "description-pipeline Airflow plugin",
                    }
                )
            )
            return 1
        unpause = _run(args.venv, env, "dags", "unpause", DAG_ID)
        if unpause.returncode != 0:
            print(json.dumps({"ok": False, "stage": "unpause", "stderr": unpause.stderr[-500:]}))
            return 1
        # Trigger exactly as the operator page does: the portal's client, a real operator JWT and
        # the pinned strict request model.  A CLI trigger could not catch a body regression.
        airflow = AirflowApi(f"http://127.0.0.1:{api_port}")
        token = _operator_jwt(args.venv, env)
        try:
            airflow.trigger_dag_run(token, DAG_ID, run_id, {"handoff_path": HANDOFF_PATH})
        except Exception as error:  # noqa: BLE001 - the smoke reports the exact stage
            print(json.dumps({"ok": False, "stage": "portal_trigger", "error": str(error)}))
            return 1
        deadline = time.time() + args.timeout
        triggering_user_name = None
        dag_state = None
        while time.time() < deadline:
            try:
                run = airflow.dag_run(token, DAG_ID, run_id)
            except Exception:  # noqa: BLE001 - the run may not be visible for a moment
                time.sleep(5)
                continue
            triggering_user_name = run.get("triggering_user_name")
            dag_state = run.get("state")
            if dag_state in {"success", "failed"}:
                break
            time.sleep(5)
        else:
            tails = {c: (log_dir / f"{c}.log").read_text(encoding="utf-8")[-400:] for c in logs}
            print(
                json.dumps({"ok": False, "stage": "timeout", "run_id": run_id, "dag_state": dag_state, "logs": tails})
            )
            return 1

        # The scheduler serialized every task: the same operator API that renders the run reports
        # each task instance, its state and its attempt count.
        instances = airflow.task_instances(token, DAG_ID, run_id)
        task_states = {
            str(item.get("task_id")): {"state": item.get("state"), "try_number": item.get("try_number")}
            for item in instances
        }
        problems: list[str] = []
        if dag_state != "failed":
            problems.append(f"DAG run state is {dag_state!r}, expected the documented rejection (failed)")
        for task_id, expected_state in (
            ("resolve_handoff", "success"),
            ("start_job", "success"),
            ("wait_for_job", "success"),
            ("fetch_capture", "success"),
            ("run_generate", "success"),
            ("run_verify", "failed"),
        ):
            actual = task_states.get(task_id, {}).get("state")
            if actual != expected_state:
                problems.append(f"task {task_id} is {actual!r}, expected {expected_state!r}")
        for task_id in ("run_publish", "confirm_job"):
            # Blocked without execution: the scheduler marked the task upstream_failed/skipped and
            # it never entered a running attempt, so a publish that started and failed early can
            # never be mistaken for a prevention proof.
            actual = task_states.get(task_id) or {}
            if actual.get("state") not in {"upstream_failed", "skipped"} or actual.get("try_number"):
                problems.append(
                    f"task {task_id} is {actual.get('state')!r} with {actual.get('try_number')!r} attempts; "
                    "publication must be blocked without ever executing"
                )

        store = LinuxStore(store_root)
        generated = _read_json(store.receipt_path(native_id, "generate"))
        verified = _read_json(store.receipt_path(native_id, "verify"))
        meta = store.meta(native_id) or {}
        if generated.get("state") != "generated":
            problems.append(f"generate checkpoint is {generated.get('state')!r}, not 'generated'")
        if verified.get("state") != "failed":
            problems.append(f"verify receipt is {verified.get('state')!r}, not 'failed'")
        error = str(verified.get("error") or "")
        if "URDF verification failed" not in error:
            problems.append(f"verify receipt error is not the documented verification rejection: {error!r}")
        diagnostic = Path(str(verified.get("diagnostic_path") or ""))
        quality = _read_json(diagnostic / "reports/quality.json")
        checks = [item for item in quality.get("checks") or [] if isinstance(item, dict)]
        gates = {item.get("id"): item for item in checks}
        failed_gates = sorted(
            item.get("id") for item in checks if item.get("passed") is False or item.get("state") == "failed"
        )
        unexpected_gates = sorted(set(failed_gates) - set(CONTROL_LIMITS))
        missing_required = [gate for gate in REQUIRED_CONTROL_GATES if not (gates.get(gate) or {}).get("passed")]
        if unexpected_gates:
            problems.append(f"quality failed outside the documented control limits: {unexpected_gates}")
        if missing_required:
            problems.append(f"quality skipped required gates: {missing_required}")
        if quality.get("subject_sha256") != generated.get("subject_sha256"):
            problems.append("verify quality is not bound to the generated subject")
        publish_receipt = store.receipt_path(native_id, "publish")
        pr_json = sorted(
            path.relative_to(store.run_dir(native_id)).as_posix() for path in store.run_dir(native_id).rglob("pr.json")
        )
        if publish_receipt.is_file():
            problems.append("a publish receipt exists although publication must not execute")
        if pr_json:
            problems.append(f"publication evidence exists although publication must not execute: {pr_json}")
        jobs = list(state["jobs"].values())
        if len(jobs) != 1 or jobs[0].get("status") != "native_complete":
            problems.append("the mocked endpoint did not serve exactly one native_complete job")
        if not state["artifact_paths"]:
            problems.append("the sealed capture transfer was never streamed")
        if meta.get("last_stage") != "verify":
            problems.append(f"the attempt advanced past verification: last_stage={meta.get('last_stage')!r}")

        print(
            json.dumps(
                {
                    "ok": not problems,
                    "qualification": QUALIFICATION,
                    "run_id": run_id,
                    "native_run_id": native_id,
                    "api_port": api_port,
                    "auth_health": auth_health,
                    "trigger": "portal-airflow-client",
                    "triggering_user_name": triggering_user_name,
                    "dag_state": dag_state,
                    "task_states": task_states,
                    "resolved_paths": state["resolved_paths"],
                    "artifact_paths": state["artifact_paths"],
                    "handoff_sha256": fixture["handoff_sha256"],
                    "generate": {"state": generated.get("state"), "subject_sha256": generated.get("subject_sha256")},
                    "verify": {
                        "state": verified.get("state"),
                        "error": verified.get("error"),
                        "diagnostic_path": verified.get("diagnostic_path"),
                        "failed_gates": failed_gates,
                        "unexpected_gates": unexpected_gates,
                        "missing_required_gates": missing_required,
                        "subject_sha256": verified.get("subject_sha256"),
                    },
                    "publication": {"executed": bool(publish_receipt.is_file() or pr_json), "pr_json": pr_json or None},
                    "problems": problems,
                }
            )
        )
        return 0 if not problems else 1
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
