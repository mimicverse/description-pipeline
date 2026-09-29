import tempfile
import unittest
from pathlib import Path

from tools.solidworks_export.backends import FakeBackend
from tools.solidworks_export.config import config_from_dict
from tools.solidworks_export.errors import UsageError
from tools.solidworks_export.exporter import run_export
from tests.solidworks_export.helpers import make_config_dict


class LegacyBoundaryTests(unittest.TestCase):
    def test_retired_native_export_cannot_create_a_second_production_model(self):
        with tempfile.TemporaryDirectory() as temporary:
            backend = FakeBackend()
            backend.name = "solidworks"
            output = Path(temporary) / "out"
            with self.assertRaisesRegex(UsageError, "retired"):
                run_export(backend, config_from_dict(make_config_dict()), str(output), "unused.SLDASM")
            self.assertFalse(output.exists())
