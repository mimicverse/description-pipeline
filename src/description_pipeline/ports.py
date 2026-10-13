"""Canonical default ports owned by the pipeline code (DEP-01).

Every port literal lives in exactly one layer-local home: this module for the
pipeline code, ``deploy/operator/render_operator.py`` for the deployment, and
the operator-facing examples (``docs/`` and ``*.example*`` files) as
documentation.  Deployment scripts consume the rendered ``resolved.env``
instead of repeating these numbers, and ``tests/v1/test_port_defaults.py``
fails on stray literals in the audited scopes.
"""

#: Loopback listener of the Windows SolidWorks execution endpoint.
SOLIDWORKS_ENDPOINT_PORT = 8765
#: Plain-HTTP portal listener behind the TLS reverse proxy.
PORTAL_HTTP_PORT = 8780
