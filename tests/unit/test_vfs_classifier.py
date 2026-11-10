from datetime import date
from pathlib import Path

import pytest

from alay.adapters.vfs_classifier import PageSnapshot, VfsMarkers, classify
from alay.domain import CheckStatus, TransientSourceError

FIX = Path(__file__).parent.parent / "fixtures" / "vfs"
URL = "https://visa.vfsglobal.com/tur/en/fra/booking"


def fx(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


def snap(html: str, status: int | None = 200, url: str = URL, **kw) -> PageSnapshot:
    return PageSnapshot(final_url=url, http_status=status, html=html, step="6", **kw)


def test_real_cloudflare_page_is_captcha(adapter_spec):
    r = classify(snap(fx("captcha_real.html"), url="https://x/login"), adapter_spec)
    assert r.status == CheckStatus.CAPTCHA_REQUIRED
    assert r.raw["rule"] == "captcha"


@pytest.mark.parametrize("code", [403, 503])
def test_captcha_wins_over_403_and_503(adapter_spec, code):
    r = classify(snap(fx("captcha_real.html"), status=code), adapter_spec)
    assert r.status == CheckStatus.CAPTCHA_REQUIRED


def test_5xx_without_challenge_raises_transient(adapter_spec):
    with pytest.raises(TransientSourceError) as ei:
        classify(snap("<p>oops</p>", status=502), adapter_spec)
    assert str(ei.value) == "http_5xx: HTTP 502"


def test_429_is_error_rate_limited(adapter_spec):
    r = classify(snap("<p>slow down</p>", status=429), adapter_spec)
    assert r.status == CheckStatus.ERROR
    assert r.error == "rate_limited"


def test_403_plain_is_access_denied(adapter_spec):
    r = classify(snap("<p>Forbidden</p>", status=403), adapter_spec)
    assert r.status == CheckStatus.ERROR
    assert r.error == "access_denied_403"


def test_login_form_is_login_required(adapter_spec):
    r = classify(snap(fx("login.html")), adapter_spec)
    assert r.status == CheckStatus.LOGIN_REQUIRED
    assert r.raw["rule"] == "login"


def test_login_url_is_login_required(adapter_spec):
    r = classify(snap("<p>hi</p>", url="https://x/tur/en/fra/login"), adapter_spec)
    assert r.status == CheckStatus.LOGIN_REQUIRED


def test_no_slots(adapter_spec):
    r = classify(snap(fx("no_slots.html")), adapter_spec)
    assert r.status == CheckStatus.NO_SLOTS


def test_waitlist(adapter_spec):
    r = classify(snap(fx("waitlist.html")), adapter_spec)
    assert r.status == CheckStatus.WAITLIST
    assert r.has_waitlist is True


def test_available_parses_dates(adapter_spec):
    r = classify(snap(fx("available.html"), response_time_ms=123), adapter_spec)
    assert r.status == CheckStatus.AVAILABLE
    assert r.available_dates == [date(2026, 11, 18), date(2026, 11, 20)]
    assert r.response_time_ms == 123
    assert r.raw == {
        "rule": "available",
        "step": "6",
        "http_status": 200,
        "final_url": URL,
    }


def test_available_marker_without_dates_is_unknown(adapter_spec):
    r = classify(snap("<p>Earliest available slot soon; 99-99-2026</p>"), adapter_spec)
    assert r.status == CheckStatus.UNKNOWN


def test_result_text_preferred_over_page(adapter_spec):
    html = "<p>Earliest</p><footer>No appointment slots are currently available</footer>"
    text = "Earliest available slot for Istanbul is : 18-11-2026"
    r = classify(snap(html, result_text=text), adapter_spec)
    assert r.status == CheckStatus.AVAILABLE


def test_unknown_when_nothing_matches(adapter_spec):
    r = classify(snap(fx("unknown.html")), adapter_spec)
    assert r.status == CheckStatus.UNKNOWN
    assert r.raw["rule"] == "unknown"


def test_custom_markers_override_defaults(adapter_spec):
    m = VfsMarkers(no_slots=["Keine Termine"])
    r1 = classify(snap("<p>keine termine frei</p>"), adapter_spec, m)
    assert r1.status == CheckStatus.NO_SLOTS
    r2 = classify(snap(fx("no_slots.html")), adapter_spec, m)
    assert r2.status == CheckStatus.UNKNOWN
