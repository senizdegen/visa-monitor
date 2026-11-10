"""PostgreSQL job queue: scheduling, claiming with leases, worker-loss handling (spec §5.2, §7.1, §7.2).

Every function takes an ``AsyncSession`` and commits its own short transaction; callers
open a fresh session per operation. All time comparisons use the database clock
(``now()``) so that several workers agree on leases and due times.
"""

import uuid
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import func, literal_column, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from alay.db.models import AdapterRow, CheckJobRow
from alay.db.repo_adapters import to_spec
from alay.db.repo_checks import new_check_row
from alay.domain import AdapterSpec, CheckStatus, Source

ACTIVE = ("pending", "running")


@dataclass
class ClaimedJob:
    job_id: uuid.UUID
    spec: AdapterSpec
    trigger: str
    claim_count: int


async def enqueue_due(session: AsyncSession, *, live_enabled: bool) -> int:
    """§7.1 in one transaction (commits itself): one scheduled job per due adapter, then advance next_run_at.

    With ``live_enabled=False`` live adapters are not selected at all (no job, next_run_at untouched).
    Returns the number of jobs created (an adapter with an active job gets none, but is still advanced).
    """
    stmt = (
        select(AdapterRow.id)
        .where(AdapterRow.enabled, AdapterRow.next_run_at <= func.now())
        .order_by(AdapterRow.id)
        .with_for_update(skip_locked=True)
    )
    if not live_enabled:
        stmt = stmt.where(AdapterRow.source != str(Source.LIVE))
    ids = list((await session.scalars(stmt)).all())
    if not ids:
        await session.commit()
        return 0

    created = await session.scalars(
        insert(CheckJobRow)
        .values([{"adapter_id": i, "trigger": "scheduled", "status": "pending"} for i in ids])
        .on_conflict_do_nothing()
        .returning(CheckJobRow.id)
    )
    count = len(created.all())
    await session.execute(
        update(AdapterRow)
        .where(AdapterRow.id.in_(ids))
        .values(next_run_at=func.now() + AdapterRow.interval_minutes * literal_column("interval '1 minute'"))
    )
    await session.commit()
    return count


async def _active_job(session: AsyncSession, adapter_id: str) -> CheckJobRow | None:
    return await session.scalar(
        select(CheckJobRow)
        .where(CheckJobRow.adapter_id == adapter_id, CheckJobRow.status.in_(ACTIVE))
        .execution_options(populate_existing=True)
    )


async def create_manual_job(session: AsyncSession, adapter_id: str) -> tuple[CheckJobRow, bool]:
    """Create a pending manual job, or return the adapter's active job with ``deduplicated=True``.

    Race-safe: a concurrent insert hitting ``uq_check_jobs_active`` is re-selected. Commits itself.
    """
    existing = await _active_job(session, adapter_id)
    if existing is not None:
        await session.commit()
        return existing, True

    job = CheckJobRow(adapter_id=adapter_id, trigger="manual", status="pending")
    session.add(job)
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        existing = await _active_job(session, adapter_id)
        if existing is None:
            raise  # not a dedup conflict (e.g. unknown adapter_id)
        await session.commit()
        return existing, True
    await session.refresh(job)  # load server defaults (created_at, claim_count)
    await session.commit()
    return job, False


_CLAIM_SQL = text(
    """
    UPDATE check_jobs SET status = 'running', locked_by = :me,
           locked_until = now() + CAST(:lease AS interval),
           started_at = coalesce(started_at, now()),
           claim_count = claim_count + 1
    WHERE id IN (SELECT id FROM check_jobs
                 WHERE status = 'pending' OR (status = 'running' AND locked_until < now())
                 ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT :free)
    RETURNING id, adapter_id, trigger, claim_count, created_at
    """
)


async def claim_jobs(
    session: AsyncSession, *, worker_id: str, limit: int, lease_s: int
) -> list[ClaimedJob]:
    """§7.2: atomically lease up to ``limit`` pending or lease-expired jobs (commits itself).

    Callers should run ``fail_exhausted_jobs`` first so jobs past MAX_CLAIMS are failed, not reclaimed.
    """
    if limit <= 0:
        return []
    rows = (
        await session.execute(
            _CLAIM_SQL, {"me": worker_id, "lease": timedelta(seconds=lease_s), "free": limit}
        )
    ).all()
    if not rows:
        await session.commit()
        return []
    adapters = await session.scalars(
        select(AdapterRow)
        .where(AdapterRow.id.in_({r.adapter_id for r in rows}))
        .execution_options(populate_existing=True)
    )
    specs = {a.id: to_spec(a) for a in adapters}
    await session.commit()
    rows.sort(key=lambda r: r.created_at)
    return [ClaimedJob(r.id, specs[r.adapter_id], r.trigger, r.claim_count) for r in rows]


async def fail_exhausted_jobs(session: AsyncSession, *, max_claims: int) -> int:
    """Running jobs with an expired lease and ``claim_count >= max_claims`` → ``failed``/``worker_lost``.

    Each also gets a ``checks`` row ``ERROR worker_lost`` (attempts 0, duration 0). Commits itself.
    Returns the number of jobs failed.
    """
    jobs = list(
        (
            await session.scalars(
                select(CheckJobRow)
                .where(
                    CheckJobRow.status == "running",
                    CheckJobRow.locked_until < func.now(),
                    CheckJobRow.claim_count >= max_claims,
                )
                .with_for_update(skip_locked=True)
                .execution_options(populate_existing=True)
            )
        ).all()
    )
    if not jobs:
        await session.commit()
        return 0
    adapters = await session.scalars(
        select(AdapterRow).where(AdapterRow.id.in_({j.adapter_id for j in jobs}))
    )
    specs = {a.id: to_spec(a) for a in adapters}
    for job in jobs:
        result = specs[job.adapter_id].result(CheckStatus.ERROR, error="worker_lost")
        session.add(
            new_check_row(
                job_id=job.id, adapter_id=job.adapter_id, result=result, attempts=0, duration_ms=0
            )
        )
        job.status = "failed"
        job.error = "worker_lost"
        job.finished_at = func.now()
        job.locked_by = None
        job.locked_until = None
    await session.commit()
    return len(jobs)


async def get_job(session: AsyncSession, job_id: uuid.UUID) -> CheckJobRow | None:
    """Fresh copy of the job row, or None. Commits itself."""
    job = await session.get(CheckJobRow, job_id, populate_existing=True)
    await session.commit()
    return job
