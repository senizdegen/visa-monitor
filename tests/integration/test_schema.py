import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from alay.db.models import AdapterRow, CheckJobRow, CheckRow, SlotEventRow, WorkerRow
from alay.db.repo_adapters import upsert_specs


async def test_tables_exist(sessionmaker):
    async with sessionmaker() as s:
        for model in (AdapterRow, CheckJobRow, CheckRow, SlotEventRow, WorkerRow):
            assert (await s.execute(select(model))).all() == []


async def test_one_active_job_per_adapter(sessionmaker, adapter_spec):
    async with sessionmaker() as s:
        await upsert_specs(s, [adapter_spec])
        await s.commit()
    async with sessionmaker() as s:
        first = CheckJobRow(adapter_id=adapter_spec.id, trigger="scheduled", status="pending")
        s.add(first)
        await s.commit()
        s.add(CheckJobRow(adapter_id=adapter_spec.id, trigger="manual", status="pending"))
        with pytest.raises(IntegrityError):
            await s.commit()
        await s.rollback()
        first.status = "done"
        await s.commit()
        s.add(CheckJobRow(adapter_id=adapter_spec.id, trigger="manual", status="pending"))
        await s.commit()
