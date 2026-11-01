"""Composition uses a test-installed resource and the existing private stores."""

import json
import unittest
from unittest import mock

from description_pipeline.dynamics_assessment import composition
from tests.v1.test_dynamics_assessment_contracts import AssessmentCase
from tests.v1.test_dynamics_assessment_native import build_native_case
from tests.v1.test_dynamics_assessment_postgres import PostgresCase


class CompositionTests(AssessmentCase):
    def test_disabled_factory_never_reads_inventory_or_opens_resources(self):

        def forbidden(*_args, **_kwargs):
            unittest.TestCase().fail("Disabled assessment must not compose resources")

        self.enterContext(mock.patch.object(composition, "files", forbidden))
        self.enterContext(mock.patch.object(composition, "tool_record", forbidden))
        self.enterContext(mock.patch.object(composition, "ArtifactStore", forbidden))
        assert composition.assessment_executor_from_environment({}) is None
        assert composition.assessment_executor_from_environment({"ASSESSMENT_CONFIG_JSON": '{"enabled":false}'}) is None


class CompositionDatabaseTests(PostgresCase):
    def test_enabled_factory_reuses_installed_inventory_and_existing_private_database(self):
        tmp_path = self.root
        case = build_native_case(self.database)
        service, process_owner, _access, *_ = case
        resources = tmp_path / "test-installed-resources"
        (resources / "simulation").mkdir(parents=True)
        (resources / "simulation/teleop-runtime.json").write_text(json.dumps(service.runtime.to_record()))
        self.enterContext(
            mock.patch.object(composition, "files", lambda name: resources if name == "description_pipeline" else None)
        )
        with service.store.connect() as connection:
            dsn = connection.dsn
        root = tmp_path / "private-executor"
        root.mkdir(mode=448)
        config = {
            "enabled": True,
            "root": str(root),
            "python": str(process_owner.process.python),
            "capacity": 2,
            "limits": {
                "wall_seconds": 120,
                "memory_bytes": 2 * 1024**3,
                "cpu_seconds": 60,
                "output_bytes": 256 * 1024**2,
            },
        }
        environment = {
            "ASSESSMENT_CONFIG_JSON": json.dumps(config),
            "PLATFORM_SERVICE_DSN": dsn,
            "PLATFORM_ARTIFACT_ROOT": str(service.store.artifacts.root),
        }
        executor = composition.assessment_executor_from_environment(environment)
        assert executor.service.runtime.sha256 == service.runtime.sha256
        assert executor.service.store.artifacts.root == service.store.artifacts.root
        assert executor.service.capacity == 2 and executor.service.input_limit == 64 * 1024**2
        with executor.store.transaction() as cursor:
            cursor.execute("SELECT count(*) FROM run_admission")
            assert cursor.fetchone()[0] == 1
        root.chmod(493)
        with self.assertRaisesRegex(ValueError, "private"):
            composition.assessment_executor_from_environment(environment)
