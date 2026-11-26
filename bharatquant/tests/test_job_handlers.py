from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from bq.analysis.decision import (
    DataFreshness,
    OrderType,
    Product,
    Signal,
    TradePlan,
)
from bq.data.symbols import Symbol
from bq.data.vendors import (
    Bar,
    BarInterval,
    DataDomain,
    NoMarketDataError,
    Quote,
    VendorRouter,
)
from bq.infra.database import migrate
from bq.infra.job_handlers import (
    DEFAULT_RESOLVE_HORIZON_DAYS,
    DEFAULT_RESOLVE_MAX_EPISODES,
    MEMORY_RESOLVE_JOB,
    build_job_handlers,
)
from bq.infra.job_worker import JobWorker
from bq.infra.jobs import JobRunStore
from bq.memory import EpisodicMemoryStore, ResolutionReport
from bq.settings import SettingsStore

NOW = datetime(2026, 11, 26, 12, 0, tzinfo=UTC)


def test_memory_resolve_handler_uses_the_bounded_resolver(
    tmp_path: Path, monkeypatch
) -> None:
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    calls: list[dict[str, object]] = []

    class FakeResolver:
        def __init__(self, *_args: object, **kwargs: object) -> None:
            calls.append(kwargs)

        def resolve_due(self, *, as_of: datetime) -> ResolutionReport:
            calls.append({"as_of": as_of})
            return ResolutionReport(
                examined=1, resolved=1, skipped=0, failed=0, notes=()
            )

    monkeypatch.setattr("bq.infra.job_handlers.OutcomeResolver", FakeResolver)
    monkeypatch.setattr(
        "bq.infra.job_handlers.build_analysis_router", lambda *_args: object()
    )
    handlers = build_job_handlers(path, SettingsStore(path))
    result = handlers[MEMORY_RESOLVE_JOB](SimpleNamespace(scheduled_at=NOW))  # type: ignore[arg-type]
    assert result == {
        "examined": 1,
        "resolved": 1,
        "skipped": 0,
        "failed": 0,
        "notes": [],
    }
    assert calls[0]["horizon_days"] == DEFAULT_RESOLVE_HORIZON_DAYS
    assert calls[0]["max_episodes"] == DEFAULT_RESOLVE_MAX_EPISODES
    assert calls[1]["as_of"] == NOW


class StubVendor:
    name = "stub"
    is_realtime = False
    capabilities = frozenset({DataDomain.BARS_DAILY})

    def quote(self, symbol: Symbol) -> Quote:
        raise NoMarketDataError(symbol.canonical)

    def bars(
        self,
        symbol: Symbol,
        *,
        interval: BarInterval,
        start: datetime,
        end: datetime,
    ) -> tuple[Bar, ...]:
        del symbol, interval, start, end
        return (
            Bar(
                event_at=NOW - timedelta(days=8),
                open=Decimal("104"),
                high=Decimal("112"),
                low=Decimal("101"),
                close=Decimal("111"),
                volume=1000,
            ),
        )


def _plan(as_of: datetime) -> TradePlan:
    return TradePlan(
        signal=Signal.BUY,
        symbol="NSE:RELIANCE",
        exchange="NSE",
        product=Product.CNC,
        order_type=OrderType.LIMIT,
        entry_price=Decimal("100.00"),
        stop_loss=Decimal("95.00"),
        target_price=Decimal("110.00"),
        suggested_quantity=10,
        confidence=0.7,
        timeframe="swing",
        invalidation_reason="close below support",
        data_freshness=DataFreshness.DELAYED_YAHOO,
        as_of=as_of,
    )


def test_memory_resolve_handler_resolves_and_feeds_calibration(
    tmp_path: Path, monkeypatch
) -> None:
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    store = EpisodicMemoryStore(path, clock=SimpleNamespace(now=lambda: NOW))  # type: ignore[arg-type]
    store.record_shadow(
        run_id="run-1",
        symbol="NSE:RELIANCE",
        as_of=NOW - timedelta(days=10),
        source="analysis",
        decision_source="jev_native",
        model="jev-1.14.0",
        action="BUY",
        plan=_plan(NOW - timedelta(days=10)),
        predicted_probability=0.8,
        detail={},
    )
    router = VendorRouter()
    router.register_vendor(DataDomain.BARS_DAILY, StubVendor())
    monkeypatch.setattr(
        "bq.infra.job_handlers.build_analysis_router", lambda *_args: router
    )
    job_store = JobRunStore(path)
    job = job_store.enqueue(
        MEMORY_RESOLVE_JOB,
        "memory.resolve:test",
        scheduled_at=datetime.now(UTC) - timedelta(minutes=1),
        max_attempts=1,
    )
    worker = JobWorker(job_store, build_job_handlers(path, SettingsStore(path)))
    result = worker.run_once()
    assert result is not None and result.run_id == job.run_id
    assert result.status.value == "SUCCEEDED"
    buckets = EpisodicMemoryStore(path).calibration(as_of=NOW)
    assert len(buckets) == 1
    assert buckets[0].summary.count == 1
