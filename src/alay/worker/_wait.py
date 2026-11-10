import asyncio


async def wait_stop(stop: asyncio.Event, timeout: float) -> bool:
    """Sleep up to ``timeout`` seconds, waking early when ``stop`` is set. Returns ``stop.is_set()``."""
    try:
        await asyncio.wait_for(stop.wait(), timeout)
    except TimeoutError:
        pass
    return stop.is_set()
