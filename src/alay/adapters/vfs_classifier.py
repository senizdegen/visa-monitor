"""Pure classification of a VFS page snapshot into a CheckResult (spec 6.3)."""

import html as html_lib
import re
from dataclasses import dataclass
from datetime import date
from typing import Any
from urllib.parse import urlparse

from pydantic import BaseModel

from alay.domain import AdapterSpec, CheckResult, CheckStatus, TransientSourceError


@dataclass(frozen=True)
class PageSnapshot:
    final_url: str
    http_status: int | None
    html: str
    step: str
    result_text: str | None = None
    response_time_ms: int | None = None


class VfsMarkers(BaseModel):
    captcha: list[str] = ["challenge-platform", "cf-turnstile", "Waiting Room", "Just a moment"]
    no_slots: list[str] = ["No appointment slots are currently available"]
    waitlist: list[str] = ["waitlist"]
    available: list[str] = ["Earliest available slot"]


_TAG_RE = re.compile(r"<[^>]+>")
_DATE_RE = re.compile(r"\b(\d{2})-(\d{2})-(\d{4})\b")
_PASSWORD_RE = re.compile(r"""type=["']?password""", re.IGNORECASE)


def _has(text: str, markers: list[str]) -> bool:
    low = text.lower()
    return any(m.lower() in low for m in markers)


def _parse_dates(text: str) -> list[date]:
    found: set[date] = set()
    for d, m, y in _DATE_RE.findall(text):
        try:
            found.add(date(int(y), int(m), int(d)))
        except ValueError:
            continue
    return sorted(found)


def classify(
    snapshot: PageSnapshot, spec: AdapterSpec, markers: VfsMarkers | None = None
) -> CheckResult:
    mk = markers or VfsMarkers()
    status = snapshot.http_status
    if snapshot.result_text is not None:
        text = snapshot.result_text
    else:
        text = html_lib.unescape(_TAG_RE.sub(" ", snapshot.html))

    def done(rule: str, st: CheckStatus, **fields: Any) -> CheckResult:
        return spec.result(
            st,
            response_time_ms=snapshot.response_time_ms,
            raw={
                "rule": rule,
                "step": snapshot.step,
                "http_status": status,
                "final_url": snapshot.final_url,
            },
            **fields,
        )

    if _has(snapshot.html, mk.captcha):
        return done("captcha", CheckStatus.CAPTCHA_REQUIRED)
    if status is not None and 500 <= status < 600:
        raise TransientSourceError("http_5xx", f"HTTP {status}")
    if status == 429:
        return done("rate_limited", CheckStatus.ERROR, error="rate_limited")
    if status == 403:
        return done("access_denied_403", CheckStatus.ERROR, error="access_denied_403")
    if _PASSWORD_RE.search(snapshot.html) or "/login" in urlparse(snapshot.final_url).path:
        return done("login", CheckStatus.LOGIN_REQUIRED)
    if _has(text, mk.no_slots):
        return done("no_slots", CheckStatus.NO_SLOTS)
    if _has(text, mk.waitlist):
        return done("waitlist", CheckStatus.WAITLIST, has_waitlist=True)
    if _has(text, mk.available):
        dates = _parse_dates(text)
        if dates:
            return done("available", CheckStatus.AVAILABLE, available_dates=dates)
    return done("unknown", CheckStatus.UNKNOWN)
