"""Capture-side product-convention normalization at the single evidence throat.

SolidWorks' ``IMassProperty2.GetMomentOfInertia`` has been measured to return
the inertia tensor with the products of inertia (the off-diagonal entries)
under either the signed standard convention (``Ixy = -∫xy dm``) or the
positive-product convention (``Ixy = +∫xy dm``), depending on the installed
release: 34.0.0 returns standard, 34.4.1 returns positive (2026-10-15
direct-cause report).  The capture carries no version table.  The family is
proven per capture from the sealed data itself, by the parallel-axis identity
between each link group's two recordings - the tensor about the center of mass
(``GetMomentOfInertia(0)``) and the tensor about the assembly origin
(``GetMomentOfInertia(1)``).

For one link-group read, both candidate families predict the about-origin
tensor from the about-COM tensor and the recorded mass/COM::

    origin_ij = about_com_ij + m * (|c|^2 * delta_ij - c_i * c_j)

with the off-diagonal product term ``-m c_i c_j`` for the standard family and
``+m c_i c_j`` for the positive family.  Exactly one family must fit every
decisive sample.  A sample is decisive when its product signal
``2 * m * max|c_i c_j|`` stands at least ``SIGNAL_RTOL`` above the tensor
scale, the winning residual fits within ``MATCH_RTOL`` of that scale, and the
losing residual stays at least ``SEPARATION_MIN`` above the winning residual
floored at ``NOISE_RTOL`` of the scale.  A sample that fits neither family,
decisive samples that disagree, or too few decisive samples refuse the whole
capture: fail closed, never a default family, never a majority vote
(2026-10-15 design review points 1-2).

The family decision is consumed by the in-memory Generate projection. Raw
Capture evidence remains byte-identical for both families.
"""

from __future__ import annotations

import math
from typing import Any

from .errors import BridgeError, EXIT_CAD

STANDARD = "solidworks_standard"
POSITIVE = "solidworks_positive"

#: The winning family must fit within this fraction of the tensor scale.
#: Measured winning residuals are ~4e-19 on ~1e-2 tensors (relative ~1e-16);
#: this keeps four decades of headroom while staying three decades below a
#: genuine family mismatch (relative ~0.19 on the 2026-10-15 golden pair).
MATCH_RTOL = 1e-12
#: Residual floor as a fraction of the tensor scale, used in the separation
#: ratio so a tiny winning residual cannot inflate the ratio.
NOISE_RTOL = 1e-15
#: A decisive sample must carry a product signal at least this large,
#: relative to the tensor scale, for the two families to be distinguishable.
#: Below it the readings are "double fit" and cannot vote (design point 2).
SIGNAL_RTOL = 1e-9
#: The losing residual must exceed the winning residual by this factor
#: (measured golden-pair separations are >= 1e15).
SEPARATION_MIN = 1e3
#: Enough independent evidence to prove the family: at least two decisive
#: samples, each from a separate CAD call (a link group records two reads).
#: A single-body assembly legitimally carries one link group, so the link
#: count floor stays at one; a capture whose links all sit at zero product
#: signal has no decisive sample and refuses (design point 2).
MIN_DECISIVE_SAMPLES = 2
MIN_DECISIVE_LINKS = 1

_OFF_DIAGONAL = ((0, 1), (0, 2), (1, 2))
#: Row-major flat positions of the off-diagonal entries in a 9-value tensor.
_FLAT_OFF_DIAGONAL = (1, 2, 3, 5, 6, 7)


def _finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _refuse(code: str, message: str, detail: dict | None = None) -> BridgeError:
    return BridgeError(code, message, detail, exit_code=EXIT_CAD)


def _matrix(read: dict, key: str, where: str) -> list[list[float]]:
    value = read.get(key)
    if (
        not isinstance(value, list)
        or len(value) != 3
        or any(not isinstance(row, list) or len(row) != 3 for row in value)
        or not all(_finite(entry) for row in value for entry in row)
    ):
        raise _refuse(
            "inertia_convention_malformed",
            f"{where}: {key} is not a finite 3x3 tensor",
            {"where": where, "field": key},
        )
    return [[float(entry) for entry in row] for row in value]


def dual_read_sample(read: dict, where: str) -> dict | None:
    """One link-group dual-read sample, or ``None`` when it carries no origin read.

    The about-origin array is a diagnostic recording: when the second call
    fails the evidence stores an explicit error object and the sample cannot
    vote.  A malformed recording of either array refuses instead.
    """

    if not isinstance(read, dict):
        raise _refuse("inertia_convention_malformed", f"{where}: the link read is not an object")
    origin = read.get("inertia_values_about_origin_api")
    read_errors = read.get("read_errors")
    if read_errors is not None and not isinstance(read_errors, dict):
        raise _refuse(
            "inertia_convention_malformed",
            f"{where}: the link read has no error map",
            {"where": where},
        )
    recorded_origin_error = (
        read_errors.get("inertia_values_about_origin_api")
        if isinstance(read_errors, dict)
        else None
    )
    if isinstance(origin, dict):
        if (
            not isinstance(origin.get("code"), str)
            or not origin["code"].strip()
        ):
            raise _refuse(
                "inertia_convention_malformed",
                f"{where}: inertia_values_about_origin_api error is malformed",
                {"where": where},
            )
        if (
            isinstance(read_errors, dict)
            and recorded_origin_error != origin
        ):
            raise _refuse(
                "inertia_convention_malformed",
                f"{where}: origin error differs from its read-error entry",
                {"where": where},
            )
        return None
    if origin is None:
        if recorded_origin_error is not None:
            raise _refuse(
                "inertia_convention_malformed",
                f"{where}: origin error has no matching origin value",
                {"where": where},
            )
        return None
    if recorded_origin_error is not None:
        raise _refuse(
            "inertia_convention_malformed",
            f"{where}: origin error conflicts with a recorded tensor",
            {"where": where},
        )
    if (
        not isinstance(origin, list)
        or len(origin) != 9
        or not all(_finite(entry) for entry in origin)
    ):
        raise _refuse(
            "inertia_convention_malformed",
            f"{where}: inertia_values_about_origin_api is not a finite 9-value tensor",
            {"where": where},
        )
    com = read.get("com_m")
    mass = read.get("mass_kg")
    if not isinstance(com, list) or len(com) != 3 or not all(_finite(entry) for entry in com):
        raise _refuse("inertia_convention_malformed", f"{where}: com_m is not a finite 3-vector")
    if not _finite(mass) or float(mass) <= 0.0:
        raise _refuse("inertia_convention_malformed", f"{where}: mass_kg is not a finite positive value")
    about = _matrix(read, "inertia_tensor_about_com", where)
    mass = float(mass)
    com = [float(entry) for entry in com]
    origin = [float(entry) for entry in origin]
    scale = max([abs(entry) for row in about for entry in row] + [abs(entry) for entry in origin])
    norm = math.fsum(entry * entry for entry in com)
    residuals: dict[str, float] = {}
    for family, sign in ((STANDARD, -1.0), (POSITIVE, 1.0)):
        worst = 0.0
        for row in range(3):
            for column in range(3):
                if row == column:
                    predicted = about[row][column] + mass * (norm - com[row] * com[column])
                else:
                    predicted = about[row][column] + sign * mass * com[row] * com[column]
                worst = max(worst, abs(origin[3 * row + column] - predicted))
        residuals[family] = worst
    signal = 2.0 * mass * max(abs(com[row] * com[column]) for row, column in _OFF_DIAGONAL)
    return {
        "residual_standard": residuals[STANDARD],
        "residual_positive": residuals[POSITIVE],
        "signal": signal,
        "scale": scale,
    }


def detect_family(link_record: dict) -> dict:
    """Decide the capture's product convention from its sealed dual reads.

    Returns the decision record (per-sample residuals, votes and counts).
    Raises ``BridgeError`` when the capture cannot prove exactly one family.
    """

    links = link_record.get("links") if isinstance(link_record, dict) else None
    if not isinstance(links, dict) or not links:
        raise _refuse(
            "inertia_convention_unverified",
            "the capture carries no link-group dual-read record to prove the product convention",
        )
    samples: list[dict] = []
    missing = 0
    readings = 0
    for link in sorted(links):
        entry = links[link]
        reads = entry.get("reads") if isinstance(entry, dict) else None
        if not isinstance(reads, list) or not reads:
            raise _refuse(
                "inertia_convention_malformed",
                f"link {link}: the link-group record carries no reads",
                {"link": link},
            )
        for index, read in enumerate(reads):
            readings += 1
            where = f"link {link} read {index}"
            sample = dual_read_sample(read, where)
            if sample is None:
                missing += 1
                continue
            scale = sample["scale"]
            match = MATCH_RTOL * scale
            floor = NOISE_RTOL * scale
            winning = min(sample["residual_standard"], sample["residual_positive"])
            losing = max(sample["residual_standard"], sample["residual_positive"])
            vote: str | None = None
            if winning > match:
                raise _refuse(
                    "inertia_convention_no_fit",
                    f"{where}: neither product convention fits the dual-read identity",
                    {
                        "where": where,
                        "residual_standard": sample["residual_standard"],
                        "residual_positive": sample["residual_positive"],
                        "match_tolerance": match,
                    },
                )
            if sample["signal"] <= 0.0 or sample["signal"] < SIGNAL_RTOL * scale:
                pass  # low signal: both families double-fit, this sample cannot vote
            elif losing >= SEPARATION_MIN * max(winning, floor):
                vote = STANDARD if sample["residual_standard"] <= sample["residual_positive"] else POSITIVE
            samples.append(
                {
                    "link": link,
                    "read": index,
                    "residual_standard": sample["residual_standard"],
                    "residual_positive": sample["residual_positive"],
                    "signal": sample["signal"],
                    "scale": scale,
                    "match_tolerance": match,
                    "vote": vote,
                    "decisive": vote is not None,
                }
            )
    decisive = [sample for sample in samples if sample["decisive"]]
    votes = sorted({sample["vote"] for sample in decisive})
    if len(votes) > 1:
        raise _refuse(
            "inertia_convention_disagreement",
            "decisive link groups disagree about the product convention",
            {"votes": votes, "decisive_samples": len(decisive)},
        )
    decisive_links = len({sample["link"] for sample in decisive})
    if len(decisive) < MIN_DECISIVE_SAMPLES or decisive_links < MIN_DECISIVE_LINKS:
        raise _refuse(
            "inertia_convention_insufficient_samples",
            "the capture carries too few decisive dual-read samples to prove the product convention",
            {
                "total_samples": len(samples),
                "decisive_samples": len(decisive),
                "decisive_links": decisive_links,
                "min_decisive_samples": MIN_DECISIVE_SAMPLES,
                "min_decisive_links": MIN_DECISIVE_LINKS,
            },
        )
    return {
        "family": votes[0],
        "samples": samples,
        "readings": readings,
        "usable": len(samples),
        "missing_origin_read": missing,
        "decisive": len(decisive),
        "decisive_links": decisive_links,
    }
