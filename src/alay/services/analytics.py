from datetime import datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from alay.api.schemas import AnalyticsOut, PeriodStats, TimelineBucket
from alay.domain import Source

TZ_NAME = "Europe/Istanbul"
PERIODS: dict[str, timedelta] = {"24h": timedelta(hours=24), "7d": timedelta(days=7), "30d": timedelta(days=30)}
# timeline -> (date_trunc unit, bucket count)
TIMELINES = {"24h": ("hour", 24), "7d": ("day", 7), "30d": ("day", 30)}

_AGG = text(
    """
    SELECT
      count(*) AS total,
      count(*) FILTER (WHERE status NOT IN ('ERROR','UNKNOWN')) AS ok,
      count(*) FILTER (WHERE status = 'ERROR') AS errors,
      count(*) FILTER (WHERE status = 'AVAILABLE') AS available,
      avg(response_time_ms) AS avg_rt,
      percentile_cont(0.95) WITHIN GROUP (ORDER BY response_time_ms) AS p95_rt,
      avg(duration_ms) AS avg_dur
    FROM checks
    WHERE source = :source AND (CAST(:adapter_id AS text) IS NULL OR adapter_id = CAST(:adapter_id AS text))
      AND checked_at > CAST(:since AS timestamptz) AND checked_at <= CAST(:now AS timestamptz)
    """
)
_BY_STATUS = text(
    """
    SELECT status, count(*) AS n FROM checks
    WHERE source = :source AND (CAST(:adapter_id AS text) IS NULL OR adapter_id = CAST(:adapter_id AS text))
      AND checked_at > CAST(:since AS timestamptz) AND checked_at <= CAST(:now AS timestamptz)
    GROUP BY status
    """
)
# "Blocked by the source": CAPTCHA / login wall, or an access refusal (403 / 429).
_BLOCKED = text(
    """
    SELECT count(*) FROM checks
    WHERE source = :source AND (CAST(:adapter_id AS text) IS NULL OR adapter_id = CAST(:adapter_id AS text))
      AND checked_at > CAST(:since AS timestamptz) AND checked_at <= CAST(:now AS timestamptz)
      AND (status IN ('CAPTCHA_REQUIRED', 'LOGIN_REQUIRED')
           OR (status = 'ERROR' AND error IN ('access_denied_403', 'rate_limited')))
    """
)
_EVENTS = text(
    """
    SELECT count(*) FROM slot_events
    WHERE source = :source AND (CAST(:adapter_id AS text) IS NULL OR adapter_id = CAST(:adapter_id AS text))
      AND detected_at > CAST(:since AS timestamptz) AND detected_at <= CAST(:now AS timestamptz)
    """
)
# unit is a validated constant (hour|day), never user input, so it is safe to inline.
# `lower` is the start of the first bucket as timestamptz: a range bound on checked_at so the
# planner can skip older rows; the bucket equality alone already excludes them.
_TIMELINE = """
    WITH w AS (
      SELECT date_trunc('{unit}', CAST(:now AS timestamptz) AT TIME ZONE CAST(:tz AS text))
             - (CAST(:n AS int) - 1) * CAST(:step AS interval) AS first_bucket
    ), b AS (
      SELECT generate_series(
        (SELECT first_bucket FROM w),
        date_trunc('{unit}', CAST(:now AS timestamptz) AT TIME ZONE CAST(:tz AS text)),
        CAST(:step AS interval)
      ) AS bucket
    )
    SELECT b.bucket, c.status, count(c.id) AS n
    FROM b LEFT JOIN checks c
      ON date_trunc('{unit}', c.checked_at AT TIME ZONE CAST(:tz AS text)) = b.bucket
     AND c.source = :source AND (CAST(:adapter_id AS text) IS NULL OR c.adapter_id = CAST(:adapter_id AS text))
     AND c.checked_at >= (SELECT first_bucket FROM w) AT TIME ZONE CAST(:tz AS text)
     AND c.checked_at <= CAST(:now AS timestamptz)
    GROUP BY b.bucket, c.status
    ORDER BY b.bucket
"""


def _round(v, digits: int) -> float | None:
    return None if v is None else round(float(v), digits)


async def compute_analytics(
    session: AsyncSession,
    *,
    source: Source,
    adapter_id: str | None,
    now: datetime,
    timeline: Literal["24h", "7d", "30d"] = "24h",
    min_checks: int,
) -> AnalyticsOut:
    periods: dict[str, PeriodStats] = {}
    limitations: list[str] = []
    for key, delta in PERIODS.items():
        p = {"source": source.value, "adapter_id": adapter_id, "since": now - delta, "now": now}
        agg = (await session.execute(_AGG, p)).one()
        by_status = {r.status: r.n for r in await session.execute(_BY_STATUS, p)}
        events = (await session.execute(_EVENTS, p)).scalar_one()
        insufficient = agg.total < min_checks
        periods[key] = PeriodStats(
            checks_total=agg.total,
            by_status=by_status,
            technical_success_rate=None if insufficient else round(agg.ok / agg.total, 3),
            errors=agg.errors,
            avg_response_time_ms=None if insufficient else _round(agg.avg_rt, 1),
            p95_response_time_ms=None if insufficient else _round(agg.p95_rt, 1),
            avg_duration_ms=None if insufficient else _round(agg.avg_dur, 1),
            available_observations=agg.available,
            slot_events=events,
            insufficient_data=insufficient,
        )
        if agg.total == 0:
            limitations.append(f"Нет проверок за период {key}.")

    day = periods["24h"]
    if day.checks_total:
        p = {"source": source.value, "adapter_id": adapter_id, "since": now - PERIODS["24h"], "now": now}
        blocked = (await session.execute(_BLOCKED, p)).scalar_one()
        if blocked * 2 > day.checks_total:
            limitations.append(
                f"За 24ч {blocked} из {day.checks_total} проверок заблокированы источником "
                "(CAPTCHA, вход или отказ в доступе): данные о слотах недоступны."
            )
    if source is Source.DEMO:
        limitations.append("Демонстрационные данные mock-vfs, не реальные наблюдения.")

    unit, n = TIMELINES[timeline]
    tz = ZoneInfo(TZ_NAME)
    rows = await session.execute(
        text(_TIMELINE.format(unit=unit)),
        {
            "now": now, "tz": TZ_NAME, "n": n, "step": timedelta(hours=1) if unit == "hour" else timedelta(days=1),
            "source": source.value, "adapter_id": adapter_id,
        },
    )
    buckets: dict[datetime, dict[str, int]] = {}
    for r in rows:
        counts = buckets.setdefault(r.bucket.replace(tzinfo=tz), {})
        if r.status is not None:
            counts[r.status] = r.n
    return AnalyticsOut(
        source=source.value,
        generated_at=now,
        periods=periods,
        timeline=[
            TimelineBucket(
                bucket=b, checks=sum(c.values()), available=c.get("AVAILABLE", 0),
                errors=c.get("ERROR", 0), by_status=c,
            )
            for b, c in buckets.items()
        ],
        limitations=limitations,
    )
