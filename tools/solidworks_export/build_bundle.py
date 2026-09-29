"""Reject the retired standalone Windows distribution before creating files."""

import sys


def main() -> int:
    print(
        "Legacy SolidWorks bundle is retired. Use python tools/build_release.py --require-clean --offline; "
        "see docs/sources/solidworks.md.",
        file=sys.stderr,
    )
    return 3


if __name__ == "__main__":
    raise SystemExit(main())
