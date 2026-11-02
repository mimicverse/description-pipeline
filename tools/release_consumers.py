"""Pure validation of the existing consumer locks and original wheel bytes."""

from __future__ import annotations

import base64
import csv
import email
import hashlib
import io
from pathlib import PurePosixPath
import re
import stat
import zipfile

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name, parse_wheel_filename
from packaging.version import Version
from packaging.tags import compatible_tags, cpython_tags

CONSUMER_POOL_PREFIX = "consumers/wheels/"
CONSUMER_LOCKS = {
    "pico": "requirements/teleop-pico-py312.lock",
    "assessment": "requirements/teleop-assessment-py312.lock",
    "pico-link": "requirements/pico-link-py312.lock",
}
TARGET_ENVIRONMENT = {
    "python_version": "3.12",
    "python_full_version": "3.12.0",
    "sys_platform": "linux",
    "platform_system": "Linux",
    "platform_machine": "x86_64",
    "os_name": "posix",
    "implementation_name": "cpython",
    "platform_python_implementation": "CPython",
}


def target_tags(platforms, python="3.12", abi="cp312") -> set:
    version = tuple(int(part) for part in python.split("."))
    return set(cpython_tags(python_version=version, abis=[abi, "abi3", "none"], platforms=platforms)) | set(
        compatible_tags(python_version=version, interpreter=abi, platforms=platforms)
    )


def require(condition, message):
    if not condition:
        raise ValueError(message)


def hashed_pins(data: bytes) -> dict:
    """Parse hash-pinned target locks, retaining extras and all allowed wheel hashes."""
    pins = {}
    for line in re.sub(r"\\\r?\n", " ", data.decode("utf-8")).splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        hashes = re.findall(r"--hash=sha256:([0-9a-f]{64})(?=\s|$)", line)
        spec = re.sub(r"\s+--hash=sha256:[0-9a-f]{64}(?=\s|$)", "", line).strip()
        requirement = Requirement(spec)
        versions = list(requirement.specifier)
        name = canonicalize_name(requirement.name)
        require(
            hashes
            and requirement.url is None
            and requirement.marker is None
            and len(versions) == 1
            and versions[0].operator == "=="
            and "*" not in versions[0].version
            and name not in pins,
            "Consumer lock must contain unique exact versions and SHA256 wheel hashes",
        )
        pins[name] = {
            "version": versions[0].version,
            "extras": set(requirement.extras),
            "hashes": set(hashes),
        }
    require(bool(pins), "Consumer lock is empty")
    return pins


def locked_consumers(lock_data: dict[str, bytes], contracts: dict) -> dict:
    require(set(lock_data) == set(CONSUMER_LOCKS), "Consumer lock set is incomplete")
    require(set(contracts) == {"pico", "assessment"}, "Consumer runtime projections are incomplete")
    result = {role: hashed_pins(data) for role, data in lock_data.items()}
    for role, contract in contracts.items():
        pins = result[role]
        require(
            {name: pin["version"] for name, pin in pins.items()} == contract["dependency_versions"],
            "Consumer lock versions differ from the common runtime contract",
        )
        require(
            pins["robot-teleop"]["hashes"] == {contract["wheel_sha256"]},
            "Consumer Teleop lock differs from the common fixed wheel",
        )
        require(
            pins["robot-teleop"]["extras"] == ({"evaluation"} if role == "assessment" else set()),
            "Consumer Teleop extras differ from its explicit role",
        )
        asset = contract["actuator_model"]
        if asset is not None:
            require(
                pins["actuator-model"]["hashes"] == {asset["wheel_sha256"]},
                "Consumer actuator lock differs from the fixed wheel",
            )
            require(pins["actuator-model"]["extras"] == {"mujoco"}, "Assessment actuator extras are invalid")
    common = ("source_commit", "source_tree", "wheel_filename", "wheel_sha256", "files")
    require(
        all(contracts["pico"][key] == contracts["assessment"][key] for key in common),
        "Consumers do not share one Teleop source inventory",
    )
    return result


def wheel_record(filename: str, payload: bytes, target_tags: set) -> dict:
    """Verify wheel RECORD, target tags and metadata directly from the supplied archive."""
    name, version, _build, tags = parse_wheel_filename(filename)
    require(bool(tags & target_tags), "Consumer wheel belongs to another target: " + filename)
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        names = archive.namelist()
        require(len(names) == len(set(names)), "Consumer wheel repeats a member")
        folded = set()
        for item in archive.infolist():
            path = PurePosixPath(item.filename)
            require(
                not path.is_absolute()
                and ".." not in path.parts
                and "\\" not in item.filename
                and ":" not in item.filename
                and path.as_posix() == item.filename.rstrip("/")
                and not stat.S_ISLNK(item.external_attr >> 16)
                and item.filename.casefold() not in folded,
                "Consumer wheel contains an unsafe member",
            )
            folded.add(item.filename.casefold())
            require(not item.is_dir() or item.file_size == 0, "Consumer wheel directory carries file data")
        records = [member for member in names if member.endswith(".dist-info/RECORD")]
        require(len(records) == 1, "Consumer wheel has no unique RECORD")
        record = records[0]
        rows = list(csv.reader(io.StringIO(archive.read(record).decode())))
        files = {item.filename for item in archive.infolist() if not item.is_dir()}
        require(
            all(len(row) == 3 for row in rows) and len(rows) == len(files) and {row[0] for row in rows} == files,
            "Consumer wheel RECORD does not cover its archive",
        )
        for member, checksum, size in rows:
            if member == record:
                require(not checksum and not size, "Consumer wheel RECORD must not hash itself")
                continue
            content = archive.read(member)
            expected = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).rstrip(b"=").decode()
            require(
                checksum == "sha256=" + expected and size == str(len(content)),
                "Consumer wheel RECORD differs from its original bytes",
            )
        metadata_name = record.removesuffix("RECORD") + "METADATA"
        metadata = email.message_from_bytes(archive.read(metadata_name))
        require(
            canonicalize_name(metadata["Name"]) == canonicalize_name(name) and Version(metadata["Version"]) == version,
            "Consumer wheel metadata differs from its filename",
        )
        if metadata.get("Requires-Python"):
            from packaging.specifiers import SpecifierSet

            require(
                Version("3.12.0") in SpecifierSet(metadata["Requires-Python"]),
                "Consumer wheel excludes the supported Python target",
            )
        inventory = {
            member: hashlib.sha256(archive.read(member)).hexdigest()
            for member in names
            if member.startswith("teleop/") and not member.endswith("/")
        }
    return {
        "name": canonicalize_name(name),
        "version": str(version),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "requires": metadata.get_all("Requires-Dist", []),
        "teleop_files": inventory,
    }


def verify_consumer_pool(members: dict[str, bytes], lock_data: dict[str, bytes], contracts: dict, target_tags: set):
    """Recompute pool membership and each dependency closure from source locks and wheels."""
    consumers = locked_consumers(lock_data, contracts)
    expected = {(name, pin["version"]) for pins in consumers.values() for name, pin in pins.items()}
    wheels = {}
    for filename, payload in members.items():
        require(
            PurePosixPath(filename).name == filename and filename.endswith(".whl"),
            "Consumer pool must contain only wheel files",
        )
        try:
            row = wheel_record(filename, payload, target_tags)
        except (zipfile.BadZipFile, KeyError, TypeError, csv.Error) as error:
            raise ValueError("Consumer wheel archive or metadata is malformed") from error
        key = row["name"], row["version"]
        require(key in expected and key not in wheels, "Consumer pool carries an unexpected or repeated wheel")
        allowed = set().union(
            *(
                pins[key[0]]["hashes"]
                for pins in consumers.values()
                if key[0] in pins and pins[key[0]]["version"] == key[1]
            )
        )
        require(row["sha256"] in allowed, "Consumer wheel bytes differ from their source lock")
        wheels[key] = row
    require(set(wheels) == expected, "Consumer wheel pool is incomplete")
    for contract in contracts.values():
        name = contract["wheel_filename"]
        require(
            name in members and hashlib.sha256(members[name]).hexdigest() == contract["wheel_sha256"],
            "Consumer pool does not carry the fixed Teleop wheel",
        )
        require(
            wheels[("robot-teleop", contract["dependency_versions"]["robot-teleop"])]["teleop_files"]
            == contract["files"],
            "Teleop wheel differs from its common source inventory",
        )
        asset = contract["actuator_model"]
        if asset is not None:
            require(
                asset["wheel_filename"] in members
                and hashlib.sha256(members[asset["wheel_filename"]]).hexdigest() == asset["wheel_sha256"],
                "Consumer pool does not carry the fixed actuator wheel",
            )
    for pins in consumers.values():
        require(
            all(wheels[(name, pin["version"])]["sha256"] in pin["hashes"] for name, pin in pins.items()),
            "Consumer wheel bytes differ from one of the role locks",
        )
        extras = {name: set(pin["extras"]) for name, pin in pins.items()}
        visited = set()
        pending = list(pins)
        while pending:
            name = pending.pop()
            context = name, frozenset(extras[name])
            if context in visited:
                continue
            visited.add(context)
            for value in wheels[(name, pins[name]["version"])]["requires"]:
                requirement = Requirement(value)
                if requirement.marker and not any(
                    requirement.marker.evaluate({**TARGET_ENVIRONMENT, "extra": extra}) for extra in {"", *extras[name]}
                ):
                    continue
                dependency = canonicalize_name(requirement.name)
                require(
                    requirement.url is None and dependency in pins,
                    "Consumer wheel metadata names an unlocked dependency",
                )
                require(
                    not requirement.specifier or pins[dependency]["version"] in requirement.specifier,
                    "Consumer dependency versions conflict with wheel metadata",
                )
                previous = set(extras[dependency])
                extras[dependency].update(requirement.extras)
                if extras[dependency] != previous:
                    pending.append(dependency)
    return {role: {name: pin["version"] for name, pin in pins.items()} for role, pins in consumers.items()}
