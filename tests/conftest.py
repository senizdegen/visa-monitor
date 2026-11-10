import pytest

from alay.domain import AdapterSpec


@pytest.fixture
def adapter_spec() -> AdapterSpec:
    return AdapterSpec(
        id="vfs-tur-fra-istanbul-tourism",
        adapter_type="vfs_global",
        source="live",
        source_country="Turkey",
        destination_country="France",
        provider="VFS Global",
        center="Istanbul",
        visa_category="Tourism",
        interval_minutes=60,
    )
