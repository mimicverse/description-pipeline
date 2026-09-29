# Changelog

## [Unreleased]

### Added

- A capture records the assembly's own mass properties next to the parallel-axis combination of its
  leaf readings (`raw/mass_closure.json`), and `description check` reports a difference between the
  two as a **note** — never a blocker. A leaf that was repaired or re-materialed after the assembly
  was built (a uniform density difference, as the M3.0 servo recovery showed) is otherwise invisible:
  the leaf reader answers per part, the gyration-based rules are mass-scale invariant, and the
  material checks say nothing about magnitude. Snapshots that predate the record stay
  `not_applicable`. The native assembly read is exercised on Windows by the release rehearsal.

## [0.3.23] - 2026-09-29

First public release of the CAD-to-URDF/MJCF tool in
[`mimicverse/description-pipeline`](https://github.com/mimicverse/description-pipeline).
It provides frozen source capture, canonical modeling, URDF/MJCF generation, independent
qualification, offline Windows and Linux distributions, and model submission and promotion.
The earlier development and model history remains in the private source repository.

The package version is defined by `description_pipeline.__version__`.
