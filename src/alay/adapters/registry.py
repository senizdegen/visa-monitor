from collections.abc import Callable

from alay.adapters.base import BaseAdapter

ADAPTER_TYPES: dict[str, type[BaseAdapter]] = {}


class UnknownAdapterType(KeyError):
    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.name = name


def register(name: str) -> Callable[[type[BaseAdapter]], type[BaseAdapter]]:
    def decorator(cls: type[BaseAdapter]) -> type[BaseAdapter]:
        if name in ADAPTER_TYPES:
            raise ValueError(f"adapter type already registered: {name}")
        ADAPTER_TYPES[name] = cls
        return cls

    return decorator


def get_adapter_class(name: str) -> type[BaseAdapter]:
    try:
        return ADAPTER_TYPES[name]
    except KeyError:
        raise UnknownAdapterType(name) from None
