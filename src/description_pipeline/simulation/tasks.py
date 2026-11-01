"""Retain ownership of actual work until it settles, even after repeated cancellation."""

from __future__ import annotations

import asyncio
from anyio import CancelScope


async def settle_task(task: asyncio.Task) -> bool:
    """Wait without forwarding caller cancellation; report whether it was requested."""
    interrupted = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            interrupted = True
            break
        except BaseException:
            break
    if not task.done():
        # An enclosing AnyIO scope otherwise reissues cancellation at every
        # checkpoint. Shield only the remaining owned work, then rethrow it.
        with CancelScope(shield=True):
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    interrupted = True
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
            raise asyncio.CancelledError from error
        raise
    if interrupted:
        raise asyncio.CancelledError
    return result


async def run_thread(function, /, *args, **kwargs):
    """Keep a filesystem or durable-state thread owned until its actual completion."""
    return await wait_task(asyncio.create_task(asyncio.to_thread(function, *args, **kwargs)))
