"""R17: one canonical encoding and one numeric write format (single owners in io.py)."""

from __future__ import annotations

import hashlib
import math
import unittest
from tempfile import TemporaryDirectory
from xml.etree import ElementTree as ET
from pathlib import Path

from description_pipeline.io import PipelineError, canonical, digest, format_number, read_data
from description_pipeline.sources.solidworks import jsonio


class CanonicalEncodingTests(unittest.TestCase):
    def test_compact_encoding_and_cross_rule_digest(self) -> None:
        payload = {"b": [1, 2.5, "x"], "a": {"nested": True}}
        self.assertEqual(canonical(payload), b'{"a":{"nested":true},"b":[1,2.5,"x"]}')
        self.assertEqual(digest(payload), hashlib.sha256(canonical(payload)).hexdigest())
        # The adapter-side rule and the pipeline rule are one owner: same object, same digest.
        self.assertEqual(digest(payload), jsonio.digest_json(payload))

    def test_nan_and_infinity_are_refused(self) -> None:
        for value in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                canonical({"value": value})

    def test_negative_zero_is_normalized(self) -> None:
        self.assertEqual(canonical({"v": -0.0}), canonical({"v": 0.0}))
        self.assertEqual(canonical({"v": -0.0}), b'{"v":0.0}')
        self.assertEqual(digest({"v": -0.0}), digest({"v": 0.0}))

    def test_parse_strictness_is_preserved(self) -> None:
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "record.json"
            path.write_bytes(b'{"a": 1, "a": 2}')
            with self.assertRaises(PipelineError):
                read_data(path)
            path.write_bytes(b'{"a": 1}')
            self.assertEqual(read_data(path), {"a": 1})

    def test_parse_rejects_non_finite_tokens_and_overflow(self) -> None:
        cases = (b'{"v": NaN}', b'{"v": Infinity}', b'{"v": -Infinity}', b'{"v": 1e999}', b'{"v": -1e999}')
        for payload in cases:
            with (
                self.subTest(payload=payload),
                TemporaryDirectory() as temporary,
                self.assertRaises(PipelineError) as raised,
            ):
                path = Path(temporary) / "record.json"
                path.write_bytes(payload)
                read_data(path)
            self.assertIn(str(path), str(raised.exception))


class NumericWriteFormatTests(unittest.TestCase):
    def test_shortest_round_trip_values(self) -> None:
        for value, token in ((0.1, "0.1"), (math.pi, "3.141592653589793"), (1e-12, "1e-12")):
            with self.subTest(value=value):
                self.assertEqual(format_number(value), token)
                self.assertEqual(float(format_number(value)), value)

    def test_large_and_small_values_keep_exact_binary64(self) -> None:
        for value in (1.7976931348623157e308, 5e-324, 2.5e-7, 123456789.12345679):
            with self.subTest(value=value):
                self.assertEqual(float(format_number(value)), value)

    def test_zero_is_unified(self) -> None:
        self.assertEqual(format_number(-0.0), "0")
        self.assertEqual(format_number(0.0), "0")

    def test_non_finite_values_are_refused(self) -> None:
        for value in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(value=value), self.assertRaises(PipelineError):
                format_number(value)

    def test_generated_urdf_fragment_reparses_value_exact(self) -> None:
        values = [0.1, math.pi, 1e-12, -0.0, 2.5, -3.75]
        xml = (
            '<robot name="m3.0"><joint name="hinge"><origin xyz="'
            + " ".join(format_number(value) for value in values)
            + '"/></joint></robot>'
        )
        parsed = ET.fromstring(xml)
        tokens = parsed.find("joint/origin").attrib["xyz"].split()
        self.assertEqual(tokens[3], "0")  # -0.0 renders the unified zero
        for token, value in zip(tokens, values, strict=True):
            self.assertEqual(float(token), value)


if __name__ == "__main__":
    unittest.main()
