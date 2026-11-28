"""Run the analysis pipeline across several symbols.

The single-symbol pipeline does the agent work; this module is the batch
orchestrator a user drives from the UI. It exists so the caller does not have to
remember the isolation rules:

* **One symbol's failure never hides another's.** A delisted ticker, a vendor
  outage or a bad symbol is recorded against that symbol and the batch continues.
  A batch that fails wholesale because one name was mistyped would be useless.
* **Bounded input.** The symbol list is capped, so a paste of the whole NIFTY 500
  cannot become hundreds of vendor calls and model spend from one click.
* **Deterministic ordering.** Results come back in the order requested, so the UI
  table does not reshuffle itself between runs.

This is deliberately synchronous and sequential. The vendor router already limits
each call, and running ten symbols concurrently against one HTTP client trades a
real rate-limit risk for a latency win that does not matter for a manual batch.
If that ever changes, the fan-out belongs here rather than in the pipeline.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from bq.analysis.pipeline import AnalysisPipeline, AnalysisResult, PipelineError
from bq.data.symbols import Symbol, SymbolError, resolve

MAX_BATCH_SYMBOLS = 10


class BatchError(ValueError):
    """The batch request itself is invalid (bad size, empty list, duplicates)."""


@dataclass(frozen=True, slots=True)
class BatchItem:
    """One symbol's outcome: a result, or a typed failure with its reason."""

    requested: str
    symbol: str
    result: AnalysisResult | None
    error: str | None

    def __post_init__(self) -> None:
        if (self.result is None) == (self.error is None):
            raise ValueError("A batch item carries exactly one of result or error.")

    @property
    def ok(self) -> bool:
        return self.result is not None

    def to_dict(self) -> dict[str, object]:
        return {
            "requested": self.requested,
            "symbol": self.symbol,
            "ok": self.ok,
            "error": self.error,
            "analysis": None if self.result is None else self.result.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class BatchResult:
    """All outcomes for one submitted batch, in requested order."""

    timeframe: str
    as_of: datetime
    items: tuple[BatchItem, ...]

    @property
    def succeeded(self) -> int:
        return sum(1 for item in self.items if item.ok)

    @property
    def failed(self) -> int:
        return len(self.items) - self.succeeded

    def to_dict(self) -> dict[str, object]:
        return {
            "timeframe": self.timeframe,
            "as_of": self.as_of.isoformat(),
            "requested": len(self.items),
            "succeeded": self.succeeded,
            "failed": self.failed,
            "items": [item.to_dict() for item in self.items],
        }


def parse_symbol_list(raw: str) -> tuple[str, ...]:
    """Split a user-typed list on commas, semicolons or newlines.

    Duplicates (case-insensitively, after resolving) are collapsed so the same
    company is not analysed and billed twice in one batch.
    """
    if not isinstance(raw, str):
        raise BatchError("Symbols must be text.")
    parts = [
        part.strip() for part in raw.replace(";", ",").replace("\n", ",").split(",")
    ]
    requested = [part for part in parts if part]
    if not requested:
        raise BatchError("Enter at least one symbol.")
    if len(requested) > MAX_BATCH_SYMBOLS:
        raise BatchError(
            f"At most {MAX_BATCH_SYMBOLS} symbols per batch; got {len(requested)}."
        )
    seen: set[str] = set()
    unique: list[str] = []
    for entry in requested:
        key = entry.upper()
        if key in seen:
            continue
        seen.add(key)
        unique.append(entry)
    return tuple(unique)


class BatchAnalyst:
    """Runs the pipeline over up to ``MAX_BATCH_SYMBOLS`` symbols."""

    def __init__(self, pipeline: AnalysisPipeline) -> None:
        self.pipeline = pipeline

    def run(
        self,
        *,
        symbols: tuple[str, ...],
        timeframe: str,
        equity_paise: int,
        available_cash_paise: int,
        risk_basis_points: int,
        as_of: datetime | None = None,
    ) -> BatchResult:
        if not isinstance(symbols, tuple) or not symbols:
            raise BatchError("At least one symbol is required.")
        if len(symbols) > MAX_BATCH_SYMBOLS:
            raise BatchError(
                f"At most {MAX_BATCH_SYMBOLS} symbols per batch; got {len(symbols)}."
            )

        items: list[BatchItem] = []
        resolved_as_of: datetime | None = None
        for requested in symbols:
            try:
                symbol: Symbol = resolve(requested)
            except SymbolError as exc:
                items.append(
                    BatchItem(
                        requested=requested,
                        symbol=requested,
                        result=None,
                        error=f"invalid symbol: {exc}",
                    )
                )
                continue
            if not symbol.tradeable:
                items.append(
                    BatchItem(
                        requested=requested,
                        symbol=symbol.canonical,
                        result=None,
                        error="indices cannot be analysed for a trade plan",
                    )
                )
                continue
            try:
                result = self.pipeline.run(
                    symbol=symbol,
                    timeframe=timeframe,
                    equity_paise=equity_paise,
                    available_cash_paise=available_cash_paise,
                    risk_basis_points=risk_basis_points,
                    as_of=as_of,
                )
            except PipelineError as exc:
                items.append(
                    BatchItem(
                        requested=requested,
                        symbol=symbol.canonical,
                        result=None,
                        error=str(exc),
                    )
                )
                continue
            resolved_as_of = resolved_as_of or result.as_of
            items.append(
                BatchItem(
                    requested=requested,
                    symbol=result.symbol,
                    result=result,
                    error=None,
                )
            )

        if resolved_as_of is None:
            # Every symbol failed. Timestamp the batch with the clock so the
            # response still carries a coherent as_of for the UI.
            resolved_as_of = as_of or self.pipeline.clock.now()

        return BatchResult(
            timeframe=timeframe, as_of=resolved_as_of, items=tuple(items)
        )


__all__ = [
    "BatchAnalyst",
    "BatchError",
    "BatchItem",
    "BatchResult",
    "MAX_BATCH_SYMBOLS",
    "parse_symbol_list",
]
