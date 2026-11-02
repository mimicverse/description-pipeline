#!/usr/bin/env python3
"""Install or verify configured Airflow resource pools, including deferred work.

Installation preserves existing descriptions and rechecks committed capacity.
Health reads the same configured pools without changing their state.
"""

from __future__ import annotations

import argparse
import json
import os

DEFAULT_NAME = "windows_native"
DEFAULT_SLOTS = 1
DEFAULT_DESCRIPTION = "SolidWorks native stage; capacity 1 (seven-stage contract)"


def configured_pools(environment=None):
    """Derive active pools from the already verified generation environment."""
    environment = os.environ if environment is None else environment
    pools = [(DEFAULT_NAME, DEFAULT_SLOTS, DEFAULT_DESCRIPTION)]
    config = json.loads(environment.get("ASSESSMENT_CONFIG_JSON", '{"enabled":false}'))
    if not isinstance(config, dict) or type(config.get("enabled")) is not bool:
        raise ValueError("Assessment configuration must carry an explicit enable flag")
    if config["enabled"]:
        from description_pipeline.dynamics_assessment.contracts import POOL_ID

        capacity = config.get("capacity")
        if type(capacity) is not int or not 1 <= capacity <= 8:
            raise ValueError("Assessment pool capacity must be between 1 and 8")
        pools.append((POOL_ID, capacity, "Optional dynamics assessment; owned resource capacity"))
    return pools


def require_pool(session, pool_class, name, slots):
    """Read the actual pool without changing capacity during a health check."""
    pool = session.query(pool_class).filter(pool_class.pool == name).one_or_none()
    if pool is None or int(pool.slots) != int(slots) or not pool.include_deferred:
        raise SystemExit(f"pool {name!r} must retain deferred tasks within capacity {slots}")
    return pool


def ensure_pool(name, slots, description, *, session_factory=None, pool_class=None):
    """Create or update ``name`` so that it carries exactly ``slots`` slots.

    Returns ``{"name", "slots", "created"}``; raises ``SystemExit`` when the
    verified state does not match the requested capacity.
    """

    if session_factory is None:
        from airflow.utils.session import create_session

        session_factory = create_session
    if pool_class is None:
        from airflow.models.pool import Pool

        pool_class = Pool

    with session_factory() as session:
        pool = session.query(pool_class).filter(pool_class.pool == name).one_or_none()
        created = pool is None
        if created:
            pool = pool_class(pool=name, slots=slots, description=description, include_deferred=True)
            session.add(pool)
        else:
            pool.slots = slots
            pool.include_deferred = True
        session.commit()
        verified = require_pool(session, pool_class, name, slots)
        return {"name": name, "slots": int(verified.slots), "created": created}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Install or verify the generation's configured Airflow pools.")
    parser.add_argument("--check", action="store_true", help="Read and verify without creating or updating pools")
    args = parser.parse_args(argv)
    for name, slots, description in configured_pools():
        if args.check:
            from airflow.models.pool import Pool
            from airflow.utils.session import create_session

            with create_session() as session:
                require_pool(session, Pool, name, slots)
            print(f"pool {name}: capacity {slots} verified")
        else:
            result = ensure_pool(name, slots, description)
            print(f"pool {name}: capacity {slots} (created={result['created']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
