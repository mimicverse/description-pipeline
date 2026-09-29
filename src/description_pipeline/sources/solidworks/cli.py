"""Command line entry point for the Windows collection worker.

    python -m description_pipeline.sources.solidworks.worker --serve --port 8765
    python -m description_pipeline.sources.solidworks.worker --doctor

``worker.ps1`` calls the same entry point; nothing here may require SolidWorks
unless a job or the doctor actually asks for CAD.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from collections.abc import Sequence

from description_pipeline.io import pin_utf8_streams

from . import doctor as doctor_module
from .worker import Worker, serve
from .errors import BridgeError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="description-pipeline.solidworks.worker", description=__doc__)
    parser.add_argument("--serve", action="store_true", help="run the loopback job API")
    parser.add_argument("--doctor", action="store_true", help="print diagnostics and exit")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--jobs-root", type=Path, default=None)
    parser.add_argument("--assembly", default=None, help="assembly to probe for --doctor")
    parser.add_argument("--configuration", default=None)
    parser.add_argument("--watchdog-seconds", type=float, default=900.0)
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    pin_utf8_streams()
    args = build_parser().parse_args(argv)
    if not args.serve and not args.doctor:
        build_parser().print_help()
        return 2

    if args.doctor:
        backend = None
        if args.assembly:
            from .native import SolidWorksBackend

            backend = SolidWorksBackend()

        def diagnose():
            return doctor_module.diagnose_owned(backend, args.assembly, args.configuration)

        if backend is None:
            report = diagnose()
        else:
            from .executor import ComExecutor

            executor = ComExecutor(backend)
            try:
                report = executor.run(diagnose, timeout=args.watchdog_seconds)
            except Exception as error:
                payload = (
                    error.to_dict()
                    if isinstance(error, BridgeError)
                    else {"code": "cad_api_error", "message": str(error), "type": type(error).__name__}
                )
                print(json.dumps({"ok": False, "error": payload}, ensure_ascii=False))
                return error.exit_code if isinstance(error, BridgeError) else 2
            finally:
                executor.close()
        if args.json:
            print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
        else:
            for check in report["checks"]:
                print(f"[{check['status']:>11}] {check['name']}: {json.dumps(check, ensure_ascii=False)}")
            for note in report.get("advisories") or []:
                print(f"[     notice] {note.get('message', note)}")
                for document in (note.get("documents") or [])[:5]:
                    print(f"               {document}")
            print(
                "installed={installed} worker_alive={worker_alive} "
                "solidworks={solidworks_reachable} collectable={cad_collectable}".format(**report)
            )
        return 0 if report["installed"] and (not args.assembly or report["cad_collectable"]) else 1

    worker = Worker(jobs_root=args.jobs_root, watchdog_seconds=args.watchdog_seconds)
    server, thread = serve(worker, args.host, args.port)
    print(
        json.dumps(
            {
                "status": "serving",
                "host": args.host,
                "port": server.server_address[1],
                "worker_version": worker.version,
                "jobs_root": str(worker.jobs_root),
            },
            ensure_ascii=False,
            sort_keys=True,
        ),
        flush=True,
    )
    try:
        thread.join()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        worker.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
