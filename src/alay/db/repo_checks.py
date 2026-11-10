"""Check results and slot events (spec §5.3, §5.4, §7.3).

Every function takes an ``AsyncSession`` and commits (or rolls back) its own short
transaction; callers open a fresh session per operation.
"""

import logging
import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Select, func, select
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.ext.asyncio import AsyncSession

from alay.db.models import AdapterRow, CheckJobRow, CheckRow, SlotEventRow
from alay.domain import AdapterSpec, CheckResult
from alay.services.slot_events import compute_slot_event

if TYPE_CHECKING:  # avoid importing adapters/playwright into API processes
    from alay.services.checks import CheckOutcome

logger = logging.getLogger(__name__)


def new_check_row(
    *, job_id: uuid.UUID, adapter_id: str, result: CheckResult, attempts: int, duration_ms: int
) -> CheckRow:
    """Build (not add) a ``checks`` row carrying every ``CheckResult`` field."""
    return CheckRow(
        job_id=job_id,
        adapter_id=adapter_id,
        checked_at=result.checked_at,
        status=str(result.status),
        available_dates=list(result.available_dates),
        slots_count=result.slots_count,
        has_waitlist=result.has_waitlist,
        source=str(result.source),
        error=result.error,
        response_time_ms=result.response_time_ms,
        duration_ms=duration_ms,
        attempts=attempts,
        evidence_path=result.evidence_path,
        raw=dict(result.raw),
    )


async def save_outcome(
    session: AsyncSession,
    *,
    job_id: uuid.UUID,
    worker_id: str,
    spec: AdapterSpec,
    outcome: "CheckOutcome",
) -> bool:
    """Persist a check result and finish its job in one transaction (commits itself).

    Returns False (and rolls back) if the job is no longer running under ``worker_id``
    — i.e. the lease was taken over by another worker.
    """
    job = await session.scalar(
        select(CheckJobRow)
        .where(CheckJobRow.id == job_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if job is None or job.status != "running" or job.locked_by != worker_id:
        await session.rollback()
        logger.warning(
            "lease lost, result discarded",
            extra={"job_id": str(job_id), "adapter_id": spec.id, "worker_id": worker_id},
        )
        return False

    result = outcome.result
    check = new_check_row(
        job_id=job_id,
        adapter_id=spec.id,
        result=result,
        attempts=outcome.attempts,
        duration_ms=outcome.duration_ms,
    )
    session.add(check)
    await session.flush()  # assigns check.id

    previous = await session.scalar(
        select(SlotEventRow.dates_after)
        .where(SlotEventRow.adapter_id == spec.id)
        .order_by(SlotEventRow.detected_at.desc(), SlotEventRow.id.desc())
        .limit(1)
    )
    event = compute_slot_event(list(previous or []), result)
    if event is not None:
        kind, before, after = event
        session.add(
            SlotEventRow(
                adapter_id=spec.id,
                check_id=check.id,
                detected_at=result.checked_at,
                event_type=str(kind),
                dates_before=before,
                dates_after=after,
                source=str(result.source),
            )
        )

    job.status = "done"
    job.finished_at = func.now()
    job.locked_by = None
    job.locked_until = None
    await session.commit()
    return True


def _window(stmt: Select, column, since: datetime | None, until: datetime | None) -> Select:
    if since is not None:
        stmt = stmt.where(column >= since)
    if until is not None:
        stmt = stmt.where(column < until)
    return stmt


async def list_checks(
    session: AsyncSession,
    *,
    adapter_id: str | None = None,
    status: str | None = None,
    source: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[CheckRow]:
    """Checks newest first (checked_at DESC, id DESC); since inclusive, until exclusive. Commits itself."""
    stmt = select(CheckRow)
    if adapter_id is not None:
        stmt = stmt.where(CheckRow.adapter_id == adapter_id)
    if status is not None:
        stmt = stmt.where(CheckRow.status == str(status))
    if source is not None:
        stmt = stmt.where(CheckRow.source == str(source))
    stmt = _window(stmt, CheckRow.checked_at, since, until)
    stmt = stmt.order_by(CheckRow.checked_at.desc(), CheckRow.id.desc()).limit(limit).offset(offset)
    rows = list((await session.scalars(stmt)).all())
    await session.commit()
    return rows


async def latest_per_adapter(
    session: AsyncSession, *, source: str | None = None, enabled_only: bool = True
) -> list[CheckRow]:
    """Most recent check of each adapter (DISTINCT ON adapter_id). Commits itself."""
    stmt = select(CheckRow).ext(distinct_on(CheckRow.adapter_id))
    if enabled_only:
        stmt = stmt.join(AdapterRow, AdapterRow.id == CheckRow.adapter_id).where(AdapterRow.enabled)
    if source is not None:
        stmt = stmt.where(CheckRow.source == str(source))
    stmt = stmt.order_by(CheckRow.adapter_id, CheckRow.checked_at.desc(), CheckRow.id.desc())
    rows = list((await session.scalars(stmt)).all())
    await session.commit()
    return rows


async def list_slot_events(
    session: AsyncSession,
    *,
    adapter_id: str | None = None,
    source: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[SlotEventRow]:
    """Slot events newest first (detected_at DESC, id DESC); since inclusive, until exclusive. Commits itself."""
    stmt = select(SlotEventRow)
    if adapter_id is not None:
        stmt = stmt.where(SlotEventRow.adapter_id == adapter_id)
    if source is not None:
        stmt = stmt.where(SlotEventRow.source == str(source))
    stmt = _window(stmt, SlotEventRow.detected_at, since, until)
    stmt = stmt.order_by(SlotEventRow.detected_at.desc(), SlotEventRow.id.desc())
    rows = list((await session.scalars(stmt.limit(limit).offset(offset))).all())
    await session.commit()
    return rows


async def check_for_job(session: AsyncSession, job_id: uuid.UUID) -> CheckRow | None:
    """The check row written for ``job_id``, if any. Commits itself."""
    row = await session.scalar(select(CheckRow).where(CheckRow.job_id == job_id))
    await session.commit()
    return row
