"""Read-only canonical run projection tests."""

from __future__ import annotations

import unittest
from datetime import UTC, datetime

from description_pipeline.platform.queries import read_run_projection, render_stage_summary


class _Cursor:
    def __init__(self) -> None:
        self.statements: list[str] = []
        self.result = []
        self.closed = False

    def execute(self, statement: str, parameters) -> None:
        self.statements.append(statement)
        if "FROM run_admission" in statement:
            self.result = [
                (
                    "request-1",
                    None,
                    "freeze",
                    "a" * 64,
                    datetime(2026, 10, 25, tzinfo=UTC),
                )
            ]
        elif "FROM command" in statement:
            self.result = [("freeze-command", "freeze", "stage", "b" * 64, None)]
        else:
            self.result = []

    def fetchone(self):
        return self.result[0] if self.result else None

    def fetchall(self):
        return list(self.result)

    def close(self):
        self.closed = True


class _Connection:
    def __init__(self) -> None:
        self._cursor = _Cursor()

    def cursor(self):
        return self._cursor


class RunQueryTests(unittest.TestCase):
    def test_projection_is_read_only_and_ordered_by_contract(self):
        connection = _Connection()
        projection = read_run_projection(connection, "run-1")
        statements = " ".join(connection._cursor.statements).upper()
        self.assertNotIn("INSERT ", statements)
        self.assertNotIn("UPDATE ", statements)
        self.assertNotIn("DELETE ", statements)
        self.assertEqual(
            [row["stage"] for row in projection["stages"]],
            ["freeze", "discover", "capture", "generate", "verify", "publish"],
        )
        summary = render_stage_summary(projection)
        self.assertEqual(summary[0]["state"], "waiting")
        self.assertEqual(summary[0]["command_id"], "freeze-command")
        self.assertEqual([row["stage"] for row in summary[1:]], [
            "discover",
            "capture",
            "generate",
            "verify",
            "publish",
        ])


if __name__ == "__main__":
    unittest.main()
