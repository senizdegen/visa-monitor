from pathlib import Path
from urllib.parse import urlparse

import yaml
from pydantic import ValidationError
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from alay.adapters.vfs_global import login_allowed
from alay.db.models import AdapterRow
from alay.db.repo_adapters import upsert_specs
from alay.domain import AdapterSpec, Source


class AdaptersFileError(Exception):
    pass


def load_adapters_file(
    path: Path, default_interval: int, *, login_allowed_hosts: list[str]
) -> list[AdapterSpec]:
    try:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as e:
        raise AdaptersFileError(f"cannot read {path}: {e}") from e
    if not isinstance(data, dict):
        raise AdaptersFileError(f"{path}: top level must be a mapping with an 'adapters' key")
    if "adapters" not in data:
        raise AdaptersFileError(f"{path}: missing top-level 'adapters' key")
    entries = data["adapters"]
    if not isinstance(entries, list):
        raise AdaptersFileError(f"{path}: 'adapters' must be a list")
    specs: list[AdapterSpec] = []
    seen: set[str] = set()
    for i, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise AdaptersFileError(f"adapter #{i}: must be a mapping")
        label = entry.get("id") or f"#{i}"
        try:
            spec = AdapterSpec(**{"interval_minutes": default_interval, **entry})
        except ValidationError as e:
            problems = "; ".join(
                f"{'.'.join(str(p) for p in err['loc']) or '<root>'}: {err['msg']}" for err in e.errors()
            )
            raise AdaptersFileError(f"adapter {label}: {problems}") from e
        if spec.id in seen:
            raise AdaptersFileError(f"adapter {spec.id}: duplicate id (adapter #{i})")
        seen.add(spec.id)
        start_url = spec.config.get("start_url")
        if (
            spec.source is Source.LIVE
            and isinstance(start_url, str)
            and login_allowed(start_url, login_allowed_hosts)
        ):
            # Hosts in LOGIN_ALLOWED_HOSTS are the local training site: its pages are demo data
            # and must never be stored or shown as live observations.
            raise AdaptersFileError(
                f"adapter {spec.id}: source live, but start_url host {urlparse(start_url).hostname!r} "
                "is in LOGIN_ALLOWED_HOSTS (demo-only); use source: demo"
            )
        specs.append(spec)
    return specs


async def sync_adapters(session: AsyncSession, specs: list[AdapterSpec]) -> None:
    await upsert_specs(session, specs)
    ids = [s.id for s in specs]
    stmt = update(AdapterRow).where(AdapterRow.enabled.is_(True))
    if ids:
        stmt = stmt.where(AdapterRow.id.not_in(ids))
    await session.execute(stmt.values(enabled=False))
