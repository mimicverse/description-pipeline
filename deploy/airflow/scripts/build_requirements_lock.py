"""Turn a plain name==version pin list into a hash-pinned requirements lock.

Every pin must have a wheel for the target platform (--only-binary=:all:), so the
lock installs without build dependencies. The pin list is the name==version head of
an existing lock (or any file with one pin per line); hashes and the wheel filename
provenance comment are rewritten in place.

Usage:
  build_requirements_lock.py --python <venv>/bin/python
    --pins deploy/airflow/requirements.lock --output deploy/airflow/requirements.lock

Verification happens twice: every pin must resolve to exactly one downloaded wheel,
and "pip install --dry-run --require-hashes" must accept the rendered lock.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import subprocess
import sys
import tempfile
from pathlib import Path

PIN_RE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s;]+)")


def normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def read_pins(path: Path) -> list[tuple[str, str]]:
    pins: list[tuple[str, str]] = []
    seen: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        match = PIN_RE.match(line)
        if not match:
            continue
        name, version = match.group(1), match.group(2)
        key = normalize(name)
        if key in seen:
            raise SystemExit(f"duplicate pin for {name} in {path}")
        seen.add(key)
        pins.append((name, version))
    if not pins:
        raise SystemExit(f"no pins found in {path}")
    return pins


def wheel_dist_name(filename: str) -> str | None:
    if not filename.endswith(".whl"):
        return None
    parts = filename[: -len(".whl")].split("-")
    if len(parts) < 5:
        return None
    return normalize("-".join(parts[:-4]))


def download(python: str, pin: str, dest: Path) -> Path:
    before = set(dest.iterdir())
    result = subprocess.run(
        [python, "-m", "pip", "download", "--no-deps", "--only-binary=:all:", "--dest", str(dest), pin],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise SystemExit(f"pip download failed for {pin}:\n{result.stdout}\n{result.stderr}")
    new = [path for path in dest.iterdir() if path not in before]
    if len(new) != 1 or new[0].suffix != ".whl":
        raise SystemExit(f"{pin} did not resolve to exactly one wheel: {[p.name for p in new]}")
    return new[0]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--python", required=True, help="python of the tested environment (its pip is used)")
    parser.add_argument("--pins", required=True, help="file whose name==version lines are the pin list")
    parser.add_argument("--output", required=True)
    parser.add_argument("--header", default="Hash-pinned resolved stack for Linux CPython 3.12 (tested Airflow 3.3.2).")
    args = parser.parse_args()
    pins = read_pins(Path(args.pins))
    with tempfile.TemporaryDirectory(prefix="airflow-lock-") as tmp:
        dest = Path(tmp)
        entries: list[tuple[str, str, str, str, str]] = []
        for name, version in pins:
            wheel = download(args.python, f"{name}=={version}", dest)
            digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
            if wheel_dist_name(wheel.name) != normalize(name):
                raise SystemExit(f"{name}=={version} downloaded unexpected wheel {wheel.name}")
            entries.append((name, version, digest, wheel.name, normalize(name)))
        entries.sort(key=lambda item: item[4])
        lines = [
            f"# {args.header}",
            f"# {len(entries)} pins; regenerate with scripts/build_requirements_lock.py "
            f"--python <venv>/bin/python --pins requirements.lock --output requirements.lock",
            "# Install with: pip install --require-hashes --only-binary=:all: -r requirements.lock",
        ]
        lines += [f"{name}=={version} --hash=sha256:{digest}  # {wheel}" for name, version, digest, wheel, _ in entries]
        rendered = "\n".join(lines) + "\n"
        candidate = dest / "requirements.lock.candidate"
        candidate.write_text(rendered, encoding="utf-8")
        verify = subprocess.run(
            [
                args.python,
                "-m",
                "pip",
                "install",
                "--dry-run",
                "--require-hashes",
                "--only-binary=:all:",
                "--ignore-installed",
                "-r",
                str(candidate),
            ],
            capture_output=True,
            text=True,
        )
        if verify.returncode != 0:
            sys.stderr.write(verify.stdout + verify.stderr)
            raise SystemExit("rendered lock failed pip --require-hashes verification")
    Path(args.output).write_text(rendered, encoding="utf-8")
    print(f"wrote {args.output}: {len(entries)} hash-pinned wheels")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
