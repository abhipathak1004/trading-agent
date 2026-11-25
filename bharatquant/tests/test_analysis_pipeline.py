import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from bq.analysis.debate import Debate
from bq.analysis.pipeline import (
    AnalysisPipeline,
    PipelineError,
)
from bq.analysis.risk_team import RiskAnalyst
from bq.data.symbols import Symbol, resolve
from bq.data.vendors import (
    Bar,
    BarInterval,
    DataDomain,
    NoMarketDataError,
    Quote,
    VendorRouter,
)

START = datetime(2026, 1, 1, tzinfo=UTC)
AS_OF = datetime(2026, 6, 1, tzinfo=UTC)


class FixedClock:
    def __init__(self, at: datetime) -> None:
        self.at = at

    def now(self) -> datetime:
        return self.at


def _bars(count: int, *, base: float = 100.0, step: float = 0.6) -> tuple[Bar, ...]:
    out = []
    for index in range(count):
        price = Decimal(str(round(base + index * step, 2)))
        half = Decimal("0.6")
        out.append(
            Bar(
                event_at=START + timedelta(days=index),
                open=price,
                high=price + half,
                low=price - half,
                close=price,
                volume=1000 + index,
            )
        )
    return tuple(out)


class StubVendor:
    def __init__(
        self, name: str, bars: tuple[Bar, ...], *, realtime: bool = False
    ) -> None:
        self.name = name
        self.is_realtime = realtime
        self.capabilities = frozenset({DataDomain.BARS_DAILY, DataDomain.BARS_INTRADAY})
        self._bars = bars
        self.calls = 0

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
        self.calls += 1
        return self._bars


def _router(vendor: StubVendor) -> VendorRouter:
    router = VendorRouter()
    router.register_vendor(DataDomain.BARS_DAILY, vendor)
    router.register_vendor(DataDomain.BARS_INTRADAY, vendor)
    return router


class FakeOutcome:
    def __init__(self, text: str) -> None:
        self.text = text


class ScriptedModel:
    """Replies by task_id so node ordering cannot break the test."""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: dict[str, int] = {}

    def complete(
        self,
        *,
        prompt: str,
        system: str = "",
        task_id: str = "",
        task_version: str = "",
    ) -> FakeOutcome:
        self.calls[task_id] = self.calls.get(task_id, 0) + 1
        if self.fail:
            raise RuntimeError("provider down")
        replies = {
            "analysis.bull": json.dumps(
                {
                    "argument": "uptrend intact",
                    "strongest_points": ["SMA20 above SMA50"],
                }
            ),
            "analysis.bear": json.dumps(
                {
                    "argument": "volume light",
                    "strongest_points": ["volume below average"],
                }
            ),
            "analysis.judge": json.dumps(
                {"verdict": "BUY", "reasoning": "bull case stronger", "confidence": 0.9}
            ),
            "analysis.risk": json.dumps(
                {
                    "invalidation_reason": "closes below swing support",
                    "early_warnings": ["momentum flattening"],
                    "volatility_flag": False,
                }
            ),
        }
        return FakeOutcome(replies[task_id])


def _pipeline(
    model: ScriptedModel | None = None, *, bars: tuple[Bar, ...] | None = None
) -> tuple[AnalysisPipeline, StubVendor]:
    vendor = StubVendor("stub", bars or _bars(200))
    debate = Debate(model) if model is not None else None
    risk = RiskAnalyst(model) if model is not None else None
    return AnalysisPipeline(
        _router(vendor),
        debate=debate,
        risk_analyst=risk,
        clock=FixedClock(AS_OF),
    ), vendor


# -- happy path ------------------------------------------------------------


def test_full_run_produces_a_plan_and_names_its_provenance() -> None:
    pipeline, vendor = _pipeline(ScriptedModel())
    result = pipeline.run(
        symbol=resolve("NSE:RELIANCE"),
        timeframe="swing",
        equity_paise=1_000_000_00,
        available_cash_paise=500_000_00,
        risk_basis_points=100,
        as_of=AS_OF,
    )
    assert vendor.calls == 1
    assert result.mode == "FULL"
    assert result.vendor_daily == "stub"
    assert result.is_realtime is False
    assert result.plan is not None
    assert result.plan.order_type.value == "LIMIT"
    assert result.plan.data_freshness.value == "delayed_yfinance"


def test_plan_quantity_equals_the_sizer_output() -> None:
    from bq.core import CurrencyPaise
    from bq.risk.sizing import size_long_fixed_fractional

    pipeline, _ = _pipeline(ScriptedModel())
    result = pipeline.run(
        symbol=resolve("NSE:RELIANCE"),
        timeframe="swing",
        equity_paise=1_000_000_00,
        available_cash_paise=500_000_00,
        risk_basis_points=100,
        as_of=AS_OF,
    )
    assert result.plan is not None
    plan = result.plan
    expected = size_long_fixed_fractional(
        equity=CurrencyPaise(1_000_000_00),
        available_cash=CurrencyPaise(500_000_00),
        entry_price=CurrencyPaise(int((plan.entry_price * 100).to_integral_value())),
        stop_price=CurrencyPaise(int((plan.stop_loss * 100).to_integral_value())),
        risk_basis_points=100,
    )
    assert plan.suggested_quantity == expected.quantity.value


@pytest.mark.parametrize("ticker", ["NSE:RELIANCE", "NSE:TCS", "NSE:HDFCBANK"])
def test_three_nse_symbols_run_end_to_end(ticker: str) -> None:
    pipeline, _ = _pipeline(ScriptedModel())
    result = pipeline.run(
        symbol=resolve(ticker),
        timeframe="swing",
        equity_paise=2_000_000_00,
        available_cash_paise=1_000_000_00,
        risk_basis_points=75,
        as_of=AS_OF,
    )
    assert result.symbol == ticker
    assert result.report.bar_count > 0
    assert result.plan is not None


# -- degradation -----------------------------------------------------------


def test_provider_outage_degrades_to_rules_only() -> None:
    pipeline, _ = _pipeline(ScriptedModel(fail=True))
    result = pipeline.run(
        symbol=resolve("NSE:RELIANCE"),
        timeframe="swing",
        equity_paise=1_000_000_00,
        available_cash_paise=500_000_00,
        risk_basis_points=100,
        as_of=AS_OF,
    )
    assert result.mode == "RULES_ONLY"
    assert result.bull is None and result.bear is None
    # The nodes still ran and recorded the degradation, so sizing was not skipped.
    assert "judge" not in result.skipped_nodes
    assert any("unavailable" in note for note in result.notes)
    # A plan can still be produced, and it still states its invalidation.
    assert result.plan is not None
    assert result.plan.invalidation_reason


def test_no_debate_configured_is_rules_only() -> None:
    pipeline, _ = _pipeline(None)
    result = pipeline.run(
        symbol=resolve("NSE:RELIANCE"),
        timeframe="swing",
        equity_paise=1_000_000_00,
        available_cash_paise=500_000_00,
        risk_basis_points=100,
        as_of=AS_OF,
    )
    assert result.mode == "RULES_ONLY"


def test_a_downtrend_chart_produces_no_buy_plan() -> None:
    pipeline, _ = _pipeline(ScriptedModel(), bars=_bars(200, base=300.0, step=-0.8))
    result = pipeline.run(
        symbol=resolve("NSE:TCS"),
        timeframe="swing",
        equity_paise=1_000_000_00,
        available_cash_paise=500_000_00,
        risk_basis_points=100,
        as_of=AS_OF,
    )
    assert result.plan is None
    assert "no_deterministic_long_case" in result.vetoes


def test_judge_cannot_create_a_buy_on_negative_technicals() -> None:
    """Even a bullish judge reply cannot force BUY against the numbers."""
    pipeline, _ = _pipeline(ScriptedModel(), bars=_bars(200, base=300.0, step=-0.8))
    result = pipeline.run(
        symbol=resolve("NSE:TCS"),
        timeframe="swing",
        equity_paise=1_000_000_00,
        available_cash_paise=500_000_00,
        risk_basis_points=100,
        as_of=AS_OF,
    )
    assert result.verdict is None or result.verdict.verdict == "BUY"
    assert result.plan is None


# -- validation ------------------------------------------------------------


def test_unknown_timeframe_is_refused() -> None:
    pipeline, _ = _pipeline(ScriptedModel())
    with pytest.raises(PipelineError):
        pipeline.run(
            symbol=resolve("NSE:RELIANCE"),
            timeframe="scalp",
            equity_paise=1_000_000_00,
            available_cash_paise=500_000_00,
            risk_basis_points=100,
        )


def test_no_data_raises_a_typed_pipeline_error() -> None:
    class EmptyVendor(StubVendor):
        def bars(
            self,
            symbol: Symbol,
            *,
            interval: BarInterval,
            start: datetime,
            end: datetime,
        ) -> tuple[Bar, ...]:
            raise NoMarketDataError(symbol.canonical)

    router = VendorRouter()
    router.register_vendor(DataDomain.BARS_DAILY, EmptyVendor("empty", ()))
    pipeline = AnalysisPipeline(router, clock=FixedClock(AS_OF))
    with pytest.raises(PipelineError):
        pipeline.run(
            symbol=resolve("NSE:RELIANCE"),
            timeframe="swing",
            equity_paise=1_000_000_00,
            available_cash_paise=500_000_00,
            risk_basis_points=100,
            as_of=AS_OF,
        )


def test_negative_equity_is_refused() -> None:
    pipeline, _ = _pipeline(ScriptedModel())
    with pytest.raises(PipelineError):
        pipeline.run(
            symbol=resolve("NSE:RELIANCE"),
            timeframe="swing",
            equity_paise=-1,
            available_cash_paise=0,
            risk_basis_points=100,
        )


# -- look-ahead ------------------------------------------------------------


def test_bars_after_as_of_are_excluded_from_the_run() -> None:
    """A decision dated mid-history must not see later bars."""
    all_bars = _bars(300)
    cutoff = all_bars[150].event_at
    pipeline, _ = _pipeline(ScriptedModel(), bars=all_bars)
    result = pipeline.run(
        symbol=resolve("NSE:RELIANCE"),
        timeframe="swing",
        equity_paise=1_000_000_00,
        available_cash_paise=500_000_00,
        risk_basis_points=100,
        as_of=cutoff,
    )
    assert result.as_of <= cutoff
    assert result.report.as_of <= cutoff
    # No indicator can be dated after the cutoff.
    assert result.report.indicators.as_of <= cutoff


def test_result_is_json_safe() -> None:
    pipeline, _ = _pipeline(ScriptedModel())
    result = pipeline.run(
        symbol=resolve("NSE:RELIANCE"),
        timeframe="swing",
        equity_paise=1_000_000_00,
        available_cash_paise=500_000_00,
        risk_basis_points=100,
        as_of=AS_OF,
    )
    payload = json.loads(json.dumps(result.to_dict()))
    assert payload["data_provenance"]["data_freshness"] == "delayed_yfinance"
    assert payload["report"]["symbol"] == "NSE:RELIANCE"


def test_realtime_vendor_labels_freshness_as_realtime() -> None:
    vendor = StubVendor("kite", _bars(200), realtime=True)
    pipeline = AnalysisPipeline(_router(vendor), clock=FixedClock(AS_OF))
    result = pipeline.run(
        symbol=resolve("NSE:RELIANCE"),
        timeframe="swing",
        equity_paise=1_000_000_00,
        available_cash_paise=500_000_00,
        risk_basis_points=100,
        as_of=AS_OF,
    )
    assert result.is_realtime is True
    assert result.to_dict()["data_provenance"]["data_freshness"] == "realtime_kite"  # type: ignore[index]


# -- price precision --------------------------------------------------------


def test_plan_prices_are_two_decimal() -> None:
    """Rupee prices carry at most two decimals; a 16-decimal stop is unplaceable."""
    pipeline, _ = _pipeline(ScriptedModel())
    result = pipeline.run(
        symbol=resolve("NSE:RELIANCE"),
        timeframe="swing",
        equity_paise=1_000_000_00,
        available_cash_paise=500_000_00,
        risk_basis_points=100,
        as_of=AS_OF,
    )
    assert result.plan is not None
    for price in (
        result.plan.entry_price,
        result.plan.stop_loss,
        result.plan.target_price,
    ):
        # A value rounded to paise has no fractional part beyond 2 places.
        assert price == price.quantize(Decimal("0.01"))


def test_plan_stop_is_below_entry_and_target_above() -> None:
    pipeline, _ = _pipeline(ScriptedModel())
    result = pipeline.run(
        symbol=resolve("NSE:RELIANCE"),
        timeframe="swing",
        equity_paise=1_000_000_00,
        available_cash_paise=500_000_00,
        risk_basis_points=100,
        as_of=AS_OF,
    )
    assert result.plan is not None
    plan = result.plan
    assert plan.stop_loss < plan.entry_price < plan.target_price
    assert plan.risk_per_share > 0
