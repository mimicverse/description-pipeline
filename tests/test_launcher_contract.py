"""The Windows launchers and the tool share a JSON contract that only a static check can enforce.

``submit.ps1`` and ``worker.ps1`` parse the records the CLI and the worker produce; the PowerShell
suites replace those producers with doubles, so a renamed field would first show up on a real
machine, in the middle of a CAD session.  Each row names the field a launcher reads and the literal
the producer must contain, and two negative controls prove the check reports drift.
"""

from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEPLOY = "src/description_pipeline/sources/solidworks/deploy"

#: (what it carries, launcher, field the launcher reads, producer file, literal the producer must contain)
CONTRACT = (
    ("model update record", "submit.ps1", "record.message", "src/description_pipeline/cli.py", '"message":'),
    (
        "model update record",
        "submit.ps1",
        "record.error",
        "src/description_pipeline/cli.py",
        '"error": type(error).__name__',
    ),
    (
        "model update record",
        "submit.ps1",
        "record.pull_request",
        "src/description_pipeline/repository/__init__.py",
        '"pull_request"',
    ),
    ("model update record", "submit.ps1", "record.ok", "src/description_pipeline/repository/__init__.py", '"ok":'),
    (
        "model update record",
        "submit.ps1",
        "record.passed",
        "src/description_pipeline/repository/__init__.py",
        '"passed":',
    ),
    (
        "model update record",
        "submit.ps1",
        "record.central_validation.state",
        "src/description_pipeline/repository/__init__.py",
        '"central_validation"',
    ),
    (
        "model update record",
        "submit.ps1",
        "record.central_validation.retry",
        "src/description_pipeline/repository/__init__.py",
        '"retry":',
    ),
    (
        "model update record",
        "submit.ps1",
        "record.diagnostic_path",
        "src/description_pipeline/cli.py",
        '"diagnostic_path"',
    ),
    (
        "worker /health",
        "worker.ps1",
        "health.worker_version",
        "src/description_pipeline/sources/solidworks/worker.py",
        '"worker_version"',
    ),
    (
        "worker /health",
        "worker.ps1",
        "health.maintenance",
        "src/description_pipeline/sources/solidworks/worker.py",
        '"maintenance"',
    ),
    (
        "worker /health",
        "worker.ps1",
        "health.runner.current / runner.queued",
        "src/description_pipeline/sources/solidworks/jobs.py",
        '{"current":',
    ),
    (
        "worker /health",
        "worker.ps1",
        "health.jobs.queued / jobs.running",
        "src/description_pipeline/sources/solidworks/jobs.py",
        '{"queued": len(queued), "running": len(running)',
    ),
    (
        "worker /health",
        "worker.ps1",
        "health.cad_operation_active",
        "src/description_pipeline/sources/solidworks/worker.py",
        '"cad_operation_active"',
    ),
    (
        "worker /health",
        "worker.ps1",
        "health.cad_recovery_required",
        "src/description_pipeline/sources/solidworks/worker.py",
        '"cad_recovery_required"',
    ),
    (
        "worker /doctor",
        "worker.ps1",
        "report.installed",
        "src/description_pipeline/sources/solidworks/doctor.py",
        '"installed"',
    ),
    (
        "worker /doctor",
        "worker.ps1",
        "report.worker_alive",
        "src/description_pipeline/sources/solidworks/doctor.py",
        '"worker_alive"',
    ),
    (
        "worker /doctor",
        "worker.ps1",
        "report.solidworks_reachable",
        "src/description_pipeline/sources/solidworks/doctor.py",
        '"solidworks_reachable"',
    ),
    (
        "worker /doctor",
        "worker.ps1",
        "report.cad_collectable",
        "src/description_pipeline/sources/solidworks/doctor.py",
        '"cad_collectable"',
    ),
    (
        "worker /doctor",
        "worker.ps1",
        "report.advisories",
        "src/description_pipeline/sources/solidworks/doctor.py",
        '"advisories"',
    ),
)


def check(rows: tuple[tuple[str, str, str, str, str], ...]) -> list[str]:
    problems: list[str] = []
    for _carries, launcher, field, producer, literal in rows:
        launcher_text = (ROOT / DEPLOY / launcher).read_text(encoding="utf-8")
        producer_text = (ROOT / producer).read_text(encoding="utf-8")
        if field.split(".")[-1] not in launcher_text:
            problems.append(f"{launcher} does not read {field} any more (contract row is stale)")
        if literal not in producer_text:
            problems.append(f"{producer} no longer produces {literal} for {launcher}:{field}")
    return problems


class LauncherContractTests(unittest.TestCase):
    def test_every_field_the_launchers_read_is_produced(self):
        self.assertGreaterEqual(len(CONTRACT), 18, "the contract covers both launchers")
        self.assertEqual(check(CONTRACT), [], "PowerShell 读的字段必须仍由工具产出")

    def test_drift_is_reported(self):
        controls = (
            (
                "renamed producer field",
                "submit.ps1",
                "record.message",
                "src/description_pipeline/cli.py",
                '"no_such_literal":',
            ),
            (
                "stale launcher row",
                "submit.ps1",
                "record.this_field_does_not_exist",
                "src/description_pipeline/cli.py",
                '"message":',
            ),
        )
        self.assertEqual(len(check(controls)), 2, "a drifted contract row must be reported")


if __name__ == "__main__":
    unittest.main()
