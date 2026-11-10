from pathlib import Path
from typing import Literal

from fastapi import APIRouter
from fastapi.responses import HTMLResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape

router = APIRouter()

_env = Environment(
    loader=FileSystemLoader(Path(__file__).resolve().parent.parent / "templates"),
    autoescape=select_autoescape(["html"]),
)


@router.get("/dashboard", response_class=HTMLResponse, include_in_schema=False)
async def dashboard(source: Literal["live", "demo"] = "live") -> HTMLResponse:
    return HTMLResponse(_env.get_template("dashboard.html").render(source=source))
