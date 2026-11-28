"""The approval gate: a preview computes without writing, approval adopts.

ADR-050 kept "no session executes without approval" by copying the fund and
adopting the copy's files. The engine runs in-process now, so the gate is
stronger *and* simpler: ``advance`` is pure given (fund, state, session, broker,
data), so a preview runs it against an in-memory broker and writes nothing. This
module pins the three properties that make that safe:

1. A preview leaves the fund byte-identical.
2. An approval appends *exactly* the record the preview computed, and its hash
   chains to the head, so a hand-edited preview cannot be admitted.
3. The broker's book after approval is the one the record says.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from bq.aihf.config import resolve_paths
from bq.aihf.engine.fund.spec import FundSpec
from bq.aihf.engine.paper.deployed import BROKER_FILE, deploy
from bq.aihf.engine.paper.ledger import Ledger
from bq.aihf.service import AiHfConflict, PaperStore
from bq.data.vendors.base import Bar
from bq.data.vendors.router import VendorResult
from bq.infra.database import migrate
from bq.settings import SettingsStore


class _FlatRouter:
    """A flat price series, so a session is computable with no network."""

    def __init__(self) -> None:
        self.bars_requests = 0

    def bars(self, symbol, *, interval, start, end, domain=None):
        self.bars_requests += 1
        close = Decimal("1000")
        bars = []
        day = start.date()
        while day <= end.date():
            if day.weekday() < 5:
                bars.append(
                    Bar(
                        event_at=datetime.combine(
                            day, datetime.min.time(), tzinfo=UTC
                        ).replace(hour=15, minute=30),
                        open=close,
                        high=close,
                        low=close,
                        close=close,
                        volume=1_000_000,
                    )
                )
            day += timedelta(days=1)
        return VendorResult(data=tuple(bars), source="flat", is_realtime=False)

    def quote(self, symbol):
        from bq.data.vendors.base import Quote

        return VendorResult(
            data=Quote(
                symbol=symbol,
                last_price=Decimal("1000"),
                as_of=datetime(2026, 11, 25, 3, 30, tzinfo=UTC),
                is_realtime=False,
                delay_seconds=0.0,
            ),
            source="flat",
            is_realtime=False,
        )


def _spec() -> FundSpec:
    return FundSpec.model_validate(
        {
            "schema_version": 2,
            "name": "gate-fund",
            "strategies": [
                {
                    "name": "q",
                    "blend": {"mode": "long_only"},
                    "models": [{"name": "pead"}],
                }
            ],
            "risk": {"max_position_pct": 0.25, "max_gross_exposure": 1.0},
            "capital": 1_000_000.0,
            "benchmark": "NIFTY 50",
        }
    )


@pytest.fixture
def store(tmp_path):
    database = tmp_path / "bq.db"
    migrate(database, create=True)
    return SettingsStore(database)


@pytest.fixture
def paths(tmp_path):
    return resolve_paths(database_path=tmp_path / "bq.db")


def _deploy_fund(paths) -> None:
    paths.paper.mkdir(parents=True, exist_ok=True)
    deploy("alpha", _spec(), ["RELIANCE"], root=paths.paper)


def _digest(directory) -> str:
    import hashlib

    digest = hashlib.sha256()
    for entry in sorted(directory.rglob("*")):
        if entry.is_file():
            digest.update(entry.relative_to(directory).as_posix().encode())
            digest.update(entry.read_bytes())
    return digest.hexdigest()


def test_a_preview_writes_nothing_into_the_fund(store, paths) -> None:
    """The whole point: the session is computed, and the fund is untouched."""
    _deploy_fund(paths)
    directory = paths.fund_dir("alpha")
    before = _digest(directory)

    client = _bouquet(store, paths)
    store_obj = PaperStore(paths, store, data_client=client)
    # Drive the compute directly so the assertion is about the fund, not the
    # route's status mapping.
    import asyncio

    asyncio.run(store_obj.preview("alpha"))

    assert _digest(directory) == before
    # A preview file exists, but it is outside the fund directory.
    assert (paths.previews / "alpha.json").is_file()


def test_approval_adopts_exactly_the_previewed_record(store, paths) -> None:
    _deploy_fund(paths)
    directory = paths.fund_dir("alpha")
    client = _bouquet(store, paths)
    store_obj = PaperStore(paths, store, data_client=client)

    import asyncio

    preview = asyncio.run(store_obj.preview("alpha"))
    stored = json.loads((paths.previews / "alpha.json").read_text())
    previewed_session = stored["record"]["session"]
    previewed_hash = stored["record"]["hash"]

    adopted = asyncio.run(store_obj.execute("alpha", preview_id=preview["preview_id"]))
    assert adopted["adopted"]["session"] == previewed_session

    ledger = Ledger(directory)
    records = ledger.records()
    assert len(records) == 1
    assert records[0].session == previewed_session
    # The adopted record is byte-for-byte what the preview computed.
    assert ledger.read(previewed_session).hash == previewed_hash
    # And the chain verifies.
    ledger.replay(_spec().capital, full=True)


def test_a_tampered_preview_is_refused(store, paths) -> None:
    """A preview file edited by hand must not be adoptable: `Ledger.append`
    recomputes the hash and rejects a mismatch."""
    _deploy_fund(paths)
    client = _bouquet(store, paths)
    store_obj = PaperStore(paths, store, data_client=client)

    import asyncio

    preview = asyncio.run(store_obj.preview("alpha"))
    path = paths.previews / "alpha.json"
    payload = json.loads(path.read_text())
    # Forge a better NAV without recomputing the hash.
    payload["record"]["nav"] = payload["record"]["nav"] + 1_000_000
    path.write_text(json.dumps(payload))

    with pytest.raises(AiHfConflict, match="could not be adopted"):
        asyncio.run(store_obj.execute("alpha", preview_id=preview["preview_id"]))


def test_a_stale_preview_is_refused_after_the_ledger_advances(store, paths) -> None:
    _deploy_fund(paths)
    client = _bouquet(store, paths)
    store_obj = PaperStore(paths, store, data_client=client)

    import asyncio

    preview = asyncio.run(store_obj.preview("alpha"))
    asyncio.run(store_obj.execute("alpha", preview_id=preview["preview_id"]))

    # Approving the same preview again must not append a second time.
    with pytest.raises((AiHfConflict, Exception)):
        asyncio.run(store_obj.execute("alpha", preview_id=preview["preview_id"]))


def test_the_broker_book_matches_the_adopted_record(store, paths) -> None:
    _deploy_fund(paths)
    directory = paths.fund_dir("alpha")
    client = _bouquet(store, paths)
    store_obj = PaperStore(paths, store, data_client=client)

    import asyncio

    preview = asyncio.run(store_obj.preview("alpha"))
    adopted = asyncio.run(store_obj.execute("alpha", preview_id=preview["preview_id"]))
    session = adopted["adopted"]

    book = json.loads((directory / BROKER_FILE).read_text())
    assert book["cash"] == pytest.approx(session["cash"])
    assert book["shares"] == session["positions"]


def _bouquet(store: SettingsStore, paths):  # noqa: ANN001, ANN202
    """A data client over the fake router and this test's database."""
    from bq.aihf.bridge.data import BharatQuantDataClient

    return BharatQuantDataClient(
        database_path=store.path, router=_FlatRouter(), clock=store.clock
    )
