import asyncio
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, text

from alay.db.models import CheckJobRow, CheckRow
from alay.db.repo_adapters import upsert_specs
from alay.db.repo_jobs import (
    claim_jobs,
    create_manual_job,
    enqueue_due,
    fail_exhausted_jobs,
    get_job,
)
from alay.domain import CheckStatus, Source


async def _add(sessionmaker, *specs):
    async with sessionmaker() as s:
        await upsert_specs(s, list(specs))
        await s.commit()


async def _scalar(sessionmaker, sql, **params):
    async with sessionmaker() as s:
        return await s.scalar(text(sql), params)


async def _jobs(sessionmaker) -> list[CheckJobRow]:
    async with sessionmaker() as s:
        return list((await s.scalars(select(CheckJobRow).order_by(CheckJobRow.created_at))).all())


async def test_enqueue_due_creates_job_and_advances_next_run(sessionmaker, adapter_spec):
    await _add(sessionmaker, adapter_spec)
    async with sessionmaker() as s:
        assert await enqueue_due(s, live_enabled=True) == 1
    [job] = await _jobs(sessionmaker)
    assert (job.adapter_id, job.trigger, job.status, job.claim_count) == (
        adapter_spec.id, "scheduled", "pending", 0,
    )  # fmt: skip
    delta = await _scalar(sessionmaker, "SELECT extract(epoch FROM next_run_at - now()) FROM adapters")
    assert 3600 - 10 <= float(delta) <= 3600


async def test_enqueue_after_long_downtime_creates_one_job(sessionmaker, adapter_spec):
    await _add(sessionmaker, adapter_spec)
    async with sessionmaker() as s:
        await s.execute(text("UPDATE adapters SET next_run_at = now() - interval '5 hours'"))
        await s.commit()
    async with sessionmaker() as s:
        assert await enqueue_due(s, live_enabled=True) == 1
    async with sessionmaker() as s:
        assert await enqueue_due(s, live_enabled=True) == 0
    assert len(await _jobs(sessionmaker)) == 1


async def test_enqueue_skips_disabled_and_live_when_off(sessionmaker, adapter_spec):
    live = adapter_spec
    demo = adapter_spec.model_copy(update={"id": "demo", "source": Source.DEMO})
    disabled = adapter_spec.model_copy(update={"id": "off", "source": Source.DEMO, "enabled": False})
    await _add(sessionmaker, live, demo, disabled)
    live_before = await _scalar(sessionmaker, "SELECT next_run_at FROM adapters WHERE id = :i", i=live.id)

    async with sessionmaker() as s:
        assert await enqueue_due(s, live_enabled=False) == 1
    assert [j.adapter_id for j in await _jobs(sessionmaker)] == ["demo"]
    live_after = await _scalar(sessionmaker, "SELECT next_run_at FROM adapters WHERE id = :i", i=live.id)
    assert live_after == live_before

    async with sessionmaker() as s:
        assert await enqueue_due(s, live_enabled=True) == 1
    assert sorted(j.adapter_id for j in await _jobs(sessionmaker)) == ["demo", live.id]


async def test_enqueue_does_not_duplicate_active_job(sessionmaker, adapter_spec):
    await _add(sessionmaker, adapter_spec)
    async with sessionmaker() as s:
        await create_manual_job(s, adapter_spec.id)
    async with sessionmaker() as s:
        assert await enqueue_due(s, live_enabled=True) == 0
    jobs = await _jobs(sessionmaker)
    assert [(j.trigger, j.status) for j in jobs] == [("manual", "pending")]
    delta = await _scalar(sessionmaker, "SELECT extract(epoch FROM next_run_at - now()) FROM adapters")
    assert float(delta) > 3500


async def test_manual_job_dedup(sessionmaker, adapter_spec):
    await _add(sessionmaker, adapter_spec)
    async with sessionmaker() as s:
        first, dedup1 = await create_manual_job(s, adapter_spec.id)
    async with sessionmaker() as s:
        second, dedup2 = await create_manual_job(s, adapter_spec.id)
    assert (dedup1, dedup2) == (False, True)
    assert second.id == first.id
    assert first.trigger == "manual" and first.status == "pending" and first.created_at is not None


async def test_manual_job_concurrent_dedup(sessionmaker, adapter_spec):
    await _add(sessionmaker, adapter_spec)

    async def create():
        async with sessionmaker() as s:
            return await create_manual_job(s, adapter_spec.id)

    (a, da), (b, db) = await asyncio.gather(create(), create())
    assert a.id == b.id
    assert sorted([da, db]) == [False, True]
    assert len(await _jobs(sessionmaker)) == 1


async def test_concurrent_claims_never_share_a_job(sessionmaker, adapter_spec):
    specs = [adapter_spec.model_copy(update={"id": f"a{i}"}) for i in range(10)]
    await _add(sessionmaker, *specs)

    for _round in range(10):  # several rounds: a single race can be won before the rival starts
        async with sessionmaker() as s:
            await s.execute(text("UPDATE check_jobs SET status = 'done' WHERE status = 'running'"))
            await s.commit()
        for spec in specs:
            async with sessionmaker() as s:
                await create_manual_job(s, spec.id)

        async with sessionmaker() as sa, sessionmaker() as sb:
            # check out both connections up front so the two UPDATEs are really in flight together
            await sa.connection()
            await sb.connection()
            got_a, got_b = await asyncio.gather(
                claim_jobs(sa, worker_id="worker-a", limit=10, lease_s=180),
                claim_jobs(sb, worker_id="worker-b", limit=10, lease_s=180),
            )
        ids_a = {j.job_id for j in got_a}
        ids_b = {j.job_id for j in got_b}
        assert ids_a.isdisjoint(ids_b)
        assert len(ids_a | ids_b) == 10
        assert {j.spec.id for j in got_a + got_b} == {s.id for s in specs}
        assert all(j.claim_count == 1 and j.trigger == "manual" for j in got_a + got_b)
        jobs = {j.id: j for j in await _jobs(sessionmaker) if j.status == "running"}
        assert set(jobs) == ids_a | ids_b
        assert all(jobs[i].locked_by == "worker-a" and jobs[i].started_at for i in ids_a)
        assert all(jobs[i].locked_by == "worker-b" and jobs[i].started_at for i in ids_b)


async def test_expired_lease_is_reclaimed(sessionmaker, adapter_spec):
    await _add(sessionmaker, adapter_spec)
    async with sessionmaker() as s:
        job, _ = await create_manual_job(s, adapter_spec.id)
    async with sessionmaker() as s:
        [first] = await claim_jobs(s, worker_id="a", limit=5, lease_s=0)
    assert first.spec == adapter_spec and first.claim_count == 1
    async with sessionmaker() as s:
        started = (await get_job(s, job.id)).started_at
    async with sessionmaker() as s:
        [second] = await claim_jobs(s, worker_id="b", limit=5, lease_s=180)
    assert second.job_id == job.id and second.claim_count == 2
    async with sessionmaker() as s:
        assert await claim_jobs(s, worker_id="c", limit=5, lease_s=180) == []
    async with sessionmaker() as s:
        row = await get_job(s, job.id)
    assert (row.status, row.locked_by, row.claim_count) == ("running", "b", 2)
    assert row.started_at == started  # coalesce keeps the first start
    assert row.locked_until > datetime.now(UTC) + timedelta(seconds=100)


async def test_exhausted_job_fails_with_error_check(sessionmaker, adapter_spec):
    exhausted = adapter_spec
    retryable = adapter_spec.model_copy(update={"id": "retryable"})
    leased = adapter_spec.model_copy(update={"id": "leased"})
    await _add(sessionmaker, exhausted, retryable, leased)
    async with sessionmaker() as s:
        for adapter_id, claims, until in [
            (exhausted.id, 3, "now() - interval '1 minute'"),
            ("retryable", 1, "now() - interval '1 minute'"),
            ("leased", 3, "now() + interval '1 minute'"),
        ]:
            await s.execute(
                text(
                    "INSERT INTO check_jobs (adapter_id, trigger, status, claim_count, locked_by,"
                    f" locked_until, started_at) VALUES (:a, 'scheduled', 'running', :c, 'dead', {until}, now())"
                ),
                {"a": adapter_id, "c": claims},
            )
        await s.commit()

    async with sessionmaker() as s:
        assert await fail_exhausted_jobs(s, max_claims=3) == 1

    by_adapter = {j.adapter_id: j for j in await _jobs(sessionmaker)}
    failed = by_adapter[exhausted.id]
    assert (failed.status, failed.error, failed.locked_by, failed.locked_until) == (
        "failed", "worker_lost", None, None,
    )  # fmt: skip
    assert failed.finished_at is not None
    assert by_adapter["retryable"].status == "running"
    assert by_adapter["leased"].status == "running"

    async with sessionmaker() as s:
        checks = list((await s.scalars(select(CheckRow))).all())
    assert len(checks) == 1
    [check] = checks
    assert (check.job_id, check.adapter_id, check.status, check.error) == (
        failed.id, exhausted.id, CheckStatus.ERROR, "worker_lost",
    )  # fmt: skip
    assert (check.attempts, check.duration_ms, check.source) == (0, 0, "live")
    assert abs(check.checked_at - datetime.now(UTC)) < timedelta(seconds=30)

    async with sessionmaker() as s:
        assert await fail_exhausted_jobs(s, max_claims=3) == 0
        assert await s.scalar(select(func.count()).select_from(CheckRow)) == 1


async def test_get_job_missing_returns_none(sessionmaker):
    async with sessionmaker() as s:
        assert await get_job(s, uuid.uuid4()) is None
