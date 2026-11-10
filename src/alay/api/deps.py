from collections.abc import AsyncIterator

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from alay.config import Settings
from alay.db.models import AdapterRow
from alay.db.repo_adapters import list_adapters


def get_settings_dep(request: Request) -> Settings:
    return request.app.state.settings


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    """Fresh session per request; repo functions commit their own transactions."""
    async with request.app.state.sessionmaker() as session:
        yield session


async def adapters_by_id(session: AsyncSession) -> dict[str, AdapterRow]:
    rows = await list_adapters(session)
    await session.commit()
    return {r.id: r for r in rows}
