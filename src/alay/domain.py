from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Any

from pydantic import AwareDatetime, BaseModel, Field, model_validator


class CheckStatus(StrEnum):
    AVAILABLE = "AVAILABLE"
    NO_SLOTS = "NO_SLOTS"
    WAITLIST = "WAITLIST"
    LOGIN_REQUIRED = "LOGIN_REQUIRED"
    CAPTCHA_REQUIRED = "CAPTCHA_REQUIRED"
    ERROR = "ERROR"
    UNKNOWN = "UNKNOWN"


class Source(StrEnum):
    LIVE = "live"
    DEMO = "demo"


class TransientSourceError(Exception):
    """Temporary source failure (timeout, network, HTTP 5xx); retried by the service."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


class CheckResult(BaseModel):
    source_country: str
    destination_country: str
    provider: str
    center: str
    visa_category: str
    checked_at: AwareDatetime = Field(default_factory=lambda: datetime.now(UTC))
    status: CheckStatus
    available_dates: list[date] = []
    slots_count: int | None = None
    has_waitlist: bool = False
    source: Source
    error: str | None = None
    response_time_ms: int | None = None
    evidence_path: str | None = None
    raw: dict[str, Any] = {}

    @model_validator(mode="after")
    def _check_invariants(self) -> "CheckResult":
        available = self.status == CheckStatus.AVAILABLE
        if self.available_dates and not available:
            raise ValueError("available_dates allowed only with AVAILABLE")
        if available:
            if not self.available_dates and not (self.slots_count and self.slots_count > 0):
                raise ValueError("AVAILABLE needs available_dates or slots_count > 0")
            self.available_dates = sorted(set(self.available_dates))
            if self.slots_count is None and self.available_dates:
                self.slots_count = len(self.available_dates)
        if self.status == CheckStatus.ERROR and not self.error:
            raise ValueError("ERROR requires an error message")
        if self.status == CheckStatus.WAITLIST:
            if "has_waitlist" in self.model_fields_set and not self.has_waitlist:
                raise ValueError("WAITLIST requires has_waitlist=True")
            self.has_waitlist = True
        return self


class AdapterSpec(BaseModel, extra="forbid"):
    id: str
    adapter_type: str
    source: Source
    source_country: str
    destination_country: str
    provider: str
    center: str
    visa_category: str
    timezone: str = "Europe/Istanbul"
    config: dict[str, Any] = {}
    interval_minutes: int = Field(gt=0)
    enabled: bool = True

    def result(self, status: CheckStatus, **fields: Any) -> CheckResult:
        return CheckResult(
            source_country=self.source_country,
            destination_country=self.destination_country,
            provider=self.provider,
            center=self.center,
            visa_category=self.visa_category,
            source=self.source,
            status=status,
            **fields,
        )
