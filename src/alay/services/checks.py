import asyncio
import logging
import time
from dataclasses import dataclass

from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_attempt, wait_exponential

import alay.adapters  # noqa: F401  (registers real adapter types)
from alay.adapters.base import AdapterContext
from alay.adapters.registry import UnknownAdapterType, get_adapter_class
from alay.domain import AdapterSpec, CheckResult, CheckStatus, TransientSourceError

logger = logging.getLogger(__name__)


@dataclass
class CheckOutcome:
    result: CheckResult
    attempts: int
    duration_ms: int


async def _sleep(seconds: float) -> None:
    await asyncio.sleep(seconds)


async def run_check(spec: AdapterSpec, ctx: AdapterContext) -> CheckOutcome:
    """Run one adapter check with retries and timeouts (spec §7.3). Never raises."""
    s = ctx.settings
    started = time.monotonic()
    attempts = 0

    def finish(result: CheckResult) -> CheckOutcome:
        return CheckOutcome(result, attempts, int((time.monotonic() - started) * 1000))

    def error(msg: str) -> CheckOutcome:
        return finish(spec.result(CheckStatus.ERROR, error=msg))

    try:
        adapter = get_adapter_class(spec.adapter_type)(spec, ctx)
    except UnknownAdapterType as e:
        return error(f"unknown_adapter_type: {e.name}")
    except Exception as e:
        logger.exception("adapter init failed", extra={"adapter_id": spec.id})
        return error(f"adapter_crash: {type(e).__name__}: {e}")

    async def attempt() -> CheckResult:
        nonlocal attempts
        attempts += 1
        async with asyncio.timeout(s.check_timeout_s):
            return await adapter.check()

    retrying = AsyncRetrying(
        retry=retry_if_exception_type((TransientSourceError, TimeoutError)),
        stop=stop_after_attempt(s.retry_attempts),
        wait=wait_exponential(multiplier=s.retry_wait_base_s, min=s.retry_wait_base_s),
        sleep=lambda secs: _sleep(secs),
        reraise=True,
    )
    total = asyncio.timeout(s.check_total_timeout_s)
    try:
        async with total:
            result = await retrying(attempt)
        return finish(result)
    except TransientSourceError as e:
        return error(str(e))
    except TimeoutError:
        return error("total_timeout" if total.expired() else "timeout")
    except Exception as e:
        logger.exception("adapter crashed", extra={"adapter_id": spec.id})
        return error(f"adapter_crash: {type(e).__name__}: {e}")
