import uuid
from datetime import UTC, date, datetime
from typing import Any, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel

from alay.db.models import AdapterRow, CheckJobRow, CheckRow, SlotEventRow


def to_local(dt: datetime, tz: str) -> datetime:
    """Render a datetime in ``tz`` (naive input is taken as UTC)."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(ZoneInfo(tz))


def _opt(dt: datetime | None, tz: str) -> datetime | None:
    return None if dt is None else to_local(dt, tz)


class CheckOut(BaseModel):
    id: int
    adapter_id: str
    job_id: uuid.UUID
    source_country: str
    destination_country: str
    provider: str
    center: str
    visa_category: str
    checked_at: datetime
    status: str
    available_dates: list[date]
    slots_count: int | None
    has_waitlist: bool
    source: str
    error: str | None
    response_time_ms: int | None
    attempts: int
    duration_ms: int
    evidence_url: str | None
    raw: dict[str, Any]

    @classmethod
    def from_row(cls, row: CheckRow, adapter: AdapterRow) -> "CheckOut":
        return cls(
            id=row.id,
            adapter_id=row.adapter_id,
            job_id=row.job_id,
            source_country=adapter.source_country,
            destination_country=adapter.destination_country,
            provider=adapter.provider,
            center=adapter.center,
            visa_category=adapter.visa_category,
            checked_at=to_local(row.checked_at, adapter.timezone),
            status=row.status,
            available_dates=list(row.available_dates or []),
            slots_count=row.slots_count,
            has_waitlist=row.has_waitlist,
            source=row.source,
            error=row.error,
            response_time_ms=row.response_time_ms,
            attempts=row.attempts,
            duration_ms=row.duration_ms,
            evidence_url=f"/evidence/{row.evidence_path}" if row.evidence_path else None,
            raw=dict(row.raw or {}),
        )


class AdapterOut(BaseModel):
    id: str
    adapter_type: str
    source: str
    source_country: str
    destination_country: str
    provider: str
    center: str
    visa_category: str
    timezone: str
    interval_minutes: int
    enabled: bool
    next_run_at: datetime
    last_check: CheckOut | None = None

    @classmethod
    def from_row(cls, row: AdapterRow, last: CheckRow | None) -> "AdapterOut":
        return cls(
            id=row.id,
            adapter_type=row.adapter_type,
            source=row.source,
            source_country=row.source_country,
            destination_country=row.destination_country,
            provider=row.provider,
            center=row.center,
            visa_category=row.visa_category,
            timezone=row.timezone,
            interval_minutes=row.interval_minutes,
            enabled=row.enabled,
            next_run_at=to_local(row.next_run_at, row.timezone),
            last_check=None if last is None else CheckOut.from_row(last, row),
        )


class JobOut(BaseModel):
    id: uuid.UUID
    adapter_id: str
    trigger: str
    status: str
    claim_count: int
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    error: str | None
    result: CheckOut | None = None

    @classmethod
    def from_row(cls, job: CheckJobRow, adapter: AdapterRow, check: CheckRow | None) -> "JobOut":
        tz = adapter.timezone
        return cls(
            id=job.id,
            adapter_id=job.adapter_id,
            trigger=job.trigger,
            status=job.status,
            claim_count=job.claim_count,
            created_at=to_local(job.created_at, tz),
            started_at=_opt(job.started_at, tz),
            finished_at=_opt(job.finished_at, tz),
            error=job.error,
            result=None if check is None else CheckOut.from_row(check, adapter),
        )


class ManualCheckOut(BaseModel):
    job_id: uuid.UUID
    status: str
    deduplicated: bool
    result: CheckOut | None = None


class SlotEventOut(BaseModel):
    id: int
    adapter_id: str
    check_id: int
    detected_at: datetime
    event_type: str
    dates_before: list[date]
    dates_after: list[date]
    source: str

    @classmethod
    def from_row(cls, row: SlotEventRow, tz: str) -> "SlotEventOut":
        return cls(
            id=row.id,
            adapter_id=row.adapter_id,
            check_id=row.check_id,
            detected_at=to_local(row.detected_at, tz),
            event_type=row.event_type,
            dates_before=list(row.dates_before or []),
            dates_after=list(row.dates_after or []),
            source=row.source,
        )


class HealthOut(BaseModel):
    status: Literal["ok", "degraded", "down"]
    database: bool
    workers_alive: int
    stale_pending_jobs: int
