import uuid
from datetime import UTC, datetime, timedelta

import httpx

from alay.api.main import create_app
from alay.config import Settings
from alay.db.models import AdapterRow, CheckJobRow, CheckRow, SlotEventRow
from alay.domain import Source
from alay.services.analytics import compute_analytics

NOW = datetime(2026, 11, 10, 12, 30, tzinfo=UTC)


async def seed_adapter(session, adapter_id="a1", source="live"):
    if await session.get(AdapterRow, adapter_id) is None:
        session.add(
            AdapterRow(
                id=adapter_id, adapter_type="fake", source=source, source_country="Turkey",
                destination_country="France", provider="P", center="C", visa_category="V",
                timezone="Europe/Istanbul", config={}, interval_minutes=60,
            )
        )
        await session.flush()


async def add_check(session, status, *, age=timedelta(hours=1), source="live", adapter_id="a1",
                    response_ms=None, duration_ms=1000, at=None, error=None):
    await seed_adapter(session, adapter_id, source)
    job = CheckJobRow(id=uuid.uuid4(), adapter_id=adapter_id, trigger="scheduled", status="done")
    session.add(job)
    await session.flush()
    row = CheckRow(
        job_id=job.id, adapter_id=adapter_id, checked_at=at or (NOW - age), status=status,
        source=source, response_time_ms=response_ms, duration_ms=duration_ms, error=error,
    )
    session.add(row)
    await session.flush()
    return row


async def run(sessionmaker, **kw):
    kw.setdefault("source", Source.LIVE)
    kw.setdefault("adapter_id", None)
    kw.setdefault("now", NOW)
    kw.setdefault("min_checks", 5)
    async with sessionmaker() as s:
        return await compute_analytics(s, **kw)


async def test_period_counts_and_success_rate(sessionmaker):
    async with sessionmaker() as s:
        for _ in range(8):
            await add_check(s, "CAPTCHA_REQUIRED")
        await add_check(s, "ERROR")
        await add_check(s, "UNKNOWN")
        await s.commit()
    out = await run(sessionmaker)
    p = out.periods["24h"]
    assert p.checks_total == 10
    assert p.by_status == {"CAPTCHA_REQUIRED": 8, "ERROR": 1, "UNKNOWN": 1}
    assert p.technical_success_rate == 0.8
    assert p.errors == 1
    assert p.insufficient_data is False


async def test_insufficient_data_nulls_rates(sessionmaker):
    async with sessionmaker() as s:
        for _ in range(3):
            await add_check(s, "NO_SLOTS", response_ms=100)
        await s.commit()
    p = (await run(sessionmaker)).periods["24h"]
    assert p.insufficient_data is True
    assert p.technical_success_rate is None
    assert p.avg_response_time_ms is None
    assert p.p95_response_time_ms is None
    assert p.avg_duration_ms is None
    assert p.checks_total == 3


async def test_live_and_demo_never_mixed(sessionmaker):
    async with sessionmaker() as s:
        for _ in range(5):
            await add_check(s, "NO_SLOTS")
        for _ in range(4):
            await add_check(s, "AVAILABLE", source="demo", adapter_id="d1")
        await s.commit()
    live = (await run(sessionmaker)).periods["24h"]
    demo = (await run(sessionmaker, source=Source.DEMO)).periods["24h"]
    assert live.available_observations == 0 and live.checks_total == 5
    assert demo.available_observations == 4 and demo.checks_total == 4


async def test_p95_and_avg_response_time(sessionmaker):
    async with sessionmaker() as s:
        for i in range(1, 21):
            await add_check(s, "NO_SLOTS", response_ms=i * 100, duration_ms=2000)
        await add_check(s, "ERROR", response_ms=None, duration_ms=1000)
        await s.commit()
    p = (await run(sessionmaker)).periods["24h"]
    assert p.avg_response_time_ms == 1050.0
    assert p.p95_response_time_ms == 1905.0  # percentile_cont(0.95) over 100..2000
    assert p.avg_duration_ms == 1952.4


async def test_periods_window_boundaries(sessionmaker):
    async with sessionmaker() as s:
        for _ in range(5):
            await add_check(s, "NO_SLOTS", age=timedelta(days=2))
        await add_check(s, "NO_SLOTS", age=timedelta(days=40))
        await s.commit()
    out = await run(sessionmaker)
    assert out.periods["24h"].checks_total == 0
    assert out.periods["7d"].checks_total == 5
    assert out.periods["30d"].checks_total == 5


async def test_slot_event_counts(sessionmaker):
    async with sessionmaker() as s:
        c = await add_check(s, "AVAILABLE")
        for src, age in (("live", 1), ("live", 48), ("demo", 1)):
            s.add(SlotEventRow(adapter_id="a1", check_id=c.id, detected_at=NOW - timedelta(hours=age),
                               event_type="APPEARED", source=src))
        await s.commit()
    out = await run(sessionmaker)
    assert out.periods["24h"].slot_events == 1
    assert out.periods["7d"].slot_events == 2


async def test_timeline_hourly_has_24_buckets_with_zero_fill(sessionmaker):
    async with sessionmaker() as s:
        await add_check(s, "AVAILABLE", at=NOW - timedelta(minutes=10))
        await add_check(s, "ERROR", at=NOW - timedelta(minutes=20))
        await add_check(s, "NO_SLOTS", at=NOW - timedelta(hours=5))
        await s.commit()
    out = await run(sessionmaker)
    tl = out.timeline
    assert len(tl) == 24
    assert tl[-1].bucket.isoformat() == "2026-11-10T15:00:00+03:00"
    assert tl[0].bucket.isoformat() == "2026-11-09T16:00:00+03:00"
    assert (tl[-1].checks, tl[-1].available, tl[-1].errors) == (2, 1, 1)
    assert tl[-1].by_status == {"AVAILABLE": 1, "ERROR": 1}
    assert sum(b.checks for b in tl) == 3
    assert tl[0].checks == 0 and tl[0].by_status == {}
    assert len((await run(sessionmaker, timeline="7d")).timeline) == 7
    assert len((await run(sessionmaker, timeline="30d")).timeline) == 30


async def test_captcha_limitation_message(sessionmaker):
    async with sessionmaker() as s:
        for _ in range(4):
            await add_check(s, "CAPTCHA_REQUIRED")
        await add_check(s, "LOGIN_REQUIRED")
        await add_check(s, "NO_SLOTS")
        await s.commit()
    out = await run(sessionmaker)
    assert "За 24ч 5 из 6 проверок заблокированы источником (CAPTCHA, вход или отказ в доступе): данные о слотах недоступны." in out.limitations
    assert "Нет проверок за период 24h." not in out.limitations
    assert "Нет проверок за период 7d." not in out.limitations


BLOCKED_MSG = "заблокированы источником"


async def test_access_denied_counts_as_blocked(sessionmaker):
    async with sessionmaker() as s:
        for _ in range(3):
            await add_check(s, "ERROR", error="access_denied_403")
        await add_check(s, "NO_SLOTS")
        await s.commit()
    out = await run(sessionmaker)
    assert (
        "За 24ч 3 из 4 проверок заблокированы источником (CAPTCHA, вход или отказ в доступе): "
        "данные о слотах недоступны."
    ) in out.limitations


async def test_timeout_errors_do_not_count_as_blocked(sessionmaker):
    async with sessionmaker() as s:
        for _ in range(3):
            await add_check(s, "ERROR", error="timeout: Page.goto")
        await add_check(s, "NO_SLOTS")
        await s.commit()
    out = await run(sessionmaker)
    assert not any(BLOCKED_MSG in m for m in out.limitations)


async def test_no_checks_messages_and_live_has_no_demo_message(sessionmaker):
    out = await run(sessionmaker, source=Source.DEMO)
    assert out.limitations == [
        "Нет проверок за период 24h.",
        "Нет проверок за период 7d.",
        "Нет проверок за период 30d.",
        "Демонстрационные данные mock-vfs, не реальные наблюдения.",
    ]
    live = await run(sessionmaker)
    assert "Демонстрационные данные mock-vfs, не реальные наблюдения." not in live.limitations


async def test_demo_limitation_message(sessionmaker):
    async with sessionmaker() as s:
        for _ in range(5):
            await add_check(s, "AVAILABLE", source="demo", adapter_id="d1")
        await s.commit()
    out = await run(sessionmaker, source=Source.DEMO)
    assert "Демонстрационные данные mock-vfs, не реальные наблюдения." in out.limitations


async def test_adapter_filter(sessionmaker):
    async with sessionmaker() as s:
        await add_check(s, "NO_SLOTS", adapter_id="a1")
        await add_check(s, "NO_SLOTS", adapter_id="a2")
        await s.commit()
    out = await run(sessionmaker, adapter_id="a2")
    assert out.periods["24h"].checks_total == 1


async def test_analytics_endpoint_defaults_to_live(sessionmaker):
    recent = datetime.now(UTC) - timedelta(seconds=5)
    async with sessionmaker() as s:
        await add_check(s, "NO_SLOTS", at=recent)
        await add_check(s, "AVAILABLE", source="demo", adapter_id="d1", at=recent)
        await s.commit()
    app = create_app(Settings(), sessionmaker)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://t") as c:
        r = await c.get("/analytics")
        assert r.status_code == 200
        body = r.json()
        assert body["source"] == "live"
        assert body["periods"]["24h"]["checks_total"] == 1
        assert len(body["timeline"]) == 24
        d = await c.get("/analytics", params={"source": "demo", "timeline": "7d"})
        assert d.json()["periods"]["24h"]["checks_total"] == 1
        assert len(d.json()["timeline"]) == 7
        for bad in ({"source": "x"}, {"timeline": "1h"}):
            assert (await c.get("/analytics", params=bad)).status_code == 422


async def test_timeline_bucket_edges_unchanged(sessionmaker):
    # First hourly bucket for NOW starts 2026-11-09T16:00+03:00 == 13:00 UTC.
    first = datetime(2026, 11, 9, 13, 0, tzinfo=UTC)
    async with sessionmaker() as s:
        await add_check(s, "NO_SLOTS", at=first)
        await add_check(s, "NO_SLOTS", at=first - timedelta(microseconds=1))
        await add_check(s, "NO_SLOTS", at=NOW)
        await add_check(s, "NO_SLOTS", at=NOW + timedelta(microseconds=1))
        await s.commit()
    tl = (await run(sessionmaker)).timeline
    assert tl[0].checks == 1
    assert tl[-1].checks == 1
    assert sum(b.checks for b in tl) == 2


async def test_timeline_query_bounds_checked_at_from_below(sessionmaker):
    import re

    from sqlalchemy import text

    async with sessionmaker() as s:
        await add_check(s, "NO_SLOTS")
        await s.commit()
        orig = s.execute
        captured = []

        async def spy(stmt, params=None, *a, **k):
            captured.append((stmt, params))
            return await orig(stmt, params, *a, **k)

        s.execute = spy
        await compute_analytics(s, source=Source.LIVE, adapter_id=None, now=NOW, min_checks=5)
        stmt, params = next((st, p) for st, p in captured if "generate_series" in str(st))
        plan = "\n".join(r[0] for r in await orig(text("EXPLAIN " + str(stmt)), params))
    assert re.search(r"checked_at >= ", plan), plan
