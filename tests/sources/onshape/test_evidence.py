import tempfile
import unittest
from pathlib import Path

from description_pipeline.io import write_json
from description_pipeline.sources.onshape.evidence import NOT_BOUND, evidence_problems


class ExclusionIdentityTests(unittest.TestCase):
    def test_an_id_embedded_in_unrelated_text_is_not_entity_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_json(root / "raw/note.json", {"note": "not-the-arm-instance"})
            self.assertEqual(evidence_problems(root, "raw/note.json", "arm", "part", "studio"), [NOT_BOUND])

    def test_mass_reading_requires_the_correct_part_studio(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_json(root / "raw/mass_properties_other.json", {"bodies": {"part": {"mass": [1]}}})
            self.assertEqual(
                evidence_problems(root, "raw/mass_properties_other.json", "arm", "part", "studio"), [NOT_BOUND]
            )
            write_json(root / "raw/mass_properties_studio.json", {"bodies": {"part": {"mass": [1]}}})
            self.assertEqual(evidence_problems(root, "raw/mass_properties_studio.json", "arm", "part", "studio"), [])
