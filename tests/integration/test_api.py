import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, timedelta

import httpx
import pytest
from sqlalchemy import text

from alay.adapters.base import AdapterContext
from alay.api.main import create_app
from alay.config import Settings
from alay.db.repo_adapters import upsert_specs
from alay.db.repo_checks import save_outcome
from alay.db.repo_jobs import claim_jobs
from alay.domain import AdapterSpec, CheckStatus
from alay.services.checks import CheckOutcome, run_check
from tests import fakes  # noqa: F401  (registers "fake" adapter type)

T0 = datetime(2026, 11, 10, 9, 0, tzinfo=UTC)


@asynccontextmanager
async def client_for(sessionmaker, **overrides):
    settings = Settings(**overrides)
    app = create_app(settings, sessionmaker)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://t") as c:
        yield c


def variant(spec: AdapterSpec, **kw) -> AdapterSpec:
    return AdapterSpec.model_validate({**spec.model_dump(), **kw})


@pytest.fixture
def live(adapter_spec):
    return adapter_spec


@pytest.fixture
def demo(adapter_spec):
    return variant(
        adapter_spec, id="demo-1", source="demo", adapter_type="fake", config={"steps": ["NO_SLOTS"]}
    )


async def _add(sm, *specs):
    async with sm() as s:
        await upsert_specs(s, list(specs))
        await s.commit()


async def _process(sm, spec, status=CheckStatus.NO_SLOTS, *, at=T0, **fields):
    """Do what a worker does for one pending job of ``spec``."""
    async with sm() as s:
        [claimed] = await claim_jobs(s, worker_id="w", limit=5, lease_s=180, max_claims=3)
    result = spec.result(status, checked_at=at, **fields)
    async with sm() as s:
        await save_outcome(
            s,
            job_id=claimed.job_id,
            worker_id="w",
            spec=spec,
            outcome=CheckOutcome(result, attempts=1, duration_ms=12),
        )
    return claimed.job_id


async def _run_job(sm, spec, *args, **kw):
    async with client_for(sm) as c:
        r = await c.post(f"/adapters/{spec.id}/check")
    await _process(sm, spec, *args, **kw)
    return r.json()["job_id"]


async def test_list_adapters_with_last_status(sessionmaker, live, demo):
    await _add(sessionmaker, live, demo)
    await _run_job(sessionmaker, demo)
    async with client_for(sessionmaker) as c:
        r = await c.get("/adapters")
    assert r.status_code == 200
    by_id = {a["id"]: a for a in r.json()}
    assert by_id[live.id]["last_check"] is None
    assert by_id["demo-1"]["last_check"]["status"] == "NO_SLOTS"
    assert by_id["demo-1"]["interval_minutes"] == 60 and "next_run_at" in by_id["demo-1"]


async def test_manual_check_202_and_dedup(sessionmaker, demo):
    await _add(sessionmaker, demo)
    async with client_for(sessionmaker) as c:
        r1 = await c.post("/adapters/demo-1/check")
        r2 = await c.post("/adapters/demo-1/check")
    assert r1.status_code == 202 and r2.status_code == 202
    assert r1.json()["deduplicated"] is False and r1.json()["status"] == "pending"
    assert r2.json()["job_id"] == r1.json()["job_id"] and r2.json()["deduplicated"] is True


async def test_manual_check_404_unknown_adapter(sessionmaker):
    async with client_for(sessionmaker) as c:
        r = await c.post("/adapters/nope/check")
    assert r.status_code == 404


async def test_manual_check_409_live_when_disabled(sessionmaker, live):
    await _add(sessionmaker, live)
    async with client_for(sessionmaker, live_checks_enabled=False) as c:
        r = await c.post(f"/adapters/{live.id}/check")
    assert r.status_code == 409
    assert "LIVE_CHECKS_ENABLED" in r.json()["detail"]


async def test_manual_check_allowed_for_disabled_adapter(sessionmaker, demo):
    await _add(sessionmaker, variant(demo, enabled=False))
    async with client_for(sessionmaker) as c:
        r = await c.post("/adapters/demo-1/check")
    assert r.status_code == 202


async def test_wait_returns_202_when_no_worker(sessionmaker, demo):
    await _add(sessionmaker, demo)
    async with client_for(sessionmaker, manual_wait_s=1) as c:
        t = asyncio.get_running_loop().time()
        r = await c.post("/adapters/demo-1/check?wait=true")
        elapsed = asyncio.get_running_loop().time() - t
    assert r.status_code == 202 and r.json()["result"] is None
    assert 0.9 <= elapsed < 2.5


async def test_wait_returns_200_with_result(sessionmaker, demo, tmp_path):
    await _add(sessionmaker, demo)

    async def worker():
        for _ in range(40):
            async with sessionmaker() as s:
                claimed = await claim_jobs(s, worker_id="w", limit=1, lease_s=180, max_claims=3)
            if claimed:
                outcome = await run_check(demo, AdapterContext(Settings(), tmp_path))
                async with sessionmaker() as s:
                    await save_outcome(
                        s, job_id=claimed[0].job_id, worker_id="w", spec=demo, outcome=outcome
                    )
                return
            await asyncio.sleep(0.1)

    task = asyncio.create_task(worker())
    async with client_for(sessionmaker, manual_wait_s=10) as c:
        r = await c.post("/adapters/demo-1/check?wait=true")
    await task
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "done" and body["result"]["status"] == "NO_SLOTS"
    assert body["result"]["adapter_id"] == "demo-1"


async def test_get_job_with_result(sessionmaker, demo):
    await _add(sessionmaker, demo)
    async with client_for(sessionmaker) as c:
        job_id = (await c.post("/adapters/demo-1/check")).json()["job_id"]
        pending = (await c.get(f"/jobs/{job_id}")).json()
        await _process(sessionmaker, demo)
        done = (await c.get(f"/jobs/{job_id}")).json()
    assert pending["status"] == "pending" and pending["result"] is None
    assert pending["trigger"] == "manual"
    assert done["status"] == "done" and done["result"]["job_id"] == job_id


async def test_get_job_404(sessionmaker):
    async with client_for(sessionmaker) as c:
        r = await c.get("/jobs/00000000-0000-0000-0000-000000000000")
    assert r.status_code == 404


async def test_checks_filters_pagination_and_limit_cap(sessionmaker, demo):
    other = variant(demo, id="demo-2")
    await _add(sessionmaker, demo, other)
    for i, spec in enumerate([demo, demo, demo, other]):
        async with client_for(sessionmaker) as c:
            await c.post(f"/adapters/{spec.id}/check")
        status = CheckStatus.WAITLIST if i == 1 else CheckStatus.NO_SLOTS
        await _process(sessionmaker, spec, status, at=T0 + timedelta(hours=i))
    async with client_for(sessionmaker) as c:
        allr = (await c.get("/checks")).json()
        d1 = (await c.get("/checks", params={"adapter_id": "demo-1"})).json()
        wl = (await c.get("/checks", params={"status": "WAITLIST"})).json()
        page = (await c.get("/checks", params={"limit": 1, "offset": 1})).json()
        src = (await c.get("/checks", params={"source": "live"})).json()
        bad = await c.get("/checks", params={"limit": 501})
        zero = await c.get("/checks", params={"limit": 0})
    assert len(allr) == 4 and allr[0]["adapter_id"] == "demo-2"  # newest first
    assert len(d1) == 3 and len(wl) == 1 and wl[0]["has_waitlist"] is True
    assert [p["id"] for p in page] == [allr[1]["id"]]
    assert src == []
    assert bad.status_code == 422 and zero.status_code == 422


async def test_naive_since_treated_as_utc(sessionmaker, demo):
    await _add(sessionmaker, demo)
    await _run_job(sessionmaker, demo, at=T0)  # 09:00 UTC
    async with client_for(sessionmaker) as c:
        inc = (await c.get("/checks", params={"since": "2026-11-10T09:00:00"})).json()
        exc = (await c.get("/checks", params={"since": "2026-11-10T09:00:01"})).json()
        # 09:00 naive == 09:00 UTC, not 09:00 +03:00 (06:00 UTC)
        until = (await c.get("/checks", params={"until": "2026-11-10T09:00:00"})).json()
        aware = (await c.get("/checks", params={"until": "2026-11-10T12:00:01+03:00"})).json()
    assert len(inc) == 1 and exc == [] and until == [] and len(aware) == 1


async def test_times_rendered_in_adapter_timezone(sessionmaker, demo):
    await _add(sessionmaker, demo)
    job_id = await _run_job(sessionmaker, demo, at=T0)
    async with client_for(sessionmaker) as c:
        chk = (await c.get("/checks")).json()[0]
        job = (await c.get(f"/jobs/{job_id}")).json()
        adapters = (await c.get("/adapters")).json()
    assert chk["checked_at"].endswith("+03:00") and chk["checked_at"].startswith("2026-11-10T12:00:00")
    assert job["created_at"].endswith("+03:00")
    assert adapters[0]["next_run_at"].endswith("+03:00")


async def test_availability_latest_per_adapter(sessionmaker, live, demo):
    off = variant(demo, id="demo-off", enabled=False)
    await _add(sessionmaker, live, demo, off)
    await _run_job(sessionmaker, demo, at=T0)
    await _run_job(
        sessionmaker, demo, CheckStatus.AVAILABLE, at=T0 + timedelta(hours=1), available_dates=[date(2026, 12, 1)]
    )
    await _run_job(sessionmaker, off)
    async with client_for(sessionmaker) as c:
        r = (await c.get("/availability")).json()
        live_only = (await c.get("/availability", params={"source": "live"})).json()
    assert [x["adapter_id"] for x in r] == ["demo-1"]  # disabled and check-less adapters omitted
    assert r[0]["status"] == "AVAILABLE" and r[0]["available_dates"] == ["2026-12-01"]
    assert live_only == []


async def test_slot_events_endpoint(sessionmaker, demo):
    await _add(sessionmaker, demo)
    await _run_job(sessionmaker, demo, CheckStatus.AVAILABLE, at=T0, available_dates=[date(2026, 12, 1)])
    async with client_for(sessionmaker) as c:
        r = (await c.get("/slot-events")).json()
        none = (await c.get("/slot-events", params={"source": "live"})).json()
    assert len(r) == 1 and r[0]["event_type"] == "APPEARED"
    assert r[0]["dates_after"] == ["2026-12-01"] and r[0]["detected_at"].endswith("+03:00")
    assert none == []


async def test_health_ok_degraded_down(sessionmaker, demo):
    async with client_for(sessionmaker) as c:
        r = await c.get("/health")
        assert r.status_code == 200 and r.json()["status"] == "degraded"
        assert r.json()["workers_alive"] == 0
        async with sessionmaker() as s:
            await s.execute(text("INSERT INTO workers (id) VALUES ('w1')"))
            await s.commit()
        r = await c.get("/health")
        assert r.json() == {"status": "ok", "database": True, "workers_alive": 1, "stale_pending_jobs": 0}
        # stale heartbeat -> degraded
        async with sessionmaker() as s:
            await s.execute(text("UPDATE workers SET last_seen_at = now() - interval '61 seconds'"))
            await s.commit()
        assert (await c.get("/health")).json()["status"] == "degraded"
        # fresh worker but a pending job older than 5 min -> degraded
        await _add(sessionmaker, demo)
        await c.post("/adapters/demo-1/check")
        async with sessionmaker() as s:
            await s.execute(text("UPDATE workers SET last_seen_at = now()"))
            await s.execute(text("UPDATE check_jobs SET created_at = now() - interval '6 minutes'"))
            await s.commit()
        r = (await c.get("/health")).json()
        assert r["status"] == "degraded" and r["stale_pending_jobs"] == 1

    def broken():
        raise RuntimeError("db down")

    async with client_for(broken) as c:
        r = await c.get("/health")
    assert r.status_code == 503
    assert r.json()["status"] == "down" and r.json()["database"] is False


async def test_swagger_available(sessionmaker):
    async with client_for(sessionmaker) as c:
        spec = (await c.get("/openapi.json")).json()
        docs = await c.get("/docs")
    assert docs.status_code == 200
    assert {"/adapters", "/adapters/{adapter_id}/check", "/jobs/{job_id}", "/checks", "/availability",
            "/slot-events", "/health"} <= set(spec["paths"])  # fmt: skip
