"""Slot statuses may come only from the result block at step `result` (review finding 1)."""

import pytest

from alay.adapters.vfs_classifier import PageSnapshot
from alay.adapters.vfs_global import classify_step
from alay.domain import CheckStatus

URL = "https://visa.vfsglobal.com/tur/en/fra/"
LANDING_WITH_SCRIPT = (
    "<html><body><h1>Welcome</h1>"
    "<script>var i18n = {cta: 'Join the waitlist'};</script></body></html>"
)


def snap(html: str, step: str, result_text: str | None = None) -> PageSnapshot:
    return PageSnapshot(final_url=URL, http_status=200, html=html, step=step, result_text=result_text)


@pytest.mark.parametrize(
    "html",
    [
        LANDING_WITH_SCRIPT,
        "<p>No appointment slots are currently available</p>",
        "<p>Earliest available slot for Istanbul is : 18-11-2026</p>",
    ],
)
@pytest.mark.parametrize("step", ["open", "login", "start_booking", "select"])
def test_slot_marker_outside_result_is_unknown(adapter_spec, html, step):
    r = classify_step(snap(html, step), adapter_spec)
    assert r.status == CheckStatus.UNKNOWN
    assert r.available_dates == []
    assert r.has_waitlist is False
    assert r.raw["rule"] == "slot_marker_outside_result"
    assert r.raw["step"] == step


def test_result_step_without_result_text_is_unknown(adapter_spec):
    # step `result` reached by a timeout: the slot block never appeared.
    r = classify_step(snap(LANDING_WITH_SCRIPT, "result"), adapter_spec)
    assert r.status == CheckStatus.UNKNOWN
    assert r.raw["rule"] == "slot_marker_outside_result"


@pytest.mark.parametrize(
    "text,status",
    [
        ("All slots are booked. Waitlist is open for this category.", CheckStatus.WAITLIST),
        ("No appointment slots are currently available", CheckStatus.NO_SLOTS),
        ("Earliest available slot for Istanbul is : 18-11-2026", CheckStatus.AVAILABLE),
    ],
)
def test_result_block_may_return_slot_statuses(adapter_spec, text, status):
    r = classify_step(snap("<div>page</div>", "result", result_text=text), adapter_spec)
    assert r.status == status
    assert r.raw["step"] == "result"


def test_non_slot_statuses_untouched_at_open(adapter_spec):
    r = classify_step(snap("<div class='cf-turnstile'></div>", "open"), adapter_spec)
    assert r.status == CheckStatus.CAPTCHA_REQUIRED
    assert r.raw["rule"] == "captcha"
