import asyncio
import os
import socket
import time

import pytest
from sqlalchemy import func, select, text

from alay.config import Settings
from alay.db.models import CheckJobRow, CheckRow, WorkerRow
from alay.db.repo_adapters import upsert_specs
from alay.db.repo_jobs import create_manual_job
from alay.domain import AdapterSpec, CheckStatus
from alay.worker.main import run_worker
from tests.fakes import FakeAdapter

WORKER_ID = f"{socket.gethostname()}-{os.getpid()}"


@pytest.fixture(autouse=True)
def _reset():
    FakeAdapter.reset()


def fake_spec(adapter_id: str, steps: list[str]) -> AdapterSpec:
    return AdapterSpec(
        id=adapter_id, adapter_type="fake", source="demo", source_country="Turkey",
        destination_country="France", provider="P", center="C", visa_category="T",
        interval_minutes=60, config={"steps": steps},
    )  # fmt: skip


def settings(tmp_path, **overrides) -> Settings:
    values = {
        "scheduler_tick_s": 0.1,
        "executor_idle_s": 0.1,
        "heartbeat_s": 0.1,
        "retry_wait_base_s": 0,
        "shutdown_grace_s": 5,
        "evidence_dir": str(tmp_path),
        **overrides,
    }
    return Settings(_env_file=None, **values)


async def _add(sessionmaker, *specs):
    async with sessionmaker() as s:
        await upsert_specs(s, list(specs))
        await s.commit()


async def _checks(sessionmaker) -> list[CheckRow]:
    async with sessionmaker() as s:
        return list((await s.scalars(select(CheckRow).order_by(CheckRow.adapter_id))).all())


async def _jobs(sessionmaker) -> list[CheckJobRow]:
    async with sessionmaker() as s:
        return list((await s.scalars(select(CheckJobRow).order_by(CheckJobRow.created_at))).all())


async def _until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if await predicate():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("condition not met in time")


class Worker:
    """Runs run_worker in a background task; `stop()` sets the event and waits for it to return."""

    def __init__(self, s: Settings, sm):
        self.event = asyncio.Event()
        self.task = asyncio.create_task(run_worker(s, self.event, sessionmaker=sm))

    async def stop(self, timeout=10.0) -> float:
        started = time.monotonic()
        self.event.set()
        await asyncio.wait_for(self.task, timeout)
        return time.monotonic() - started


async def test_scheduled_check_runs_end_to_end(sessionmaker, tmp_path):
    await _add(sessionmaker, fake_spec("fake-a", ["NO_SLOTS"]))
    w = Worker(settings(tmp_path), sessionmaker)
    await asyncio.sleep(1.5)
    await w.stop()

    [check] = await _checks(sessionmaker)
    assert (check.adapter_id, check.status, check.attempts) == ("fake-a", "NO_SLOTS", 1)
    [job] = await _jobs(sessionmaker)
    assert (job.id, job.trigger, job.status) == (check.job_id, "scheduled", "done")


async def test_manual_job_processed(sessionmaker, tmp_path):
    await _add(sessionmaker, fake_spec("fake-a", ["WAITLIST"]))
    async with sessionmaker() as s:  # not due: only the manual job may run
        await s.execute(text("UPDATE adapters SET next_run_at = now() + interval '1 hour'"))
        await s.commit()
    async with sessionmaker() as s:
        job, _ = await create_manual_job(s, "fake-a")

    w = Worker(settings(tmp_path), sessionmaker)

    async def done():
        return [j.status for j in await _jobs(sessionmaker)] == ["done"]

    await _until(done)
    await w.stop()

    [check] = await _checks(sessionmaker)
    assert (check.job_id, check.status) == (job.id, "WAITLIST")
    [row] = await _jobs(sessionmaker)
    assert row.trigger == "manual"


async def test_heartbeat_row_present_then_removed_on_stop(sessionmaker, tmp_path):
    w = Worker(settings(tmp_path), sessionmaker)

    async def beating():
        async with sessionmaker() as s:
            row = await s.get(WorkerRow, WORKER_ID, populate_existing=True)
            return row is not None and row.last_seen_at > row.started_at

    await _until(beating)  # row exists and has been refreshed at least once
    await w.stop()

    async with sessionmaker() as s:
        assert await s.scalar(select(func.count()).select_from(WorkerRow)) == 0


async def test_stop_waits_for_inflight_check(sessionmaker, tmp_path):
    await _add(sessionmaker, fake_spec("fake-a", ["sleep:1", "NO_SLOTS"]))
    w = Worker(settings(tmp_path), sessionmaker)

    async def running():
        return [j.status for j in await _jobs(sessionmaker)] == ["running"]

    await _until(running)
    elapsed = await w.stop()

    assert 0.3 < elapsed < 5  # waited for the check, well inside shutdown_grace_s
    [check] = await _checks(sessionmaker)
    assert check.status == "NO_SLOTS"
    [job] = await _jobs(sessionmaker)
    assert job.status == "done"


async def test_inflight_check_cancelled_after_grace(sessionmaker, tmp_path):
    await _add(sessionmaker, fake_spec("fake-a", ["sleep:30"]))
    w = Worker(settings(tmp_path, shutdown_grace_s=0.3), sessionmaker)

    async def running():
        return [j.status for j in await _jobs(sessionmaker)] == ["running"]

    await _until(running)
    elapsed = await w.stop()

    assert elapsed < 3
    assert await _checks(sessionmaker) == []
    [job] = await _jobs(sessionmaker)
    assert (job.status, job.locked_by) == ("running", WORKER_ID)  # lease kept → reclaimed later


async def test_one_failing_adapter_does_not_block_others(sessionmaker, tmp_path):
    await _add(sessionmaker, fake_spec("fake-a", ["crash"]), fake_spec("fake-b", ["NO_SLOTS"]))
    w = Worker(settings(tmp_path), sessionmaker)

    async def both():
        return len(await _checks(sessionmaker)) == 2

    await _until(both)
    await w.stop()

    a, b = await _checks(sessionmaker)
    assert (a.adapter_id, a.status) == ("fake-a", CheckStatus.ERROR)
    assert a.error.startswith("adapter_crash: RuntimeError")
    assert (b.adapter_id, b.status) == ("fake-b", CheckStatus.NO_SLOTS)
    assert [j.status for j in await _jobs(sessionmaker)] == ["done", "done"]


class FlakySessionmaker:
    """Fails the first `failures` session creations with a connection-like error."""

    def __init__(self, sm, failures: int):
        self.sm, self.failures = sm, failures

    def __call__(self):
        if self.failures > 0:
            self.failures -= 1
            raise ConnectionError("db down")
        return self.sm()


async def test_loops_survive_db_errors(sessionmaker, tmp_path):
    await _add(sessionmaker, fake_spec("fake-a", ["NO_SLOTS"]))
    w = Worker(settings(tmp_path), FlakySessionmaker(sessionmaker, failures=6))

    async def checked():
        return len(await _checks(sessionmaker)) == 1

    await _until(checked)
    await w.stop()
    assert w.task.result() is None
