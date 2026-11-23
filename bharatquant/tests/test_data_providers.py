from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from bq.data.providers import (
    IncompleteProviderResult,
    ProviderContractError,
    RecordRequest,
    UnsupportedCapability,
    fetch_records,
)
from bq.data.records import DataRecord, RecordKind

AS_OF = datetime(2026, 11, 21, 12, 0, tzinfo=UTC)


def record(key: str) -> DataRecord:
    return DataRecord.create(
        kind=RecordKind.EOD_BAR,
        record_key=key,
        source_id="fixture",
        source_url="https://example.test/data",
        unit="INR/share",
        known_at=AS_OF,
        received_at=AS_OF,
        payload={"close": "100"},
    )


class FixtureProvider:
    source_id = "fixture"
    capabilities = frozenset({RecordKind.EOD_BAR})

    def __init__(self, records: tuple[DataRecord, ...]) -> None:
        self.records = records
        self.calls = 0

    def fetch(self, request: RecordRequest) -> tuple[DataRecord, ...]:
        self.calls += 1
        return self.records


def test_provider_contract_orders_results_and_requires_supported_capability() -> None:
    first, second = record("first"), record("second")
    provider = FixtureProvider((second, first))
    request = RecordRequest(RecordKind.EOD_BAR, ("first", "second"), AS_OF)
    assert fetch_records(provider, request) == (first, second)
    with pytest.raises(UnsupportedCapability):
        fetch_records(provider, replace(request, kind=RecordKind.FILING))
    assert provider.calls == 1


@pytest.mark.parametrize(
    "keys",
    [(), ("first", "first"), ("bad key",), tuple(f"key-{i}" for i in range(257))],
)
def test_request_rejects_empty_duplicate_invalid_or_excessive_keys(
    keys: tuple[str, ...],
) -> None:
    with pytest.raises(ValueError):
        RecordRequest(RecordKind.EOD_BAR, keys, AS_OF)


def test_request_requires_aware_as_of_and_allows_256_keys() -> None:
    keys = tuple(f"key-{i}" for i in range(256))
    assert len(RecordRequest(RecordKind.EOD_BAR, keys, AS_OF).record_keys) == 256
    with pytest.raises(ValueError, match="timezone-aware"):
        RecordRequest(RecordKind.EOD_BAR, ("first",), AS_OF.replace(tzinfo=None))


@pytest.mark.parametrize(
    "records",
    [
        (record("unexpected"),),
        (replace(record("first"), source_id="other"),),
        (replace(record("first"), kind=RecordKind.FILING),),
        (replace(record("first"), known_at=AS_OF + timedelta(seconds=1)),),
        (record("first"), record("first")),
    ],
)
def test_provider_rejects_wrong_duplicate_or_future_records(
    records: tuple[DataRecord, ...],
) -> None:
    request = RecordRequest(RecordKind.EOD_BAR, ("first", "second"), AS_OF)
    with pytest.raises(ProviderContractError):
        fetch_records(FixtureProvider(records), request)


def test_provider_reports_missing_records_and_propagates_failures() -> None:
    request = RecordRequest(RecordKind.EOD_BAR, ("first", "second"), AS_OF)
    with pytest.raises(IncompleteProviderResult):
        fetch_records(FixtureProvider((record("first"),)), request)

    class FailedProvider(FixtureProvider):
        def fetch(self, request: RecordRequest) -> tuple[DataRecord, ...]:
            raise TimeoutError("provider unavailable")

    with pytest.raises(TimeoutError, match="provider unavailable"):
        fetch_records(FailedProvider(()), request)
