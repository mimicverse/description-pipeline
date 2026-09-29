"""Stable error codes and process exit codes for the bridge.

Exit codes (architecture doc, section 4.1):
  0 success / 1 usage / 2 environment / 3 upstream CAD / 4 timeout / 5 internal
"""

from __future__ import annotations


EXIT_OK = 0
EXIT_USAGE = 1
EXIT_ENV = 2
EXIT_CAD = 3
EXIT_TIMEOUT = 4
EXIT_INTERNAL = 5


class BridgeError(Exception):
    """Error carrying a stable code, a human message and a process exit code."""

    def __init__(
        self,
        code: str,
        message: str,
        detail: object | None = None,
        exit_code: int = EXIT_INTERNAL,
        http_status: int = 500,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail
        self.exit_code = exit_code
        self.http_status = http_status

    def to_dict(self) -> dict:
        payload: dict = {"code": self.code, "message": self.message}
        if self.detail is not None:
            payload["detail"] = self.detail
        return payload


class ConfigError(BridgeError):
    def __init__(self, message: str, detail: object | None = None) -> None:
        super().__init__("invalid_config", message, detail, EXIT_USAGE, 400)


class UsageError(BridgeError):
    def __init__(self, message: str, detail: object | None = None) -> None:
        super().__init__("usage_error", message, detail, EXIT_USAGE, 400)


class EnvironmentError_(BridgeError):
    """Environment problem on the Windows side (service, SolidWorks, pywin32)."""

    def __init__(
        self,
        code: str,
        message: str,
        detail: object | None = None,
    ) -> None:
        super().__init__(code, message, detail, EXIT_ENV, 503)


class CadError(BridgeError):
    """Upstream SolidWorks/CAD failure."""

    def __init__(
        self,
        code: str,
        message: str,
        detail: object | None = None,
    ) -> None:
        super().__init__(code, message, detail, EXIT_CAD, 502)


def exit_code_for(code: str) -> int:
    """Map an error code string to the documented process exit code."""

    if code in ("usage_error", "invalid_config", "job_not_found"):
        return EXIT_USAGE
    if code in ("no_active_instance", "no_pywin32", "bridge_unavailable"):
        return EXIT_ENV
    if code in ("modal_dialog_blocked",):
        return EXIT_TIMEOUT
    if code.startswith("cad_") or code in (
        "document_not_open",
        "document_open_failed",
        "export_failed",
    ):
        return EXIT_CAD
    return EXIT_INTERNAL
