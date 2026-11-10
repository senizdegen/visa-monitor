from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from alay.db.models import AdapterRow
from alay.domain import AdapterSpec

_SPEC_FIELDS = [
    "adapter_type", "source", "source_country", "destination_country", "provider",
    "center", "visa_category", "timezone", "config", "interval_minutes", "enabled",
]  # fmt: skip


async def upsert_specs(session: AsyncSession, specs: list[AdapterSpec]) -> None:
    """Insert or update by id. next_run_at is set (now()) only on insert."""
    for spec in specs:
        values = {"id": spec.id, **{f: getattr(spec, f) for f in _SPEC_FIELDS}}
        values["source"] = str(spec.source.value)
        stmt = insert(AdapterRow).values(**values)
        stmt = stmt.on_conflict_do_update(
            index_elements=[AdapterRow.id],
            set_={**{f: stmt.excluded[f] for f in _SPEC_FIELDS}, "updated_at": text("now()")},
        )
        await session.execute(stmt)


async def list_adapters(session: AsyncSession) -> list[AdapterRow]:
    return list((await session.scalars(select(AdapterRow).order_by(AdapterRow.id))).all())


async def get_adapter(session: AsyncSession, adapter_id: str) -> AdapterRow | None:
    return await session.get(AdapterRow, adapter_id, populate_existing=True)


def to_spec(row: AdapterRow) -> AdapterSpec:
    return AdapterSpec(id=row.id, **{f: getattr(row, f) for f in _SPEC_FIELDS})
