"""Native saved-configuration facts do not depend on rebuild flags."""

from __future__ import annotations

import unittest

from description_pipeline.sources.solidworks.discovery import (
    _saved_configuration_observed as source_configuration_observed,
)
from description_pipeline.verification.native_discovery import (
    _saved_configuration_observed as verifier_configuration_observed,
)


class NativeConfigurationFactsTest(unittest.TestCase):
    def test_rebuild_observations_do_not_qualify_configuration_identity(self):
        record = {
            "document_state": {
                "before": {
                    "read_only": True,
                    "configuration": "Default",
                    "needs_rebuild2_value": 3,
                    "configuration_needs_rebuild": True,
                },
                "after": {
                    "read_only": True,
                    "configuration": "Default",
                    "needs_rebuild2_value": 2,
                    "configuration_needs_rebuild": True,
                },
            },
            "source_selection": {"files": {"part.sldprt": "a" * 64}},
            "files": {"part.sldprt": "a" * 64},
        }
        self.assertTrue(source_configuration_observed(record, "Default"))
        self.assertTrue(verifier_configuration_observed(record, "Default"))

    def test_sealed_source_bytes_and_read_only_configuration_remain_required(self):
        record = {
            "document_state": {
                "before": {"read_only": True, "configuration": "Default"},
                "after": {"read_only": False, "configuration": "Default"},
            },
            "source_selection": {"files": {"part.sldprt": "a" * 64}},
            "files": {"part.sldprt": "b" * 64},
        }
        self.assertFalse(source_configuration_observed(record, "Default"))
        self.assertFalse(verifier_configuration_observed(record, "Default"))


if __name__ == "__main__":
    unittest.main()
