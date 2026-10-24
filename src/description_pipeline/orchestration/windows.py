"""Windows entry point for isolated native operations."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
from http.server import ThreadingHTTPServer
from pathlib import Path

from ..delivery import PIPELINE_ID
from ..io import PipelineError, digest, read_data
from ..ports import SOLIDWORKS_ENDPOINT_PORT
from ..runtime import tool_record
from .native_operation_runtime import (
    CAPTURE_KIND,
    DISCOVER_KIND,
    CaptureOperationExecutor,
    DiscoverOperationExecutor,
    NativeOperationExecutor,
    NativeOperationRuntime,
)
from .native_operation_server import native_operation_handler

CONFIG_SCHEMA = "solidworks-to-urdf.endpoint/v1"
_CONFIG_FIELDS = frozenset(
    {
        "schema_version",
        "package_root",
        "output_root",
        "state_root",
        "token_file",
        "host",
        "port",
    }
)
_REQUIRED_CONFIG_FIELDS = frozenset(
    {
        "schema_version",
        "package_root",
        "output_root",
        "state_root",
        "token_file",
    }
)


def _root(value: object, label: str) -> Path:
    if not isinstance(value, str):
        raise PipelineError(f"{label} must be an absolute real directory")
    path = Path(value)
    if not path.is_absolute() or path.is_symlink() or path.is_junction():
        raise PipelineError(f"{label} must be an absolute real directory")
    return path.resolve()


def read_config(path: Path) -> dict:
    """Read the endpoint's loopback address, state root and bearer token."""

    config = read_data(Path(path))
    if not isinstance(config, dict) or config.get("schema_version") != CONFIG_SCHEMA:
        raise PipelineError("Unknown endpoint configuration")
    missing = _REQUIRED_CONFIG_FIELDS - set(config)
    unknown = set(config) - _CONFIG_FIELDS
    if missing or unknown:
        raise PipelineError("Unknown endpoint configuration key")
    config_digest = digest(config)
    package_root = _root(config["package_root"], "package_root")
    output_root = _root(config["output_root"], "output_root")
    state_root = _root(config["state_root"], "state_root")
    if not package_root.is_dir():
        raise PipelineError("Package root does not exist")
    if not output_root.is_dir():
        raise PipelineError("Output root does not exist")
    roots = (package_root, output_root, state_root)
    for index, first in enumerate(roots):
        for second in roots[index + 1 :]:
            if first.is_relative_to(second) or second.is_relative_to(first):
                raise PipelineError("Endpoint roots must be separate")
    token_file = _root(config["token_file"], "token_file")
    if not token_file.is_file():
        raise PipelineError("Missing endpoint token file")
    token = token_file.read_text(encoding="utf-8").strip()
    if (
        len(token) < 32
        or not token.isascii()
        or any(character.isspace() for character in token)
    ):
        raise PipelineError(
            "Endpoint token must contain at least 32 non-whitespace ASCII characters"
        )
    host = config.get("host", "127.0.0.1")
    if host not in {"127.0.0.1", "::1", "localhost"}:
        raise PipelineError(
            "The Windows endpoint must bind to loopback and use the authenticated SSH tunnel"
        )
    port = config.get("port", SOLIDWORKS_ENDPOINT_PORT)
    if type(port) is not int or not 1 <= port <= 65535:
        raise PipelineError("Invalid endpoint port")
    return {
        "package_root": package_root,
        "output_root": output_root,
        "state_root": state_root,
        "host": host,
        "port": port,
        "token": token,
        "config_digest": config_digest,
    }


def _native_tool_record() -> dict:
    return tool_record(role="native")


def _cad_ownership_file() -> Path:
    from ..sources.solidworks import isolation

    return isolation._ownership_path()


def build_runtime(
    config: dict,
    *,
    executors: dict[str, NativeOperationExecutor] | None = None,
) -> NativeOperationRuntime:
    """Compose Discover production execution and any explicitly supplied Capture."""

    ownership_file = _cad_ownership_file()
    os.environ.setdefault("DESCRIPTION_CAD_OWNERSHIP_FILE", str(ownership_file))
    registry: dict[str, NativeOperationExecutor] = {
        DISCOVER_KIND: DiscoverOperationExecutor(
            cad_ownership_file=ownership_file,
        ),
        CAPTURE_KIND: CaptureOperationExecutor(),
    }
    registry.update(executors or {})
    return NativeOperationRuntime(
        state_root=config["state_root"],
        native_tool_factory=_native_tool_record,
        executors=registry,
    )


def handler(runtime: NativeOperationRuntime, token: str):
    return native_operation_handler(runtime, token)


def serve(config_path: Path) -> None:
    if sys.platform != "win32":
        raise PipelineError(
            "The native execution endpoint requires Windows with licensed SolidWorks"
        )
    config = read_config(config_path)
    with contextlib.ExitStack() as stack:
        runtime = build_runtime(config)
        stack.callback(runtime.close)
        server = ThreadingHTTPServer(
            (config["host"], config["port"]),
            handler(runtime, config["token"]),
        )
        stack.callback(server.server_close)
        print(
            json.dumps(
                {
                    "pipeline_id": PIPELINE_ID,
                    "endpoint": f"{config['host']}:{config['port']}",
                    "ready": True,
                }
            ),
            flush=True,
        )
        try:
            server.serve_forever(poll_interval=0.5)
        except KeyboardInterrupt:
            return


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="description-windows")
    commands = parser.add_subparsers(dest="operation", required=True)
    serve_command = commands.add_parser("serve", help="run the loopback native-operation endpoint")
    serve_command.add_argument("--config", type=Path, required=True)
    arguments = parser.parse_args(argv)
    if arguments.operation == "serve":
        serve(arguments.config)
        return 0
    raise AssertionError(f"unhandled operation: {arguments.operation}")


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "CONFIG_SCHEMA",
    "build_runtime",
    "handler",
    "main",
    "read_config",
    "serve",
]
