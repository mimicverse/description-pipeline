"""Retain ownership of actual work until it settles, even after repeated cancellation."""

from __future__ import annotations

import asyncio
from anyio import CancelScope


async def settle_task(task: asyncio.Task) -> asyncio.CancelledError | None:
    """Wait without forwarding cancellation; retain its original scope identity and reason."""
    interrupted = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as error:
            interrupted = error
            break
        except BaseException:
            break
    if not task.done():
        # An enclosing AnyIO scope otherwise reissues cancellation at every
        # checkpoint. Shield only the remaining owned work, then propagate cancellation.
        with CancelScope(shield=True):
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError as error:
                    if interrupted is None:
                        interrupted = error
                except BaseException:
                    break
    return interrupted


async def wait_task(task: asyncio.Task):
    """Finish owned work before propagating caller cancellation or its actual failure."""
    interrupted = await settle_task(task)
    try:
        result = task.result()
    except BaseException as error:
        if interrupted:
            if interrupted is error:
                raise
            raise interrupted from error
        raise
    if interrupted:
        raise interrupted
    return result


async def run_thread(function, /, *args, **kwargs):
    """Keep a filesystem or durable-state thread owned until its actual completion."""
    return await wait_task(asyncio.create_task(asyncio.to_thread(function, *args, **kwargs)))
