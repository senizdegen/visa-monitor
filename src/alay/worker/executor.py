"""Executor (spec §7.2): claim jobs up to ``worker_concurrency``, run each check, save the outcome."""

import asyncio
import logging

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alay.adapters.base import AdapterContext
from alay.config import Settings
from alay.db.repo_checks import save_outcome
from alay.db.repo_jobs import ClaimedJob, claim_jobs, fail_exhausted_jobs
from alay.services.checks import run_check

logger = logging.getLogger(__name__)


class Executor:
    def __init__(
        self,
        sm: async_sessionmaker[AsyncSession],
        settings: Settings,
        ctx: AdapterContext,
        worker_id: str,
    ) -> None:
        self._sm = sm
        self._settings = settings
        self._ctx = ctx
        self._worker_id = worker_id
        self._sem = asyncio.Semaphore(settings.worker_concurrency)
        self._tasks: set[asyncio.Task[None]] = set()

    async def run(self, stop: asyncio.Event) -> None:
        """Claim and run jobs until ``stop``; then let in-flight checks finish for up to
        ``shutdown_grace_s`` and cancel the rest (their jobs keep the lease and get reclaimed)."""
        try:
            while not stop.is_set():
                try:
                    claimed = await self._claim()
                except Exception:
                    logger.exception("claim_failed", extra={"worker_id": self._worker_id})
                    claimed = []
                for job in claimed:  # already leased to us: run them even if stop was just set
                    self._spawn(job)
                if not claimed:
                    await self._idle(stop)
        except BaseException:
            await self._cancel(self._tasks)
            raise
        await self._drain()

    async def _claim(self) -> list[ClaimedJob]:
        s = self._settings
        async with self._sm() as session:
            failed = await fail_exhausted_jobs(session, max_claims=s.max_claims)
        if failed:
            logger.warning("jobs_failed_worker_lost", extra={"count": failed})
        free = s.worker_concurrency - len(self._tasks)
        if free <= 0:
            return []
        async with self._sm() as session:
            return await claim_jobs(
                session,
                worker_id=self._worker_id,
                limit=free,
                lease_s=s.job_lease_s,
                max_claims=s.max_claims,
            )

    def _spawn(self, job: ClaimedJob) -> None:
        task = asyncio.create_task(self._process(job), name=f"check-{job.job_id}")
        self._tasks.add(task)
        task.add_done_callback(self._task_done)

    def _task_done(self, task: asyncio.Task[None]) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and (exc := task.exception()) is not None:
            logger.error("job_task_failed", exc_info=exc)

    async def _process(self, job: ClaimedJob) -> None:
        async with self._sem:
            outcome = await run_check(job.spec, self._ctx)
            result = outcome.result
            extra = {"job_id": str(job.job_id), "adapter_id": job.spec.id}
            logger.info(
                "check_finished",
                extra={
                    **extra,
                    "status": str(result.status),
                    "attempts": outcome.attempts,
                    "duration_ms": outcome.duration_ms,
                    "error": result.error,
                },
            )
            try:
                async with self._sm() as session:
                    saved = await save_outcome(
                        session,
                        job_id=job.job_id,
                        worker_id=self._worker_id,
                        spec=job.spec,
                        outcome=outcome,
                    )
            except Exception:  # the job keeps its lease and is reclaimed once it expires
                logger.exception("save_failed", extra=extra)
                return
            if not saved:
                logger.warning("save_rejected", extra=extra)

    async def _idle(self, stop: asyncio.Event) -> None:
        """Sleep ``executor_idle_s``; wake early on stop or when a running check frees a slot."""
        waiter = asyncio.ensure_future(stop.wait())
        try:
            await asyncio.wait(
                {waiter, *self._tasks},
                timeout=self._settings.executor_idle_s,
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            waiter.cancel()

    async def _drain(self) -> None:
        if not self._tasks:
            return
        logger.info("draining", extra={"in_flight": len(self._tasks)})
        _, pending = await asyncio.wait(set(self._tasks), timeout=self._settings.shutdown_grace_s)
        if pending:
            logger.warning("shutdown_grace_exceeded", extra={"cancelled": len(pending)})
            await self._cancel(pending)

    @staticmethod
    async def _cancel(tasks: set[asyncio.Task[None]]) -> None:
        tasks = set(tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
