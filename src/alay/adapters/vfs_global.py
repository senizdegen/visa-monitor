"""Playwright adapter for VFS-style booking sites (spec 6.3)."""

import logging
import time
from urllib.parse import urlparse

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page
from playwright.async_api import TimeoutError as PlaywrightTimeout
from pydantic import BaseModel

from alay.adapters.base import BaseAdapter
from alay.adapters.registry import register
from alay.adapters.vfs_classifier import PageSnapshot, VfsMarkers, classify
from alay.domain import CheckResult, CheckStatus, TransientSourceError

log = logging.getLogger(__name__)


class VfsSelectors(BaseModel):
    email_label: str = "Email"
    password_label: str = "Password"
    sign_in: str = "Sign In"
    start_booking: str = "Start New Booking"
    centre_label: str = "Application Centre"
    category_label: str = "Appointment Category"
    subcategory_label: str = "Sub-category"
    result: str = "[data-testid=slot-result], .alert"


class VfsConfig(BaseModel):
    start_url: str
    category: str
    subcategory: str
    login: bool = False
    timeout_s: float = 30
    selectors: VfsSelectors = VfsSelectors()
    markers: VfsMarkers = VfsMarkers()


def login_allowed(start_url: str, allowed_hosts: list[str]) -> bool:
    host = urlparse(start_url).hostname
    return host is not None and host in allowed_hosts


@register("vfs_global")
class VfsGlobalAdapter(BaseAdapter):
    async def check(self) -> CheckResult:
        cfg = VfsConfig.model_validate(self.spec.config)
        browser = await self.ctx.browser.get()
        context = await browser.new_context()
        try:
            page = await context.new_page()
            page.set_default_timeout(cfg.timeout_s * 1000)
            result = await self._flow(page, cfg)
            return await self._attach_screenshot(page, result)
        finally:
            await context.close()

    async def _snapshot(
        self, page: Page, step: str, http_status: int | None, rt: int | None, text=None
    ) -> PageSnapshot:
        return PageSnapshot(
            final_url=page.url,
            http_status=http_status,
            html=await page.content(),
            step=step,
            result_text=text,
            response_time_ms=rt,
        )

    async def _flow(self, page: Page, cfg: VfsConfig) -> CheckResult:
        sel = cfg.selectors
        started = time.monotonic()
        try:
            response = await page.goto(cfg.start_url)
        except PlaywrightTimeout as e:
            raise TransientSourceError("timeout", str(e)) from e
        except PlaywrightError as e:
            if "net::" in str(e):
                raise TransientSourceError("network", str(e)) from e
            raise
        rt = int((time.monotonic() - started) * 1000)
        status = response.status if response else None

        first = classify(await self._snapshot(page, "open", status, rt), self.spec, cfg.markers)
        if first.status not in (CheckStatus.LOGIN_REQUIRED, CheckStatus.UNKNOWN):
            return first

        step = "login"
        try:
            allowed = login_allowed(cfg.start_url, self.ctx.settings.login_allowed_hosts)
            if not (cfg.login and allowed):
                if first.status is CheckStatus.UNKNOWN:
                    return first
                return self.spec.result(
                    CheckStatus.LOGIN_REQUIRED,
                    response_time_ms=rt,
                    raw={
                        "rule": "login",
                        "step": "login",
                        "http_status": status,
                        "final_url": page.url,
                        "login_blocked": bool(cfg.login and not allowed),
                    },
                )
            password = page.get_by_label(sel.password_label)
            await password.wait_for(state="visible")
            await page.get_by_label(sel.email_label).fill(self.ctx.settings.mock_vfs_email)
            await password.fill(self.ctx.settings.mock_vfs_password)
            await page.get_by_role("button", name=sel.sign_in).click()

            step = "start_booking"
            await page.get_by_role("button", name=sel.start_booking).click()

            step = "select"
            await page.get_by_label(sel.centre_label).select_option(label=self.spec.center)
            await page.get_by_label(sel.category_label).select_option(label=cfg.category)
            await page.get_by_label(sel.subcategory_label).select_option(label=cfg.subcategory)

            step = "result"
            box = page.locator(sel.result).first
            await box.wait_for()
            text = await box.inner_text()
        except PlaywrightTimeout:
            return classify(await self._snapshot(page, step, None, rt), self.spec, cfg.markers)
        return classify(await self._snapshot(page, "result", None, rt, text), self.spec, cfg.markers)

    async def _attach_screenshot(self, page: Page, result: CheckResult) -> CheckResult:
        rel = f"{self.spec.id}/{result.checked_at:%Y%m%dT%H%M%S}.png"
        path = self.ctx.evidence_dir / self.spec.id / f"{result.checked_at:%Y%m%dT%H%M%S}.png"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            await page.screenshot(path=str(path))
        except Exception:
            log.warning("screenshot failed", extra={"adapter_id": self.spec.id}, exc_info=True)
            return result
        return result.model_copy(update={"evidence_path": rel})
