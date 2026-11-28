from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from bq.analysis.briefs import SpecialistBrief
from bq.analysis.confidence_engine import ConfidenceEngine
from bq.analysis.decision import (
    DataFreshness,
    OrderType,
    Product,
    Signal,
    TradePlan,
)
from bq.analysis.technical import SubScore, TechnicalReport
from bq.compute.indicators import IndicatorSnapshot, VolumeStats
from bq.compute.setup_study import SetupStudy, study_trend_continuation
from bq.data.vendors.base import Bar
from bq.infra.database import migrate
from bq.memory.setup_rates import SetupBaseRateStore

NOW = datetime(2026, 11, 26, 12, 0, tzinfo=UTC)


def _bars(count: int = 500) -> tuple[Bar, ...]:
    start = NOW - timedelta(days=count)
    bars: list[Bar] = []
    price = 100.0
    for index in range(count):
        up = index % 3 != 2
        price += 1.0 if up else -0.8
        value = Decimal(str(round(price, 2)))
        bars.append(
            Bar(
                event_at=start + timedelta(days=index),
                open=value,
                high=value + Decimal("0.5"),
                low=value - Decimal("0.5"),
                close=value,
                volume=10_000 if up else 1_000,
            )
        )
    return tuple(bars)


def _report(*, with_volume: bool = True) -> TechnicalReport:
    indicator = IndicatorSnapshot(
        as_of=NOW,
        bar_count=120,
        close=100.0,
        sma_20=98.0,
        sma_50=95.0,
        ema_20=99.0,
        rsi_14=60.0,
        macd=None,
        bollinger=None,
        atr_14=2.0,
        vwap=97.0,
        adx_14=27.0,
        max_drawdown=0.1,
        volume=VolumeStats(1500.0, 1000.0, 1.5) if with_volume else None,
        swings=None,
    )
    return TechnicalReport(
        symbol="NSE:RELIANCE",
        as_of=NOW,
        timeframe="swing",
        last_close=Decimal("100.00"),
        bar_count=120,
        trend=SubScore(0.8, ("trend",)),
        momentum=SubScore(0.5, ("momentum",)),
        volatility=SubScore(0.2, ("volatility",)),
        structure=SubScore(0.8, ("structure",)),
        indicators=indicator,
    )


def _plan() -> TradePlan:
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
        as_of=NOW,
    )


def _specialists() -> tuple[SpecialistBrief, ...]:
    return (
        SpecialistBrief(
            agent="market_regime",
            symbol="INDEX:NIFTY 50",
            as_of=NOW,
            facts=("Regime: trend_up.",),
        ),
    )


def test_trend_setup_study_has_a_bounded_sample(tmp_path: Path) -> None:
    bars = _bars()
    study = study_trend_continuation("NSE:RELIANCE", bars)
    assert study.sample_count > 100
    assert 0.0 <= study.win_rate <= 1.0
    assert study.data_as_of == bars[-1].event_at

    path = tmp_path / "bq.db"
    migrate(path, create=True)
    store = SetupBaseRateStore(path)
    rate = store.record(study)
    latest = store.latest(
        setup_id=study.setup_id,
        symbol=study.symbol,
        horizon_days=study.horizon_days,
        as_of=bars[-1].event_at,
    )
    assert latest is not None
    assert latest.rate_id == rate.rate_id
    assert latest.sample_count == study.sample_count


def test_confidence_assessment_confirms_four_families(tmp_path: Path) -> None:
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    bars = _bars()
    rate = SetupBaseRateStore(path).record(
        SetupStudy(
            setup_id="trend_continuation_v1",
            symbol="NSE:RELIANCE",
            horizon_days=10,
            sample_count=120,
            win_count=104,
            win_rate=0.86,
            data_as_of=bars[-1].event_at,
        )
    )
    result = ConfidenceEngine(path).assess(
        bars=bars,
        report=_report(),
        plan=_plan(),
        specialists=_specialists(),
        as_of=NOW,
    )
    assert result is not None
    assert result.base_rate.rate_id == rate.rate_id
    assert result.assessment.confirming_families == 4
    assert result.assessment.level.value == "VERY_HIGH"
    assert result.assessment.disposition.value == "SUGGEST"
    assert result.assessment.vetoes == ()
    assert result.assessment.calibrated is False


def test_missing_volume_vetoes_the_candidate(tmp_path: Path) -> None:
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    bars = _bars()
    SetupBaseRateStore(path).record(
        SetupStudy(
            setup_id="trend_continuation_v1",
            symbol="NSE:RELIANCE",
            horizon_days=10,
            sample_count=120,
            win_count=104,
            win_rate=0.86,
            data_as_of=bars[-1].event_at,
        )
    )
    result = ConfidenceEngine(path).assess(
        bars=bars,
        report=_report(with_volume=False),
        plan=_plan(),
        specialists=_specialists(),
        as_of=NOW,
    )
    assert result is not None
    assert "mandatory_structure_volume_not_confirmed" in result.assessment.vetoes
    assert result.assessment.disposition.value == "DISCARD"


def test_thin_base_rate_vetoes_the_candidate(tmp_path: Path) -> None:
    path = tmp_path / "bq.db"
    migrate(path, create=True)
    bars = _bars()
    SetupBaseRateStore(path).record(
        SetupStudy(
            setup_id="trend_continuation_v1",
            symbol="NSE:RELIANCE",
            horizon_days=10,
            sample_count=20,
            win_count=15,
            win_rate=0.75,
            data_as_of=bars[-1].event_at,
        )
    )
    result = ConfidenceEngine(path).assess(
        bars=bars,
        report=_report(),
        plan=_plan(),
        specialists=_specialists(),
        as_of=NOW,
    )
    assert result is not None
    assert "insufficient_history" in result.assessment.vetoes
