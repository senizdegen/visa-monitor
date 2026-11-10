from datetime import date
from enum import StrEnum

from alay.domain import CheckResult, CheckStatus


class SlotEventType(StrEnum):
    APPEARED = "APPEARED"
    CHANGED = "CHANGED"
    DISAPPEARED = "DISAPPEARED"


def compute_slot_event(
    previous: list[date], result: CheckResult
) -> tuple[SlotEventType, list[date], list[date]] | None:
    """Spec §5.4: diff against previous dates; indeterminate statuses never produce events."""
    if result.status == CheckStatus.AVAILABLE:
        current = sorted(set(result.available_dates))
    elif result.status in (CheckStatus.NO_SLOTS, CheckStatus.WAITLIST):
        current = []
    else:
        return None
    before = sorted(set(previous))
    if before == current:
        return None
    if not before:
        kind = SlotEventType.APPEARED
    elif not current:
        kind = SlotEventType.DISAPPEARED
    else:
        kind = SlotEventType.CHANGED
    return kind, before, current
