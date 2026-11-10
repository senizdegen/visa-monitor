import asyncio
import logging
from pathlib import Path

from alay.config import get_settings
from alay.db.session import create_engine, make_sessionmaker
from alay.logging import setup_logging
from alay.services.adapters_sync import load_adapters_file, sync_adapters

log = logging.getLogger("alay.sync_adapters")


async def main() -> None:
    settings = get_settings()
    setup_logging(settings.log_level)
    specs = load_adapters_file(
        Path(settings.adapters_file),
        settings.default_interval_minutes,
        login_allowed_hosts=settings.login_allowed_hosts,
    )
    engine = create_engine(settings.database_url)
    try:
        async with make_sessionmaker(engine)() as session:
            await sync_adapters(session, specs)
            await session.commit()
    finally:
        await engine.dispose()
    log.info("adapters synced", extra={"count": len(specs)})


if __name__ == "__main__":
    asyncio.run(main())
