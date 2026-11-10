import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from alay.api.deps import get_session
from alay.api.schemas import JobOut
from alay.db import repo_adapters, repo_checks, repo_jobs

router = APIRouter()


@router.get("/jobs/{job_id}", response_model=JobOut)
async def get_job(job_id: uuid.UUID, session: Annotated[AsyncSession, Depends(get_session)]):
    job = await repo_jobs.get_job(session, job_id)
    if job is None:
        raise HTTPException(404, "job not found")
    adapter = await repo_adapters.get_adapter(session, job.adapter_id)
    await session.commit()
    check = await repo_checks.check_for_job(session, job_id)
    return JobOut.from_row(job, adapter, check)
