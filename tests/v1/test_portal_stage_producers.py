"""Consumer checks for the canonical projection's actual stage producer."""

import copy
import unittest

from description_pipeline.orchestration.portal import AirflowApi, PortalApp, PortalConfig, PortalError


def projection():
    artifact = {
        "artifact_id": "a" * 64, "kind": "stage_output:generate", "locator": "cas://artifact/" + "a" * 64,
        "manifest_sha256": "b" * 64, "set_sha256": "c" * 64,
        "producer_run_id": "original-run", "producer_stage": "generate", "producer_tool_sha256": "d" * 64,
    }
    return {
        "run_id": "current-run", "parent_run_id": "middle-run",
        "stages": {"generate": {
            "state": "completed", "in_scope": False,
            "reuse": {"reused": True, "source_run": "original-run", "source_command_id": "generate-original"},
            "command": {"command_id": "generate-original", "run_id": "original-run", "input_artifacts": []},
        }},
        "by_stage": {"generate": {"model/robot.json": {"artifact": artifact, "bytes": 2, "sha256": "e" * 64}}},
    }


class RegisteredProducerTests(unittest.TestCase):
    def setUp(self):
        self.app = PortalApp(PortalConfig(airflow=AirflowApi("http://127.0.0.1:8080")))

    def test_multi_hop_source_remains_the_original_producer(self):
        source = projection()
        before = copy.deepcopy(source)
        view = self.app._registered_stage_view(None, source)
        row = next(item for item in view["stages"] if item["id"] == "generate")
        self.assertEqual(row["state"], "completed")
        self.assertFalse(row["in_scope"])
        self.assertEqual(row["reuse"]["source_run"], "original-run")
        self.assertEqual(source, before)

    def test_reuse_must_bind_command_and_artifact(self):
        for target in ("command", "artifact", "missing-reuse", "wrong-command-id"):
            with self.subTest(target=target):
                source = projection()
                row = source["stages"]["generate"]
                if target == "command":
                    row["command"]["run_id"] = "another-run"
                elif target == "artifact":
                    source["by_stage"]["generate"]["model/robot.json"]["artifact"]["producer_run_id"] = "another-run"
                elif target == "missing-reuse":
                    del row["reuse"]
                else:
                    row["reuse"]["source_command_id"] = "another-command"
                with self.assertRaises(PortalError) as raised:
                    self.app._registered_stage_view(None, source)
                self.assertEqual(raised.exception.status, 502)
