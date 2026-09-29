#!/usr/bin/env python3
"""Compatibility entry for the packaged simulation acceptance runner."""

from description_pipeline.verification.simulation import main

if __name__ == "__main__":
    raise SystemExit(main())
