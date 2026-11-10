from datetime import date, datetime, timedelta

SCENARIOS: tuple[str, ...] = (
    "available",
    "available-one",
    "no-slots",
    "waitlist",
    "captcha",
    "login-wall",
    "server-error",
    "slow",
    "changed-layout",
)

ROTATION: tuple[str, ...] = (
    "no-slots",
    "available",
    "available-one",
    "no-slots",
    "waitlist",
    "captcha",
    "server-error",
)

NO_SLOTS_TEXT = "No appointment slots are currently available"
WAITLIST_TEXT = "All slots are booked. Waitlist is open for this category."


def resolve_scenario(name: str, now: datetime) -> str:
    """Map a URL scenario segment to a concrete scenario; `rotate` follows the minute."""
    if name == "rotate":
        return ROTATION[now.minute % len(ROTATION)]
    if name in SCENARIOS:
        return name
    raise KeyError(name)


def _fmt(d: date) -> str:
    return d.strftime("%d-%m-%Y")


def slot_message(scenario: str, centre: str, today: date) -> tuple[str, list[date]]:
    if scenario == "available":
        dates = [today + timedelta(days=8), today + timedelta(days=10)]
        text = (
            f"Earliest available slot for {centre} is : {_fmt(dates[0])}. "
            f"Other available dates: {_fmt(dates[1])}"
        )
        return text, dates
    if scenario == "available-one":
        d = today + timedelta(days=10)
        return f"Earliest available slot for {centre} is : {_fmt(d)}.", [d]
    if scenario == "waitlist":
        return WAITLIST_TEXT, []
    if scenario in ("no-slots", "login-wall", "changed-layout"):
        return NO_SLOTS_TEXT, []
    raise KeyError(scenario)
