from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import func, select, text

from alay.db.models import SlotEventRow
from alay.db.repo_adapters import upsert_specs
from alay.db.repo_checks import (
    check_for_job,
    latest_per_adapter,
    list_checks,
    list_slot_events,
    save_outcome,
)
from alay.db.repo_jobs import claim_jobs, create_manual_job, get_job
from alay.domain import CheckStatus, Source
from alay.services.checks import CheckOutcome

T0 = datetime(2026, 11, 10, 9, 0, tzinfo=UTC)
A, B = date(2026, 12, 1), date(2026, 12, 2)


async def _add(sessionmaker, *specs):
    async with sessionmaker() as s:
        await upsert_specs(s, list(specs))
        await s.commit()


async def _claimed(sessionmaker, spec, worker="w1"):
    async with sessionmaker() as s:
        job, _ = await create_manual_job(s, spec.id)
    async with sessionmaker() as s:
        [claimed] = await claim_jobs(s, worker_id=worker, limit=1, lease_s=180, max_claims=3)
    assert claimed.job_id == job.id
    return claimed


async def _run(sessionmaker, spec, status, *, at, **fields):
    claimed = await _claimed(sessionmaker, spec)
    outcome = CheckOutcome(spec.result(status, checked_at=at, **fields), attempts=1, duration_ms=10)
    async with sessionmaker() as s:
        assert await save_outcome(s, job_id=claimed.job_id, worker_id="w1", spec=spec, outcome=outcome)
    return claimed.job_id


async def test_save_outcome_writes_check_and_marks_done(sessionmaker, adapter_spec):
    await _add(sessionmaker, adapter_spec)
    claimed = await _claimed(sessionmaker, adapter_spec)
    result = adapter_spec.result(
        CheckStatus.AVAILABLE,
        checked_at=T0,
        available_dates=[B, A],
        response_time_ms=321,
        evidence_path="evidence/x.png",
        raw={"final_url": "https://example.test/x", "http": 200},
    )
    outcome = CheckOutcome(result, attempts=2, duration_ms=4567)
    async with sessionmaker() as s:
        ok = await save_outcome(
            s, job_id=claimed.job_id, worker_id="w1", spec=adapter_spec, outcome=outcome
        )
    assert ok is True

    async with sessionmaker() as s:
        check = await check_for_job(s, claimed.job_id)
    assert check is not None
    assert (check.adapter_id, check.job_id, check.checked_at, check.status) == (
        adapter_spec.id, claimed.job_id, T0, "AVAILABLE",
    )  # fmt: skip
    assert check.available_dates == [A, B] and check.slots_count == 2
    assert (check.has_waitlist, check.source, check.error) == (False, "live", None)
    assert (check.response_time_ms, check.duration_ms, check.attempts) == (321, 4567, 2)
    assert check.evidence_path == "evidence/x.png"
    assert check.raw == {"final_url": "https://example.test/x", "http": 200}

    async with sessionmaker() as s:
        job = await get_job(s, claimed.job_id)
    assert (job.status, job.locked_by, job.locked_until) == ("done", None, None)
    assert job.finished_at is not None

    async with sessionmaker() as s:
        [event] = await list_slot_events(s, adapter_id=adapter_spec.id)
    assert (event.event_type, event.dates_before, event.dates_after) == ("APPEARED", [], [A, B])
    assert (event.check_id, event.detected_at, event.source) == (check.id, T0, "live")


async def test_save_outcome_rejected_when_lease_stolen(sessionmaker, adapter_spec):
    await _add(sessionmaker, adapter_spec)
    claimed = await _claimed(sessionmaker, adapter_spec)
    async with sessionmaker() as s:
        await s.execute(text("UPDATE check_jobs SET locked_by = 'w2'"))
        await s.commit()
    outcome = CheckOutcome(adapter_spec.result(CheckStatus.NO_SLOTS), attempts=1, duration_ms=5)
    async with sessionmaker() as s:
        ok = await save_outcome(
            s, job_id=claimed.job_id, worker_id="w1", spec=adapter_spec, outcome=outcome
        )
    assert ok is False
    async with sessionmaker() as s:
        assert await check_for_job(s, claimed.job_id) is None
        job = await get_job(s, claimed.job_id)
    assert (job.status, job.locked_by) == ("running", "w2")


async def test_save_outcome_rejected_when_job_not_running(sessionmaker, adapter_spec):
    await _add(sessionmaker, adapter_spec)
    job_id = await _run(sessionmaker, adapter_spec, CheckStatus.NO_SLOTS, at=T0)
    outcome = CheckOutcome(adapter_spec.result(CheckStatus.NO_SLOTS), attempts=1, duration_ms=5)
    async with sessionmaker() as s:
        assert not await save_outcome(
            s, job_id=job_id, worker_id="w1", spec=adapter_spec, outcome=outcome
        )


async def test_save_outcome_event_sequence(sessionmaker, adapter_spec):
    await _add(sessionmaker, adapter_spec)
    steps = [
        (CheckStatus.NO_SLOTS, {}),
        (CheckStatus.AVAILABLE, {"available_dates": [A, B]}),
        (CheckStatus.AVAILABLE, {"available_dates": [B, A]}),
        (CheckStatus.CAPTCHA_REQUIRED, {}),
        (CheckStatus.AVAILABLE, {"available_dates": [B]}),
        (CheckStatus.NO_SLOTS, {}),
    ]
    for i, (status, fields) in enumerate(steps):
        await _run(sessionmaker, adapter_spec, status, at=T0 + timedelta(minutes=i), **fields)

    async with sessionmaker() as s:
        events = await list_slot_events(s, adapter_id=adapter_spec.id)
        assert await s.scalar(select(func.count()).select_from(SlotEventRow)) == 3
        checks = await list_checks(s, adapter_id=adapter_spec.id)
    events.reverse()
    assert [(e.event_type, e.dates_before, e.dates_after) for e in events] == [
        ("APPEARED", [], [A, B]),
        ("CHANGED", [A, B], [B]),
        ("DISAPPEARED", [B], []),
    ]
    assert [e.detected_at for e in events] == [T0 + timedelta(minutes=m) for m in (1, 4, 5)]
    check_ids = {c.checked_at: c.id for c in checks}
    assert [e.check_id for e in events] == [check_ids[e.detected_at] for e in events]


async def test_list_checks_filters_and_order(sessionmaker, adapter_spec):
    live = adapter_spec
    demo = adapter_spec.model_copy(update={"id": "demo", "source": Source.DEMO})
    await _add(sessionmaker, live, demo)
    await _run(sessionmaker, live, CheckStatus.NO_SLOTS, at=T0)
    await _run(sessionmaker, demo, CheckStatus.ERROR, at=T0 + timedelta(hours=1), error="http_503")
    await _run(sessionmaker, live, CheckStatus.AVAILABLE, at=T0 + timedelta(hours=2), available_dates=[A])
    await _run(sessionmaker, demo, CheckStatus.NO_SLOTS, at=T0 + timedelta(hours=2))  # same time, higher id

    def key(rows):
        return [(r.adapter_id, r.status, r.checked_at - T0) for r in rows]

    h = timedelta(hours=1)
    async with sessionmaker() as s:
        assert key(await list_checks(s)) == [
            ("demo", "NO_SLOTS", 2 * h),
            (live.id, "AVAILABLE", 2 * h),
            ("demo", "ERROR", h),
            (live.id, "NO_SLOTS", 0 * h),
        ]
        assert key(await list_checks(s, adapter_id=live.id)) == [
            (live.id, "AVAILABLE", 2 * h),
            (live.id, "NO_SLOTS", 0 * h),
        ]
        assert [r.status for r in await list_checks(s, status=CheckStatus.NO_SLOTS)] == ["NO_SLOTS"] * 2
        assert {r.adapter_id for r in await list_checks(s, source="demo")} == {"demo"}
        # since inclusive, until exclusive
        assert key(await list_checks(s, since=T0 + h, until=T0 + 2 * h)) == [("demo", "ERROR", h)]
        assert len(await list_checks(s, since=T0 + h)) == 3
        assert key(await list_checks(s, limit=2, offset=1)) == [
            (live.id, "AVAILABLE", 2 * h),
            ("demo", "ERROR", h),
        ]


async def test_list_slot_events_filters(sessionmaker, adapter_spec):
    live = adapter_spec
    demo = adapter_spec.model_copy(update={"id": "demo", "source": Source.DEMO})
    await _add(sessionmaker, live, demo)
    await _run(sessionmaker, live, CheckStatus.AVAILABLE, at=T0, available_dates=[A])
    await _run(sessionmaker, demo, CheckStatus.AVAILABLE, at=T0 + timedelta(hours=1), available_dates=[B])
    async with sessionmaker() as s:
        assert [e.adapter_id for e in await list_slot_events(s)] == ["demo", live.id]
        assert [e.adapter_id for e in await list_slot_events(s, source="live")] == [live.id]
        assert [e.adapter_id for e in await list_slot_events(s, since=T0, until=T0 + timedelta(hours=1))] == [live.id]
        assert [e.adapter_id for e in await list_slot_events(s, limit=1, offset=1)] == [live.id]


async def test_latest_per_adapter(sessionmaker, adapter_spec):
    live = adapter_spec
    demo = adapter_spec.model_copy(update={"id": "demo", "source": Source.DEMO})
    off = adapter_spec.model_copy(update={"id": "off", "source": Source.DEMO})
    await _add(sessionmaker, live, demo, off)
    await _run(sessionmaker, live, CheckStatus.NO_SLOTS, at=T0 + timedelta(hours=2))
    await _run(sessionmaker, live, CheckStatus.AVAILABLE, at=T0 + timedelta(hours=1), available_dates=[A])
    await _run(sessionmaker, demo, CheckStatus.ERROR, at=T0, error="timeout")
    await _run(sessionmaker, demo, CheckStatus.WAITLIST, at=T0 + timedelta(hours=3))
    await _run(sessionmaker, off, CheckStatus.UNKNOWN, at=T0)
    async with sessionmaker() as s:
        await s.execute(text("UPDATE adapters SET enabled = false WHERE id = 'off'"))
        await s.commit()

    async with sessionmaker() as s:
        rows = await latest_per_adapter(s)
        assert sorted((r.adapter_id, r.status) for r in rows) == [
            ("demo", "WAITLIST"),
            (live.id, "NO_SLOTS"),
        ]
        rows = await latest_per_adapter(s, enabled_only=False)
        assert sorted((r.adapter_id, r.status) for r in rows) == [
            ("demo", "WAITLIST"),
            ("off", "UNKNOWN"),
            (live.id, "NO_SLOTS"),
        ]
        rows = await latest_per_adapter(s, source="live")
        assert [(r.adapter_id, r.status) for r in rows] == [(live.id, "NO_SLOTS")]


async def test_save_outcome_rejects_spec_of_other_adapter(sessionmaker, adapter_spec):
    other = adapter_spec.model_copy(update={"id": "other"})
    await _add(sessionmaker, adapter_spec, other)
    claimed = await _claimed(sessionmaker, adapter_spec)
    outcome = CheckOutcome(other.result(CheckStatus.NO_SLOTS), attempts=1, duration_ms=5)
    async with sessionmaker() as s:
        with pytest.raises(ValueError, match="adapter"):
            await save_outcome(s, job_id=claimed.job_id, worker_id="w1", spec=other, outcome=outcome)
    async with sessionmaker() as s:
        assert await check_for_job(s, claimed.job_id) is None
        assert (await get_job(s, claimed.job_id)).status == "running"
