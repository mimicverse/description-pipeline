"""Real-run closure proof for the native capture entry (PQ-06 / gpt R01 follow-up).

The production endpoint constructs ``JobStore`` without a runner, so its real
execution path is ``default runner -> solidworks.capture``.  This probe runs one
real capture through exactly that path in a fresh interpreter: the portable
domains (generation, verification, publication and the mechanical scene
builder) are evicted right before the call, and every import of them during the
call is recorded by a ``sys.meta_path`` finder with its importing file/line.

The finder sees absolute module names (relative imports included) and
importlib-driven imports; a positive self-control at the start proves it: a real
``from .model import Robot`` executed with a src-path frame and a dynamic
``importlib.import_module`` canary must both be recorded, the first attributed
to ``src/``.  During the real run only test-scaffolding origins are allowed; an
import originating in ``src/description_pipeline`` fails the proof.
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
import importlib
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

records = []
phase = {"name": "idle"}


def _forbidden(name):
    return any(name == item or name.startswith(item + ".") for item in FORBIDDEN)


def _origin():
    frame = sys._getframe(2)
    depth = 0
    while frame is not None and depth < 40:
        filename = frame.f_code.co_filename
        if not (
            "importlib" in filename
            or filename.startswith("<frozen")
            or filename in {"<string>", "<stdin>"}
        ):
            return filename, frame.f_lineno
        frame = frame.f_back
        depth += 1
    return "<unknown>", 0


class _ProbeFinder:
    def find_spec(self, fullname, path=None, target=None):
        if _forbidden(fullname):
            origin, line = _origin()
            records.append({"phase": phase["name"], "module": fullname, "origin": origin, "line": line})
        return None


sys.meta_path.insert(0, _ProbeFinder())

from tests.v1 import split_capture_fixture
from description_pipeline.orchestration import windows

state_root = Path(tempfile.mkdtemp(prefix="native-run-closure-"))
src_root = (Path.cwd() / "src").resolve()


def evict_forbidden():
    evicted = []
    for name in list(sys.modules):
        if _forbidden(name):
            del sys.modules[name]
            evicted.append(name)
    return sorted(evicted)


# ---- detector self-control: a real relative import executed with a src-path
# frame, then a dynamic importlib import.  Both must be recorded; the relative
# one must be attributed to src/.
phase["name"] = "canary"
evict_forbidden()
canary_file = str(src_root / "description_pipeline" / "probe_canary.py")
exec(
    compile("from .model import Robot\n", canary_file, "exec"),
    {"__package__": "description_pipeline", "__name__": "description_pipeline.probe_canary"},
)
evict_forbidden()
importlib.import_module("description_pipeline.model")
canary_records = [row for row in records if row["phase"] == "canary"]
canary_modules = sorted({row["module"] for row in canary_records})
canary_src = sorted(
    {row["module"] for row in canary_records if Path(row["origin"]).resolve().is_relative_to(src_root)}
)
if not canary_records:
    raise SystemExit("the import detector missed real imports entirely")
if not canary_src:
    raise SystemExit("the import detector failed to attribute the src-path relative import")

jobs = windows.Jobs({"state_root": state_root / "endpoint"})
captured = {}


def production_runner(*args, **kwargs):
    captured["evicted"] = evict_forbidden()
    start = len(records)
    phase["name"] = "run"
    result = jobs.runner(*args, **kwargs)
    phase["name"] = "idle"
    captured["records"] = records[start:]
    captured["after"] = sorted(name for name in list(sys.modules) if _forbidden(name))
    captured["receipt"] = {key: result.get(key) for key in ("state", "native_complete", "error")}
    return result


fixture = split_capture_fixture.build_fixture_capture(
    state_root / "fixture",
    run_id="7c9b44b3-0b9c-5ffe-9f4f-4bbdafb4a865",
    runner=production_runner,
)
run_records = captured.get("records", [])
offenders = [row for row in run_records if Path(row["origin"]).resolve().is_relative_to(src_root)]


def _explained(module_name):
    return any(
        module_name == row["module"]
        or module_name.startswith(row["module"] + ".")
        or row["module"].startswith(module_name + ".")
        for row in run_records
    )


unexplained = [name for name in captured.get("after", []) if not _explained(name)]
print(
    json.dumps(
        {
            "archive": fixture["archive"].is_file(),
            "canary_modules": canary_modules,
            "canary_src": canary_src,
            "captured": {key: value for key, value in captured.items() if key != "records"},
            "records": run_records,
            "offenders": offenders,
            "unexplained": unexplained,
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
        # Positive control: the detector is live (relative + dynamic imports recorded).
        self.assertIn("description_pipeline.model", report["canary_modules"], msg=completed.stdout)
        self.assertIn("description_pipeline.model", report["canary_src"], msg=completed.stdout)
        # The real run: no portable-domain import from src, and every remaining
        # forbidden module in sys.modules is explained by a recorded origin.
        self.assertEqual(report["offenders"], [], msg=json.dumps(report, indent=1, sort_keys=True))
        self.assertEqual(report["unexplained"], [], msg=json.dumps(report, indent=1, sort_keys=True))
        self.assertTrue(report["archive"])
        self.assertIs(report["captured"]["receipt"]["native_complete"], True, msg=completed.stdout)
        self.assertIsNone(report["captured"]["receipt"]["error"])
        self.assertTrue(report["captured"]["evicted"], msg=completed.stdout)


if __name__ == "__main__":
    unittest.main()
