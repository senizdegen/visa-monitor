from functools import lru_cache
from typing import Annotated

from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://alay:alay@localhost:5433/alay"
    test_database_url: str = "postgresql+asyncpg://alay:alay@localhost:5433/alay_test"
    adapters_file: str = "adapters.yaml"
    login_allowed_hosts: Annotated[list[str], NoDecode] = ["mock-vfs", "localhost"]
    mock_vfs_email: str = "demo@alay.test"
    mock_vfs_password: str = "demo-password"
    log_level: str = "INFO"

    default_interval_minutes: int = 60
    scheduler_tick_s: int = 5
    worker_concurrency: int = 3
    check_timeout_s: float = 90
    check_total_timeout_s: float = 150
    retry_attempts: int = 3
    retry_wait_base_s: int = 2
    job_lease_s: int = 180
    max_claims: int = 3
    executor_idle_s: int = 1
    heartbeat_s: int = 15
    shutdown_grace_s: int = 60
    manual_wait_s: int = 60
    min_checks_for_stats: int = 5
    live_checks_enabled: bool = True
    headful: bool = False
    evidence_dir: str = "./data/evidence"

    @field_validator("login_allowed_hosts", mode="before")
    @classmethod
    def _split_hosts(cls, v):
        if isinstance(v, str):
            return [h.strip() for h in v.split(",") if h.strip()]
        return v


@lru_cache
def get_settings() -> Settings:
    return Settings()
