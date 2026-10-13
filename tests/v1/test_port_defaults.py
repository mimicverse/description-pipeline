"""DEP-01: every deployment port has exactly one literal home per layer.

Pipeline code defaults live in :mod:`description_pipeline.ports`; deployment
defaults live in ``deploy/operator/render_operator.py`` and reach every
template, unit and script through the rendered ``resolved.env``.  Operator
documentation (``docs/`` and ``*.example*`` files) keeps the literal values
as the operator-facing authority and is not audited; comments count as
documentation too.
"""

from __future__ import annotations

import importlib.util
import re
import unittest
from pathlib import Path

from description_pipeline import ports

ROOT = Path(__file__).resolve().parents[2]
AUDITED = ("src/description_pipeline", "deploy", "tools")
CODE_OWNER = "src/description_pipeline/ports.py"
DEPLOY_OWNER = "deploy/operator/render_operator.py"
#: Windows-host candidate/promotion tooling mirrors the authored per-host endpoint
#: configuration (``deploy/windows/endpoint.example.json`` documents ``endpoint.json``);
#: only the pipeline-code endpoint default may appear there, any other port literal is stray.
WINDOWS_MIRROR = "deploy/windows"
LITERALS = ("8791", "18765", "5433", "8765", "8788", "8443", "8780")
PATTERN = re.compile(r"(?<![\w.])(" + "|".join(LITERALS) + r")(?![\w.])")


def _deploy_defaults() -> dict:
    spec = importlib.util.spec_from_file_location("deploy_render_operator", ROOT / DEPLOY_OWNER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.PORT_DEFAULTS


class PortDefaultsTests(unittest.TestCase):
    def test_no_stray_literals_in_the_audited_scopes(self):
        stray: dict[str, list[str]] = {}
        for scope in AUDITED:
            for path in sorted((ROOT / scope).rglob("*")):
                if not path.is_file():
                    continue
                relative = path.relative_to(ROOT).as_posix()
                if relative in {CODE_OWNER, DEPLOY_OWNER}:
                    continue
                if "example" in path.name or "vendor" in path.parts or path.suffix == ".pyc":
                    continue
                try:
                    text = path.read_text(encoding="utf-8")
                except UnicodeDecodeError:
                    continue
                for number, line in enumerate(text.splitlines(), 1):
                    if line.strip().startswith("#"):
                        continue
                    for match in PATTERN.finditer(line):
                        if relative.startswith(WINDOWS_MIRROR) and match.group(0) == str(
                            ports.SOLIDWORKS_ENDPOINT_PORT
                        ):
                            continue
                        stray.setdefault(relative, []).append(f"{number}:{match.group(0)}")
        self.assertEqual(stray, {}, "port literals must live only in the two layer owners")

    def test_layer_owners_hold_the_canonical_values(self):
        defaults = _deploy_defaults()
        self.assertEqual(
            defaults,
            {
                "OPERATOR_HTTPS_PORT": 8443,
                "OPERATOR_UPSTREAM_PORT": 8788,
                "AIRFLOW_API_PORT": 8791,
                "TUNNEL_LOOPBACK_PORT": 18765,
                "POSTGRES_PORT": 5433,
                "SOLIDWORKS_ENDPOINT_PORT": 8765,
            },
        )
        # The one shared value must agree across the code and deployment layers.
        self.assertEqual(defaults["SOLIDWORKS_ENDPOINT_PORT"], ports.SOLIDWORKS_ENDPOINT_PORT)
        self.assertEqual(ports.SOLIDWORKS_ENDPOINT_PORT, 8765)
        self.assertEqual(ports.PORTAL_HTTP_PORT, 8780)

    def test_templates_and_units_consume_placeholders(self):
        self.assertIn("@AIRFLOW_API_PORT@", (ROOT / "deploy/operator/nginx.conf.template").read_text())
        self.assertIn("@TUNNEL_LOOPBACK_PORT@", (ROOT / "deploy/operator/portal.json.template").read_text())
        for relative, token in (
            ("deploy/airflow/systemd/description-airflow-api-server.service", "@AIRFLOW_API_PORT@"),
            ("deploy/airflow/systemd/description-postgres.service", "@POSTGRES_PORT@"),
            ("deploy/airflow/systemd/description-solidworks-tunnel.service", "@TUNNEL_LOOPBACK_PORT@"),
        ):
            self.assertIn(token, (ROOT / relative).read_text())


if __name__ == "__main__":
    unittest.main()
