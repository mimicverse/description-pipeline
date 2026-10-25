"""CLI tests for canonical application-selected effect reconciliation."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import unittest
from pathlib import Path
from types import SimpleNamespace

from description_pipeline.io import digest
from description_pipeline.pipeline.contracts import StageCommand
from description_pipeline.pipeline.effects import EffectState

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "deploy" / "operator" / "reconcile-effect.py"
RUN_ID = "cfa3e725-abc5-5b60-94ad-6ba838110408"


def load_script():
    spec = importlib.util.spec_from_file_location("reconcile_effect_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def command(stage: str, *, effect_key: str | None = None) -> StageCommand:
    request = {"stage": stage}
    if effect_key is not None:
        request["effect_key"] = effect_key
    return StageCommand.create(
        command_id=f"{stage}:" + digest({"stage": stage}),
        run_id=RUN_ID,
        stage=stage,
        idempotency_key=f"{RUN_ID}/{stage}",
        request=request,
        input_artifacts=(),
        config_snapshot_sha256=digest({"config": 1}),
        contract_sha256=digest({"contract": 1}),
        policy_sha256=digest({"policy": 1}),
        expected_executor_sha256=digest({"executor": stage}),
        predecessor_decision_id=None,
    )


class FakeFacts:
    def __init__(self, stage_command: StageCommand) -> None:
        self.stage_command = stage_command
        self.calls = []

    def load_stage_command(self, run_id: str, stage: str):
        self.calls.append((run_id, stage))
        return self.stage_command


class FakeApplication:
    def __init__(self, stage_command: StageCommand, *, reconciler=True) -> None:
        self.facts = FakeFacts(stage_command)
        if reconciler:
            self.calls = []
            self.reconcile_effect = self._reconcile_effect

    def _reconcile_effect(self, *, command_id: str, effect_key: str):
        self.calls.append((command_id, effect_key))
        return SimpleNamespace(
            effect_id="effect-" + command_id,
            effect_key=effect_key,
            settlement=SimpleNamespace(state=EffectState.SATISFIED),
            fact=SimpleNamespace(fact_id="fact-reconciled", fact_type="reconciled"),
        )


class ReconcileEffectCliTests(unittest.TestCase):
    def run_cli(self, module, stage_command: StageCommand, effect_key: str, arguments=()):
        application = FakeApplication(stage_command)
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            result = module.main(
                [
                    "--run-id",
                    RUN_ID,
                    "--stage",
                    stage_command.stage,
                    "--effect-key",
                    effect_key,
                    *arguments,
                ],
                application_factory=lambda: application,
            )
        return result, stdout, stderr, application

    def test_publish_git_effect_uses_the_application_registry(self) -> None:
        module = load_script()
        publish = command("publish")
        result, stdout, stderr, application = self.run_cli(
            module,
            publish,
            "git-ref-update",
        )
        self.assertEqual(result, 0, stderr.getvalue())
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["stage"], "publish")
        self.assertEqual(payload["effect_key"], "git-ref-update")
        self.assertEqual(payload["effect_state"], "satisfied")
        self.assertEqual(payload["fact_type"], "reconciled")
        self.assertEqual(application.facts.calls, [(RUN_ID, "publish")])
        self.assertEqual(application.calls, [(publish.command_id, "git-ref-update")])

    def test_discover_native_effect_is_not_a_special_case_in_the_operator(self) -> None:
        module = load_script()
        discover = command("discover")
        effect_key = f"native-discover:{discover.command_id}"
        result, stdout, stderr, application = self.run_cli(module, discover, effect_key)
        self.assertEqual(result, 0, stderr.getvalue())
        self.assertEqual(json.loads(stdout.getvalue())["effect_key"], effect_key)
        self.assertEqual(application.calls, [(discover.command_id, effect_key)])

    def test_application_reconciliation_capability_is_required(self) -> None:
        module = load_script()
        capture = command("capture")
        application = FakeApplication(capture, reconciler=False)
        with contextlib.redirect_stderr(io.StringIO()) as stderr:
            result = module.main(
                [
                    "--run-id",
                    RUN_ID,
                    "--stage",
                    "capture",
                    "--effect-key",
                    "native-capture",
                ],
                application_factory=lambda: application,
            )
        self.assertEqual(result, 1)
        self.assertIn("effect reconciler", stderr.getvalue())

    def test_entrypoint_has_no_parallel_registry_or_legacy_store_dependency(self) -> None:
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertNotIn("PlatformStore", source)
        self.assertNotIn("completed_native_capture_recovery", source)
        self.assertNotIn("reconciliation_adapters", source)
        self.assertNotIn("NativeOperationEffectObserver", source)

    def test_legacy_outcome_and_rebuild_inputs_are_not_accepted(self) -> None:
        module = load_script()
        legacy_arguments = (
            ["--outcome", "completed"],
            ["--evidence", "failure.json"],
            ["--job", "job.json"],
            ["--derived", "derived"],
            ["--archive", "archive.zip"],
            ["--operation-id", "operation"],
            ["--task-id", "discover"],
            ["--map-index", "-1"],
        )
        base = ["--run-id", RUN_ID, "--stage", "capture", "--effect-key", "native-capture"]
        for extras in legacy_arguments:
            with self.subTest(argument=extras[0]), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as raised:
                    module.parse_args(base + extras)
                self.assertEqual(raised.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
