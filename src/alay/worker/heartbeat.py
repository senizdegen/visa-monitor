"""Heartbeat loop (spec §7.4): keep this worker's ``workers`` row fresh; delete it on exit."""

import asyncio
import logging

from sqlalchemy import delete, func
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alay.config import Settings
from alay.db.models import WorkerRow
from alay.worker._wait import wait_stop

logger = logging.getLogger(__name__)


async def _beat(sm: async_sessionmaker[AsyncSession], worker_id: str) -> None:
    stmt = insert(WorkerRow).values(id=worker_id)
    stmt = stmt.on_conflict_do_update(index_elements=[WorkerRow.id], set_={"last_seen_at": func.now()})
    async with sm() as session:
        await session.execute(stmt)
        await session.commit()


async def _remove(sm: async_sessionmaker[AsyncSession], worker_id: str) -> None:
    async with sm() as session:
        await session.execute(delete(WorkerRow).where(WorkerRow.id == worker_id))
        await session.commit()


async def heartbeat_loop(
    sm: async_sessionmaker[AsyncSession], worker_id: str, settings: Settings, stop: asyncio.Event
) -> None:
    try:
        while not stop.is_set():
            try:
                await _beat(sm, worker_id)
            except Exception:
                logger.exception("heartbeat_failed", extra={"worker_id": worker_id})
            await wait_stop(stop, settings.heartbeat_s)
    finally:
        try:
            await _remove(sm, worker_id)
        except Exception:
            logger.exception("worker_row_delete_failed", extra={"worker_id": worker_id})
