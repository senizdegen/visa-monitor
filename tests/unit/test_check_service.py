import pytest

from alay.adapters.base import AdapterContext
from alay.config import Settings
from alay.domain import AdapterSpec, CheckStatus
from alay.services import checks
from alay.services.checks import run_check
from tests.fakes import FakeAdapter


@pytest.fixture(autouse=True)
def _reset():
    FakeAdapter.reset()


def make(steps, adapter_type="fake", **settings):
    spec = AdapterSpec(
        id="fake-1", adapter_type=adapter_type, source="demo", source_country="Turkey",
        destination_country="France", provider="P", center="C", visa_category="T",
        interval_minutes=60, config={"steps": steps},
    )
    s = Settings(_env_file=None, **{"retry_wait_base_s": 0, "check_timeout_s": 0.5, **settings})
    return spec, AdapterContext(settings=s, evidence_dir=None)


async def test_retries_transient_then_succeeds():
    out = await run_check(*make(["transient", "transient", "NO_SLOTS"]))
    assert out.result.status == CheckStatus.NO_SLOTS and out.attempts == 3


async def test_gives_up_after_three():
    out = await run_check(*make(["transient"]))
    assert out.result.status == CheckStatus.ERROR
    assert out.attempts == 3
    assert out.result.error.startswith("fake_transient")


async def test_captcha_not_retried():
    out = await run_check(*make(["CAPTCHA_REQUIRED"]))
    assert out.result.status == CheckStatus.CAPTCHA_REQUIRED and out.attempts == 1


async def test_crash_becomes_error():
    out = await run_check(*make(["crash"]))
    assert out.result.status == CheckStatus.ERROR
    assert out.result.error == "adapter_crash: RuntimeError: boom"


async def test_attempt_timeout_is_retried():
    out = await run_check(*make(["sleep:2", "NO_SLOTS"]))
    assert out.result.status == CheckStatus.NO_SLOTS and out.attempts == 2


async def test_attempt_timeout_exhausted():
    out = await run_check(*make(["sleep:2"], check_timeout_s=0.05))
    assert out.result.error == "timeout" and out.attempts == 3


async def test_total_timeout():
    out = await run_check(*make(["sleep:2"], check_timeout_s=1, check_total_timeout_s=0.1))
    assert out.result.status == CheckStatus.ERROR and out.result.error == "total_timeout"


async def test_unknown_type():
    out = await run_check(*make([], adapter_type="nope"))
    assert out.result.error == "unknown_adapter_type: nope" and out.attempts == 0


async def test_duration_measured():
    out = await run_check(*make(["NO_SLOTS"]))
    assert out.duration_ms >= 0


async def test_backoff_waits(monkeypatch):
    sleeps = []

    async def fake_sleep(s):
        sleeps.append(s)

    monkeypatch.setattr(checks, "_sleep", fake_sleep)
    out = await run_check(*make(["transient"], retry_wait_base_s=2))
    assert out.attempts == 3 and sleeps == [2, 4]
