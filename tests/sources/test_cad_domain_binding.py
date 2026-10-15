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

    def test_an_unbound_host_never_hands_jobs_a_domain_of_their_own(self) -> None:
        # The old production fallback handed each endpoint-side Jobs its own
        # state-root domain; with the fallback gone, an unbound host refuses.
        empty = self.root / "no-endpoint"
        empty.mkdir()
        config_path = self._endpoint_config(self.root / "jobs-state", "job-config.json")
        with patch.dict(os.environ, _clean_env(DESCRIPTION_ENDPOINT_ROOT=str(empty)), clear=True):
            jobs = Jobs(read_config(config_path))
            try:
                with self.assertRaises(isolation.DomainUnbound):
                    _ = jobs.cad_ownership_file
            finally:
                jobs.close()

    def test_endpoint_and_standalone_default_share_one_domain(self) -> None:
        # Endpoint side: a Jobs instance whose state root deliberately differs
        # from the binding; standalone side: the default resolver in a child
        # process, exactly as a diagnostic or operator tool would resolve.
        self._endpoint_config(self.root / "endpoint-state")
        state = (self.root / "endpoint-state").resolve()
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
