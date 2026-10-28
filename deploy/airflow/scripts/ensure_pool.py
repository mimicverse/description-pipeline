#!/usr/bin/env python3
"""Ensure the native-stage Airflow pool exists with the contract capacity.

Contract: ``docs/contracts.md`` stage contract (native operations use a
capacity-1 pool; waiting for native results releases Airflow worker slots)
and frozen interface IF-02.  The DAG's native tasks wait on pool
``windows_native``; a missing pool makes the first triggered run fail, so
installation creates it idempotently right after ``airflow db migrate``.

Idempotency: a missing pool is created with its slots and description; an
existing pool keeps its description while capacity and deferred occupancy are
enforced. Deferred native work retains its pool slot without occupying an
Airflow worker. The pool is re-read after
commit and verified; anything else exits non-zero so installation fails closed.
"""

from __future__ import annotations

import argparse

DEFAULT_NAME = "windows_native"
DEFAULT_SLOTS = 1
DEFAULT_DESCRIPTION = "SolidWorks native stage; capacity 1 (six-stage contract)"


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
        verified = session.query(pool_class).filter(pool_class.pool == name).one_or_none()
        if verified is None or int(verified.slots) != int(slots) or not verified.include_deferred:
            raise SystemExit(f"pool {name!r} must retain deferred tasks within capacity {slots}")
        return {"name": name, "slots": int(verified.slots), "created": created}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Ensure the native-stage Airflow pool exists.")
    parser.add_argument("--name", default=DEFAULT_NAME)
    parser.add_argument("--slots", type=int, default=DEFAULT_SLOTS)
    parser.add_argument("--description", default=DEFAULT_DESCRIPTION)
    args = parser.parse_args(argv)
    if args.slots < 1:
        raise SystemExit("--slots must be a positive integer")
    result = ensure_pool(args.name, args.slots, args.description)
    print(f"pool {result['name']}: capacity {result['slots']} (created={result['created']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
