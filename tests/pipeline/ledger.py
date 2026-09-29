"""The joint ledger every pipeline fixture has to declare.

``config/joint_names.yaml`` is a delivery file: ``description check`` refuses a candidate whose
movable joints are not registered there (``URDF208``), exactly like ``tools/audit.py --policy
strict``.  A fixture that builds a model therefore writes the file with the same shape the shipped
examples and the ``model init`` template use.
"""

from __future__ import annotations

from pathlib import Path

HEADER = "# Structural inspection order only; NOT controller, policy or hardware order.\n"


def text(*names: str) -> str:
    """The ledger for ``names``, in the order the design lists them."""

    return HEADER + "structural_joint_names:\n" + "".join(f"  - {name}\n" for name in names)


def write(root: Path, *names: str) -> None:
    """Write the ledger of the workspace at ``root``; call it whenever the movable joints change."""

    path = Path(root) / "config" / "joint_names.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text(*names), encoding="utf-8")
