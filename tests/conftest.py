import asyncio
import os
from pathlib import Path

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from alay.config import get_settings
from alay.db.session import create_engine, make_sessionmaker
from alay.domain import AdapterSpec

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def adapter_spec() -> AdapterSpec:
    return AdapterSpec(
        id="vfs-tur-fra-istanbul-tourism",
        adapter_type="vfs_global",
        source="live",
        source_country="Turkey",
        destination_country="France",
        provider="VFS Global",
        center="Istanbul",
        visa_category="Tourism",
        interval_minutes=60,
    )


@pytest_asyncio.fixture(scope="session")
async def engine():
    url = get_settings().test_database_url
    raw = create_async_engine(url, isolation_level="AUTOCOMMIT")
    async with raw.connect() as conn:
        await conn.execute(text("DROP SCHEMA public CASCADE"))
        await conn.execute(text("CREATE SCHEMA public"))
    await raw.dispose()

    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    old = os.environ.get("ALEMBIC_DATABASE_URL")
    os.environ["ALEMBIC_DATABASE_URL"] = url
    try:
        await asyncio.to_thread(command.upgrade, cfg, "head")
    finally:
        if old is None:
            os.environ.pop("ALEMBIC_DATABASE_URL", None)
        else:
            os.environ["ALEMBIC_DATABASE_URL"] = old

    eng = create_engine(url)
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def sessionmaker(engine):
    async with engine.begin() as conn:
        await conn.execute(
            text("TRUNCATE adapters, check_jobs, checks, slot_events, workers RESTART IDENTITY CASCADE")
        )
    return make_sessionmaker(engine)
