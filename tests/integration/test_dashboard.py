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


async def test_dashboard_times_show_their_offset(sessionmaker, browser):
    import asyncio
    import re
    import socket
    import uuid
    from datetime import UTC, datetime, timedelta

    import uvicorn

    from alay.db.models import AdapterRow, CheckJobRow, CheckRow

    async with sessionmaker() as s:
        s.add(AdapterRow(
            id="a1", adapter_type="fake", source="live", source_country="Turkey",
            destination_country="France", provider="P", center="Istanbul", visa_category="Tourism",
            timezone="Europe/Istanbul", config={}, interval_minutes=60,
        ))
        await s.flush()
        job = CheckJobRow(id=uuid.uuid4(), adapter_id="a1", trigger="scheduled", status="done")
        s.add(job)
        await s.flush()
        s.add(CheckRow(job_id=job.id, adapter_id="a1", status="NO_SLOTS", source="live", duration_ms=1000,
                       checked_at=datetime.now(UTC) - timedelta(minutes=5)))
        await s.commit()

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(
        create_app(Settings(), sessionmaker), host="127.0.0.1", port=port, log_level="warning"))
    task = asyncio.create_task(server.serve())
    try:
        while not server.started:
            if task.done():
                task.result()
            await asyncio.sleep(0.05)
        ctx = await (await browser.get()).new_context()
        try:
            page = await ctx.new_page()
            origin = f"http://127.0.0.1:{port}"

            async def offline(route):  # tests never touch the internet (Chart.js CDN)
                if route.request.url.startswith(origin):
                    await route.continue_()
                else:
                    await route.abort()

            await page.route("**/*", offline)
            await page.goto(f"{origin}/dashboard")
            await page.wait_for_function("document.querySelector('#meta').textContent.length > 0")
            meta = await page.text_content("#meta")
            cell = await page.text_content("#checks tbody tr td")
        finally:
            await ctx.close()
    finally:
        server.should_exit = True
        await task
    stamp = r"\d{4}-\d{2}-\d{2} \d{2}:\d{2} \([+-]\d{2}:\d{2}\)"
    assert re.fullmatch(rf"Обновлено: {stamp} · автообновление каждые 30 с", meta), meta
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2} \(\+03:00\)", cell), cell
