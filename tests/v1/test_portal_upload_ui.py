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
        self.assertIn("将拒绝上传", app)
        self.assertIn("state.viewer.resize", app)
        self.assertIn("limits.max_files", app)
        self.assertIn("启动状态尚未确认", app)
        self.assertIn("不要重复提交", app)
        self.assertIn("error.dagRunId", app)
        self.assertIn('$("folder-input").disabled = true', app)
        self.assertIn("上传完成，正在创建运行", app)
        self.assertIn("state.blockedPick", app)
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
            "run-error-detail",
        ):
            self.assertIn(f'id="{element}"', html)
        app = (STATIC / "app.js").read_text(encoding="utf-8")
        for name in ("showUploadView", "showRunView", "renderStepper", "selectInspectTab"):
            self.assertIn(f"function {name}(", app)
        self.assertIn('$("upload-view").hidden = true', app)
        self.assertIn('$("detail-card").hidden = true', app)
        self.assertGreaterEqual(app.count("run-error-detail"), 3)
        self.assertIn("terminalNow", app)


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


class ReportUiContractTests(unittest.TestCase):
    """The run detail renders C's canonical report payload without local copies."""

    def app(self) -> str:
        return (STATIC / "app.js").read_text(encoding="utf-8")

    def test_report_payload_fields_are_rendered(self) -> None:
        app = self.app()
        for token in (
            "run.report",
            "report.overall",
            "headline_zh",
            "engineering_state",
            "report.failure",
            "title_zh",
            "meaning_zh",
            "raw_error",
            "raw_detail",
            "report.measured",
            "expected_mass_window",
            "mass_closure",
            "urdf_mass_kg",
            "whole_cad_mass_kg",
            "label_zh",
            "state_zh",
            "raw_details",
            "counts",
            "countsText",
            "external_review",
            "scopes",
            "automatic_exclusion",
            "note_zh",
            "manual_scope_note_zh",
            "executed",
            "independent",
            "boundary",
        ):
            self.assertIn(token, app)

    def test_no_legacy_stage_view_rendering_survives(self) -> None:
        app = self.app()
        for legacy in (
            "legacyRow",
            "reviewFacts",
            "stage_view",
            "confirmations_zh",
            "unsupported_zh",
            "review_scope",
            "checks_executed",
        ):
            self.assertNotIn(legacy, app)
        self.assertIn("报告数据暂不可用", app)

    def test_failed_stage_is_auto_selected_and_selection_persists(self) -> None:
        app = self.app()
        self.assertIn("localStorage.getItem", app)
        self.assertIn("localStorage.setItem", app)
        self.assertIn("portal.stage.", app)
        self.assertIn('stage.state === "failed"', app)

    def test_blocked_stages_are_neutral_not_failed(self) -> None:
        app = self.app()
        self.assertNotIn('state === "failed" || state === "blocked") return "bad"', app)
        self.assertIn('if (state === "failed") return "bad"', app)
        css = (STATIC / "style.css").read_text(encoding="utf-8")
        self.assertNotIn(".step.blocked .step-meta { color: var(--bad); }", css)
        self.assertNotIn(".chip.failed, .chip.blocked", css)
        self.assertIn(".step.blocked .step-meta { color: var(--muted); }", css)

    def test_preview_uses_full_width_when_absent(self) -> None:
        app = self.app()
        css = (STATIC / "style.css").read_text(encoding="utf-8")
        self.assertIn("no-preview", app)
        self.assertIn("updatePreviewLayout", app)
        self.assertIn(".run-workspace.no-preview", css)
        self.assertNotIn("repeat(4, minmax(0, 1fr))", css)
        self.assertNotIn("stage-boundaries", app)
        self.assertNotIn("stage-boundaries", css)

    def test_manual_scope_is_gated_and_honest(self) -> None:
        app = self.app()
        self.assertIn("工程评审范围", app)
        self.assertIn("未就绪", app)
        self.assertIn("自动检查无法核验", app)
        self.assertIn("不重复检查", app)
        self.assertNotIn("项待确认", app)

    def test_stepper_keeps_names_and_shows_affected_object(self) -> None:
        app = self.app()
        css = (STATIC / "style.css").read_text(encoding="utf-8")
        self.assertIn("STEP_STATE_SHORT", app)
        self.assertIn("已阻断", app)
        self.assertIn("minmax(3.5em, 1fr)", css)
        self.assertIn("max-width: 5.5em", css)
        self.assertIn("涉及对象", app)
        self.assertIn("visibleFindings", app)


if __name__ == "__main__":
    unittest.main()
