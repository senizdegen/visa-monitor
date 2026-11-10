import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    ARRAY,
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    ForeignKey,
    Identity,
    Index,
    Integer,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import DateTime

NOW = text("now()")
TZ = DateTime(timezone=True)
SOURCES = "('live','demo')"


class Base(DeclarativeBase):
    pass


class AdapterRow(Base):
    __tablename__ = "adapters"
    __table_args__ = (
        CheckConstraint("interval_minutes > 0", name="ck_adapters_interval_positive"),
        CheckConstraint(f"source IN {SOURCES}", name="ck_adapters_source"),
        Index("ix_adapters_next_run_enabled", "next_run_at", postgresql_where=text("enabled")),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    adapter_type: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(Text)
    source_country: Mapped[str] = mapped_column(Text)
    destination_country: Mapped[str] = mapped_column(Text)
    provider: Mapped[str] = mapped_column(Text)
    center: Mapped[str] = mapped_column(Text)
    visa_category: Mapped[str] = mapped_column(Text)
    timezone: Mapped[str] = mapped_column(Text)
    config: Mapped[dict[str, Any]] = mapped_column(JSONB)
    interval_minutes: Mapped[int] = mapped_column(Integer)
    enabled: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    next_run_at: Mapped[datetime] = mapped_column(TZ, server_default=NOW)
    created_at: Mapped[datetime] = mapped_column(TZ, server_default=NOW)
    updated_at: Mapped[datetime] = mapped_column(TZ, server_default=NOW)


class CheckJobRow(Base):
    __tablename__ = "check_jobs"
    __table_args__ = (
        CheckConstraint("status IN ('pending','running','done','failed')", name="ck_check_jobs_status"),
        CheckConstraint("trigger IN ('scheduled','manual')", name="ck_check_jobs_trigger"),
        Index(
            "uq_check_jobs_active",
            "adapter_id",
            unique=True,
            postgresql_where=text("status IN ('pending','running')"),
        ),
        Index("ix_check_jobs_status_created", "status", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    adapter_id: Mapped[str] = mapped_column(ForeignKey("adapters.id"))
    trigger: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text)
    claim_count: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    locked_by: Mapped[str | None] = mapped_column(Text)
    locked_until: Mapped[datetime | None] = mapped_column(TZ)
    created_at: Mapped[datetime] = mapped_column(TZ, server_default=NOW)
    started_at: Mapped[datetime | None] = mapped_column(TZ)
    finished_at: Mapped[datetime | None] = mapped_column(TZ)
    error: Mapped[str | None] = mapped_column(Text)


class CheckRow(Base):
    __tablename__ = "checks"
    __table_args__ = (
        CheckConstraint(f"source IN {SOURCES}", name="ck_checks_source"),
        Index("ix_checks_adapter_checked", "adapter_id", text("checked_at DESC")),
        Index("ix_checks_checked_at", "checked_at"),
        Index("ix_checks_status", "status"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    job_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("check_jobs.id"), unique=True)
    adapter_id: Mapped[str] = mapped_column(ForeignKey("adapters.id"))
    checked_at: Mapped[datetime] = mapped_column(TZ)
    status: Mapped[str] = mapped_column(Text)
    available_dates: Mapped[list[date]] = mapped_column(ARRAY(Date), server_default=text("'{}'"))
    slots_count: Mapped[int | None] = mapped_column(Integer)
    has_waitlist: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    source: Mapped[str] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    response_time_ms: Mapped[int | None] = mapped_column(Integer)
    duration_ms: Mapped[int] = mapped_column(Integer)
    attempts: Mapped[int] = mapped_column(Integer, server_default=text("1"))
    evidence_path: Mapped[str | None] = mapped_column(Text)
    raw: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))


class SlotEventRow(Base):
    __tablename__ = "slot_events"
    __table_args__ = (
        CheckConstraint("event_type IN ('APPEARED','CHANGED','DISAPPEARED')", name="ck_slot_events_type"),
        CheckConstraint(f"source IN {SOURCES}", name="ck_slot_events_source"),
        Index("ix_slot_events_adapter_detected", "adapter_id", text("detected_at DESC")),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    adapter_id: Mapped[str] = mapped_column(ForeignKey("adapters.id"))
    check_id: Mapped[int] = mapped_column(ForeignKey("checks.id"))
    detected_at: Mapped[datetime] = mapped_column(TZ)
    event_type: Mapped[str] = mapped_column(Text)
    dates_before: Mapped[list[date]] = mapped_column(ARRAY(Date), server_default=text("'{}'"))
    dates_after: Mapped[list[date]] = mapped_column(ARRAY(Date), server_default=text("'{}'"))
    source: Mapped[str] = mapped_column(Text)


class WorkerRow(Base):
    __tablename__ = "workers"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    started_at: Mapped[datetime] = mapped_column(TZ, server_default=NOW)
    last_seen_at: Mapped[datetime] = mapped_column(TZ, server_default=NOW)
