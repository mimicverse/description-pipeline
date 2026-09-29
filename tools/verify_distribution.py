"""Verify offline installation and reconstruction outside the source checkout.

    python tools/verify_distribution.py dist/*-linux-*.zip --report verification.json
    python tools/verify_distribution.py dist/*-windows-*.zip --windows --report windows.json

The synthetic fixture checks distribution behavior; it grants no CAD or hardware qualification.
The Windows check inspects the archive and resolves its wheels without running Windows or COM.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path, PurePosixPath, PureWindowsPath
from xml.etree import ElementTree as ET


def fixture() -> dict:
    geometry = {"kind": "box", "size": [0.1, 0.08, 0.06], "xyz": [0, 0, 0], "rpy": [0, 0, 0]}
    inertia = [(0.08**2 + 0.06**2) / 12, 0, 0, (0.1**2 + 0.06**2) / 12, 0, (0.1**2 + 0.08**2) / 12]
    links = [
        {
            "id": name,
            "name": name,
            "inertial": {"mass": 1.0, "xyz": [0, 0, 0], "rpy": [0, 0, 0], "inertia": inertia},
            "visuals": [copy.deepcopy(geometry)],
            "collisions": [copy.deepcopy(geometry)],
            "provenance": {"source_entities": [name], "inertia_model": "uniform_density_visual"},
        }
        for name in ("base", "arm", "slider")
    ]
    joints = [
        {
            "id": name,
            "name": name,
            "type": kind,
            "parent": parent,
            "child": child,
            "xyz": [0.02, 0.01, 0.3],
            "rpy": [0.1, 0.2, 0.3],
            "axis": [0, 0, 1],
            "limits": {"lower": -0.1, "upper": 0.2, "effort": 2, "velocity": 3},
            "dynamics": {"damping": 0.01, "friction": 0},
            "provenance": {"reference": "synthetic_distribution_fixture"},
        }
        for name, kind, parent, child in (
            ("hinge", "revolute", "base", "arm"),
            ("slide", "prismatic", "arm", "slider"),
        )
    ]
    return {
        "schema_version": "description.scene/v1",
        "name": "robot",
        "units": "SI",
        "links": links,
        "joints": joints,
        "frames": [],
        "actuators": [],
        "sensors": [],
        "constraints": [],
        "control": {},
        "contact_excludes": [],
        "provenance": {"expected_entities": [link["id"] for link in links], "fixture": True},
    }


def unsafe_member(name: str) -> bool:
    """Whether an archive member could escape the directory it is extracted into.

    The archive names its members in its own namespace, so the check cannot use the host's rules:
    ``/tmp/x`` is absolute only under POSIX and ``C:/x`` only under Windows, and either bundle may be
    unpacked on the other system by hand.
    """

    path = PurePosixPath(name)
    return (
        path.is_absolute() or PureWindowsPath(name).is_absolute() or ".." in path.parts or "\\" in name or ":" in name
    )


def checked_members(archive: zipfile.ZipFile, *, windows: bool = False) -> list[str]:
    """The member names of ``archive``, after refusing duplicates and extraction escapes."""

    names = archive.namelist()
    if len(names) != len(set(names)):
        raise RuntimeError("Duplicate Windows archive entries" if windows else "Duplicate archive entries")
    for name in names:
        if unsafe_member(name):
            raise RuntimeError(f"Invalid archive member: {name}")
    return names


def identity_mismatch(expected: dict, installed: dict) -> str:
    """Name what differs between the packaged identity and the installed one.

    The identity pins the exact CPython patch version and the platform the bundle was built on, so
    the most common refusal is "this release is being verified with the wrong interpreter".  Printing
    two dictionaries leaves the reader to diff them by eye — measured on v0.3.21, whose Linux bundle
    pins CPython 3.12.10 while the verifier ran 3.12.14 — so name the fields, and say what to do.
    """

    fields = [field for field in sorted(set(expected) | set(installed)) if expected.get(field) != installed.get(field)]
    parts: list[str] = []
    for field in fields:
        if field == "dependencies":
            before, after = expected.get(field) or {}, installed.get(field) or {}
            missing = sorted(set(before) - set(after))
            extra = sorted(set(after) - set(before))
            changed = sorted(key for key in set(before) & set(after) if before[key] != after[key])
            parts.append(f"dependencies: missing {missing}, extra {extra}, changed {changed}")
        else:
            parts.append(f"{field} {expected.get(field)!r} packaged vs {installed.get(field)!r} installed")
    hint = ""
    if {"python", "platform"} & set(fields):
        platform = expected.get("platform") or {}
        hint = (
            f"; this bundle pins CPython {expected.get('python')} on {platform.get('system', '?')}"
            " — verify it with that exact interpreter"
        )
    return "Installed tool identity differs — " + "; ".join(parts) + hint


#: The delivery ledger's shape: the same header the shipped examples and the `model init` template use.
LEDGER_HEADER = "# Structural inspection order only; NOT controller, policy or hardware order.\n"


def write_joint_ledger(model: Path, scene: dict) -> list[str]:
    """Register the scene's movable joints, the step the first-use guide asks an author for.

    ``description check`` refuses a movable joint that is not registered (`URDF208`), exactly like
    ``tools/audit.py --policy strict``, so the smoke's author flow has to fill the template
    ``model init`` wrote before it builds.  Returns the names it registered.
    """

    names = [item["name"] for item in scene["joints"] if item["type"] != "fixed"]
    path = model / "config" / "joint_names.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        LEDGER_HEADER + "structural_joint_names:\n" + "".join(f"  - {name}\n" for name in names), encoding="utf-8"
    )
    return names


def smoke(root: Path) -> dict:
    # Imports execute only inside the newly installed environment.
    import description_pipeline
    from description_pipeline.build import tool_identity
    from description_pipeline.io import file_digest, read_data, write_json
    from description_pipeline.sources.snapshot import write_manifest
    from description_pipeline.sources.solidworks.deploy import export_deploy_resources

    package = Path(description_pipeline.__file__).resolve()
    if not package.is_relative_to(Path(sys.prefix).resolve()):
        raise RuntimeError(f"Smoke test imported a package outside its environment: {package}")
    expected = read_data(root / "toolchain.json")
    identity = tool_identity()
    if expected != identity:
        raise RuntimeError(identity_mismatch(expected, identity))
    bundle = root / "bundle"
    launchers = {}
    for name in ("install.sh", "submit.sh"):
        launcher = bundle / name
        if not launcher.is_file():
            raise RuntimeError(f"Linux bundle is missing {name}")
        syntax = subprocess.run(["bash", "-n", str(launcher)], capture_output=True, text=True, encoding="utf-8")
        if syntax.returncode:
            raise RuntimeError(f"Invalid {name}: {syntax.stderr}")
        launchers[name] = str(launcher)
    exported = export_deploy_resources(root / "windows-resources")
    source = root / "raw"
    source.mkdir()
    scene = fixture()
    write_json(source / "scene.json", scene)
    write_manifest(source, kind="fixture", identity={"case": "offline-distribution"}, evidence_class="fixture")
    write_json(root / "source.json", {"provider": "fixture", "path": str(source)})

    def command(*args: str, passed: bool = True) -> dict:
        execution = subprocess.run(
            [sys.executable, "-I", "-m", "description_pipeline", *args],
            cwd=root,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        if (execution.returncode == 0) != passed:
            raise RuntimeError(f"CLI {args}: {execution.returncode}\n{execution.stdout}\n{execution.stderr}")
        return json.loads(execution.stdout or execution.stderr)

    model = root / "author-workspace"
    command(
        "model", "init", "--root", str(model), "--hardware", "fixture", "--source-config", str(root / "source.json")
    )
    # The pipeline refuses a movable joint that the delivery ledger does not register (URDF208),
    # exactly like `tools/audit.py --policy strict`, so the author flow fills the template
    # `model init` wrote before building — the same step the first-use guide asks an author for.
    write_joint_ledger(model, scene)
    command("source", "freeze", "--root", str(model))
    first = command("build", "--root", str(model))
    command("check", "--root", str(model))
    first_files = read_data(model / "manifest.json")["files"]
    relocated = root / "consumer-workspace"
    shutil.copytree(model, relocated, ignore=shutil.ignore_patterns("build"))
    shutil.rmtree(model)
    shutil.rmtree(source)
    second = command("build", "--root", str(relocated))
    if second["subject"] != first["subject"] or read_data(relocated / "manifest.json")["files"] != first_files:
        raise RuntimeError("Relocated offline reconstruction changed model identity or files")
    command("check", "--root", str(relocated))
    launcher_environment = dict(os.environ)
    # ``sys.executable`` may be a symlink to the host interpreter; ``sys.prefix``
    # remains the fresh virtual environment that the verifier just installed.
    launcher_environment["DESCRIPTION_VENV"] = sys.prefix
    submit_help = subprocess.run(
        ["bash", str(bundle / "submit.sh"), "--help"],
        cwd=bundle,
        env=launcher_environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if submit_help.returncode:
        raise RuntimeError(f"Linux submission launcher failed: {submit_help.stdout}{submit_help.stderr}")
    xml_path = relocated / "mjcf/robot.xml"
    original = xml_path.read_bytes()
    xml = ET.parse(xml_path)
    inertial = xml.find(".//inertial")
    if inertial is None:
        raise RuntimeError("Fixture has no actual consumer inertia")
    inertial.set("mass", "1.1")
    xml.write(xml_path)
    rejected = command("check", "--root", str(relocated), passed=False)
    xml_path.write_bytes(original)
    command("check", "--root", str(relocated))
    return {
        "passed": True,
        "toolchain": identity,
        "installed_package": str(package),
        "evidence_class": "fixture",
        "subject": first["subject"],
        "subject_files": first_files,
        "source_removed_before_rebuild": True,
        "relocated_without_cache": True,
        "mutation_rejected": rejected["blockers"],
        "launchers": launchers,
        "checks": second["checks"],
        "exported_resources": {str(path.relative_to(root)): file_digest(path) for path in exported},
    }


def verify(bundle: Path) -> dict:
    with tempfile.TemporaryDirectory(prefix="description-distribution-") as temporary:
        root = Path(temporary)
        extracted = root / "bundle"
        with zipfile.ZipFile(bundle) as archive:
            checked_members(archive)
            archive.extractall(extracted)
        manifest = extracted / "files.json"
        if not manifest.is_file():
            raise RuntimeError("Bundle has no files.json manifest")
        expected = json.loads(manifest.read_text(encoding="utf-8"))
        actual = {
            path.relative_to(extracted).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in extracted.rglob("*")
            if path.is_file() and path != extracted / "files.json"
        }
        if actual != expected:
            raise RuntimeError("Offline bundle does not match its complete file manifest")
        venv = root / "environment"
        subprocess.run([sys.executable, "-I", "-m", "venv", str(venv)], check=True)
        python = venv / "bin/python"
        subprocess.run(
            [
                str(python),
                "-I",
                "-m",
                "pip",
                "install",
                "--no-index",
                "--require-hashes",
                "--find-links",
                str(extracted / "wheels"),
                "-r",
                str(extracted / "requirements.lock"),
            ],
            check=True,
        )
        runner = root / "installed-smoke.py"
        shutil.copyfile(__file__, runner)
        shutil.copyfile(extracted / "toolchain.json", root / "toolchain.json")
        completed = subprocess.run(
            [str(python), "-I", str(runner), "--smoke", str(root)],
            cwd=root,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        if completed.returncode:
            raise RuntimeError(completed.stdout + completed.stderr)
        return {
            "bundle": str(bundle.resolve()),
            "bundle_sha256": hashlib.sha256(bundle.read_bytes()).hexdigest(),
            **json.loads(completed.stdout),
        }


def verify_windows(bundle: Path) -> dict:
    from packaging.requirements import Requirement
    from packaging.utils import canonicalize_name

    with tempfile.TemporaryDirectory(prefix="description-windows-distribution-") as temporary:
        root = Path(temporary)
        with zipfile.ZipFile(bundle) as archive:
            checked_members(archive, windows=True)
            archive.extractall(root)
        package = root / "src/description_pipeline"
        release = package / "tool-release.json"
        if not release.is_file():
            raise RuntimeError("Windows bundle carries no src/description_pipeline/tool-release.json")
        identity = json.loads(release.read_text(encoding="utf-8"))
        hashes = {
            path.relative_to(package).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in package.rglob("*")
            if path.is_file()
            and "__pycache__" not in path.parts
            and path.name != "tool-release.json"
            and path.suffix not in {".pyc", ".pyo"}
        }
        digest = hashlib.sha256(
            (json.dumps(hashes, sort_keys=True, ensure_ascii=False, allow_nan=False, indent=2) + "\n").encode()
        ).hexdigest()
        version = json.loads((root / "version.json").read_text(encoding="utf-8"))["version"]
        if digest != identity["package_digest"] or version != identity["version"] or identity["development"]:
            raise RuntimeError("Windows archive does not match its committed tool identity")
        resources = package / "sources/solidworks/deploy"
        for name in ("worker.ps1", "worker-host.example.json", "submit.ps1", "submit-host.example.json"):
            if (root / name).read_bytes() != (resources / name).read_bytes():
                raise RuntimeError(f"Windows launcher differs from its package resource: {name}")
        requirements = root / "requirements.txt"
        declared = {}
        for line in requirements.read_text(encoding="utf-8").splitlines():
            if not line.strip() or line.startswith("#"):
                continue
            spec, separator, checksum = line.partition(" --hash=sha256:")
            if not separator or len(checksum) != 64:
                raise RuntimeError("Every Windows dependency must be hash-locked")
            item = Requirement(spec)
            name = canonicalize_name(item.name)
            if name in declared:
                raise RuntimeError(f"Duplicate Windows requirement: {name}")
            declared[name] = str(item.specifier)
        locked = {}
        for line in (resources / "requirements/win-py312.lock").read_text(encoding="utf-8").splitlines():
            if line.strip() and not line.startswith("#"):
                item = Requirement(line)
                locked[canonicalize_name(item.name)] = str(item.specifier)
        if declared != locked:
            raise RuntimeError("Windows archive dependencies differ from its packaged lock")
        # The same runtime also runs the public pipeline locally (single-machine entry), so
        # the archive must carry the consumer dependencies and not only the worker's.
        required_runtime = {"mujoco", "pywin32", "numpy", "pyyaml", "jsonschema", "packaging"}
        missing_runtime = sorted(name for name in required_runtime if name not in locked)
        if missing_runtime:
            raise RuntimeError(f"Windows runtime lacks local pipeline dependencies: {missing_runtime}")
        report = root / "resolution.json"
        subprocess.run(
            [
                sys.executable,
                "-I",
                "-m",
                "pip",
                "install",
                "--dry-run",
                "--ignore-installed",
                "--no-index",
                "--require-hashes",
                "--find-links",
                str(root / "wheels"),
                "--platform",
                "win_amd64",
                "--python-version",
                "3.12",
                "--implementation",
                "cp",
                "--abi",
                "cp312",
                "--only-binary=:all:",
                "--report",
                str(report),
                "-r",
                str(requirements),
            ],
            check=True,
        )
        resolved = json.loads(report.read_text(encoding="utf-8"))["install"]
        if len(resolved) != len(locked) or len(list((root / "wheels").glob("*.whl"))) != len(locked):
            raise RuntimeError("Windows dependency closure is incomplete or has unexpected wheels")
        return {
            "passed": True,
            "bundle": str(bundle.resolve()),
            "bundle_sha256": hashlib.sha256(bundle.read_bytes()).hexdigest(),
            "method": "archive_and_windows_dependency_resolution",
            "native_windows_installation": "not_executed_by_this_check",
            "source_commit": identity["source_commit"],
            "package_digest": digest,
            "wheel_count": len(resolved),
        }


def _pin_utf8_streams() -> None:
    """Windows encodes redirected streams with the ANSI code page; callers read UTF-8."""

    for stream in (sys.stdin, sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8")


def main() -> None:
    _pin_utf8_streams()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=Path, nargs="?")
    parser.add_argument("--report", type=Path)
    parser.add_argument("--windows", action="store_true", help="check a Windows archive without native execution")
    parser.add_argument("--smoke", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.smoke:
        print(json.dumps(smoke(args.smoke)))
        return
    if args.bundle is None:
        parser.error("bundle is required")
    result = verify_windows(args.bundle) if args.windows else verify(args.bundle)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: result[key] for key in ("passed", "bundle", "bundle_sha256")}, indent=2))


if __name__ == "__main__":
    main()
