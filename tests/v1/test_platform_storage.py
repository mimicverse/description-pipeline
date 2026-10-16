"""Sealed locator resolution: exactly one run and stage, confined root, no symlinks."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from description_pipeline.io import PipelineError
from description_pipeline.platform.storage import (
    parse_sealed_uri,
    sealed_resolver,
    sealed_resolver_from_environment,
)


class SealedResolverTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_resolves_exactly_one_run_and_stage_under_the_root(self) -> None:
        resolver = sealed_resolver(self.root)
        self.assertEqual(parse_sealed_uri("store://sealed/run-1/generate"), ("run-1", "generate"))
        self.assertEqual(resolver("store://sealed/run-1/generate"), self.root / "sealed" / "run-1" / "generate")

    def test_rejects_other_shapes_and_escapes(self) -> None:
        resolver = sealed_resolver(self.root)
        for uri in (
            "store://other/run-1/generate",
            "store://sealed/run-1",
            "store://sealed/run-1/generate/extra",
            "store://sealed/run-1/not-a-stage",
            "store://sealed/../etc/passwd",
            "store://sealed/run-1/../../etc",
            "store://sealed//generate",
        ):
            with self.subTest(uri=uri), self.assertRaises(PipelineError):
                resolver(uri)

    def test_refuses_a_symbolic_link_component(self) -> None:
        outside = Path(self._tmp.name) / "outside"
        outside.mkdir()
        (self.root / "sealed").mkdir()
        (self.root / "sealed" / "run-1").symlink_to(outside)
        resolver = sealed_resolver(self.root)
        with self.assertRaisesRegex(PipelineError, "symbolic link"):
            resolver("store://sealed/run-1/generate")

    def test_environment_resolver_requires_the_configured_root(self) -> None:
        with self.assertRaisesRegex(PipelineError, "PIPELINE_STORE_ROOT"):
            sealed_resolver_from_environment({})
        resolver = sealed_resolver_from_environment({"PIPELINE_STORE_ROOT": str(self.root)})
        self.assertEqual(resolver("store://sealed/run-2/verify"), self.root / "sealed" / "run-2" / "verify")
