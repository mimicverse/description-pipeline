"""Install and verify a source-pinned offline candidate in this private audit directory."""
from pathlib import Path
import hashlib
import json
import subprocess
import sys
import zipfile

base = Path(__file__).resolve().parent
expected = json.loads((base / "candidate-identity.json").read_text(encoding="utf-8"))
archive = base / f"description-{expected['version']}-windows-cp312-x86_64.zip"
assert hashlib.sha256(archive.read_bytes()).hexdigest() == expected["windows_archive_sha256"]
offline = base / "offline"
runtime = base / "runtime"
assert not offline.exists() and not runtime.exists(), "Preserve previous audit installations"
with zipfile.ZipFile(archive) as source:
    source.extractall(offline)
files = json.loads((offline / "files.json").read_text())
for name, digest in files.items():
    assert hashlib.sha256((offline / name).read_bytes()).hexdigest() == digest, name
subprocess.run([sys.executable, "-I", "-m", "venv", str(runtime)], check=True)
python = runtime / "Scripts/python.exe"
installed = subprocess.run(
    [
        str(python), "-I", "-m", "pip", "install", "--no-index", "--require-hashes",
        "--find-links", str(offline / "wheels"), "-r", str(offline / "requirements.lock"),
    ],
    capture_output=True,
)
(base / "pip-install.log").write_bytes(installed.stdout + installed.stderr)
installed.check_returncode()
code = (
    "import importlib.util,json;from description_pipeline.runtime import tool_record;"
    "from description_pipeline.solidworks import doctor;"
    "print(json.dumps({'tool':tool_record(),'doctor':doctor(),"
    "'mujoco_absent':importlib.util.find_spec('mujoco') is None}))"
)
reply = subprocess.run([str(python), "-I", "-c", code], cwd=base, capture_output=True, check=True)
identity = json.loads(reply.stdout)
(base / "installed-windows.json").write_text(json.dumps(identity, indent=2) + "\n", encoding="utf-8")
assert identity["tool"]["version"] == expected["version"]
assert identity["tool"]["source_sha256"] == expected["source_sha256"]
assert identity["tool"]["release"]["source_commit"] == expected["source_commit"]
assert identity["doctor"]["passed"] and identity["mujoco_absent"]
wheel = offline / "wheels" / f"mimicverse_description-{expected['version']}-py3-none-any.whl"
with (base / "distribution-verify.log").open("wb") as log:
    subprocess.run(
        [
            str(python), "-I", str(base / "verify_distribution.py"), str(wheel),
            "--runtime-lock", str(base / "requirements-windows-py312.lock"),
            "--wheelhouse", str(offline / "wheels"),
            "--report", str(base / "windows-distribution-verification.json"),
        ],
        cwd=base, stdout=log, stderr=subprocess.STDOUT, check=True,
    )
verified = json.loads((base / "windows-distribution-verification.json").read_text())
assert verified["passed"] and verified["identity"] == identity["tool"]["release"]
print(
    json.dumps(
        {
            "passed": True,
            "version": expected["version"],
            "source_sha256": expected["source_sha256"],
            "mujoco_absent": True,
            "distribution_checks": verified["checks"],
        }
    )
)
