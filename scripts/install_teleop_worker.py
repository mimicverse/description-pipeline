"""Install an explicit offline Teleop consumer or lightweight PICO link."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import venv


def deployment_resource(root: Path, release: dict, relative: str) -> bytes:
    """Snapshot one member of the existing release deployment inventory."""
    path = root / relative
    if not path.is_file() or any(item.is_symlink() for item in (path, *path.parents) if item.is_relative_to(root)):
        raise ValueError("Deployment resource is missing or crosses a link: " + relative)
    payload = path.read_bytes()
    if hashlib.sha256(payload).hexdigest() != release["deployment_files"].get(relative):
        raise ValueError("Deployment resource differs from the release inventory: " + relative)
    return payload


def installed_product(release: dict):
    """Use the installed matching product; source development requires explicit PYTHONPATH."""
    from description_pipeline import runtime

    identity = runtime.tool_record(role="portable")["release"]
    if identity is None or identity != {key: value for key, value in release.items() if key != "artifacts"}:
        raise ValueError("Installed product and deployment do not belong to the same release")
    package = Path(runtime.__file__).resolve().parent
    return package, identity


def installed_consumer(package: Path, identity: dict, role: str, temporary: Path):
    """Project the package's canonical contract and reuse its installed native checker."""
    from description_pipeline.simulation import runtime as checker
    from description_pipeline.simulation.schemas import load_runtime_contract

    contract_path = package / "simulation/teleop-runtime.json"
    payload = contract_path.read_bytes()
    if hashlib.sha256(payload).hexdigest() != identity["package_files"].get("simulation/teleop-runtime.json"):
        raise ValueError("Installed consumer contract differs from its release inventory")
    snapshot = temporary / "teleop-runtime.json"
    snapshot.write_bytes(payload)
    contract = load_runtime_contract(snapshot, consumer=role)
    checker_path = Path(checker.__file__).resolve()
    if not checker_path.is_relative_to(package):
        raise ValueError("Consumer checker is outside the installed release")
    return contract, checker_path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--role", choices=("pico", "assessment", "pico-link"), required=True)
    parser.add_argument("--wheel", type=Path, help="Fixed Teleop wheel for pico or assessment")
    parser.add_argument("--actuator-wheel", type=Path, help="Fixed actuator-model wheel for assessment")
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--wheelhouse", type=Path, required=True, help="Extracted release consumer wheel pool")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    if sys.version_info[:2] != (3, 12) or sys.platform != "linux":
        parser.error("The private environments require Linux and CPython 3.12")
    if not args.destination.is_absolute() or args.destination.exists() or args.destination.is_symlink():
        parser.error("The destination must be a fresh absolute path")
    if not args.wheelhouse.is_dir() or args.wheelhouse.is_symlink():
        parser.error("The offline consumer wheel pool must be an ordinary directory")
    with tempfile.TemporaryDirectory(prefix="teleop-install-") as temporary:
        temporary = Path(temporary)
        try:
            release = json.loads((root / "release.json").read_bytes())
            if not isinstance(release, dict) or not isinstance(release.get("deployment_files"), dict):
                raise ValueError("The deployment has no release resource inventory")
            deployment_resource(root, release, "scripts/install_teleop_worker.py")
            package, identity = installed_product(release)
            lock_name = ("pico-link" if args.role == "pico-link" else "teleop-" + args.role) + "-py312.lock"
            lock_bytes = deployment_resource(root, release, "requirements/" + lock_name)
            assets = []
            if args.role in {"pico", "assessment"}:
                if args.wheel is None:
                    parser.error("The selected consumer requires --wheel")
                contract, checker_path = installed_consumer(package, identity, args.role, temporary)
                assets.append(
                    ("robot-teleop[evaluation]" if args.role == "assessment" else "robot-teleop", args.wheel, contract)
                )
                if args.role == "assessment":
                    if args.actuator_wheel is None:
                        parser.error("The assessment role requires --actuator-wheel")
                    assets.append(("actuator-model[mujoco]", args.actuator_wheel, contract["actuator_model"]))
                elif args.actuator_wheel is not None:
                    parser.error("The PICO role does not install actuator-model")
            elif args.wheel is not None or args.actuator_wheel is not None:
                parser.error("The PICO link role does not install model wheels")
            link_bytes = (
                deployment_resource(root, release, "scripts/pico_pipeline_link.py")
                if args.role == "pico-link"
                else None
            )
            snapshots = []
            for name, path, expected in assets:
                payload = path.read_bytes()
                if (
                    path.name != expected["wheel_filename"]
                    or hashlib.sha256(payload).hexdigest() != expected["wheel_sha256"]
                ):
                    raise ValueError("A supplied model wheel differs from the fixed consumer contract")
                snapshots.append((name, path.name, payload, expected["wheel_sha256"]))
        except (ImportError, OSError, ValueError, KeyError) as error:
            parser.error(str(error))
        lock = temporary / "consumer.lock"
        lock.write_bytes(lock_bytes)
        command = [
            "-m",
            "pip",
            "install",
            "--no-index",
            "--find-links",
            str(args.wheelhouse.resolve()),
            "--require-hashes",
            "--only-binary=:all:",
            "-r",
            str(lock),
        ]
        if snapshots:
            staging = temporary / "wheels"
            staging.mkdir()
            rows = []
            for name, filename, payload, sha in snapshots:
                path = staging / filename
                path.write_bytes(payload)
                rows.append(f"{name} @ {path.as_uri()} --hash=sha256:{sha}")
            models = temporary / "models.lock"
            models.write_text("\n".join(rows) + "\n")
            command += ["-r", str(models)]
        venv.EnvBuilder(with_pip=True).create(args.destination)
        python = args.destination / "bin/python"
        subprocess.run([str(python), *command], check=True)
        subprocess.run([str(python), "-m", "pip", "check"], check=True)
        if args.role == "pico-link":
            script = args.destination / "libexec/pico_pipeline_link.py"
            script.parent.mkdir()
            script.write_bytes(link_bytes)
            launcher = args.destination / "bin/pico-link"
            with launcher.open("x", encoding="utf-8") as stream:
                stream.write(f'#!/bin/sh\nexec {shlex.quote(str(python))} -I -B {shlex.quote(str(script))} "$@"\n')
            launcher.chmod(0o755)
            print(f"PICO link installed. Start a session with: {launcher} /path/to/pico-link-session.json")
        else:
            bootstrap = (
                "import json,runpy,sys;api=runpy.run_path(sys.argv[1]);"
                "print(json.dumps(api['verify_runtime_contract'](json.load(sys.stdin))))"
            )
            probe = subprocess.run(
                [str(python), "-I", "-B", "-c", bootstrap, str(checker_path)],
                input=json.dumps(contract),
                capture_output=True,
                text=True,
                check=True,
            )
            print(json.dumps({"role": args.role, "python": str(python), "runtime": json.loads(probe.stdout)}))


if __name__ == "__main__":
    main()
