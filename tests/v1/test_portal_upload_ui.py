"""Focused UI/deploy contract tests for the browser folder-upload flow.

These assertions pin the contract between the static operator page, the
deployment rendering, and the portal upload endpoint: one local folder picker
(no manual platform path), multipart ``files`` upload with relative paths,
bounded limits, and a dedicated upload root rendered into ``portal.json``.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OPERATOR = ROOT / "deploy" / "operator"
RENDER = OPERATOR / "render_operator.py"
STATIC = ROOT / "src" / "description_pipeline" / "orchestration" / "static"


def base_env(state: Path) -> dict[str, str]:
    return {
        "OPERATOR_HOST": "rehearsal.local",
        "OPERATOR_BIND": "127.0.0.1",
        "OPERATOR_HTTPS_PORT": "18443",
        "OPERATOR_STATE": str(state),
        "OPERATOR_UPSTREAM": "127.0.0.1:18788",
        "AIRFLOW_VENV": str(state.parent / "venv"),
        "AIRFLOW_HOME": str(state.parent / "home"),
        "AIRFLOW_DB_URL": "postgresql+psycopg2://solidworks@/airflow_meta?host=/tmp/socket&port=5433",
        "NGINX_BIN": "/usr/sbin/nginx",
        "SOLIDWORKS_SSH_HOST": "windows-m3",
        "SOLIDWORKS_HANDOFF_ROOT": str(state.parent / "handoffs"),
    }


class UploadUiContractTests(unittest.TestCase):
    def test_page_has_one_folder_picker_and_no_manual_path(self) -> None:
        html = (STATIC / "index.html").read_text(encoding="utf-8")
        self.assertIn('type="file"', html)
        self.assertIn("webkitdirectory", html)
        self.assertNotIn("handoff_path", html)
        self.assertNotIn("handoff-path", html)
        for element in (
            "folder-input",
            "choose-button",
            "folder-summary",
            "start-button",
            "cancel-button",
            "upload-status",
            "run-error",
        ):
            self.assertIn(f'id="{element}"', html)

    def test_app_uploads_multipart_files_with_relative_paths(self) -> None:
        app = (STATIC / "app.js").read_text(encoding="utf-8")
        self.assertIn('form.append("files"', app)
        self.assertIn("webkitRelativePath", app)
        self.assertIn('request.open("POST", "/api/runs")', app)
        self.assertIn("X-CSRF-Token", app)
        self.assertNotIn("body: { handoff_path", app)
        self.assertIn("maxTotalBytes: 2 * 1024 * 1024 * 1024", app)
        self.assertIn("maxFiles: 4096", app)
        self.assertIn("maxFileBytes: 512 * 1024 * 1024", app)
        self.assertIn("session.upload_limits", app)
        self.assertIn('id="upload-limits"', (STATIC / "index.html").read_text(encoding="utf-8"))
        self.assertIn('startsWith("~$")', app)
        self.assertIn("临时锁文件", app)
        self.assertIn("ratio >= 1", app)
        self.assertIn("不会自动重试", app)
        self.assertNotIn("UPLOAD_LIMITS.", app)

    def test_every_static_id_used_by_app_exists_in_page(self) -> None:
        html = (STATIC / "index.html").read_text(encoding="utf-8")
        app = (STATIC / "app.js").read_text(encoding="utf-8")
        used = sorted(set(re.findall(r'\$\("([^"]+)"\)', app)))
        missing = [name for name in used if f'id="{name}"' not in html]
        self.assertEqual(missing, [], f"ids referenced by app.js but missing in index.html: {missing}")

    def test_layout_switches_between_upload_and_run_views(self) -> None:
        html = (STATIC / "index.html").read_text(encoding="utf-8")
        for element in (
            "upload-view",
            "detail-card",
            "new-run-button",
            "stage-detail",
            "inspect-tabs",
            "run-dot",
            "run-state-label",
            "run-initiator",
            "run-ident",
            "task-progress",
        ):
            self.assertIn(f'id="{element}"', html)
        app = (STATIC / "app.js").read_text(encoding="utf-8")
        for name in ("showUploadView", "showRunView", "renderStepper", "selectInspectTab"):
            self.assertIn(f"function {name}(", app)
        self.assertIn('$("upload-view").hidden = true', app)
        self.assertIn('$("detail-card").hidden = true', app)


class UploadRenderTests(unittest.TestCase):
    def _render(self, state: Path) -> subprocess.CompletedProcess[str]:
        env = base_env(state)
        env_file = state.parent / "operator.env"
        env_file.parent.mkdir(parents=True, exist_ok=True)
        env_file.write_text("".join(f"{key}={value}\n" for key, value in env.items()), encoding="utf-8")
        merged = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(Path.home())}
        merged.update(env)
        return subprocess.run(
            [sys.executable, str(RENDER), "--env-file", str(env_file)],
            capture_output=True,
            text=True,
            env=merged,
            cwd=str(ROOT),
        )

    def test_render_supplies_upload_root_and_bounded_nginx_route(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state"
            (state.parent / "handoffs").mkdir(parents=True, exist_ok=True)
            result = self._render(state)
            self.assertEqual(result.returncode, 0, result.stderr)
            portal = json.loads((state / "portal.json").read_text(encoding="utf-8"))
            self.assertEqual(portal["portal"]["upload_root"], str(state.parent / "handoffs"))
            nginx = (state / "nginx" / "nginx.conf").read_text(encoding="utf-8")
            self.assertIn("client_max_body_size 2112m;", nginx)
            self.assertIn("client_body_timeout 600s;", nginx)
            self.assertIn("location ^~ /api/runs", nginx)
            self.assertIn("proxy_request_buffering off;", nginx)
            self.assertIn("proxy_read_timeout 3600s;", nginx)


if __name__ == "__main__":
    unittest.main()
