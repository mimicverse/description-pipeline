"""Exercise cold parent imports and late helper drift in a private release fixture."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

from description_pipeline import __version__
from description_pipeline.delivery import PIPELINE_ID
from description_pipeline.io import digest


ENTRY = r"""import asyncio,hashlib,json,sys
from pathlib import Path
from description_pipeline.simulation import recording
from description_pipeline.simulation import service as service_module
from description_pipeline.owned_process import resource_state
root,worker,kind=Path(sys.argv[1]),Path(sys.argv[2]),sys.argv[3]
marker=root/'HELPER-EXECUTED'
result={}
identities=[]
original_identity=service_module.process_identity
def observe_identity(pid,nonce):
    identity=original_identity(pid,nonce)
    identities.append(identity)
    return identity
service_module.process_identity=observe_identity
async def register(identity):
    identities.append(identity)
async def run():
    service=None
    try:
        service=service_module.SimulationService(None,None,root/'service-root',worker)
        result.update(constructed=True,release_inventory_bound=service._consumer_execution['release_inventory_bound'])
        if kind=='baseline' or kind=='cold_runtime':
            return
        name='runtime.py' if kind=='late_runtime' else 'verify_recording.py'
        helper=Path(recording.__file__).with_name(name)
        result['original_helper_sha256']=hashlib.sha256(helper.read_bytes()).hexdigest()
        result['expected_helper_sha256']=service._consumer_execution[
            'runtime_helper_sha256' if kind=='late_runtime' else 'helper_sha256']
        result['parent_runtime_imported_before']='description_pipeline.simulation.runtime' in sys.modules
        result['parent_recording_imported_before']='description_pipeline.simulation.verify_recording' in sys.modules
        helper.write_bytes(helper.read_bytes()+('\nPath('+repr(str(marker))+').write_text(\'executed\')\n').encode())
        if kind=='late_runtime':
            await service.start()
        else:
            await recording.run_verification({'execution':service._consumer_execution},worker,register_process=register)
        result['unexpected_success']=True
    except Exception as error:
        result['error']=str(error)
    finally:
        if service is not None:
            await service.close()
asyncio.run(run())
result.update(parent_runtime_imported_after='description_pipeline.simulation.runtime' in sys.modules,
              parent_recording_imported_after='description_pipeline.simulation.verify_recording' in sys.modules,
              marker_executed=marker.exists(),helpers=len(identities),
              process_identities=identities,
              helpers_dead=bool(identities) and all(resource_state(i)=='dead' for i in identities))
print(json.dumps(result))
"""


def main():
    config = json.load(sys.stdin)
    root = Path(config["root"])
    root.mkdir(mode=0o700)
    private = root / "private-release"
    package = private / "description_pipeline"
    shutil.copytree(config["package"], package, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    files = {
        path.relative_to(package).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(package.rglob("*"))
        if path.is_file() and path.name != "tool-release.json"
    }
    release = {
        "schema_version": "solidworks-to-urdf.release/v1",
        "pipeline_id": PIPELINE_ID,
        "version": __version__,
        "source_sha256": digest(files),
        "package_files": files,
    }
    identity = package / "tool-release.json"
    identity.write_text(json.dumps(release, sort_keys=True) + "\n")
    original_identity = identity.read_bytes()
    environment = {**os.environ, "PYTHONPATH": str(private), "PYTHONDONTWRITEBYTECODE": "1"}

    def execute(kind):
        run = subprocess.run(
            [sys.executable, "-B", "-c", ENTRY, str(root), config["worker"], kind],
            cwd=private,
            env=environment,
            capture_output=True,
            text=True,
            timeout=30,
        )
        (root / (kind + "-stdout.log")).write_text(run.stdout)
        (root / (kind + "-stderr.log")).write_text(run.stderr)
        if run.returncode:
            raise RuntimeError(run.stdout + run.stderr)
        return json.loads(run.stdout)

    baseline = execute("baseline")
    if not baseline.get("constructed") or not baseline.get("release_inventory_bound"):
        raise ValueError("The original private release did not construct")
    if config["kind"] == "cold_runtime":
        helper = package / "simulation/runtime.py"
        marker = root / "HELPER-EXECUTED"
        helper.write_bytes(helper.read_bytes() + f"\nPath({str(marker)!r}).write_text('executed')\n".encode())
    result = execute(config["kind"])
    result.update(
        kind=config["kind"],
        baseline=baseline,
        original_release_inventory_preserved=identity.read_bytes() == original_identity,
        inventory_sha256=hashlib.sha256(original_identity).hexdigest(),
        source_release_sha256=release["source_sha256"],
        inventory_expected_helper_sha256=files[
            "simulation/runtime.py" if config["kind"] != "late_recording" else "simulation/verify_recording.py"
        ],
        software_fixture=True,
    )
    (root / "RESULT.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
