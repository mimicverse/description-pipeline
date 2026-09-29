"""Explicit MuJoCo contact parameters, shared by definition and consumer checks."""

import math

from ..io import PipelineError

FIELDS = {"friction", "condim", "solref", "solimp", "margin", "gap"}


def validate_contact(value: dict) -> None:
    if not isinstance(value, dict) or set(value) != FIELDS:
        raise PipelineError("Contact requires friction, condim, solref, solimp, margin and gap")
    for name, length in (("friction", 3), ("solref", 2), ("solimp", 5)):
        items = value[name]
        if (
            not isinstance(items, list)
            or len(items) != length
            or any(type(x) not in (int, float) or not math.isfinite(x) for x in items)
        ):
            raise PipelineError(f"Invalid contact {name}")
    if type(value["condim"]) is not int or value["condim"] not in {1, 3, 4, 6}:
        raise PipelineError("Contact condim must be 1, 3, 4 or 6")
    if min(value["friction"]) < 0 or min(value["solref"]) <= 0:
        raise PipelineError("Friction must be nonnegative; supported solref uses positive time constant and damping")
    low, high, width, midpoint, power = value["solimp"]
    if not (0 < low <= high < 1 and width > 0 and 0 < midpoint < 1 and power >= 1):
        raise PipelineError("Invalid contact impedance parameters")
    for name in ("margin", "gap"):
        if type(value[name]) not in (int, float) or not math.isfinite(value[name]) or value[name] < 0:
            raise PipelineError(f"Invalid contact {name}")
