from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from bq.data.records import RECORD_KEY, DataRecord, RecordKind

MAX_RECORD_KEYS = 256


class ProviderContractError(ValueError):
    pass


class UnsupportedCapability(ProviderContractError):
    pass


class IncompleteProviderResult(ProviderContractError):
    pass


@dataclass(frozen=True, slots=True)
class RecordRequest:
    kind: RecordKind
    record_keys: tuple[str, ...]
    as_of: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.kind, RecordKind):
            raise TypeError("Invalid requested record kind.")
        if (
            not isinstance(self.record_keys, tuple)
            or not 1 <= len(self.record_keys) <= MAX_RECORD_KEYS
            or any(
                not isinstance(key, str) or RECORD_KEY.fullmatch(key) is None
                for key in self.record_keys
            )
        ):
            raise ValueError("Request must contain 1 to 256 valid record keys.")
        if len(set(self.record_keys)) != len(self.record_keys):
            raise ValueError("Requested record keys must be unique.")
        if not isinstance(self.as_of, datetime) or self.as_of.utcoffset() is None:
            raise ValueError("Request as_of must be timezone-aware.")


class DataProvider(Protocol):
    @property
    def source_id(self) -> str: ...

    @property
    def capabilities(self) -> frozenset[RecordKind]: ...

    def fetch(self, request: RecordRequest) -> tuple[DataRecord, ...]: ...


def fetch_records(
    provider: DataProvider, request: RecordRequest
) -> tuple[DataRecord, ...]:
    source_id = provider.source_id
    capabilities = provider.capabilities
    if not isinstance(source_id, str) or RECORD_KEY.fullmatch(source_id) is None:
        raise ProviderContractError("Provider source identifier is invalid.")
    if not isinstance(capabilities, frozenset) or any(
        not isinstance(kind, RecordKind) for kind in capabilities
    ):
        raise ProviderContractError("Provider capabilities are invalid.")
    if request.kind not in capabilities:
        raise UnsupportedCapability(f"Provider does not support {request.kind.value}.")

    records = provider.fetch(request)
    if not isinstance(records, tuple) or len(records) > len(request.record_keys):
        raise ProviderContractError("Provider returned an invalid result size or type.")
    requested = set(request.record_keys)
    by_key: dict[str, DataRecord] = {}
    for record in records:
        if (
            not isinstance(record, DataRecord)
            or record.kind is not request.kind
            or record.source_id != source_id
            or record.record_key not in requested
            or record.record_key in by_key
            or record.known_at > request.as_of
            or record.received_at > request.as_of
        ):
            raise ProviderContractError(
                "Provider returned a mismatched or unavailable record."
            )
        by_key[record.record_key] = record
    if len(by_key) != len(requested):
        raise IncompleteProviderResult(
            "Provider did not return every requested record."
        )
    return tuple(by_key[key] for key in request.record_keys)
