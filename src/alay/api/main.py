from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from alay.api.routes import adapters, analytics, checks, dashboard, health, jobs
from alay.config import Settings, get_settings
from alay.db.session import create_engine, make_sessionmaker


def create_app(settings: Settings | None = None, sessionmaker=None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        engine = None
        if sessionmaker is None:  # engine is created here, so importing this module never connects
            engine = create_engine(settings.database_url)
            app.state.sessionmaker = make_sessionmaker(engine)
        try:
            yield
        finally:
            if engine is not None:
                await engine.dispose()

    app = FastAPI(title="alay visa monitor", lifespan=lifespan)
    app.state.settings = settings
    app.state.sessionmaker = sessionmaker
    for r in (adapters.router, jobs.router, checks.router, health.router, analytics.router, dashboard.router):
        app.include_router(r)
    app.mount(
        "/evidence", StaticFiles(directory=Path(settings.evidence_dir), check_dir=False), name="evidence"
    )
    return app


app = create_app()
