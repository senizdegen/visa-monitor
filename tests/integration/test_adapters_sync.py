from datetime import UTC, datetime, timedelta

import pytest

from alay.db.models import CheckJobRow
from alay.db.repo_adapters import get_adapter, list_adapters, to_spec
from alay.services.adapters_sync import AdaptersFileError, load_adapters_file, sync_adapters


async def test_sync_inserts_and_sets_next_run_now(sessionmaker, adapter_spec):
    before = datetime.now(UTC)
    async with sessionmaker() as s:
        await sync_adapters(s, [adapter_spec])
        await s.commit()
    async with sessionmaker() as s:
        row = await get_adapter(s, adapter_spec.id)
        assert row is not None
        assert before - timedelta(seconds=5) <= row.next_run_at <= datetime.now(UTC) + timedelta(seconds=5)
        assert to_spec(row) == adapter_spec


async def test_sync_preserves_next_run_at_on_resync(sessionmaker, adapter_spec):
    async with sessionmaker() as s:
        await sync_adapters(s, [adapter_spec])
        row = await get_adapter(s, adapter_spec.id)
        later = datetime.now(UTC) + timedelta(hours=1)
        row.next_run_at = later
        await s.commit()
    async with sessionmaker() as s:
        await sync_adapters(s, [adapter_spec.model_copy(update={"interval_minutes": 15})])
        await s.commit()
    async with sessionmaker() as s:
        row = await get_adapter(s, adapter_spec.id)
        assert row.interval_minutes == 15
        assert row.next_run_at == later


async def test_sync_disables_removed_adapter_and_keeps_history(sessionmaker, adapter_spec):
    async with sessionmaker() as s:
        await sync_adapters(s, [adapter_spec])
        s.add(CheckJobRow(adapter_id=adapter_spec.id, trigger="manual", status="done"))
        await s.commit()
    async with sessionmaker() as s:
        await sync_adapters(s, [])
        await s.commit()
    async with sessionmaker() as s:
        rows = await list_adapters(s)
        assert [(r.id, r.enabled) for r in rows] == [(adapter_spec.id, False)]
        from sqlalchemy import func, select

        assert await s.scalar(select(func.count()).select_from(CheckJobRow)) == 1


ENTRY = """
adapters:
  - id: {id}
    adapter_type: vfs_global
    source: live
    source_country: Turkey
    destination_country: France
    provider: VFS Global
    center: Istanbul
    visa_category: Tourism
{extra}
"""


def test_load_file_applies_default_interval(tmp_path):
    p = tmp_path / "a.yaml"
    p.write_text(ENTRY.format(id="ok-one", extra=""), encoding="utf-8")
    specs = load_adapters_file(p, 42)
    assert [(s.id, s.interval_minutes) for s in specs] == [("ok-one", 42)]


def test_load_file_error_names_adapter(tmp_path):
    p = tmp_path / "a.yaml"
    p.write_text(ENTRY.format(id="bad-one", extra="    interval_minutes: 0"), encoding="utf-8")
    with pytest.raises(AdaptersFileError) as e:
        load_adapters_file(p, 60)
    assert "bad-one" in str(e.value)
    assert "interval_minutes" in str(e.value)


@pytest.mark.parametrize("content", ["", "- a\n- b\n", "adapter: []\n", "adapters: 5\n", "adapters:\n"])
def test_load_file_rejects_malformed_structure(tmp_path, content):
    p = tmp_path / "a.yaml"
    p.write_text(content, encoding="utf-8")
    with pytest.raises(AdaptersFileError):
        load_adapters_file(p, 60)


def test_load_file_explicit_empty_list_ok(tmp_path):
    p = tmp_path / "a.yaml"
    p.write_text("adapters: []\n", encoding="utf-8")
    assert load_adapters_file(p, 60) == []
