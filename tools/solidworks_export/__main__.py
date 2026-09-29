"""Entry point: ``python -m solidworks_export serve`` or ``... <command>``."""

from __future__ import annotations

import sys

from . import __version__, cli, server


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] in ("-V", "--version"):
        print(f"solidworks_export {__version__}")
        return 0
    if args and args[0] == "serve":
        return server.serve_main(args[1:])
    return cli.main(args)


if __name__ == "__main__":
    sys.exit(main())
