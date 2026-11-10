import pytest

from tests.db_guard import unsafe_test_db_reason

MAIN = "postgresql+asyncpg://alay:alay@localhost:5433/alay"


def test_default_test_url_is_safe():
    assert unsafe_test_db_reason("postgresql+asyncpg://alay:alay@localhost:5433/alay_test", MAIN) is None


@pytest.mark.parametrize(
    "test_url",
    [
        MAIN,  # the main database itself
        "postgresql+asyncpg://alay:alay@localhost:5433/alay_prod",  # name lacks _test
        "postgresql+asyncpg://alay:alay@localhost:5433/postgres",
        "postgresql+asyncpg://alay:alay@localhost:5433",  # no database name
    ],
)
def test_unsafe_test_urls_are_refused(test_url):
    reason = unsafe_test_db_reason(test_url, MAIN)
    assert reason and "TEST_DATABASE_URL" in reason


def test_same_url_as_main_refused_even_if_named_test():
    url = "postgresql+asyncpg://alay:alay@localhost:5433/alay_test"
    assert unsafe_test_db_reason(url, url)
