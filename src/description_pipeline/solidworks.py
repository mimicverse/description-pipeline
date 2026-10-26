"""Runtime doctor for the Windows native and Linux portable installations."""

from __future__ import annotations

import importlib.metadata
import platform
import shutil

from . import __version__
from .delivery import PIPELINE_ID
from .runtime import (
    RUNTIME_VERSIONS,
    native_readiness,
    python_version_is_supported,
    required_packages,
    runtime_role,
)


def doctor() -> dict:
    """Check the pinned runtime and readiness required by this host's role."""

    role = runtime_role()
    results = [
        {
            "id": "python",
            "passed": python_version_is_supported(platform.python_version()),
            "actual": platform.python_version(),
        }
    ]
    for name in required_packages(role):
        expected = RUNTIME_VERSIONS[name]
        try:
            actual = importlib.metadata.version(name)
            results.append(
                {
                    "id": name,
                    "passed": actual == expected,
                    "actual": actual,
                    "expected": expected,
                }
            )
        except importlib.metadata.PackageNotFoundError:
            results.append(
                {
                    "id": name,
                    "passed": False,
                    "message": "Reinstall the pinned release runtime",
                }
            )
    native = False
    native_detail = "Native CAD capture runs on the Windows worker"
    if role == "native":
        try:
            detail = native_readiness()
            native, native_detail = True, detail["solidworks_executable"]
            results.append(
                {
                    "id": "native.solidworks",
                    "passed": True,
                    "details": detail,
                }
            )
        except Exception as error:
            native_detail = str(error)
            results.append(
                {
                    "id": "native.solidworks",
                    "passed": False,
                    "message": str(error),
                }
            )
    else:
        from .verification.consumer import readiness

        try:
            results.append(
                {
                    "id": "consumer.urdf",
                    "passed": True,
                    "details": readiness(),
                }
            )
        except Exception as error:
            results.append(
                {
                    "id": "consumer.urdf",
                    "passed": False,
                    "message": str(error),
                    "details": getattr(error, "details", {}),
                }
            )
    result = {
        "pipeline_id": PIPELINE_ID,
        "version": __version__,
        "role": role,
        "passed": all(item["passed"] for item in results),
        "checks": results,
        "native_capture_available": native,
        "native_capture_detail": native_detail,
    }
    if role == "portable":
        result.update(
            git_available=shutil.which("git") is not None,
            github_cli_available=shutil.which("gh") is not None,
        )
    return result
