import httpx
import pytest

from alay.api.main import create_app
from alay.config import Settings


@pytest.fixture
async def client(sessionmaker):
    app = create_app(Settings(), sessionmaker)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://t") as c:
        yield c


async def test_dashboard_renders(client):
    r = await client.get("/dashboard")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert "chart.js@4.4.1" in r.text
    assert "/analytics" in r.text


async def test_demo_banner_only_in_demo(client):
    live = await client.get("/dashboard")
    demo = await client.get("/dashboard?source=demo")
    assert '<div id="demo-banner" hidden>Демонстрационные данные</div>' in live.text
    assert '<div id="demo-banner">Демонстрационные данные</div>' in demo.text


async def test_invalid_source_is_422(client):
    assert (await client.get("/dashboard?source=x")).status_code == 422
