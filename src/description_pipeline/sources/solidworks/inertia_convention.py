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

``normalize_capture_inertia`` is called once per capture, at the single point
where the native raw evidence is finalized (``freeze._seal_inertia_convention``).
A standard capture is kept byte-identical; a positive capture has the products
of inertia of every recorded tensor negated exactly once (all capture
documents, see ``_FLIPPERS``), and the sealed evidence gains
``raw/inertia_convention.json`` with the per-sample residuals, votes, counts,
and the applied transform.  The transform is its own inverse: negating the
products again restores the raw bytes, so the record is reversible and
auditable.
"""

from __future__ import annotations

import math
from typing import Any
from collections.abc import Callable

from .errors import BridgeError, EXIT_CAD

STANDARD = "solidworks_standard"
POSITIVE = "solidworks_positive"

SCHEMA = "solidworks-to-urdf.inertia-convention/v1"

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
    if origin is None or isinstance(origin, dict):
        return None
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


def _negate_matrix(tensor: Any) -> None:
    if (
        isinstance(tensor, list)
        and len(tensor) == 3
        and all(isinstance(row, list) and len(row) == 3 for row in tensor)
    ):
        for row, column in _OFF_DIAGONAL:
            tensor[row][column] = -tensor[row][column]
            tensor[column][row] = -tensor[column][row]


def _negate_flat(tensor: Any) -> None:
    if isinstance(tensor, list) and len(tensor) == 9:
        for index in _FLAT_OFF_DIAGONAL:
            tensor[index] = -tensor[index]


def _flip_measurement(block: Any) -> None:
    if isinstance(block, dict):
        _negate_matrix(block.get("inertia_kg_m2"))


def _flip_reading(reading: Any) -> None:
    if not isinstance(reading, dict):
        return
    _negate_matrix(reading.get("inertia"))
    _flip_measurement(reading.get("part_measurement"))


def _flip_reading_map(readings: Any) -> None:
    if isinstance(readings, dict):
        for reading in readings.values():
            _flip_reading(reading)


def _flip_mass_properties(document: dict) -> None:
    for payload in document.values():
        if isinstance(payload, dict):
            _negate_matrix(payload.get("inertia"))


def _flip_scene_raw(document: dict) -> None:
    _flip_mass_properties(document.get("mass_properties") or {})
    notes = document.get("notes")
    if isinstance(notes, dict):
        _flip_reading_map(notes.get("effective_instances"))


def _flip_effective_instances(document: dict) -> None:
    _flip_reading_map(document.get("readings"))
    warnings = document.get("warnings")
    if isinstance(warnings, list):
        for warning in warnings:
            if isinstance(warning, dict):
                _flip_measurement(warning.get("measurement"))
                _flip_measurement(warning.get("part_measurement"))


def _flip_mass_closure(document: dict) -> None:
    top = document.get("top_level")
    if isinstance(top, dict):
        _negate_matrix(top.get("inertia"))
    context = document.get("component_context")
    instances = context.get("instances") if isinstance(context, dict) else None
    if isinstance(instances, list):
        for row in instances:
            if isinstance(row, dict):
                _flip_reading(row.get("effective"))


def _flip_material_provenance(document: dict) -> None:
    warnings = document.get("warnings")
    if isinstance(warnings, list):
        for warning in warnings:
            if isinstance(warning, dict):
                measured = warning.get("measured")
                if isinstance(measured, dict):
                    _negate_matrix(measured.get("inertia_kg_m2"))


def _flip_link_group(document: dict) -> None:
    links = document.get("links")
    if not isinstance(links, dict):
        return
    for entry in links.values():
        reads = entry.get("reads") if isinstance(entry, dict) else None
        if not isinstance(reads, list):
            continue
        for read in reads:
            if isinstance(read, dict):
                _negate_matrix(read.get("inertia_tensor_about_com"))
                _negate_flat(read.get("inertia_values_about_origin_api"))


#: The complete tensor coverage of one capture: every raw native document that
#: records an inertia tensor, keyed by the file name in ``raw/``.  This registry
#: is the one place a new tensor recording must be added; the coverage is pinned
#: by ``tests/sources/test_solidworks_inertia_convention.py`` walking a real
#: capture's raw set.
_FLIPPERS: dict[str, Callable[[dict], None]] = {
    "mass_properties.json": _flip_mass_properties,
    "scene_raw.json": _flip_scene_raw,
    "effective_instances.json": _flip_effective_instances,
    "mass_closure.json": _flip_mass_closure,
    "material_provenance.json": _flip_material_provenance,
    "link_group_mass_properties.json": _flip_link_group,
}


def normalize_capture_inertia(documents: dict[str, Any]) -> dict:
    """Detect, normalize once, and return the sealed convention record.

    ``documents`` maps each raw evidence file name to its parsed document and
    is mutated in place: a positive-products capture has every recorded tensor's
    products negated exactly once (standard captures are untouched).  The
    returned record is written to ``raw/inertia_convention.json`` by the caller.
    """

    link_record = documents.get("link_group_mass_properties.json")
    if not isinstance(link_record, dict):
        raise _refuse(
            "inertia_convention_unverified",
            "the capture carries no link-group mass record to prove the product convention",
        )
    decision = detect_family(link_record)
    applied = decision["family"] == POSITIVE
    if applied:
        for name, flipper in _FLIPPERS.items():
            document = documents.get(name)
            if isinstance(document, dict):
                flipper(document)
        # The flip is an involution: the normalized record must now prove the
        # standard family, or the capture is refused instead of sealed.
        recheck = detect_family(link_record)
        if recheck["family"] != STANDARD:
            raise _refuse(
                "inertia_convention_normalization_failed",
                "the normalized capture does not prove the standard product convention",
                {"detected_after_normalization": recheck["family"]},
            )
    return {
        "schema_version": SCHEMA,
        "detected": decision["family"],
        "capture_transform": {
            "applied": applied,
            "operation": "negate_products_of_inertia" if applied else None,
            "target_convention": STANDARD,
            "reversible": True,
            "note": (
                "negating the off-diagonal entries of every recorded tensor again restores the "
                "raw capture bytes"
            ),
        },
        "thresholds": {
            "match_rtol": MATCH_RTOL,
            "noise_rtol": NOISE_RTOL,
            "signal_rtol": SIGNAL_RTOL,
            "separation_min": SEPARATION_MIN,
            "min_decisive_samples": MIN_DECISIVE_SAMPLES,
            "min_decisive_links": MIN_DECISIVE_LINKS,
        },
        "samples": {
            "readings": decision["readings"],
            "usable": decision["usable"],
            "decisive": decision["decisive"],
            "undecidable": decision["usable"] - decision["decisive"],
            "missing_origin_read": decision["missing_origin_read"],
            "decisive_links": decision["decisive_links"],
        },
        "links": decision["samples"],
    }
