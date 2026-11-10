from alay.config import Settings


def test_defaults(monkeypatch):
    s = Settings(_env_file=None)
    assert s.default_interval_minutes == 60
    assert s.retry_attempts == 3 and s.retry_wait_base_s == 2
    assert s.job_lease_s == 180 and s.max_claims == 3
    assert s.login_allowed_hosts == ["mock-vfs", "localhost"]
    assert s.live_checks_enabled is True


def test_login_hosts_from_comma_env(monkeypatch):
    monkeypatch.setenv("LOGIN_ALLOWED_HOSTS", "a, b")
    assert Settings(_env_file=None).login_allowed_hosts == ["a", "b"]
