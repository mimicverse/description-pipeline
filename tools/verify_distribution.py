"""Verify wheel records and source identity, then exercise its isolated installed CLI."""

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import io
import json
import subprocess
import sys
import tempfile
import tarfile
import stat
import zipfile
from pathlib import Path

from description_pipeline.io import (
    PipelineError,
    artifact_path_parts,
    digest,
    file_digest,
    inventory,
    read_data,
    write_json,
)


def safe_members(names):
    keys = []
    for name in names:
        artifact_path_parts(name.rstrip("/"))
        keys.append(name.rstrip("/").casefold())
    if len(keys) != len(set(keys)):
        raise PipelineError("Duplicate or case-colliding archive member")


def verify_release(directory, identity):
    """Check the complete release boundary, including every non-wheel artifact."""
    release = read_data(directory / "release.json")
    checksums = read_data(directory / "SHA256SUMS.json")
    if inventory(directory, exclude=("SHA256SUMS.json",)) != checksums:
        raise PipelineError("Release assets differ from SHA256SUMS.json")
    artifacts = {name: checksum for name, checksum in checksums.items() if name != "release.json"}
    if (
        release.get("artifacts") != artifacts
        or {key: value for key, value in release.items() if key != "artifacts"} != identity
    ):
        raise PipelineError("Release manifest does not bind the artifact set and wheel source identity")
    for name in artifacts:
        path = directory / name
        if name.endswith(".tar.gz"):
            with tarfile.open(path) as archive:
                members = archive.getmembers()
                safe_members(member.name for member in members)
                if any(not (member.isfile() or member.isdir()) for member in members):
                    raise PipelineError("Source archive contains a link or special file")
                prefix = name.removesuffix(".tar.gz") + "/src/description_pipeline/"
                files = {
                    member.name.removeprefix(prefix): hashlib.sha256(archive.extractfile(member).read()).hexdigest()
                    for member in members
                    if member.isfile()
                    and member.name.startswith(prefix)
                    and member.name != prefix + "tool-release.json"
                }
                stream = archive.extractfile(prefix + "tool-release.json")
                if stream is None or json.load(stream) != identity or files != identity["package_files"]:
                    raise PipelineError("Source archive does not contain the wheel's exact package identity")
        elif name.endswith(".zip"):
            with zipfile.ZipFile(path) as archive:
                safe_members(archive.namelist())
                if any(stat.S_ISLNK(info.external_attr >> 16) for info in archive.infolist()):
                    raise PipelineError("Release ZIP contains a symlink")
                if "files.json" in archive.namelist():
                    expected = json.loads(archive.read("files.json"))
                    actual = {
                        name: hashlib.sha256(archive.read(name)).hexdigest()
                        for name in archive.namelist()
                        if name != "files.json"
                    }
                    if expected != actual:
                        raise PipelineError("Offline ZIP differs from its complete file manifest")


def wheel_identity(wheel):
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        safe_members(names)
        if any(stat.S_ISLNK(info.external_attr >> 16) for info in archive.infolist()):
            raise PipelineError("Wheel contains a symlink")
        (record,) = (name for name in names if name.endswith(".dist-info/RECORD"))
        rows = list(csv.reader(io.StringIO(archive.read(record).decode())))
        if {row[0] for row in rows} != set(names):
            raise PipelineError("Wheel RECORD does not cover the complete archive")
        for name, checksum, size in rows:
            if name == record:
                if checksum or size:
                    raise PipelineError("RECORD must not hash itself")
                continue
            payload = archive.read(name)
            encoded = base64.urlsafe_b64encode(hashlib.sha256(payload).digest()).rstrip(b"=").decode()
            if checksum != "sha256=" + encoded or size != str(len(payload)):
                raise PipelineError(f"Wheel RECORD mismatch: {name}")
        prefix = "description_pipeline/"
        release = prefix + "tool-release.json"
        identity = json.loads(archive.read(release))
        files = {
            name.removeprefix(prefix): hashlib.sha256(archive.read(name)).hexdigest()
            for name in names
            if name.startswith(prefix) and name != release
        }
        if identity["package_files"] != files or identity["source_sha256"] != digest(files):
            raise PipelineError("Wheel source files differ from embedded release identity")
        return identity


def verify(wheel, runtime_lock, *, wheelhouse=None, bundle=None):
    identity = wheel_identity(wheel)
    if (wheel.parent / "release.json").is_file():
        verify_release(wheel.parent, identity)
    with tempfile.TemporaryDirectory(prefix="description-installed-") as temporary:
        root = Path(temporary)
        subprocess.run([sys.executable, "-I", "-m", "venv", str(root / "venv")], check=True)
        python = root / "venv" / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
        install = [str(python), "-I", "-m", "pip", "install"]
        if wheelhouse:
            install += ["--no-index", "--find-links", str(wheelhouse)]
        subprocess.run([*install, "-r", str(runtime_lock)], check=True)
        subprocess.run([*install, "--no-deps", str(wheel)], check=True)
        code = (
            "import json;from description_pipeline.solidworks import tool_record,doctor;"
            "r=tool_record();d=doctor();print(json.dumps({'tool':r,'doctor':d}));"
            "raise SystemExit(0 if d['passed'] else 1)"
        )
        result = subprocess.run([str(python), "-I", "-c", code], cwd=root, text=True, capture_output=True, check=True)
        installed = json.loads(result.stdout)
        if installed["tool"]["release"] != identity:
            raise PipelineError("Installed source identity differs from wheel")
        version = subprocess.check_output(
            [str(python), "-I", "-m", "description_pipeline.cli", "--version"], cwd=root, text=True
        ).strip()
        if version != "solidworks-to-urdf " + identity["version"]:
            raise PipelineError("Installed CLI version differs from wheel")
        checks = {"wheel_record": True, "source_identity": True, "installed_cli": True}
        if bundle:
            subprocess.run([str(python), "-I", "-m", "description_pipeline.cli", "check", str(bundle)], check=True)
            subprocess.run(
                [
                    str(python),
                    "-I",
                    "-m",
                    "description_pipeline.cli",
                    "rebuild",
                    str(bundle),
                    "--output",
                    str(root / "rebuilt"),
                ],
                check=True,
            )
            checks["native_bundle_replay"] = True
        return {
            "passed": True,
            "wheel_sha256": file_digest(wheel),
            "identity": identity,
            "runtime": installed["tool"]["runtime"],
            "checks": checks,
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wheel", type=Path)
    parser.add_argument("--runtime-lock", type=Path, required=True)
    parser.add_argument("--wheelhouse", type=Path)
    parser.add_argument("--bundle", type=Path, help="optional actual passing native bundle to check and rebuild")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    result = verify(args.wheel.resolve(), args.runtime_lock.resolve(), wheelhouse=args.wheelhouse, bundle=args.bundle)
    if args.report:
        write_json(args.report, result)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
