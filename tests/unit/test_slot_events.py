from datetime import date

import pytest

from alay.domain import AdapterSpec, CheckStatus
from alay.services.slot_events import SlotEventType, compute_slot_event

D1, D2, D3 = date(2026, 12, 1), date(2026, 12, 2), date(2026, 12, 3)


@pytest.fixture
def spec() -> AdapterSpec:
    return AdapterSpec(
        id="a", adapter_type="fake", source="demo", source_country="Turkey",
        destination_country="France", provider="P", center="C", visa_category="T",
        interval_minutes=60,
    )


def test_appeared(spec):
    r = spec.result(CheckStatus.AVAILABLE, available_dates=[D3, D1])
    assert compute_slot_event([], r) == (SlotEventType.APPEARED, [], [D1, D3])


def test_changed(spec):
    r = spec.result(CheckStatus.AVAILABLE, available_dates=[D2])
    assert compute_slot_event([D1], r) == (SlotEventType.CHANGED, [D1], [D2])


def test_disappeared_on_no_slots(spec):
    r = spec.result(CheckStatus.NO_SLOTS)
    assert compute_slot_event([D2, D1], r) == (SlotEventType.DISAPPEARED, [D1, D2], [])


def test_waitlist_counts_as_empty(spec):
    assert compute_slot_event([D1], spec.result(CheckStatus.WAITLIST))[0] == SlotEventType.DISAPPEARED
    assert compute_slot_event([], spec.result(CheckStatus.WAITLIST)) is None


def test_same_dates_unordered_no_event(spec):
    r = spec.result(CheckStatus.AVAILABLE, available_dates=[D1, D2])
    assert compute_slot_event([D2, D1], r) is None


@pytest.mark.parametrize("status", ["CAPTCHA_REQUIRED", "LOGIN_REQUIRED", "ERROR", "UNKNOWN"])
def test_indeterminate_status_never_creates_event(spec, status):
    kw = {"error": "x"} if status == "ERROR" else {}
    r = spec.result(CheckStatus(status), **kw)
    assert compute_slot_event([D1], r) is None
    assert compute_slot_event([], r) is None
