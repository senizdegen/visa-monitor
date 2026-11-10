import asyncio
import signal

from alay.config import get_settings
from alay.logging import setup_logging
from alay.worker.main import run_worker


async def _main() -> None:
    settings = get_settings()
    setup_logging(settings.log_level)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # Windows event loops have no add_signal_handler
            signal.signal(sig, lambda *_: loop.call_soon_threadsafe(stop.set))
    await run_worker(settings, stop)


if __name__ == "__main__":
    asyncio.run(_main())
