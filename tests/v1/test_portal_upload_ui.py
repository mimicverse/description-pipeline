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
import shutil
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
            # The login must land on the canonical origin before a state cookie is minted, and
            # callbacks on another origin are re-pointed there with their query preserved.
            self.assertIn("location = /auth/feishu/login", nginx)
            self.assertIn("location = /auth/feishu/callback", nginx)
            self.assertIn('if ($host != "rehearsal.local")', nginx)
            self.assertIn("return 302 https://rehearsal.local:18443/auth/feishu/login;", nginx)
            self.assertIn("https://rehearsal.local:18443$uri$is_args$args;", nginx)


class RerunUiContractTests(unittest.TestCase):
    """Per-stage rerun action bound to C's confirmed attempts contract."""

    def app(self) -> str:
        return (STATIC / "app.js").read_text(encoding="utf-8")

    def test_rerun_contract_is_rendered(self) -> None:
        app = self.app()
        for token in (
            "stage_reruns",
            "/attempts",
            "body: { stage: stageId }",
            "resume_from_name_zh",
            "parent_dag_run_id",
            "reason_zh",
            "earliest_required",
            "renderRerunPanel",
            "rerunFromStage",
            "shortRunId",
            "从此步骤重新运行",
            "继续原作业",
            "仅发起人或平台管理员可重新运行。",
            "将重算该步骤及其后续阶段；原始运行与证据保留，重跑为新关联运行。",
            "重新运行 · 来源",
            "prerequisites",
            "recomputes",
            "retains",
            "PREREQ_STATE_ZH",
            "rerunStageName",
            "前置条件：",
            "将重算：",
            "最早可重新运行：",
            "复用既有输入/结果",
            "nextRunId",
            "stageToShow",
            "stageSelectionKey(nextRunId)",
            "run-origin",
        ):
            self.assertIn(token, app)
        # The superseded picker design and its parameter name are gone.
        self.assertNotIn("from_stage", app)
        self.assertNotIn("重试失败步骤", app)


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
        self.assertIn("text.textContent = scope.automatic_exclusion", app)
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


class AssemblySelectorUiContractTests(unittest.TestCase):
    """One delivered assembly is chosen from the picked folder before the upload starts."""

    def test_selector_markup_and_payload_contract(self) -> None:
        html = (STATIC / "index.html").read_text(encoding="utf-8")
        app = (STATIC / "app.js").read_text(encoding="utf-8")
        css = (STATIC / "style.css").read_text(encoding="utf-8")
        for element in ('id="assembly-row"', 'id="assembly-select"', 'id="assembly-note"'):
            self.assertIn(element, html)
        for token in (
            'endsWith(".sldasm")',
            "applyAssemblies",
            "updateStartEnabled",
            "state.assemblyChoice",
            'form.append("main_assembly", state.assemblyChoice)',
            "主装配",
            "请选择主装配…",
        ):
            self.assertIn(token, app)
        self.assertIn(".assembly-row", css)


class RunHistoryUiContractTests(unittest.TestCase):
    """Readable run history with rename and explicit reversible delete/restore.

    The list shows the engineering folder (or the custom display title), a human timestamp,
    the state and the recorded initiator; rerun lineage stays a secondary chip. Delete moves a
    run into the explicit 已删除 view (never a one-way hide), rename only changes the displayed
    name, and deleted runs stay readable while retry/rerun wait for a restore.
    """

    def page(self) -> str:
        return (STATIC / "index.html").read_text(encoding="utf-8")

    def app(self) -> str:
        return (STATIC / "app.js").read_text(encoding="utf-8")

    def test_page_title_is_exact(self) -> None:
        page = self.page()
        self.assertIn("<title>SolidWorks2URDF 交付操作台</title>", page)
        self.assertIn("<h1>SolidWorks2URDF 交付操作台</h1>", page)

    def test_rows_render_name_time_state_actor_and_secondary_rerun_chip(self) -> None:
        app = self.app()
        for token in (
            "runDisplayTitle",
            "formatRunTime",
            "RUN_STATES[run.state]",
            "状态待确认",
            '发起人：${run.user || "未记录"}',
            "从${run.resume_from_name_zh}重跑",
            "重跑来源：",
            "open.title",
            "formatTime(run.started_at)",
        ):
            self.assertIn(token, app)
        # The rerun-dominated list title is gone; the parent id only lives in secondary detail.
        self.assertNotIn("自${run.resume_from_name_zh}重新运行", app)

    def test_explicit_views_with_real_paging(self) -> None:
        page = self.page()
        app = self.app()
        self.assertIn('id="runs-view-active"', page)
        self.assertIn('id="runs-view-deleted"', page)
        for token in (
            "include_deleted=1",
            "next_offset",
            "loadMoreRuns",
            "加载更多",
            "当前已加载记录中没有可显示的运行，可继续加载更多。",
            "已显示全部",
            "state.runView",
            "state.runsNextOffset",
        ):
            self.assertIn(token, app)

    def test_rename_delete_restore_contract_and_delete_confirmation(self) -> None:
        app = self.app()
        page = self.page()
        css = (STATIC / "style.css").read_text(encoding="utf-8")
        self.assertIn('id="run-actions"', page)
        for token in (
            'method: "PATCH"',
            "body: { title }",
            'method: "DELETE"',
            "/restore",
            "run.can_manage === true",
            "run-menu",
            "run-menu-trigger",
            "run-menu-pop",
            "aria-haspopup",
            "aria-expanded",
            "操作：",
            "重命名「",
            "run-menu-buttons",
            "运行结束前不能删除。",
            "运行状态未知，暂不能删除。",
            "从运行列表移除，可在「已删除」中恢复；模型、证据和 PR 保留。",
            "已删除 · ",
            "删除人：",
            "overlayRun",
        ):
            self.assertIn(token, app)
        # No permanently visible row actions and no viewer-guessed deleter.
        self.assertNotIn("run-row-actions", app)
        self.assertNotIn(".run-row-actions", css)
        self.assertNotIn("buildRunActions", app)
        self.assertNotIn("run.deleted_by = state.user", app)

    def test_followup_regressions_are_pinned(self) -> None:
        app = self.app()
        for token in (
            'from "/static/run_history.js"',
            "resolveDeleted",
            "resolveTitle",
            "menuAction(state.runActionsFor, runId, canManage)",
            "overlayRun(state.runsList, run,",
            "deleted: payload.deleted !== false,",
            'deleted_by: typeof payload.deleted_by === "string" ? payload.deleted_by : null',
            "run.parent_dag_run_id || (listEntry && listEntry.parent_dag_run_id)",
            "runActionsFor: null,",
        ):
            self.assertIn(token, app)
        # The temporary delete flow re-read the list and lost the loaded pages.
        self.assertNotIn("The backend actor is authoritative", app)

    def test_deleted_runs_stay_readable_with_restore_first(self) -> None:
        app = self.app()
        page = self.page()
        self.assertIn('id="run-deleted"', page)
        self.assertIn("此运行已删除：请先恢复后再重试或重跑。", app)
        self.assertIn("此运行已删除：请先恢复后再重跑。", app)
        self.assertIn("RETRY_REASONS.deleted", app)

    def test_keyboard_and_focus_contract(self) -> None:
        app = self.app()
        for token in (
            'event.key === "Escape"',
            "input.focus()",
            "confirm.focus()",
            "trigger.focus()",
        ):
            self.assertIn(token, app)


class RunHistoryJsLogicTests(unittest.TestCase):
    """Focused Node regression for the pure run-history list helpers."""

    @unittest.skipUnless(shutil.which("node"), "node is required to run the JS logic regression")
    def test_run_history_logic_regression(self) -> None:
        script = ROOT / "tests" / "js" / "run_history_logic_test.mjs"
        result = subprocess.run(
            [shutil.which("node"), str(script)],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, f"{result.stdout}\n{result.stderr}")


class ActivityUiContractTests(unittest.TestCase):
    """Live-activity card: mounts, wording, and the no-fabrication hard rules."""

    def activity(self) -> str:
        return (STATIC / "activity.js").read_text(encoding="utf-8")

    def test_page_mounts_the_card_in_overview_and_running_stage(self) -> None:
        html = (STATIC / "index.html").read_text(encoding="utf-8")
        self.assertIn('id="activity-overview"', html)
        self.assertIn('id="activity-stage"', html)
        app = (STATIC / "app.js").read_text(encoding="utf-8")
        for token in (
            'from "/static/activity.js"',
            "renderActivity(run);",
            "activityView(run.activity",
            "activityPlacement(",
            "startActivityTimer()",
            "stopActivityTimer()",
        ):
            self.assertIn(token, app)

    def test_wording_states_and_hard_rules_are_pinned(self) -> None:
        source = self.activity()
        for token in (
            "暂无详细进度",
            "暂无新更新",
            "刚刚更新",
            "已处理",
            "updated_at",
            "queued",
            "waiting",
            "busy",
            "finished",
            "最后活动",
            "打开文档",
            "重建模型",
            "discover.open_document",
        ):
            self.assertIn(token, source)
        # No percentage figures in any rendered text (modulo arithmetic outside strings is
        # fine), no forecasts, no HTML injection surface.
        self.assertIsNone(re.search(r'"[^"\n]*%[^"\n]*"', source), "percent in double-quoted text")
        self.assertIsNone(re.search(r"`[^`\n]*%[^`\n]*`", source), "percent in template text")
        self.assertNotIn("预计", source)
        self.assertNotIn("剩余", source)
        self.assertNotIn("innerHTML", source)
        app = (STATIC / "app.js").read_text(encoding="utf-8")
        self.assertIn("buildActivityCard", app)
        self.assertNotIn("innerHTML", app)


class ActivityJsLogicTests(unittest.TestCase):
    """Focused Node regression for the pure live-activity view helpers."""

    @unittest.skipUnless(shutil.which("node"), "node is required to run the JS logic regression")
    def test_activity_logic_regression(self) -> None:
        script = ROOT / "tests" / "js" / "activity_logic_test.mjs"
        result = subprocess.run(
            [shutil.which("node"), str(script)],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, f"{result.stdout}\n{result.stderr}")


class FailureDisplayUiContractTests(unittest.TestCase):
    """Terminal-failure summary: reported-finding counts, bounded collapsible evidence."""

    def test_failure_summary_counts_are_findings_not_defects(self) -> None:
        app = (STATIC / "app.js").read_text(encoding="utf-8")
        for token in (
            "不代表 CAD 缺陷",
            "discovery.link_name_missing",
            "并不代表 CAD 中不存在坐标系",
            "不需要为每个供应商内部叶件单独添加坐标系",
            "示例对象：",
            "未读取到机器人名称",
            "缺少机器人名称",
        ):
            self.assertIn(token, app)

    def test_large_finding_groups_are_lazy_and_bounded(self) -> None:
        app = (STATIC / "app.js").read_text(encoding="utf-8")
        for token in (
            "items.length <= 12",
            "populated",
            'section.addEventListener("toggle"',
            "if (!knownRun) void refreshRuns();",
        ):
            self.assertIn(token, app)
        # The eager pre-open that expanded hundreds of rows must stay gone.
        self.assertNotIn("if (index === 0) section.open = true;", app)


if __name__ == "__main__":
    unittest.main()
