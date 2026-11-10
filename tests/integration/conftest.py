import asyncio
import socket
from pathlib import Path

import pytest
import uvicorn

from alay.adapters.base import AdapterContext
from alay.config import Settings
from alay.domain import AdapterSpec
from alay.mock_vfs.app import create_app
from alay.worker.browser import BrowserProvider

EMAIL, PASSWORD = "demo@alay.test", "demo-password"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
async def mock_site():
    port = _free_port()
    app = create_app(email=EMAIL, password=PASSWORD, slow_seconds=5)
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    )
    task = asyncio.create_task(server.serve())
    while not server.started:
        if task.done():
            task.result()
        await asyncio.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    await task


@pytest.fixture(scope="session")
async def browser():
    provider = BrowserProvider()
    yield provider
    await provider.close()


@pytest.fixture
def make_adapter(mock_site, browser, tmp_path: Path):
    def make(
        scenario, *, login=True, timeout_s=2, allowed_hosts=("127.0.0.1",), source="demo", **cfg
    ):
        spec = AdapterSpec(
            id="vfs-demo",
            adapter_type="vfs_global",
            source=source,
            source_country="Turkey",
            destination_country="France",
            provider="Mock VFS",
            center="Istanbul",
            visa_category="Tourism",
            interval_minutes=60,
            config={
                "start_url": f"{mock_site}/{scenario}/login",
                "category": "Short Stay",
                "subcategory": "Tourism",
                "login": login,
                "timeout_s": timeout_s,
                **cfg,
            },
        )
        settings = Settings(
            login_allowed_hosts=list(allowed_hosts),
            mock_vfs_email=EMAIL,
            mock_vfs_password=PASSWORD,
        )
        from alay.adapters.vfs_global import VfsGlobalAdapter

        return VfsGlobalAdapter(
            spec, AdapterContext(settings=settings, evidence_dir=tmp_path, browser=browser)
        )

    return make
