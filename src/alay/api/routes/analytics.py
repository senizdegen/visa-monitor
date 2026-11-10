from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from alay.api.deps import get_session
from alay.api.schemas import AnalyticsOut
from alay.domain import Source
from alay.services.analytics import compute_analytics

router = APIRouter()


@router.get("/analytics", response_model=AnalyticsOut)
async def analytics(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    source: Literal["live", "demo"] = "live",
    adapter_id: str | None = None,
    timeline: Annotated[Literal["24h", "7d", "30d"], Query()] = "24h",
):
    out = await compute_analytics(
        session,
        source=Source(source),
        adapter_id=adapter_id or None,
        now=datetime.now(UTC),
        timeline=timeline,
        min_checks=request.app.state.settings.min_checks_for_stats,
    )
    await session.commit()
    return out
