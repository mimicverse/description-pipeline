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
        self.assertIn("temporary lock files", app)
        self.assertIn("state.viewer.resize", app)
        self.assertIn("limits.max_files", app)
        self.assertIn("check the run list or retry the same submission", app)
        self.assertIn("no automatic retry will occur", app)
        self.assertIn("error.dagRunId", app)
        self.assertIn('$("folder-input").disabled = true', app)
        self.assertIn("Upload complete; creating run", app)
        self.assertIn("state.blockedPick", app)
        self.assertIn("ratio >= 1", app)
        self.assertIn("will not retry automatically", app)
        self.assertNotIn("UPLOAD_LIMITS.", app)

    def test_every_static_id_used_by_app_exists_in_page(self) -> None:
        html = (STATIC / "index.html").read_text(encoding="utf-8")
        app = (STATIC / "app.js").read_text(encoding="utf-8")
        used = sorted(set(re.findall(r'\$\("([^"]+)"\)', app)))
        missing = [name for name in used if f'id="{name}"' not in html]
        self.assertEqual(missing, [], f"ids referenced by app.js but missing in index.html: {missing}")

    def test_change_summary_is_a_required_multiline_textarea(self) -> None:
        html = (STATIC / "index.html").read_text(encoding="utf-8")
        self.assertIn('<textarea id="change-summary" rows="3"', html)
        self.assertIn('placeholder="Change summary (required)"', html)
        self.assertNotIn('maxlength="120"', html)
        self.assertNotIn('id="change-summary" class="muted small"', html)
        app = (STATIC / "app.js").read_text(encoding="utf-8")
        self.assertIn('form.append("change_summary", summaryInput.value.trim())', app)
        self.assertIn('$("change-summary")', app)

    def test_purpose_is_a_required_select_without_a_default(self) -> None:
        html = (STATIC / "index.html").read_text(encoding="utf-8")
        self.assertIn('<label for="purpose-select" class="muted small">Purpose (required)</label>', html)
        self.assertIn('<select id="purpose-select">', html)
        self.assertIn('<option value="">Select a purpose</option>', html)
        self.assertIn('<option value="engineering">Engineering delivery</option>', html)
        self.assertIn('<option value="inspection_preview">Inspection preview</option>', html)
        app = (STATIC / "app.js").read_text(encoding="utf-8")
        self.assertIn('form.append("purpose", purposeSelect.value)', app)
        self.assertIn("Purpose is required", app)
        # The select feeds the start gate and the submit body from the same id.
        self.assertEqual(app.count('$("purpose-select")'), 3)

    def test_targets_fetch_failure_is_distinct_from_an_unconfigured_platform(self) -> None:
        app = (STATIC / "app.js").read_text(encoding="utf-8")
        self.assertIn("No publication target is configured", app)
        self.assertIn("Failed to load publication targets", app)
        self.assertIn("handleUnauthorized(error, csrf)", app)
        self.assertIn("state.targetsRetryPick", app)
        self.assertIn("if (state.targetsRetryPick) void loadTargets(state.targetsRetryPick);", app)

    def test_layout_switches_between_upload_and_run_views(self) -> None:
        html = (STATIC / "index.html").read_text(encoding="utf-8")
        for element in (
            "upload-view",
            "detail-card",
            "new-run-button",
            "stage-detail",
            "stage-panel",
            "run-dot",
            "run-state-label",
            "run-initiator",
            "run-ident",
            "task-progress",
            "run-error-detail",
        ):
            self.assertIn(f'id="{element}"', html)
        app = (STATIC / "app.js").read_text(encoding="utf-8")
        for name in ("showUploadView", "showRunView", "renderStepper", "renderStageDetail"):
            self.assertIn(f"function {name}(", app)
        self.assertIn('$("upload-view").hidden = true', app)
        self.assertIn('$("detail-card").hidden = true', app)
        self.assertGreaterEqual(app.count("run-error-detail"), 3)


class SubmissionFlowJsTests(unittest.TestCase):
    """Run the real page functions against controlled asynchronous transport."""

    def test_submission_and_selection_ownership(self) -> None:
        node = shutil.which("node")
        self.assertIsNotNone(node, "node is required for the submission flow regression")
        result = subprocess.run(
            [node, str(ROOT / "tests" / "js" / "submission_flow_test.mjs")],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, f"{result.stdout}\n{result.stderr}")


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
            "const body = { stage: stageId }",
            "main_assembly_options",
            "New request from Freeze",
            "Create new request from Freeze",
            "Freeze creates a new request from the sealed originals",
            "body.main_assembly = selectedMain",
            "state.rerunMainByRun",
            "resume_from_name",
            "parent_dag_run_id",
            "message",
            "earliest_required",
            "renderRerunPanel",
            "rerunFromStage",
            "shortRunId",
            "Continue original job",
            "Only the initiator or a platform administrator can rerun.",
            "Freeze creates a new request from the sealed originals; freeze and all later "
            "stages rerun under the current rules and configuration. The original run and "
            "evidence remain.",
            "Rerun from",
            "prerequisites",
            "recomputes",
            "retains",
            "PREREQ_STATE",
            "rerunStageName",
            "Prerequisites: ",
            "Recomputes: ",
            "Earliest restart: ",
            "Reuse source",
            "nextRunId",
            "stageToShow",
            "stageSelectionKey(nextRunId)",
            "run-origin",
        ):
            self.assertIn(token, app)
        # The superseded picker design and its parameter name are gone.
        self.assertNotIn("from_stage", app)
        self.assertNotIn("Retry failed step", app)
        self.assertNotIn("window.prompt", app)


class ReportUiContractTests(unittest.TestCase):
    """The run detail renders C's canonical report payload without local copies."""

    def app(self) -> str:
        return (STATIC / "app.js").read_text(encoding="utf-8")

    def test_report_payload_fields_are_rendered(self) -> None:
        app = self.app()
        for token in (
            "run.report",
            "report.overall",
            "headline",
            "engineering_state",
            "report.failure",
            "title",
            "meaning",
            "raw_error",
            "raw_detail",
            "report.measured",
            "expected_mass_window",
            "mass_closure",
            "urdf_mass_kg",
            "whole_cad_mass_kg",
            "label",
            "state_label",
            "raw_details",
            "counts",
            "countsText",
            "external_review",
            "scopes",
            "automatic_exclusion",
            "note",
            "manual_scope_note",
            "executed",
            "independent",
            "boundary",
        ):
            self.assertIn(token, app)

    def test_stage_warnings_render_counts_values_and_blocker_kinds(self) -> None:
        """Recorded warnings reach the stage view: bounded, valued, never passes."""

        app = self.app()
        css = (STATIC / "style.css").read_text(encoding="utf-8")
        for token in (
            "warnings: Array.isArray(stage.warnings)",
            "renderStageWarnings(root, stage",
            "stage.counts.warnings",
            "Qualification blocker: ",
            "Publication blocker: ",
            "Located source examples",
            "JSON.stringify(examples, null, 2)",
            "warning.objects.slice(0, WARNING_ROW_LIMIT)",
            "for (const row of rows)",
            "records_total",
            "source_rows_omitted",
            "Open complete verification certificate",
        ):
            self.assertIn(token, app)
        self.assertIn(".warning-row", css)
        self.assertIn(".chip.warning", css)
        # Warnings stay recorded observations: no pass bookkeeping, no per-item human
        # confirmation and no substitute values.
        self.assertNotIn('sum("warnings")', app)
        self.assertNotIn("checkbox", app)

    def test_failure_card_lists_failed_checks_and_keeps_warnings_separate(self) -> None:
        """A failed stage names its exact failed checks instead of an undetermined cause."""

        app = self.app()
        for token in (
            "buildFailureCard(report.failure, report)",
            '["Error code", failure.error_code]',
            '["Phase", failure.phase]',
            '["Cause", failure.cause]',
            '["Evidence path", failure.evidence_path]',
            "Recorded values: ${inlineValue(failure.values)}",
            '"Recorded failure values"',
            "Failed automatic checks (",
            "failedChecks.slice(0, 5)",
            "A source-evidence check failed.",
            "This stage has",
            "Warnings are neither passes nor acceptance/publication evidence.",
        ):
            self.assertIn(token, app)
        self.assertNotIn("buildFailureCard(report.failure);", app)

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
        self.assertIn("Report data is unavailable", app)

    def test_failed_stage_is_auto_selected_and_selection_persists(self) -> None:
        app = self.app()
        self.assertIn("localStorage.getItem", app)
        self.assertIn("localStorage.setItem", app)
        self.assertIn("portal.stage.", app)
        self.assertIn("const STAGE_SELECTION_PRIORITY = [", app)
        self.assertIn('"unresolved",\n  "failed",\n  "cancelled",\n  "running",', app)

    def test_blocked_stages_are_neutral_not_failed(self) -> None:
        app = self.app()
        self.assertNotIn('state === "failed" || state === "blocked") return "bad"', app)
        css = (STATIC / "style.css").read_text(encoding="utf-8")
        self.assertNotIn(".step.blocked .step-meta { color: var(--bad); }", css)
        self.assertNotIn(".chip.failed, .chip.blocked", css)
        self.assertIn(".step.blocked .step-meta { color: var(--muted); }", css)

    def test_stage_state_vocabulary_keeps_canonical_terminal_states(self) -> None:
        app = self.app()
        self.assertNotIn("const STAGE_STATES", app)
        for token in (
            'waiting: "Waiting"',
            'cancelled: "Cancelled"',
            'unresolved: "Unresolved"',
            "const STAGE_SELECTION_PRIORITY = [",
            '"unresolved",\n  "failed",\n  "cancelled",\n  "running",',
            'for (const state of STAGE_SELECTION_PRIORITY)',
        ):
            self.assertIn(token, app)

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
        self.assertIn("Engineering review scope", app)
        self.assertIn("not ready", app)
        self.assertIn("facts that automatic checks cannot verify", app)
        self.assertIn("text.textContent = scope.automatic_exclusion", app)
        self.assertNotIn("pending confirmations", app)

    def test_stepper_keeps_names_and_shows_affected_object(self) -> None:
        app = self.app()
        css = (STATIC / "style.css").read_text(encoding="utf-8")
        self.assertIn("STEP_STATE_SHORT", app)
        self.assertIn("Blocked", app)
        self.assertIn(".step-label", css)
        self.assertIn("text-overflow: ellipsis", css)
        self.assertIn("Objects: ", app)
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
            "targetsRequest",
            "loadTargets(state.folderPick)",
            'form.append("main_assembly", state.assemblyChoice)',
            "Main assembly",
            "Select the main assembly...",
        ):
            self.assertIn(token, app)
        self.assertIn(".form-field", css)


class RunHistoryUiContractTests(unittest.TestCase):
    """Readable run history with rename and explicit reversible delete/restore.

    The list shows the main assembly (or the custom display title), a human timestamp,
    the state and the recorded initiator; rerun lineage stays a secondary chip. Delete moves a
    run into the explicit Deleted view (never a one-way hide), rename only changes the displayed
    name, and deleted runs stay readable while retry/rerun wait for a restore.
    """

    def page(self) -> str:
        return (STATIC / "index.html").read_text(encoding="utf-8")

    def app(self) -> str:
        return (STATIC / "app.js").read_text(encoding="utf-8")

    def test_page_title_is_exact(self) -> None:
        page = self.page()
        self.assertIn("<title>Robot Model Studio</title>", page)
        self.assertIn("<h1>Robot Model Studio</h1>", page)

    def test_rows_render_name_time_state_actor_and_secondary_rerun_chip(self) -> None:
        app = self.app()
        for token in (
            "runDisplayTitle",
            "assemblyLabel",
            "run.main_assembly",
            "formatRunTime",
            "RUN_STATES[displayState(run.state, run.automatic)]",
            "State unconfirmed",
            'Initiator: ${run.user || "not recorded"}',
            'Rerun from ${run.resume_from_name || "upstream stage"}',
            "Rerun source: ",
            "open.title",
            "formatTime(run.started_at)",
        ):
            self.assertIn(token, app)
        self.assertNotIn("handoff_path", app)
        # The rerun-dominated list title is gone; the parent id only lives in secondary detail.
        self.assertNotIn("Rerun source: ${run.resume_from_name} rerun", app)

    def test_explicit_views_with_real_paging(self) -> None:
        page = self.page()
        app = self.app()
        self.assertIn('id="runs-view-active"', page)
        self.assertIn('id="runs-view-deleted"', page)
        for token in (
            "include_deleted=1",
            "next_offset",
            "loadMoreRuns",
            "Load more",
            "No deliveries are visible in the loaded records; load more.",
            "All runs shown",
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
            "aria-expanded",
            "Actions: ",
            "Rename",
            "run-menu-buttons",
            "This delivery cannot be hidden before it ends.",
            "The delivery state is unknown, so it cannot be hidden.",
            "Remove this run from the list. It can be restored from Hidden; models, evidence, and PRs remain.",
            "Hidden · ",
            "Hidden by: ",
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
            "runActionsFor: null,",
        ):
            self.assertIn(token, app)
        # The temporary delete flow re-read the list and lost the loaded pages.
        self.assertNotIn("The backend actor is authoritative", app)

    def test_deleted_runs_stay_readable_with_restore_first(self) -> None:
        app = self.app()
        page = self.page()
        self.assertIn('id="run-deleted"', page)
        self.assertIn("This delivery is hidden; restore it before retrying or rerunning.", app)
        self.assertIn("This delivery is hidden; restore it before rerunning.", app)
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
    """Focused Node regression for the pure run-history list helpers (pinned toolchain)."""

    def test_run_history_logic_regression(self) -> None:
        node = shutil.which("node")
        self.assertIsNotNone(
            node,
            "node is required to run the JS logic regression; install the pinned runtime (.nvmrc)",
        )
        script = ROOT / "tests" / "js" / "run_history_logic_test.mjs"
        result = subprocess.run(
            [node, str(script)],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, f"{result.stdout}\n{result.stderr}")


class ActivityUiContractTests(unittest.TestCase):
    """Live-activity card: mounts, wording, and the no-fabrication hard rules."""

    def activity(self) -> str:
        return (STATIC / "activity.js").read_text(encoding="utf-8")

    def test_page_has_one_activity_mount(self) -> None:
        html = (STATIC / "index.html").read_text(encoding="utf-8")
        self.assertIn('id="activity-overview"', html)
        self.assertNotIn('id="activity-stage"', html)
        app = (STATIC / "app.js").read_text(encoding="utf-8")
        for token in (
            'from "/static/activity.js"',
            "renderActivity(run);",
            "activityView(run.activity",
            "startActivityTimer()",
            "stopActivityTimer()",
            "Related object: ",
        ):
            self.assertIn(token, app)

    def test_wording_states_and_hard_rules_are_pinned(self) -> None:
        source = self.activity()
        for token in (
            "No detailed progress",
            "No new update",
            "Updated just now",
            "Processed",
            "updated_at",
            "queued",
            "waiting",
            "busy",
            "finished",
            "Last activity",
            "Opening document",
            "Reading saved model state",
            "discover.open_document",
            "discover.session_start",
            "Starting SolidWorks",
        ):
            self.assertIn(token, source)
        # No percentage figures in any rendered text (modulo arithmetic outside strings is
        # fine), no forecasts, no HTML injection surface.
        self.assertIsNone(re.search(r'"[^"\n]*%[^"\n]*"', source), "percent in double-quoted text")
        self.assertIsNone(re.search(r"`[^`\n]*%[^`\n]*`", source), "percent in template text")
        self.assertNotIn("\u9884\u8ba1", source)
        self.assertNotIn("\u5269\u4f59", source)
        self.assertNotIn("innerHTML", source)
        app = (STATIC / "app.js").read_text(encoding="utf-8")
        self.assertIn("buildActivityCard", app)
        self.assertNotIn("innerHTML", app)


class ActivityJsLogicTests(unittest.TestCase):
    """Focused Node regression for the pure live-activity view helpers (pinned toolchain)."""

    def test_activity_logic_regression(self) -> None:
        node = shutil.which("node")
        self.assertIsNotNone(
            node,
            "node is required to run the JS logic regression; install the pinned runtime (.nvmrc)",
        )
        script = ROOT / "tests" / "js" / "activity_logic_test.mjs"
        result = subprocess.run(
            [node, str(script)],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, f"{result.stdout}\n{result.stderr}")


class StageStateJsLogicTests(unittest.TestCase):
    """Focused Node regression for stage labels, dot styles and default selection."""

    def test_stage_state_logic_regression(self) -> None:
        node = shutil.which("node")
        self.assertIsNotNone(
            node,
            "node is required to run the JS logic regression; install the pinned runtime (.nvmrc)",
        )
        script = ROOT / "tests" / "js" / "stage_state_logic_test.mjs"
        result = subprocess.run(
            [node, str(script)],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, f"{result.stdout}\n{result.stderr}")


class PreviewBatteryTests(unittest.TestCase):
    """Focused Node battery for the preview defaults (P1-P5)."""

    @unittest.skipUnless(shutil.which("node"), "node is required to run the JS battery")
    def test_successful_preview_retains_source_warnings(self) -> None:
        script = ROOT / "tests" / "js" / "preview_source_warnings_test.mjs"
        result = subprocess.run([shutil.which("node"), str(script)], capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, f"{result.stdout}\n{result.stderr}")

    @unittest.skipUnless(shutil.which("node"), "node is required to run the JS battery")
    def test_preview_battery_regression(self) -> None:
        script = ROOT / "tests" / "js" / "preview_battery_test.mjs"
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
            "not CAD defects",
            "discovery.link_name_missing",
            "does not prove that CAD has no coordinate system",
            "Do not add a separate coordinate system for every supplier-internal leaf.",
            "Example objects: ",
            "No robot name was read",
            "Missing robot name",
        ):
            self.assertIn(token, app)

    def test_large_finding_groups_are_lazy_and_bounded(self) -> None:
        app = (STATIC / "app.js").read_text(encoding="utf-8")
        for token in (
            "populated",
            'section.addEventListener("toggle"',
            "if (!knownRun) void refreshRuns();",
        ):
            self.assertIn(token, app)
        # The eager pre-open that expanded hundreds of rows must stay gone.
        self.assertNotIn("if (index === 0) section.open = true;", app)


if __name__ == "__main__":
    unittest.main()
