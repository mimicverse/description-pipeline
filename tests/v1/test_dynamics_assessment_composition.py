"""Composition uses a test-installed resource and the existing private stores."""

import json

import pytest

from description_pipeline.dynamics_assessment import composition
from tests.v1.test_dynamics_assessment_native import native_case  # noqa: F401
from tests.v1.test_dynamics_assessment_postgres import database, postgres_server  # noqa: F401


def test_disabled_factory_never_reads_inventory_or_opens_resources(monkeypatch):
    def forbidden(*_args, **_kwargs):
        pytest.fail("Disabled assessment must not compose resources")

    monkeypatch.setattr(composition, "files", forbidden)
    monkeypatch.setattr(composition, "tool_record", forbidden)
    monkeypatch.setattr(composition, "ArtifactStore", forbidden)
    assert composition.assessment_executor_from_environment({}) is None
    assert composition.assessment_executor_from_environment({"ASSESSMENT_CONFIG_JSON": '{"enabled":false}'}) is None


def test_enabled_factory_reuses_installed_inventory_and_existing_private_database(request, tmp_path, monkeypatch):
    case = request.getfixturevalue("native_case")
    service, process_owner, _access, *_ = case
    resources = tmp_path / "test-installed-resources"
    (resources / "simulation").mkdir(parents=True)
    (resources / "simulation/teleop-runtime.json").write_text(json.dumps(service.runtime.to_record()))
    monkeypatch.setattr(composition, "files", lambda name: resources if name == "description_pipeline" else None)
    with service.store.connect() as connection:
        dsn = connection.dsn
    root = tmp_path / "private-executor"
    root.mkdir(mode=0o700)
    config = {
        "enabled": True,
        "root": str(root),
        "python": str(process_owner.process.python),
        "capacity": 2,
        "limits": {"wall_seconds": 120, "memory_bytes": 2 * 1024**3, "cpu_seconds": 60, "output_bytes": 256 * 1024**2},
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
    root.chmod(0o755)
    with pytest.raises(ValueError, match="private"):
        composition.assessment_executor_from_environment(environment)
