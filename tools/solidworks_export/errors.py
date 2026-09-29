"""Compatibility alias; the native implementation lives in the installed package."""

import sys
from typing import TYPE_CHECKING
from description_pipeline.sources.solidworks import errors as _implementation

if TYPE_CHECKING:
    from description_pipeline.sources.solidworks.errors import (
        BridgeError as BridgeError,
        ConfigError as ConfigError,
        UsageError as UsageError,
        EnvironmentError_ as EnvironmentError_,
        CadError as CadError,
        exit_code_for as exit_code_for,
        EXIT_OK as EXIT_OK,
        EXIT_USAGE as EXIT_USAGE,
        EXIT_ENV as EXIT_ENV,
        EXIT_CAD as EXIT_CAD,
        EXIT_TIMEOUT as EXIT_TIMEOUT,
        EXIT_INTERNAL as EXIT_INTERNAL,
    )

sys.modules[__name__] = _implementation
