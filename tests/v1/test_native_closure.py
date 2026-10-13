"""Real-run closure proof for the native capture entry (PQ-06 / gpt R01 follow-up).

The production endpoint constructs ``JobStore`` without a runner, so its real
execution path is ``default runner -> solidworks.capture``.  This probe runs one
real capture through exactly that path in a fresh interpreter: the portable
domains (generation, verification, publication and the mechanical scene
builder) are evicted right before the call, and every import of them during the
call is attributed to its importing file.  Test scaffolding may trigger such
imports (the fixture backend composes mass readings through the scene module);
an import originating in ``src/description_pipeline`` fails the proof.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

_PROBE = r"""
import builtins
import json
import sys
import tempfile
from pathlib import Path

FORBIDDEN = (
    "description_pipeline.verification",
    "description_pipeline.repository",
    "description_pipeline.backends",
    "description_pipeline.model",
    "description_pipeline.sources.solidworks.scene",
    "description_pipeline.sources.solidworks.verify",
)

recorded = []
_real_import = builtins.__import__


def _forbidden(name):
    return any(name == item or name.startswith(item + ".") for item in FORBIDDEN)


def probe_import(name, globals=None, locals=None, fromlist=(), level=0):
    if _forbidden(name):
        frame = sys._getframe(1)
        recorded.append({"module": name, "origin": frame.f_code.co_filename, "line": frame.f_lineno})
    return _real_import(name, globals, locals, fromlist, level)


builtins.__import__ = probe_import

from tests.v1 import split_capture_fixture
from description_pipeline.orchestration import windows

state_root = Path(tempfile.mkdtemp(prefix="native-run-closure-"))
jobs = windows.Jobs({"state_root": state_root / "endpoint"})


def evict_forbidden():
    evicted = []
    for name in list(sys.modules):
        if _forbidden(name):
            del sys.modules[name]
            evicted.append(name)
    return sorted(evicted)


captured = {}


def production_runner(*args, **kwargs):
    captured["evicted"] = evict_forbidden()
    result = jobs.runner(*args, **kwargs)
    captured["after"] = sorted(name for name in list(sys.modules) if _forbidden(name))
    captured["receipt"] = {key: result.get(key) for key in ("state", "native_complete", "error")}
    return result


fixture = split_capture_fixture.build_fixture_capture(
    state_root / "fixture",
    run_id="7c9b44b3-0b9c-5ffe-9f4f-4bbdafb4a865",
    runner=production_runner,
)
src_root = (Path.cwd() / "src").resolve()
offenders = [row for row in recorded if Path(row["origin"]).resolve().is_relative_to(src_root)]
print(
    json.dumps(
        {
            "archive": fixture["archive"].is_file(),
            "captured": captured,
            "recorded": recorded,
            "offenders": offenders,
        },
        indent=1,
        sort_keys=True,
    )
)
"""


class NativeRunClosureTests(unittest.TestCase):
    def test_a_real_capture_run_imports_no_portable_domain_from_the_pipeline(self):
        env = dict(os.environ)
        env["PYTHONPATH"] = f"{REPO_ROOT / 'src'}{os.pathsep}{REPO_ROOT}"
        completed = subprocess.run(
            [sys.executable, "-B", "-c", _PROBE],
            cwd=REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, msg=completed.stderr[-2000:])
        report = json.loads(completed.stdout)
        self.assertEqual(report["offenders"], [], msg=json.dumps(report, indent=1, sort_keys=True))
        self.assertTrue(report["archive"])
        self.assertIs(report["captured"]["receipt"]["native_complete"], True, msg=completed.stdout)
        self.assertIsNone(report["captured"]["receipt"]["error"])
        self.assertTrue(report["captured"]["evicted"], msg=completed.stdout)


if __name__ == "__main__":
    unittest.main()
