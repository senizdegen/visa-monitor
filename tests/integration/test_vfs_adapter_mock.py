from datetime import UTC, datetime, timedelta

import pytest

from alay.domain import TransientSourceError


@pytest.mark.parametrize(
    "scenario,status,step",
    [
        ("available", "AVAILABLE", "result"),
        ("no-slots", "NO_SLOTS", "result"),
        ("waitlist", "WAITLIST", "result"),
        ("captcha", "CAPTCHA_REQUIRED", "open"),
        ("changed-layout", "UNKNOWN", "start_booking"),
    ],
)
async def test_scenarios(make_adapter, scenario, status, step):
    r = await make_adapter(scenario).check()
    assert r.status == status
    assert r.raw["step"] == step


async def test_available_dates_are_real_from_page(make_adapter):
    r = await make_adapter("available").check()
    today = datetime.now(UTC).date()
    assert r.available_dates == [today + timedelta(days=8), today + timedelta(days=10)]


async def test_login_wall_without_login_config(make_adapter):
    r = await make_adapter("available", login=False).check()
    assert r.status == "LOGIN_REQUIRED"
    assert r.raw["step"] == "login"
    assert r.raw["login_blocked"] is False


async def test_server_error_raises_transient(make_adapter):
    with pytest.raises(TransientSourceError) as e:
        await make_adapter("server-error").check()
    assert e.value.code == "http_5xx"


async def test_slow_raises_transient_timeout(make_adapter):
    with pytest.raises(TransientSourceError) as e:
        await make_adapter("slow", timeout_s=1).check()
    assert e.value.code == "timeout"


async def test_screenshot_written(make_adapter):
    adapter = make_adapter("available")
    r = await adapter.check()
    assert r.evidence_path and "\\" not in r.evidence_path and not r.evidence_path.startswith("/")
    assert (adapter.ctx.evidence_dir / r.evidence_path).is_file()


async def test_adapter_refuses_login_on_disallowed_host(make_adapter, monkeypatch):
    from playwright.async_api import Locator

    filled = []
    orig = Locator.fill

    async def spy(self, *a, **k):
        filled.append(a)
        return await orig(self, *a, **k)

    monkeypatch.setattr(Locator, "fill", spy)
    r = await make_adapter("available", allowed_hosts=["mock-vfs"]).check()
    assert r.status == "LOGIN_REQUIRED"
    assert r.raw["login_blocked"] is True
    assert r.raw["step"] == "login"
    assert filled == []


async def test_non_login_page_without_login_is_unknown(make_adapter, mock_site):
    adapter = make_adapter(
        "available",
        login=False,
        start_url=f"{mock_site}/api/slots?scenario=server-error&centre=Istanbul",
    )
    r = await adapter.check()
    assert r.status == "UNKNOWN"
    assert r.raw["step"] == "open"


@pytest.mark.parametrize("scenario", ["waitlist", "no-slots", "available"])
async def test_slot_text_on_open_page_is_unknown(make_adapter, mock_site, scenario):
    # A page reached by goto (step `open`) that merely contains slot wording must not
    # yield a slot status: only the result block at step `result` may (finding 1).
    adapter = make_adapter(
        "available",
        login=False,
        start_url=f"{mock_site}/api/slots?scenario={scenario}&centre=Istanbul",
    )
    r = await adapter.check()
    assert r.status == "UNKNOWN"
    assert r.raw["step"] == "open"
    assert r.raw["rule"] == "slot_marker_outside_result"


async def test_live_source_never_logs_in_even_on_allowed_host(make_adapter, monkeypatch):
    from playwright.async_api import Locator

    filled = []
    orig = Locator.fill

    async def spy(self, *a, **k):
        filled.append(a)
        return await orig(self, *a, **k)

    monkeypatch.setattr(Locator, "fill", spy)
    r = await make_adapter("available", source="live").check()
    assert r.status == "LOGIN_REQUIRED"
    assert r.source == "live"
    assert r.raw["login_blocked"] is True
    assert r.raw["step"] == "login"
    assert filled == []
