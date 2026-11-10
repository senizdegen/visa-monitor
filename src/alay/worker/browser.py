import asyncio

from playwright.async_api import Browser, Playwright, async_playwright


class BrowserProvider:
    """Lazily started, shared Chromium instance."""

    def __init__(self, headful: bool = False) -> None:
        self._headful = headful
        self._lock = asyncio.Lock()
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None

    async def get(self) -> Browser:
        async with self._lock:
            if self._browser is None or not self._browser.is_connected():
                if self._playwright is None:
                    self._playwright = await async_playwright().start()
                self._browser = await self._playwright.chromium.launch(headless=not self._headful)
            return self._browser

    async def close(self) -> None:
        async with self._lock:
            if self._browser is not None:
                await self._browser.close()
                self._browser = None
            if self._playwright is not None:
                await self._playwright.stop()
                self._playwright = None
