from datetime import date

import pytest
from pydantic import ValidationError

from alay.domain import AdapterSpec, CheckStatus, Source, TransientSourceError

BASE = dict(
    id="vfs-tur-fra-istanbul-tourism",
    adapter_type="vfs_global",
    source="live",
    source_country="Turkey",
    destination_country="France",
    provider="VFS Global",
    center="Istanbul",
    visa_category="Tourism",
)


@pytest.fixture
def spec(adapter_spec):
    return adapter_spec


def test_dates_only_with_available(spec):
    with pytest.raises(ValidationError):
        spec.result(CheckStatus.CAPTCHA_REQUIRED, available_dates=[date(2026, 11, 18)])


def test_available_needs_dates_or_count(spec):
    with pytest.raises(ValidationError):
        spec.result(CheckStatus.AVAILABLE)
    assert spec.result(CheckStatus.AVAILABLE, slots_count=3).slots_count == 3


def test_available_dates_sorted_deduped_and_counted(spec):
    r = spec.result(
        CheckStatus.AVAILABLE,
        available_dates=[date(2026, 11, 20), date(2026, 11, 18), date(2026, 11, 20)],
    )
    assert r.available_dates == [date(2026, 11, 18), date(2026, 11, 20)]
    assert r.slots_count == 2


def test_error_requires_message(spec):
    with pytest.raises(ValidationError):
        spec.result(CheckStatus.ERROR)
    assert spec.result(CheckStatus.ERROR, error="timeout: x").error == "timeout: x"


def test_waitlist_sets_flag(spec):
    with pytest.raises(ValidationError):
        spec.result(CheckStatus.WAITLIST, has_waitlist=False)
    assert spec.result(CheckStatus.WAITLIST).has_waitlist is True


def test_result_copies_metadata(spec):
    r = spec.result(CheckStatus.NO_SLOTS)
    assert (r.center, r.provider, r.source) == ("Istanbul", "VFS Global", Source.LIVE)
    assert r.checked_at.tzinfo is not None


def test_spec_rejects_zero_interval_and_unknown_keys():
    with pytest.raises(ValidationError):
        AdapterSpec(**BASE, interval_minutes=0)
    with pytest.raises(ValidationError):
        AdapterSpec(**BASE, interval_minutes=60, colour="red")


def test_transient_error_str():
    assert str(TransientSourceError("timeout", "nav 30s")) == "timeout: nav 30s"
    e = TransientSourceError("http_503")
    assert str(e) == "http_503" and e.code == "http_503"


@pytest.mark.parametrize("tz", ["Europe/Instanbul", "", "Not/AZone", "../etc/passwd"])
def test_adapter_spec_rejects_unknown_timezone(tz):
    with pytest.raises(ValidationError) as e:
        AdapterSpec(**BASE, interval_minutes=60, timezone=tz)
    assert "unknown timezone" in str(e.value)
    assert e.value.errors()[0]["loc"] == ("timezone",)


def test_adapter_spec_accepts_valid_timezone():
    assert AdapterSpec(**BASE, interval_minutes=60, timezone="Europe/Paris").timezone == "Europe/Paris"
