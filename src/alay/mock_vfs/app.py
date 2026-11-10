"""Mock VFS training site (spec 6.4). Synthetic pages; not affiliated with VFS Global.

The `rotate` path segment is resolved afresh on every request via `clock()`
(login, dashboard, booking and /api/slots alike); pages keep `rotate` in their
links so each step re-resolves.
"""

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from alay.config import get_settings
from alay.mock_vfs.scenarios import resolve_scenario, slot_message

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
CENTRES = ["Istanbul", "Ankara", "Izmir", "Gaziantep"]
CATEGORIES = ["Short Stay", "Long Stay"]
SUBCATEGORIES = ["Tourism", "Business", "Family Visit"]
COOKIE = "mock_session"


def create_app(
    *,
    email: str,
    password: str,
    slow_seconds: float = 120,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> FastAPI:
    app = FastAPI(title="Mock VFS", docs_url=None, redoc_url=None, openapi_url=None)

    def resolve(name: str) -> str:
        try:
            return resolve_scenario(name, clock())
        except KeyError:
            raise HTTPException(404, "unknown scenario") from None

    def page(request: Request, name: str, scenario: str, status: int = 200, **ctx):
        return TEMPLATES.TemplateResponse(
            request, name, {"scenario": scenario, **ctx}, status_code=status
        )

    def login_page(request: Request, scenario: str, error: str | None = None):
        if resolve(scenario) == "captcha":
            return page(request, "waiting_room.html", scenario, 403)
        return page(request, "login.html", scenario, error=error)

    @app.get("/api/slots")
    async def api_slots(
        scenario: str, centre: str = "Istanbul", category: str = "", subcategory: str = ""
    ):
        concrete = resolve(scenario)
        try:
            message, dates = slot_message(concrete, centre, clock().date())
        except KeyError:
            raise HTTPException(404, "scenario has no slot data") from None
        return JSONResponse({"message": message, "dates": [d.isoformat() for d in dates]})

    @app.get("/{scenario}/login")
    async def login_get(request: Request, scenario: str):
        concrete = resolve(scenario)
        if concrete == "server-error":
            return PlainTextResponse("Service Unavailable", status_code=503)
        if concrete == "slow":
            await asyncio.sleep(slow_seconds)
        return login_page(request, scenario)

    @app.post("/{scenario}/login")
    async def login_post(request: Request, scenario: str):
        concrete = resolve(scenario)
        if concrete == "server-error":
            return PlainTextResponse("Service Unavailable", status_code=503)
        if concrete == "captcha":
            return login_page(request, scenario)
        # urlencoded body parsed by hand: python-multipart is not a dependency.
        form = parse_qs((await request.body()).decode(), keep_blank_values=True)
        if form.get("email", [""])[0] == email and form.get("password", [""])[0] == password:
            resp = RedirectResponse(f"/{scenario}/dashboard", status_code=303)
            resp.set_cookie(COOKIE, "1", path="/", httponly=True)
            return resp
        return login_page(request, scenario, error="Invalid email or password.")

    def guard(request: Request, scenario: str):
        concrete = resolve(scenario)
        if concrete == "server-error":
            return concrete, PlainTextResponse("Service Unavailable", status_code=503)
        if concrete == "captcha":
            return concrete, page(request, "waiting_room.html", scenario, 403)
        if request.cookies.get(COOKIE) != "1":
            return concrete, RedirectResponse(f"/{scenario}/login", status_code=303)
        return concrete, None

    @app.get("/{scenario}/dashboard")
    async def dashboard(request: Request, scenario: str):
        concrete, blocked = guard(request, scenario)
        if blocked:
            return blocked
        label = "Begin Appointment" if concrete == "changed-layout" else "Start New Booking"
        return page(request, "dashboard.html", scenario, button_label=label)

    @app.get("/{scenario}/booking")
    async def booking(request: Request, scenario: str):
        _, blocked = guard(request, scenario)
        if blocked:
            return blocked
        return page(
            request,
            "booking.html",
            scenario,
            centres=CENTRES,
            categories=CATEGORIES,
            subcategories=SUBCATEGORIES,
        )

    return app


_settings = get_settings()
app = create_app(email=_settings.mock_vfs_email, password=_settings.mock_vfs_password)
