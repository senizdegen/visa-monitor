"""Refuse to wipe a database that is not clearly a disposable test database."""

from sqlalchemy.engine import make_url


def unsafe_test_db_reason(test_url: str, main_url: str) -> str | None:
    """Why `test_url` must not be dropped (None when it is safe)."""
    test, main = make_url(test_url), make_url(main_url)
    name = test.database or ""
    if not name.endswith("_test"):
        return f"TEST_DATABASE_URL database {name!r} does not end with '_test'"
    same_db = (test.host, test.port, test.database) == (main.host, main.port, main.database)
    if same_db:
        return "TEST_DATABASE_URL points at the same database as DATABASE_URL"
    return None
