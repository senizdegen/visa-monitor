from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

from alay.api.schemas import HealthOut

router = APIRouter()

_SQL = text(
    """
    SELECT
      (SELECT count(*) FROM workers WHERE last_seen_at > now() - interval '60 seconds') AS workers,
      (SELECT count(*) FROM check_jobs
        WHERE status = 'pending' AND created_at < now() - interval '5 minutes') AS stale
    """
)


@router.get("/health", response_model=HealthOut, responses={503: {"model": HealthOut}})
async def health(request: Request):
    try:
        async with request.app.state.sessionmaker() as session:
            row = (await session.execute(_SQL)).one()
            await session.commit()
    except Exception:  # noqa: BLE001  (any DB failure means "down")
        down = HealthOut(status="down", database=False, workers_alive=0, stale_pending_jobs=0)
        return JSONResponse(down.model_dump(), status_code=503)
    status = "ok" if row.workers > 0 and row.stale == 0 else "degraded"
    return HealthOut(status=status, database=True, workers_alive=row.workers, stale_pending_jobs=row.stale)
