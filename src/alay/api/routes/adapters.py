import asyncio
import time
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from alay.api.deps import adapters_by_id, get_session, get_settings_dep
from alay.api.schemas import AdapterOut, CheckOut, ManualCheckOut
from alay.config import Settings
from alay.db import repo_adapters, repo_checks, repo_jobs

router = APIRouter()
Session = Annotated[AsyncSession, Depends(get_session)]


@router.get("/adapters", response_model=list[AdapterOut])
async def list_adapters(session: Session):
    rows = await repo_adapters.list_adapters(session)
    await session.commit()
    latest = {c.adapter_id: c for c in await repo_checks.latest_per_adapter(session, enabled_only=False)}
    return [AdapterOut.from_row(r, latest.get(r.id)) for r in rows]


@router.get("/availability", response_model=list[CheckOut])
async def availability(session: Session, source: Literal["live", "demo"] | None = None):
    checks = await repo_checks.latest_per_adapter(session, source=source, enabled_only=True)
    adapters = await adapters_by_id(session)
    return [CheckOut.from_row(c, adapters[c.adapter_id]) for c in checks]


@router.post("/adapters/{adapter_id}/check", response_model=ManualCheckOut, status_code=202)
async def manual_check(
    adapter_id: str,
    response: Response,
    session: Session,
    settings: Annotated[Settings, Depends(get_settings_dep)],
    request: Request,
    wait: bool = False,
):
    adapter = await repo_adapters.get_adapter(session, adapter_id)
    await session.commit()
    if adapter is None:
        raise HTTPException(404, f"adapter {adapter_id!r} not found")
    if adapter.source == "live" and not settings.live_checks_enabled:
        raise HTTPException(409, "live checks are disabled (LIVE_CHECKS_ENABLED=false)")
    job, dedup = await repo_jobs.create_manual_job(session, adapter_id)
    out = ManualCheckOut(job_id=job.id, status=job.status, deduplicated=dedup)
    if not wait:
        return out

    deadline = time.monotonic() + settings.manual_wait_s
    while True:
        async with request.app.state.sessionmaker() as s:
            check = await repo_checks.check_for_job(s, job.id)
            if check is not None:
                done = await repo_jobs.get_job(s, job.id)
                response.status_code = 200
                return ManualCheckOut(
                    job_id=job.id,
                    status=done.status if done else "done",
                    deduplicated=dedup,
                    result=CheckOut.from_row(check, adapter),
                )
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return out
        await asyncio.sleep(min(0.5, remaining))
