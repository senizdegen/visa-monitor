"""Worker process (spec §7): scheduler, executor and heartbeat loops sharing one Chromium."""

import asyncio
import logging
import os
import socket
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alay.adapters.base import AdapterContext
from alay.config import Settings
from alay.db.session import create_engine, make_sessionmaker
from alay.worker.browser import BrowserProvider
from alay.worker.executor import Executor
from alay.worker.heartbeat import heartbeat_loop
from alay.worker.scheduler import scheduler_loop

logger = logging.getLogger(__name__)


async def run_worker(
    settings: Settings,
    stop: asyncio.Event,
    *,
    sessionmaker: async_sessionmaker[AsyncSession] | None = None,
) -> None:
    """Run until ``stop`` is set. Builds (and disposes) its own engine unless ``sessionmaker`` is given."""
    engine = None
    if sessionmaker is None:
        engine = create_engine(settings.database_url)
        sessionmaker = make_sessionmaker(engine)
    worker_id = f"{socket.gethostname()}-{os.getpid()}"
    browser = BrowserProvider(settings.headful)
    ctx = AdapterContext(settings=settings, evidence_dir=Path(settings.evidence_dir), browser=browser)
    executor = Executor(sessionmaker, settings, ctx, worker_id)
    # The heartbeat outlives the executor drain, so the row disappears only once work is done.
    heartbeat_stop = asyncio.Event()

    async def execute() -> None:
        try:
            await executor.run(stop)
        finally:
            heartbeat_stop.set()

    logger.info("worker_started", extra={"worker_id": worker_id})
    try:
        async with asyncio.TaskGroup() as tg:
            tg.create_task(scheduler_loop(sessionmaker, settings, stop))
            tg.create_task(execute())
            tg.create_task(heartbeat_loop(sessionmaker, worker_id, settings, heartbeat_stop))
    finally:
        try:
            await browser.close()
        finally:
            if engine is not None:
                await engine.dispose()
    logger.info("worker_stopped", extra={"worker_id": worker_id})
