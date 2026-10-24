"""Canonical CAD domain binding: one official path across the endpoint,
standalone defaults and service restarts; divergent bindings and overrides
refuse rather than open a second domain (no CAD, no Windows)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from . import _paths  # noqa: F401
from description_pipeline.io import write_json  # noqa: E402
from description_pipeline.orchestration import windows  # noqa: E402
from description_pipeline.orchestration.native_operation_runtime import DISCOVER_KIND  # noqa: E402
from description_pipeline.schemas import TOOL_SCHEMA  # noqa: E402
from description_pipeline.sources.solidworks import isolation  # noqa: E402
from description_pipeline.sources.solidworks.errors import EnvironmentError_  # noqa: E402

_CHILD = """
import json
from description_pipeline.sources.solidworks import isolation
from description_pipeline.sources.solidworks.errors import EnvironmentError_

lease = isolation.domain_lease()
try:
    lease.acquire()
    print(json.dumps({"acquired": True, "lease": str(lease.path)}))
    lease.release()
except EnvironmentError_ as exc:
    print(json.dumps({"acquired": False, "code": exc.code, "lease": str(lease.path)}))
"""


def _clean_env(**extra):
    values = {
        key: value
        for key, value in os.environ.items()
        if key not in ("DESCRIPTION_CAD_OWNERSHIP_FILE", "DESCRIPTION_ENDPOINT_ROOT")
    }
    values.update(extra)
    return values


class CadDomainBindingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="cad-domain-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def _endpoint_config(self, state_root: Path, name: str = "endpoint.json") -> Path:
        """One strict, real endpoint configuration; the canonical parser rejects anything weaker."""

        support = self.root / "installed"
        (support / "packages").mkdir(parents=True, exist_ok=True)
        (support / "outputs").mkdir(parents=True, exist_ok=True)
        token = support / "token"
        token.write_text("t" * 64, encoding="utf-8")
        path = self.root / name
        write_json(
            path,
            {
                "schema_version": "solidworks-to-urdf.endpoint/v1",
                "package_root": str(support / "packages"),
                "output_root": str(support / "outputs"),
                "state_root": str(state_root),
                "token_file": str(token),
            },
        )
        return path

    def _standalone(self, env):
        completed = subprocess.run(
            [sys.executable, "-c", _CHILD],
            env={**env, "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src")},
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return json.loads(completed.stdout)

    def test_operational_pin_is_the_canonical_domain(self) -> None:
        pinned = self.root / "pinned" / "cad-ownership.json"
        with patch.dict(os.environ, _clean_env(DESCRIPTION_CAD_OWNERSHIP_FILE=str(pinned)), clear=True):
            self.assertEqual(isolation._ownership_path(), pinned)
            self.assertEqual(isolation._lease_path(), Path(str(pinned) + ".lease"))

    def test_installed_endpoint_configuration_is_discovered(self) -> None:
        self._endpoint_config(self.root / "endpoint-state")
        state = (self.root / "endpoint-state").resolve()
        with patch.dict(os.environ, _clean_env(DESCRIPTION_ENDPOINT_ROOT=str(self.root)), clear=True):
            self.assertEqual(isolation._ownership_path(), state / "cad-ownership.json")
            self.assertEqual(isolation._lease_path(), Path(str(state / "cad-ownership.json") + ".lease"))

    def test_two_disagreeing_bindings_are_refused(self) -> None:
        pinned = self.root / "pinned" / "cad-ownership.json"
        self._endpoint_config(self.root / "endpoint-state")
        with patch.dict(
            os.environ,
            _clean_env(DESCRIPTION_CAD_OWNERSHIP_FILE=str(pinned), DESCRIPTION_ENDPOINT_ROOT=str(self.root)),
            clear=True,
        ):
            with self.assertRaises(EnvironmentError_) as caught:
                isolation._ownership_path()
            self.assertEqual(caught.exception.code, "cad_domain_override_refused")
        agreeing = (self.root / "endpoint-state").resolve() / "cad-ownership.json"
        with patch.dict(
            os.environ,
            _clean_env(DESCRIPTION_CAD_OWNERSHIP_FILE=str(agreeing), DESCRIPTION_ENDPOINT_ROOT=str(self.root)),
            clear=True,
        ):
            self.assertEqual(isolation._ownership_path(), agreeing)

    def test_a_divergent_override_is_refused_on_a_bound_host(self) -> None:
        pinned = self.root / "pinned" / "cad-ownership.json"
        other = self.root / "other" / "cad-ownership.json"
        with patch.dict(os.environ, _clean_env(DESCRIPTION_CAD_OWNERSHIP_FILE=str(pinned)), clear=True):
            self.assertEqual(isolation._ownership_path(pinned), pinned)
            with self.assertRaises(EnvironmentError_) as caught:
                isolation._ownership_path(other)
            self.assertEqual(caught.exception.code, "cad_domain_override_refused")
            self.assertEqual(caught.exception.detail.get("canonical"), str(pinned))

    def test_an_override_is_only_a_restatement_of_an_existing_binding(self) -> None:
        # Positive: with a binding, an equal override is accepted.
        pinned = self.root / "pinned" / "cad-ownership.json"
        with patch.dict(os.environ, _clean_env(DESCRIPTION_CAD_OWNERSHIP_FILE=str(pinned)), clear=True):
            self.assertEqual(isolation._ownership_path(pinned), pinned)
        # Negative: with no binding at all, an explicit path is refused - an
        # override can never create the first domain.
        empty = self.root / "no-endpoint"
        empty.mkdir()
        with (
            patch.dict(os.environ, _clean_env(DESCRIPTION_ENDPOINT_ROOT=str(empty)), clear=True),
            self.assertRaises(isolation.DomainUnbound),
        ):
            isolation._ownership_path(self.root / "actor-A" / "cad-ownership.json")

    def test_an_unbound_host_refuses_and_keeps_no_user_state_default(self) -> None:
        empty = self.root / "no-endpoint"
        empty.mkdir()
        with (
            patch.dict(os.environ, _clean_env(DESCRIPTION_ENDPOINT_ROOT=str(empty)), clear=True),
            self.assertRaises(isolation.DomainUnbound),
        ):
            isolation._ownership_path()
        with patch.dict(os.environ, _clean_env(), clear=True), self.assertRaises(isolation.DomainUnbound):
            # No binding at all also refuses: the old per-user state default is gone.
            isolation._ownership_path()

    def test_an_unbound_host_never_creates_a_state_root_domain(self) -> None:
        empty = self.root / "no-endpoint"
        empty.mkdir()
        with (
            patch.dict(os.environ, _clean_env(DESCRIPTION_ENDPOINT_ROOT=str(empty)), clear=True),
            self.assertRaises(isolation.DomainUnbound),
        ):
            windows._cad_ownership_file()

    def test_endpoint_and_standalone_default_share_one_domain(self) -> None:
        # Endpoint side: an operation runtime whose state root deliberately differs
        # from the CAD binding; standalone side: the default resolver in a child
        # process, exactly as a diagnostic or operator tool would resolve.
        self._endpoint_config(self.root / "endpoint-state")
        state = (self.root / "endpoint-state").resolve()
        (self.root / "packages").mkdir()
        (self.root / "outputs").mkdir()
        token = self.root / "token"
        token.write_text("t" * 64, encoding="utf-8")
        config_path = self.root / "here.json"
        write_json(
            config_path,
            {
                "schema_version": "solidworks-to-urdf.endpoint/v1",
                "package_root": str(self.root / "packages"),
                "output_root": str(self.root / "outputs"),
                "state_root": str(self.root / "jobs-state"),
                "token_file": str(token),
            },
        )
        env = _clean_env(DESCRIPTION_ENDPOINT_ROOT=str(self.root))
        with patch.dict(os.environ, env, clear=True):
            config = windows.read_config(config_path)
            tool = {
                "schema_version": TOOL_SCHEMA,
                "pipeline_id": "cad-domain-test",
                "source_sha256": "ab" * 32,
            }
            with patch.object(windows, "_native_tool_record", return_value=tool):
                runtime = windows.build_runtime(config)
            try:
                discover = runtime.executors[DISCOVER_KIND]
                self.assertEqual(discover.cad_ownership_file, state / "cad-ownership.json")
                self.assertNotEqual(
                    discover.cad_ownership_file,
                    config["state_root"] / "cad-ownership.json",
                )
                holder = isolation.domain_lease()
                holder.acquire()
                try:
                    refused = self._standalone(env)
                finally:
                    holder.release()
            finally:
                runtime.close()
        self.assertFalse(refused["acquired"], refused)
        self.assertEqual(refused["code"], "cad_domain_leased")
        self.assertEqual(Path(refused["lease"]), Path(str(state / "cad-ownership.json") + ".lease"))
        allowed = self._standalone(env)
        self.assertTrue(allowed["acquired"])
        self.assertEqual(Path(allowed["lease"]), Path(str(state / "cad-ownership.json") + ".lease"))


if __name__ == "__main__":
    unittest.main()
