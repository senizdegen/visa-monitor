from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from alay.api.deps import adapters_by_id, get_session
from alay.api.schemas import CheckOut, SlotEventOut
from alay.db import repo_checks
from alay.domain import CheckStatus

router = APIRouter()
Session = Annotated[AsyncSession, Depends(get_session)]


def _utc(dt: datetime | None) -> datetime | None:
    return dt.replace(tzinfo=UTC) if dt is not None and dt.tzinfo is None else dt


class Filters:
    def __init__(
        self,
        adapter_id: str | None = None,
        source: Literal["live", "demo"] | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: Annotated[int, Query(ge=1, le=500)] = 50,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> None:
        self.adapter_id, self.source = adapter_id, source
        self.since, self.until = _utc(since), _utc(until)
        self.limit, self.offset = limit, offset


@router.get("/checks", response_model=list[CheckOut])
async def checks(session: Session, f: Annotated[Filters, Depends()], status: CheckStatus | None = None):
    rows = await repo_checks.list_checks(
        session,
        adapter_id=f.adapter_id,
        status=status,
        source=f.source,
        since=f.since,
        until=f.until,
        limit=f.limit,
        offset=f.offset,
    )
    adapters = await adapters_by_id(session)
    return [CheckOut.from_row(c, adapters[c.adapter_id]) for c in rows]


@router.get("/slot-events", response_model=list[SlotEventOut])
async def slot_events(session: Session, f: Annotated[Filters, Depends()]):
    rows = await repo_checks.list_slot_events(
        session,
        adapter_id=f.adapter_id,
        source=f.source,
        since=f.since,
        until=f.until,
        limit=f.limit,
        offset=f.offset,
    )
    adapters = await adapters_by_id(session)
    return [SlotEventOut.from_row(e, adapters[e.adapter_id].timezone) for e in rows]
