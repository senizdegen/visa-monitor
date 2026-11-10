"""Scheduler loop (spec §7.1): turn due adapters into pending jobs every ``scheduler_tick_s``."""

import asyncio
import logging

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alay.config import Settings
from alay.db.repo_jobs import enqueue_due
from alay.worker._wait import wait_stop

logger = logging.getLogger(__name__)


async def scheduler_loop(
    sm: async_sessionmaker[AsyncSession], settings: Settings, stop: asyncio.Event
) -> None:
    while not stop.is_set():
        try:
            async with sm() as session:
                created = await enqueue_due(session, live_enabled=settings.live_checks_enabled)
            if created:
                logger.info("jobs_enqueued", extra={"count": created})
        except Exception:
            logger.exception("scheduler_tick_failed")
        await wait_stop(stop, settings.scheduler_tick_s)
