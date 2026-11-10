import time
from datetime import UTC, datetime

import httpx
import pytest

from alay.mock_vfs.app import create_app

EMAIL, PASSWORD = "demo@alay.test", "demo-password"
FIXED = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
SESSION = {"Cookie": "mock_session=1"}
BANNER = "Mock VFS — test site, not affiliated with VFS Global"


def make_client(clock=lambda: FIXED, slow_seconds=0.2):
    app = create_app(email=EMAIL, password=PASSWORD, slow_seconds=slow_seconds, clock=clock)
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://mock-vfs"
    )


@pytest.fixture
async def client():
    async with make_client() as c:
        yield c


async def login(c, scenario="available", password=PASSWORD):
    return await c.post(
        f"/{scenario}/login", data={"email": EMAIL, "password": password}
    )


async def test_captcha_login_returns_403_waiting_room(client):
    r = await client.get("/captcha/login")
    assert r.status_code == 403
    for marker in ("challenge-platform", "cf-turnstile", "Waiting Room"):
        assert marker in r.text


async def test_server_error_returns_503(client):
    assert (await client.get("/server-error/login")).status_code == 503


async def test_login_then_dashboard_flow(client):
    r = await client.get("/available/login")
    assert r.status_code == 200
    assert '<input type="password">' in r.text or "type=\"password\"" in r.text
    r = await login(client)
    assert r.status_code == 303
    assert r.headers["location"] == "/available/dashboard"
    assert "mock_session=1" in r.headers["set-cookie"]
    d = await client.get("/available/dashboard", headers=SESSION)
    assert d.status_code == 200
    assert "Start New Booking" in d.text
    assert "/available/booking" in d.text


async def test_wrong_password_stays_on_login(client):
    r = await login(client, password="nope")
    assert r.status_code == 200
    assert "Invalid" in r.text
    assert "set-cookie" not in r.headers


async def test_dashboard_requires_session(client):
    r = await client.get("/available/dashboard")
    assert r.status_code == 303
    assert r.headers["location"] == "/available/login"


async def test_changed_layout_renames_button(client):
    d = await client.get("/changed-layout/dashboard", headers=SESSION)
    assert "Begin Appointment" in d.text
    assert "Start New Booking" not in d.text


async def test_api_slots_available_dates_relative_to_today(client):
    r = await client.get("/api/slots", params={"scenario": "available", "centre": "Istanbul"})
    body = r.json()
    assert body["dates"] == ["2026-11-18", "2026-11-20"]
    assert body["message"] == (
        "Earliest available slot for Istanbul is : 18-11-2026. "
        "Other available dates: 20-11-2026"
    )


async def test_api_slots_other_scenarios(client):
    one = (await client.get("/api/slots", params={"scenario": "available-one", "centre": "Ankara"})).json()
    assert one["dates"] == ["2026-11-20"]
    ns = (await client.get("/api/slots", params={"scenario": "no-slots"})).json()
    assert ns == {"message": "No appointment slots are currently available", "dates": []}
    wl = (await client.get("/api/slots", params={"scenario": "waitlist"})).json()
    assert wl["message"] == "All slots are booked. Waitlist is open for this category."
    assert (await client.get("/api/slots", params={"scenario": "bogus"})).status_code == 404


async def test_rotate_follows_minute():
    clock = lambda: datetime(2026, 11, 10, 12, 1, tzinfo=UTC)
    async with make_client(clock) as c:
        r = await c.get("/api/slots", params={"scenario": "rotate", "centre": "Istanbul"})
        assert r.json()["dates"] == ["2026-11-18", "2026-11-20"]
        # rotate minute 5 -> captcha
    async with make_client(lambda: datetime(2026, 11, 10, 12, 5, tzinfo=UTC)) as c:
        assert (await c.get("/rotate/login")).status_code == 403
    async with make_client(lambda: datetime(2026, 11, 10, 12, 0, tzinfo=UTC)) as c:
        r = await c.get("/rotate/login")  # minute 0 -> no-slots
        assert r.status_code == 200


async def test_unknown_scenario_404(client):
    assert (await client.get("/nope/login")).status_code == 404


async def test_every_page_has_banner(client):
    pages = [
        await client.get("/available/login"),
        await client.get("/captcha/login"),
        await client.get("/available/dashboard", headers=SESSION),
        await client.get("/available/booking", headers=SESSION),
        await login(client, password="bad"),
    ]
    for r in pages:
        assert BANNER in r.text


async def test_only_waiting_room_has_captcha_markers(client):
    pages = [
        await client.get("/available/login"),
        await client.get("/available/dashboard", headers=SESSION),
        await client.get("/available/booking", headers=SESSION),
    ]
    for r in pages:
        low = r.text.lower()
        for bad in ("waiting room", "challenge-platform", "cf-turnstile", "just a moment", "waitlist"):
            assert bad not in low
    for path in ("dashboard", "booking"):
        r = await client.get(f"/available/{path}", headers=SESSION)
        assert 'type="password"' not in r.text


async def test_booking_has_labelled_selects(client):
    r = await client.get("/available/booking", headers=SESSION)
    for label in ("Application Centre", "Appointment Category", "Sub-category"):
        assert label in r.text
    assert "Gaziantep" in r.text and "Family Visit" in r.text
    assert "slot-result" in r.text


async def test_booking_requires_session(client):
    r = await client.get("/available/booking")
    assert r.status_code == 303


async def test_slow_scenario_delays(client):
    start = time.monotonic()
    r = await client.get("/slow/login")
    assert r.status_code == 200
    assert time.monotonic() - start >= 0.2
