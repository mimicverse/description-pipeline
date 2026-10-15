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
from description_pipeline.orchestration.windows import Jobs, read_config  # noqa: E402
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

    def _endpoint_config(self, state_root: Path) -> None:
        write_json(self.root / "endpoint.json", {"schema_version": "x", "state_root": str(state_root)})

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
            self.assertEqual(isolation.cad_domain_path(), pinned)

    def test_installed_endpoint_configuration_is_discovered(self) -> None:
        state = self.root / "endpoint-state"
        self._endpoint_config(state)
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
        agreeing = self.root / "endpoint-state" / "cad-ownership.json"
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

    def test_an_unbound_host_refuses_and_keeps_no_user_state_default(self) -> None:
        empty = self.root / "no-endpoint"
        empty.mkdir()
        with patch.dict(os.environ, _clean_env(DESCRIPTION_ENDPOINT_ROOT=str(empty)), clear=True):
            with self.assertRaises(isolation.DomainUnbound):
                isolation._ownership_path()
            fallback = self.root / "dev" / "cad-ownership.json"
            self.assertEqual(isolation.cad_domain_path(fallback), fallback)

    def test_endpoint_and_standalone_default_share_one_domain(self) -> None:
        # Endpoint side: a Jobs instance whose state root deliberately differs
        # from the binding; standalone side: the default resolver in a child
        # process, exactly as a diagnostic or operator tool would resolve.
        state = self.root / "endpoint-state"
        self._endpoint_config(state)
        (self.root / "packages").mkdir()
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
            config = read_config(config_path)
            jobs = Jobs(config)
            try:
                self.assertEqual(jobs.cad_ownership_file, state / "cad-ownership.json")
                self.assertNotEqual(jobs.cad_ownership_file, Path(jobs.config["state_root"]) / "cad-ownership.json")
                holder = isolation.domain_lease()
                holder.acquire()
                try:
                    refused = self._standalone(env)
                finally:
                    holder.release()
            finally:
                jobs.close()
        self.assertFalse(refused["acquired"], refused)
        self.assertEqual(refused["code"], "cad_domain_leased")
        self.assertEqual(Path(refused["lease"]), Path(str(state / "cad-ownership.json") + ".lease"))
        allowed = self._standalone(env)
        self.assertTrue(allowed["acquired"])
        self.assertEqual(Path(allowed["lease"]), Path(str(state / "cad-ownership.json") + ".lease"))


if __name__ == "__main__":
    unittest.main()
