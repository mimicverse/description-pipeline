"""Airflow plugin entry point: install the trusted request-context middleware.

The plugin ships inside this distribution (``airflow.plugins`` entry point), so the api-server
binds the request scope for every route of the root application, including ``/api/v2``. Without
the middleware ordinary operators retain shared read access but cannot create or change runs;
health surfaces the missing middleware.
"""

from __future__ import annotations

import logging
from typing import ClassVar

from airflow.plugins_manager import AirflowPlugin

from .request_context import BindRequestMiddleware

log = logging.getLogger(__name__)

#: Stable name used by health checks and logs to confirm the middleware is installed.
MIDDLEWARE_NAME = "description-pipeline-request-context"


class DescriptionPipelinePlugin(AirflowPlugin):
    """Registers exactly one root middleware; no views, no routes, no settings."""

    name = "description_pipeline"
    fastapi_root_middlewares: ClassVar[list[dict]] = [{"name": MIDDLEWARE_NAME, "middleware": BindRequestMiddleware}]


log.info("description-pipeline request-context middleware registered for the Airflow API")
