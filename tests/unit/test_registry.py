import pytest

from alay.adapters.base import BaseAdapter
from alay.adapters.registry import ADAPTER_TYPES, UnknownAdapterType, get_adapter_class, register


@pytest.fixture(autouse=True)
def _clean_registry():
    before = dict(ADAPTER_TYPES)
    yield
    ADAPTER_TYPES.clear()
    ADAPTER_TYPES.update(before)


def _make():
    class A(BaseAdapter):
        async def check(self):
            raise NotImplementedError

    return A


def test_register_and_lookup():
    cls = register("t-x")(_make())
    assert get_adapter_class("t-x") is cls


def test_unknown_type_raises():
    with pytest.raises(UnknownAdapterType):
        get_adapter_class("nope")


def test_duplicate_name_raises():
    register("t-dup")(_make())
    with pytest.raises(ValueError):
        register("t-dup")(_make())
