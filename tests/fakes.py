import asyncio
from datetime import date

from alay.adapters.base import BaseAdapter
from alay.adapters.registry import ADAPTER_TYPES, register
from alay.domain import CheckResult, CheckStatus, TransientSourceError


class FakeAdapter(BaseAdapter):
    """Scripted adapter: each check() call consumes one step of spec.config["steps"]."""

    calls: dict[str, int] = {}

    @classmethod
    def reset(cls) -> None:
        cls.calls.clear()

    async def check(self) -> CheckResult:
        steps: list[str] = self.spec.config["steps"]
        n = self.calls.get(self.spec.id, 0)
        self.calls[self.spec.id] = n + 1
        step = steps[min(n, len(steps) - 1)]
        if step == "transient":
            raise TransientSourceError("fake_transient")
        if step == "crash":
            raise RuntimeError("boom")
        if step.startswith("sleep:"):
            await asyncio.sleep(float(step.split(":", 1)[1]))
            return self.spec.result(CheckStatus.NO_SLOTS)
        if step.startswith("AVAILABLE"):
            _, _, rest = step.partition(":")
            dates = [date.fromisoformat(d) for d in rest.split(",") if d]
            return self.spec.result(CheckStatus.AVAILABLE, available_dates=dates)
        return self.spec.result(CheckStatus(step))


if "fake" not in ADAPTER_TYPES:
    register("fake")(FakeAdapter)
