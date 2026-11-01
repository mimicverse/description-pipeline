"""Verify wheel records and source identity, then exercise its isolated installed CLI."""

from __future__ import annotations

import argparse
import ast
import base64
import csv
import hashlib
import io
import json
import re
import subprocess
import sys
import tempfile
import tarfile
import stat
import zipfile
from pathlib import Path

from packaging.utils import canonicalize_name, parse_wheel_filename

from description_pipeline.cli import CLI_VERSION_PREFIX
from description_pipeline.io import (
    PipelineError,
    artifact_path_parts,
    digest,
    file_digest,
    inventory,
    read_data,
    write_json,
)
from description_pipeline.interface_records import release_domain_identity
from description_pipeline.runtime import LINUX_PLATFORMS, TARGET_ABI, TARGET_PYTHON, WINDOWS_PLATFORMS
from description_pipeline.simulation.schemas import load_runtime_contract

if __package__:
    from .release_consumers import CONSUMER_LOCKS, CONSUMER_POOL_PREFIX, target_tags, verify_consumer_pool
else:
    from release_consumers import CONSUMER_LOCKS, CONSUMER_POOL_PREFIX, target_tags, verify_consumer_pool


def safe_members(names):
    keys = []
    for name in names:
        clean = name.rstrip("/")
        parts = artifact_path_parts(clean)
        canonical = "/".join(parts)
        if canonical != clean:
            raise PipelineError(f"Archive member is not in canonical form: {name}")
        keys.append(canonical.casefold())
    if len(keys) != len(set(keys)):
        raise PipelineError("Duplicate or case-colliding archive member")


def validate_deployment_boundary(members: dict[str, bytes]) -> None:
    """Deployment output must not import test-only modules."""

    references = []
    for name, payload in sorted(members.items()):
        if not name.endswith(".py"):
            continue
        tree = ast.parse(payload.decode("utf-8"), filename=name)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                references.extend(
                    f"{name}:{alias.name}"
                    for alias in node.names
                    if alias.name == "tests" or alias.name.startswith("tests.")
                )
            elif (
                isinstance(node, ast.ImportFrom)
                and node.module is not None
                and (node.module == "tests" or node.module.startswith("tests."))
            ):
                references.append(f"{name}:{node.module}")
    if references:
        raise PipelineError(f"Deployment imports test-only modules: {sorted(references)}")


def _locked_pins(data: bytes) -> dict:
    pins = {}
    for line in data.decode("utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        name, _, version = line.partition("==")
        if not version:
            raise PipelineError(f"Unparsable dependency lock line: {line}")
        key = canonicalize_name(name)
        if key in pins:
            raise PipelineError(f"Dependency lock repeats {key}")
        pins[key] = version.strip().split()[0]
    return pins


def _target_tags(system: str) -> set:
    """The installable tag set of the archive's target (CPython 3.12, x86_64).

    Built with ``packaging.tags`` so interpreter/ABI/platform combinations stay
    exactly those a real installer accepts - including the older-CPython abi3
    ladder and the generic ``any`` platform for pure wheels - and bound to the
    single release target rule (interpreter/ABI/glibc baseline) the support
    matrix and the offline builder consume.
    """

    platforms = WINDOWS_PLATFORMS if system == "windows" else LINUX_PLATFORMS
    return target_tags(platforms, TARGET_PYTHON, TARGET_ABI)


def verify_release(directory, identity):
    """Check the complete release boundary: release.json alone binds every artifact."""
    if (directory / "SHA256SUMS.json").exists():
        raise PipelineError("a current release carries release.json only; SHA256SUMS.json is a legacy artifact")
    if not (directory / "release.json").is_file():
        raise PipelineError("a complete release carries its release.json manifest")
    release = read_data(directory / "release.json")
    artifacts = release.get("artifacts")
    if artifacts != inventory(directory, exclude=("release.json",)):
        raise PipelineError("Release assets differ from release.json")
    if {key: value for key, value in release.items() if key != "artifacts"} != identity:
        raise PipelineError("Release manifest does not bind the wheel source identity")
    if (
        not isinstance(identity.get("source_tree"), str)
        or re.fullmatch(r"[0-9a-f]{40}", identity["source_tree"]) is None
    ):
        raise PipelineError("Release manifest source_tree is malformed")
    domain = release_domain_identity()
    if identity.get("contract") != domain["contract"]:
        raise PipelineError("Release manifest does not bind the live schema/stage/policy digests")
    if identity.get("support") != domain["support"]:
        raise PipelineError("Release manifest does not bind the live support matrix")
    version = identity.get("version")
    wheel_name = f"mimicverse_description-{version}-py3-none-any.whl"
    complete = (
        wheel_name,
        f"mimicverse_description-{version}.tar.gz",
        f"description-{version}-deployment.zip",
        f"description-{version}-linux-cp312-x86_64.zip",
        f"description-{version}-windows-cp312-x86_64.zip",
    )
    for name in complete:
        if name not in artifacts:
            raise PipelineError(f"Release is incomplete: {name} is missing")
    # The manifest closes over exactly the five canonical assets: an extra entry is refused even
    # when its digest in the manifest is correct (a seventh asset, manifest-bound or not, is a
    # release the verifier does not know).
    extras = sorted(set(artifacts) - set(complete))
    if extras:
        raise PipelineError(f"Release carries assets outside the canonical five: {extras}")
    deployment_name = f"description-{version}-deployment.zip"
    with zipfile.ZipFile(directory / deployment_name) as archive:
        safe_members(archive.namelist())
        if any(stat.S_ISLNK(info.external_attr >> 16) for info in archive.infolist()):
            raise PipelineError("Release ZIP contains a symlink")
        payloads = {member: archive.read(member) for member in archive.namelist()}
        members = {member: hashlib.sha256(payload).hexdigest() for member, payload in payloads.items()}
        if members != identity.get("deployment_files"):
            raise PipelineError("Deployment members do not come from this source release")
        validate_deployment_boundary(payloads)
        try:
            consumer_locks = {role: payloads[path] for role, path in CONSUMER_LOCKS.items()}
            for script in ("scripts/install_teleop_worker.py", "scripts/pico_pipeline_link.py"):
                if script not in payloads:
                    raise PipelineError("Deployment lacks its consumer installation scripts")
        except KeyError as error:
            raise PipelineError("Deployment lacks its source consumer locks") from error
        source_locks = {}
        for platform in ("linux", "windows"):
            lock = f"requirements/{platform}-py312.lock"
            if (identity.get("runtime_locks") or {}).get(platform) != members.get(lock):
                raise PipelineError(f"Deployment lock {lock} is not this release's lock")
            if platform == "linux":
                if "deploy/airflow/requirements.lock" not in payloads:
                    raise PipelineError("Linux release lacks its Airflow wheelhouse lock")
                source_locks[platform] = payloads["deploy/airflow/requirements.lock"]
            else:
                source_locks[platform] = archive.read(lock)
    with zipfile.ZipFile(directory / wheel_name) as archive:
        contract_name = "simulation/teleop-runtime.json"
        data = archive.read("description_pipeline/" + contract_name)
        if hashlib.sha256(data).hexdigest() != identity["package_files"].get(contract_name):
            raise PipelineError("Consumer contract differs from the product source inventory")
    with tempfile.TemporaryDirectory(prefix="description-consumer-contract-") as temporary:
        path = Path(temporary) / "teleop-runtime.json"
        path.write_bytes(data)
        contracts = {role: load_runtime_contract(path, consumer=role) for role in ("pico", "assessment")}
    product = canonicalize_name("mimicverse-description")
    roles = {
        f"description-{version}-linux-cp312-x86_64.zip": "linux",
        f"description-{version}-windows-cp312-x86_64.zip": "windows",
    }
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
                relative = {
                    member.name.removeprefix(name.removesuffix(".tar.gz") + "/"): hashlib.sha256(
                        archive.extractfile(member).read()
                    ).hexdigest()
                    for member in members
                    if member.isfile()
                }
                deployment = {
                    member: checksum
                    for member, checksum in relative.items()
                    if member.split("/", 1)[0] in {"deploy", "docs", "requirements", "scripts"}
                    or member in {"README.md", "LICENSE", "CODE_OF_CONDUCT.md", "CONTRIBUTING.md", "RELEASING.md"}
                }
                if deployment != identity.get("deployment_files"):
                    raise PipelineError("Deployment inputs in the source archive differ from the manifest")
                tests = {member: checksum for member, checksum in relative.items() if member.startswith("tests/")}
                if tests != identity.get("test_files"):
                    raise PipelineError("Source archive test files differ from the tracked test closure")
        elif name.endswith(".zip"):
            if name == deployment_name:
                continue
            with zipfile.ZipFile(path) as archive:
                safe_members(archive.namelist())
                if any(stat.S_ISLNK(info.external_attr >> 16) for info in archive.infolist()):
                    raise PipelineError("Release ZIP contains a symlink")
                if name in roles:
                    if "files.json" not in archive.namelist():
                        raise PipelineError(f"Role archive {name} lacks its complete file manifest")
                    expected = {**_locked_pins(source_locks[roles[name]]), product: version}
                    wheel_members = [
                        member
                        for member in archive.namelist()
                        if member.startswith("wheels/")
                        and member.endswith(".whl")
                        and len(artifact_path_parts(member)) == 2
                    ]
                    consumer_members = [
                        member
                        for member in archive.namelist()
                        if roles[name] == "linux"
                        and member.startswith(CONSUMER_POOL_PREFIX)
                        and member.endswith(".whl")
                        and len(artifact_path_parts(member)) == 3
                    ]
                    expected_members = {
                        *wheel_members,
                        *consumer_members,
                        "requirements.lock",
                        "README.md",
                        "files.json",
                    }
                    unexpected_members = sorted(set(archive.namelist()) - expected_members)
                    if unexpected_members:
                        raise PipelineError(f"Role archive {name} carries unexpected members: {unexpected_members}")
                    wheels = {}
                    for wheel_member in wheel_members:
                        parsed, wheel_version, _, tags = parse_wheel_filename(Path(wheel_member).name)
                        if not tags & _target_tags(roles[name]):
                            raise PipelineError(
                                f"Role archive {name} carries a wheel for another target: {wheel_member}"
                            )
                        wheels[canonicalize_name(parsed)] = (str(wheel_version), wheel_member)
                    if {key: value[0] for key, value in wheels.items()} != expected or len(wheel_members) != len(
                        expected
                    ):
                        raise PipelineError(f"Role archive {name} does not carry exactly its locked wheel set")
                    member = f"wheels/{wheel_name}"
                    if archive.namelist().count(member) != 1:
                        raise PipelineError(f"Role archive {name} does not carry the release wheel")
                    if hashlib.sha256(archive.read(member)).hexdigest() != artifacts[wheel_name]:
                        raise PipelineError(f"Role archive {name} carries another wheel than this release")
                    pins = {}
                    for line in archive.read("requirements.lock").decode("utf-8").splitlines():
                        line = line.split("#", 1)[0].strip()
                        if not line:
                            continue
                        spec, separator, checksum = line.partition(" --hash=sha256:")
                        pin_name, _, pin_version = spec.partition("==")
                        key = canonicalize_name(pin_name)
                        if (
                            not separator
                            or len(checksum.strip()) != 64
                            or any(character not in "0123456789abcdef" for character in checksum.strip())
                            or not pin_version.strip()
                        ):
                            raise PipelineError(f"Role archive {name} lock line is not an exact requirement: {line}")
                        if key in pins:
                            raise PipelineError(f"Role archive {name} lock repeats {key}")
                        pins[key] = (pin_version.strip(), checksum.strip())
                    if {key: value[0] for key, value in pins.items()} != expected or len(pins) != len(expected):
                        raise PipelineError(f"Role archive {name} lock does not pin exactly its locked wheel set")
                    for key, (_, checksum) in pins.items():
                        if checksum != hashlib.sha256(archive.read(wheels[key][1])).hexdigest():
                            raise PipelineError(f"Role archive {name} lock hash differs for {key}")
                    if roles[name] == "linux":
                        try:
                            verify_consumer_pool(
                                {
                                    member.removeprefix(CONSUMER_POOL_PREFIX): archive.read(member)
                                    for member in consumer_members
                                },
                                consumer_locks,
                                contracts,
                                _target_tags("linux"),
                            )
                        except ValueError as error:
                            raise PipelineError(str(error)) from error
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


def verify(wheel, runtime_lock, *, wheelhouse=None):
    identity = wheel_identity(wheel)
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
            "import json;from description_pipeline.runtime import tool_record;"
            "from description_pipeline.solidworks import doctor;"
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
        if version != f"{CLI_VERSION_PREFIX} {identity['version']}":
            raise PipelineError("Installed CLI version differs from wheel")
        checks = {"wheel_record": True, "source_identity": True, "installed_cli": True}
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
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    result = verify(args.wheel.resolve(), args.runtime_lock.resolve(), wheelhouse=args.wheelhouse)
    if args.report:
        write_json(args.report, result)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
