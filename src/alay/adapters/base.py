from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from alay.config import Settings
from alay.domain import AdapterSpec, CheckResult


@dataclass
class AdapterContext:
    settings: Settings
    evidence_dir: Path
    # BrowserProvider (later task): object with `async get() -> playwright Browser`
    browser: Any = None


class BaseAdapter(ABC):
    def __init__(self, spec: AdapterSpec, ctx: AdapterContext) -> None:
        self.spec = spec
        self.ctx = ctx

    @abstractmethod
    async def check(self) -> CheckResult: ...
