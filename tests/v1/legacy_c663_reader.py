"""The c663 reader, kept verbatim for the cross-generation rollback proof.

Copied from ``src/description_pipeline/orchestration/airflow_client.py`` at
``c6637e9e7914c19a922ae4589fa1b325cb93fd97``.  The only surrounding change is
that the test module imports the current ``EndpointConfig`` dataclass, whose
legacy fields are unchanged.
"""

from __future__ import annotations


def config_from_airflow_connection(conn_id: str, *, timeout: float = 30.0):
    """Build the endpoint config from an Airflow connection (host/port/password/extra)."""
    from airflow.hooks.base import BaseHook

    from description_pipeline.orchestration.airflow_client import EndpointConfig, EndpointProtocolError

    connection = BaseHook.get_connection(conn_id)
    host = (connection.host or "").strip()
    if not host:
        raise EndpointProtocolError(f"Airflow connection {conn_id!r} has no host")
    if not host.startswith(("http://", "https://")):
        port = f":{connection.port}" if connection.port else ""
        host = f"http://{host}{port}"
    extra = connection.extra_dejson or {}
    return EndpointConfig(
        base_url=host,
        token=connection.password or "",
        timeout=float(extra.get("timeout", timeout)),
        handoff_roots=extra.get("handoff_roots") or (),
        repositories=extra.get("repositories") or {},
        store_root=extra.get("store_root"),
        discovery=extra.get("discovery") or {},
        targets=extra.get("targets") or {},
    )
