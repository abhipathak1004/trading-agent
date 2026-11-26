import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from bq.analysis.batch import (
    MAX_BATCH_SYMBOLS,
    BatchAnalyst,
    BatchError,
    parse_symbol_list,
)
from bq.analysis.debate import Debate, ModelCaller
from bq.analysis.pipeline import AnalysisPipeline
from bq.analysis.risk_team import RiskAnalyst
from bq.data.symbols import Symbol
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


def _bars(
    count: int = 200, *, base: float = 100.0, step: float = 0.6
) -> tuple[Bar, ...]:
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


class PerSymbolVendor:
    """Returns bars for known symbols and raises for others."""

    name = "stub"
    is_realtime = False
    capabilities = frozenset({DataDomain.BARS_DAILY, DataDomain.BARS_INTRADAY})

    def __init__(
        self, known: dict[str, tuple[Bar, ...]], *, fail: set[str] | None = None
    ):
        self.known = known
        self.fail = fail or set()

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
        if symbol.tradingsymbol in self.fail:
            raise NoMarketDataError(symbol.canonical)
        if symbol.tradingsymbol not in self.known:
            raise NoMarketDataError(symbol.canonical)
        return self.known[symbol.tradingsymbol]


class FakeOutcome:
    def __init__(self, text: str) -> None:
        self.text = text


class StubModel:
    def complete(
        self,
        *,
        prompt: str,
        system: str = "",
        task_id: str = "",
        task_version: str = "",
    ) -> FakeOutcome:
        replies = {
            "analysis.bull": json.dumps({"argument": "up", "strongest_points": ["a"]}),
            "analysis.bear": json.dumps(
                {"argument": "down", "strongest_points": ["b"]}
            ),
            "analysis.judge": json.dumps(
                {"verdict": "BUY", "reasoning": "ok", "confidence": 0.8}
            ),
            "analysis.risk": json.dumps(
                {
                    "invalidation_reason": "below support",
                    "early_warnings": [],
                    "volatility_flag": False,
                }
            ),
        }
        return FakeOutcome(replies[task_id])


def _analyst(
    known: dict[str, tuple[Bar, ...]],
    *,
    fail: set[str] | None = None,
    model: ModelCaller | None = None,
) -> BatchAnalyst:
    router = VendorRouter()
    router.register_vendor(DataDomain.BARS_DAILY, PerSymbolVendor(known, fail=fail))
    router.register_vendor(DataDomain.BARS_INTRADAY, PerSymbolVendor(known, fail=fail))
    pipeline = AnalysisPipeline(
        router,
        debate=Debate(model) if model else None,
        risk_analyst=RiskAnalyst(model) if model else None,
        clock=FixedClock(AS_OF),
    )
    return BatchAnalyst(pipeline)


# -- symbol list parsing ----------------------------------------------------


def test_comma_separated_list_is_split() -> None:
    assert parse_symbol_list("RELIANCE, TCS ,HDFCBANK") == (
        "RELIANCE",
        "TCS",
        "HDFCBANK",
    )


def test_semicolons_and_newlines_also_split() -> None:
    assert parse_symbol_list("RELIANCE;TCS\nHDFCBANK") == (
        "RELIANCE",
        "TCS",
        "HDFCBANK",
    )


def test_exchange_prefix_is_preserved() -> None:
    assert parse_symbol_list("NSE:RELIANCE, BSE:ITC") == ("NSE:RELIANCE", "BSE:ITC")


def test_duplicates_are_collapsed_case_insensitively() -> None:
    assert parse_symbol_list("RELIANCE, reliance, tcs") == ("RELIANCE", "tcs")


def test_empty_list_is_rejected() -> None:
    with pytest.raises(BatchError):
        parse_symbol_list("   ,  , ")


def test_oversized_list_is_rejected() -> None:
    too_many = ",".join(f"SYM{index}" for index in range(MAX_BATCH_SYMBOLS + 1))
    with pytest.raises(BatchError, match="At most"):
        parse_symbol_list(too_many)


# -- batch execution --------------------------------------------------------


def test_batch_returns_one_item_per_symbol_in_order() -> None:
    known = {
        "RELIANCE": _bars(),
        "TCS": _bars(base=2000.0),
        "HDFCBANK": _bars(base=1500.0),
    }
    batch = _analyst(known, model=StubModel()).run(
        symbols=("RELIANCE", "TCS", "HDFCBANK"),
        timeframe="swing",
        equity_paise=10_000_000_00,
        available_cash_paise=5_000_000_00,
        risk_basis_points=100,
        as_of=AS_OF,
    )
    assert [item.symbol for item in batch.items] == [
        "NSE:RELIANCE",
        "NSE:TCS",
        "NSE:HDFCBANK",
    ]
    assert batch.succeeded == 3
    assert batch.failed == 0


def test_one_failure_does_not_stop_the_batch() -> None:
    known = {"RELIANCE": _bars(), "TCS": _bars(base=2000.0)}
    batch = _analyst(known, fail={"TCS"}).run(
        symbols=("RELIANCE", "TCS"),
        timeframe="swing",
        equity_paise=10_000_000_00,
        available_cash_paise=5_000_000_00,
        risk_basis_points=100,
        as_of=AS_OF,
    )
    assert batch.succeeded == 1
    assert batch.failed == 1
    failed = batch.items[1]
    assert failed.ok is False
    assert failed.error is not None
    # The successful symbol still produced a full analysis.
    assert batch.items[0].result is not None


def test_an_invalid_symbol_is_reported_not_raised() -> None:
    batch = _analyst({"RELIANCE": _bars()}).run(
        symbols=("RELIANCE", "not a valid ticker!!!"),
        timeframe="swing",
        equity_paise=10_000_000_00,
        available_cash_paise=5_000_000_00,
        risk_basis_points=100,
        as_of=AS_OF,
    )
    assert batch.failed == 1
    assert "invalid symbol" in (batch.items[1].error or "")


def test_an_index_is_refused_with_a_clear_reason() -> None:
    batch = _analyst({"RELIANCE": _bars()}).run(
        symbols=("NIFTY 50",),
        timeframe="swing",
        equity_paise=10_000_000_00,
        available_cash_paise=5_000_000_00,
        risk_basis_points=100,
        as_of=AS_OF,
    )
    assert batch.failed == 1
    assert "indices" in (batch.items[0].error or "")


def test_an_empty_batch_is_rejected() -> None:
    with pytest.raises(BatchError):
        _analyst({"RELIANCE": _bars()}).run(
            symbols=(),
            timeframe="swing",
            equity_paise=1,
            available_cash_paise=1,
            risk_basis_points=100,
        )


def test_batch_serialises_to_json() -> None:
    known = {"RELIANCE": _bars(), "TCS": _bars(base=2000.0)}
    batch = _analyst(known, model=StubModel()).run(
        symbols=("RELIANCE", "TCS"),
        timeframe="swing",
        equity_paise=10_000_000_00,
        available_cash_paise=5_000_000_00,
        risk_basis_points=100,
        as_of=AS_OF,
    )
    payload = json.loads(json.dumps(batch.to_dict()))
    assert payload["requested"] == 2
    assert payload["succeeded"] == 2
    assert payload["items"][0]["analysis"]["symbol"] == "NSE:RELIANCE"


def test_a_whole_batch_of_failures_still_returns_a_timestamp() -> None:
    batch = _analyst({}, fail={"RELIANCE"}).run(
        symbols=("RELIANCE",),
        timeframe="swing",
        equity_paise=1_000_000_00,
        available_cash_paise=500_000_00,
        risk_basis_points=100,
        as_of=None,
    )
    assert batch.failed == 1
    assert isinstance(batch.as_of, datetime)


# -- agent trace ------------------------------------------------------------


def test_each_result_exposes_the_agent_trace() -> None:
    known = {"RELIANCE": _bars()}
    batch = _analyst(known, model=StubModel()).run(
        symbols=("RELIANCE",),
        timeframe="swing",
        equity_paise=10_000_000_00,
        available_cash_paise=5_000_000_00,
        risk_basis_points=100,
        as_of=AS_OF,
    )
    result = batch.items[0].result
    assert result is not None
    roles = [entry.role for entry in result.trace]
    assert "Technical analyst" in roles
    assert "Bull advocate" in roles
    assert "Bear advocate" in roles
    assert "Research judge" in roles
    assert "Position sizer" in roles


def test_degraded_mode_marks_the_llm_agents_as_degraded() -> None:
    known = {"RELIANCE": _bars()}
    # No model at all: the debate cannot run.
    batch = _analyst(known).run(
        symbols=("RELIANCE",),
        timeframe="swing",
        equity_paise=10_000_000_00,
        available_cash_paise=5_000_000_00,
        risk_basis_points=100,
        as_of=AS_OF,
    )
    result = batch.items[0].result
    assert result is not None
    assert result.mode == "RULES_ONLY"
    by_node = {entry.node: entry for entry in result.trace}
    assert by_node["bull"].status == "degraded"
    assert by_node["judge"].status == "degraded"
    # Deterministic agents still ran normally.
    assert by_node["technical"].status == "ok"
    assert by_node["size"].status in {"ok", "degraded"}


def test_the_trace_is_json_safe() -> None:
    known = {"RELIANCE": _bars()}
    batch = _analyst(known, model=StubModel()).run(
        symbols=("RELIANCE",),
        timeframe="swing",
        equity_paise=10_000_000_00,
        available_cash_paise=5_000_000_00,
        risk_basis_points=100,
        as_of=AS_OF,
    )
    payload = json.loads(json.dumps(batch.to_dict()))
    agents = payload["items"][0]["analysis"]["agents"]
    assert agents
    assert set(agents[0]) == {"node", "role", "status", "summary"}


def test_agents_are_ordered_as_the_pipeline_reads() -> None:
    """Bull must precede bear, even though the DAG sorts a layer alphabetically."""
    known = {"RELIANCE": _bars()}
    batch = _analyst(known, model=StubModel()).run(
        symbols=("RELIANCE",),
        timeframe="swing",
        equity_paise=10_000_000_00,
        available_cash_paise=5_000_000_00,
        risk_basis_points=100,
        as_of=AS_OF,
    )
    result = batch.items[0].result
    assert result is not None
    nodes = [entry.node for entry in result.trace]
    assert nodes.index("bull") < nodes.index("bear")
    assert nodes.index("technical") < nodes.index("bull")
    assert nodes.index("decision") < nodes.index("final_gate")
    assert nodes[-1] == "final_gate"
