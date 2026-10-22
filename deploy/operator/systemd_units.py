"""The one static systemd unit role inventory used by deploy consumers."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

MANIFEST_PATH = Path(__file__).resolve().parents[1] / "systemd-units.tsv"
KNOWN_ROLES = frozenset(
    {
        "airflow",
        "all",
        "database",
        "executor",
        "maintenance-v1",
        "operator",
        "services",
    }
)
ORDERED_ROLE = "maintenance-v1"
UNIT_NAME = re.compile(r"\Adescription-[a-z0-9][a-z0-9_.-]*\Z")


class UnitManifestError(ValueError):
    """The static unit manifest is absent or malformed."""


@dataclass(frozen=True)
class UnitRecord:
    name: str
    roles: frozenset[str]
    maintenance_v1_order: int | None


def _unit_name(value: str) -> str:
    if not UNIT_NAME.fullmatch(value):
        raise UnitManifestError(f"invalid unit name: {value!r}")
    return value


def load_units(path: Path = MANIFEST_PATH) -> tuple[UnitRecord, ...]:
    """Read and strictly validate the one static unit role inventory."""

    path = Path(path)
    if not path.is_file() or path.is_symlink():
        raise UnitManifestError(f"unit manifest is missing or a link: {path}")
    rows: list[UnitRecord] = []
    seen: set[str] = set()
    maintenance_orders: set[int] = set()
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise UnitManifestError(f"cannot read unit manifest: {path}: {error}") from error
    for line_number, line in enumerate(lines, 1):
        fields = line.split("\t")
        if len(fields) != 3:
            raise UnitManifestError(f"{path}:{line_number}: expected three tab-separated fields")
        name, role_text, order_text = fields
        name = _unit_name(name)
        if name in seen:
            raise UnitManifestError(f"{path}:{line_number}: duplicate unit: {name}")
        seen.add(name)
        roles = frozenset(role_text.split(","))
        if "" in roles:
            raise UnitManifestError(f"{path}:{line_number}: roles must not be empty")
        if "all" in roles:
            raise UnitManifestError(f"{path}:{line_number}: all is a query role, not a row role")
        unknown = sorted(roles - KNOWN_ROLES)
        if unknown:
            raise UnitManifestError(f"{path}:{line_number}: unknown roles: {', '.join(unknown)}")
        maintenance_order = None
        if ORDERED_ROLE in roles:
            if not order_text.isdecimal() or int(order_text) < 1:
                raise UnitManifestError(f"{path}:{line_number}: {ORDERED_ROLE} needs a positive order")
            maintenance_order = int(order_text)
            if maintenance_order in maintenance_orders:
                raise UnitManifestError(f"{path}:{line_number}: duplicate {ORDERED_ROLE} order")
            maintenance_orders.add(maintenance_order)
        elif order_text != "-":
            raise UnitManifestError(f"{path}:{line_number}: order requires the {ORDERED_ROLE} role")
        rows.append(UnitRecord(name=name, roles=roles, maintenance_v1_order=maintenance_order))
    if not rows:
        raise UnitManifestError(f"unit manifest is empty: {path}")
    ordered = sorted(maintenance_orders)
    if ordered != list(range(1, len(ordered) + 1)):
        raise UnitManifestError(f"{path}: {ORDERED_ROLE} orders must be contiguous from 1")
    return tuple(rows)


def units_for_role(role: str, path: Path = MANIFEST_PATH) -> tuple[str, ...]:
    """Names for one role; unknown or empty roles fail closed."""

    if role not in KNOWN_ROLES:
        raise UnitManifestError(f"unknown unit role: {role}")
    rows = load_units(path)
    selected = [row for row in rows if role == "all" or role in row.roles]
    if role == ORDERED_ROLE:
        selected.sort(key=lambda row: row.maintenance_v1_order)
    if not selected:
        raise UnitManifestError(f"unit role has no members: {role}")
    return tuple(row.name for row in selected)
