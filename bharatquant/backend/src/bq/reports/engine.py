"""Deterministic morning and evening report engine.

v1 intentionally has no LLM: the report's numbers, predictions and resolver rules
are computed in code. A later phase can add a synthesis layer without changing the
frozen prediction contract.
"""

from __future__ import annotations

import secrets
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from bq.core import Clock, SystemClock, canonical_digest
from bq.data.symbols import SymbolError
from bq.data.symbols import resolve as resolve_symbol
from bq.data.vendors import Bar, BarInterval, DataDomain, VendorRouter
from bq.predictions import (
    PredictionDetail,
    PredictionError,
    PredictionStore,
    StoredPrediction,
)
from bq.reports.scoring import BinaryOutcome, score_binary_outcomes
from bq.reports.store import ReportRecord, ReportStore

IST = ZoneInfo("Asia/Kolkata")
SESSION_OPEN = time(9, 15)
SESSION_CLOSE = time(15, 30)
LOOKBACK = 200
RANGE_ATR_MULTIPLE = 1.2


@dataclass(frozen=True, slots=True)
class _PredictionPlan:
    kind: str
    target: str
    statement: str
    probability: float
    threshold: float | None
    params: dict[str, object]
    rule: str
    ends_at: datetime | None = None


class ReportEngineError(ValueError):
    pass


def _next_session(now: datetime) -> tuple[datetime, datetime]:
    local = now.astimezone(IST)
    candidate = local.date()
    if local.time() >= SESSION_OPEN:
        candidate += timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate += timedelta(days=1)
    start = datetime.combine(candidate, SESSION_OPEN, tzinfo=IST).astimezone(UTC)
    end = datetime.combine(candidate, SESSION_CLOSE, tzinfo=IST).astimezone(UTC)
    return start, end


def _session_end_after(start: datetime, sessions: int) -> datetime:
    candidate = start.astimezone(IST).date()
    counted = 0
    while counted < sessions:
        candidate += timedelta(days=1)
        if candidate.weekday() < 5:
            counted += 1
    return datetime.combine(candidate, SESSION_CLOSE, tzinfo=IST).astimezone(UTC)


class ReportEngine:
    def __init__(
        self,
        path: Path,
        router: VendorRouter,
        *,
        clock: Clock | None = None,
    ) -> None:
        self.path = path
        self.router = router
        self.clock = clock or SystemClock()
        self.predictions = PredictionStore(path, self.clock)
        self.reports = ReportStore(path, self.clock)

    def _series(self, name: str, as_of: datetime) -> tuple[tuple[Bar, ...], str]:
        symbol = resolve_symbol(name)
        result = self.router.bars(
            symbol,
            interval=BarInterval.DAY_1,
            start=as_of - timedelta(days=LOOKBACK),
            end=as_of,
            domain=DataDomain.INDICES,
        )
        bars = tuple(bar for bar in result.data if bar.event_at <= as_of)
        if len(bars) < 2:
            raise ReportEngineError(f"Not enough {name} history.")
        return bars, result.source

    def morning(self, *, as_of: datetime | None = None) -> ReportRecord:
        instant = as_of or self.clock.now()
        if instant.utcoffset() is None:
            raise ReportEngineError("Report as_of must be timezone-aware.")
        nifty, nifty_source = self._series("NIFTY 50", instant)
        vix, _ = self._series("INDIA VIX", instant)
        from bq.compute.indicators import atr, bars_to_frame, sma

        frame = bars_to_frame(nifty)
        close_series = frame["close"]
        sma20 = sma(close_series, 20)
        sma50 = sma(close_series, 50)
        atr14 = atr(frame, 14)
        if sma20 is None or sma50 is None or atr14 is None or atr14 <= 0:
            raise ReportEngineError("Index history is too short for a report.")
        close = float(close_series.iloc[-1])
        previous = float(close_series.iloc[-2])
        vix_close = float(vix[-1].close)
        vix_previous = float(vix[-2].close)
        if close > sma20 > sma50:
            regime = "trend_up"
            direction_probability = 0.65
        elif close < sma20 < sma50:
            regime = "trend_down"
            direction_probability = 0.35
        else:
            regime = "range"
            direction_probability = 0.50
        if regime == "trend_up" and direction_probability >= 0.60:
            stance = "TRADE"
            stance_probability = 0.65
        elif regime == "trend_down":
            stance = "STAND DOWN"
            stance_probability = 0.60
        else:
            stance = "SELECTIVE"
            stance_probability = 0.50

        window_start, window_end = _next_session(instant)
        stance_window_end = _session_end_after(window_start, 3)
        report_id = (
            "report_"
            + canonical_digest(
                {
                    "type": "morning",
                    "as_of": instant.isoformat(),
                    "nonce": secrets.token_urlsafe(8),
                }
            )[:24]
        )
        low = previous - RANGE_ATR_MULTIPLE * atr14
        high = previous + RANGE_ATR_MULTIPLE * atr14
        predictions = [
            _PredictionPlan(
                kind="DIRECTION",
                target="NIFTY 50",
                statement="NIFTY 50 closes above the previous close.",
                probability=direction_probability,
                threshold=0.001,
                params={"reference_close": f"{previous:.2f}"},
                rule="nifty_close_above_reference",
            ),
            _PredictionPlan(
                kind="RANGE",
                target="NIFTY 50",
                statement=f"NIFTY 50 stays within {low:.2f}-{high:.2f}.",
                probability=0.60,
                threshold=None,
                params={"low": f"{low:.2f}", "high": f"{high:.2f}"},
                rule="nifty_range_contained",
            ),
            _PredictionPlan(
                kind="GAP",
                target="NIFTY 50",
                statement="NIFTY 50 opens above the previous close.",
                probability=0.50,
                threshold=0.001,
                params={"reference_close": f"{previous:.2f}"},
                rule="nifty_gap_up",
            ),
            _PredictionPlan(
                kind="VOLATILITY",
                target="INDIA VIX",
                statement="India VIX closes above the previous close.",
                probability=0.50,
                threshold=0.0,
                params={"reference_close": f"{vix_previous:.2f}"},
                rule="vix_close_above_reference",
            ),
            _PredictionPlan(
                kind="STANCE",
                target="NIFTY 50",
                statement=(
                    f"Market stance {stance}: three-session NIFTY return meets the "
                    "stance rule."
                ),
                probability=stance_probability,
                threshold=0.0015,
                params={
                    "direction": stance,
                    "reference_close": f"{close:.2f}",
                    "deadband": "0.0015",
                },
                rule="nifty_three_session_stance",
                ends_at=stance_window_end,
            ),
        ]
        frozen: list[dict[str, object]] = []
        for plan in predictions:
            prediction_id = (
                "pred_"
                + canonical_digest({"report_id": report_id, "kind": plan.kind})[:24]
            )
            self.predictions.freeze(
                prediction_id=prediction_id,
                window_start=window_start,
                window_end=plan.ends_at or window_end,
                probability=plan.probability,
                positive_threshold=plan.threshold,
            )
            detail = self.predictions.describe(
                prediction_id,
                report_id=report_id,
                kind=plan.kind,
                target=plan.target,
                statement=plan.statement,
                params=plan.params,
                resolution_rule=plan.rule,
            )
            frozen.append(
                {
                    "prediction_id": detail.prediction_id,
                    "kind": detail.kind,
                    "target": detail.target,
                    "statement": detail.statement,
                    "probability": plan.probability,
                    "params": detail.params,
                }
            )
        body = {
            "as_of": instant.isoformat(),
            "regime": regime,
            "stance": stance,
            "nifty_close": close,
            "previous_close": previous,
            "atr_14": atr14,
            "vix_close": vix_close,
            "window_start": window_start.isoformat(),
            "window_end": window_end.isoformat(),
            "predictions": frozen,
            "source": nifty_source,
        }
        lines = [
            f"# BharatQuant morning report — {instant.astimezone(IST).date().isoformat()}",
            "",
            f"Market stance: **{stance}**. Regime **{regime}**; NIFTY close "
            f"{close:.2f} vs previous {previous:.2f}; ATR {atr14:.2f}; India VIX "
            f"{vix_close:.2f}.",
            "",
            "## Frozen predictions",
            "",
        ]
        for entry in frozen:
            lines.append(
                f"- **{entry['kind']} {entry['target']}** — "
                f"{float(str(entry['probability'])) * 100:.0f}% — {entry['statement']}"
            )
        lines.extend(
            [
                "",
                f"Prediction window: {window_start.isoformat()} to {window_end.isoformat()}.",
                "",
                "This report is deterministic research only; no order was placed.",
            ]
        )
        return self.reports.record(
            report_id=report_id,
            report_type="morning",
            report_date=instant.astimezone(IST).date(),
            status="ON_TIME",
            title="Morning report",
            body_md="\n".join(lines),
            body=body,
        )

    def evening(self, *, as_of: datetime | None = None) -> ReportRecord:
        instant = as_of or self.clock.now()
        if instant.utcoffset() is None:
            raise ReportEngineError("Report as_of must be timezone-aware.")
        due = self.predictions.due(as_of=instant)
        resolved: list[dict[str, object]] = []
        outcomes: list[BinaryOutcome] = []
        notes: list[str] = []
        for prediction, detail in due:
            try:
                occurred, source = self._resolve(prediction, detail, instant)
                self.predictions.resolve(
                    prediction.prediction_id,
                    occurred=occurred,
                    evidence_source_id=source,
                    resolved_at=instant,
                )
                outcomes.append(BinaryOutcome(prediction.probability, occurred))
                resolved.append(
                    {
                        "prediction_id": prediction.prediction_id,
                        "kind": detail.kind,
                        "target": detail.target,
                        "probability": prediction.probability,
                        "occurred": occurred,
                        "source": source,
                    }
                )
            except (PredictionError, ReportEngineError, SymbolError, ValueError) as exc:
                notes.append(f"{prediction.prediction_id}: {exc}")
            except Exception as exc:  # noqa: BLE001 - one bad vendor call is isolated
                notes.append(f"{prediction.prediction_id}: {type(exc).__name__}")
        scores = score_binary_outcomes(tuple(outcomes))
        report_id = (
            "report_"
            + canonical_digest(
                {
                    "type": "evening",
                    "as_of": instant.isoformat(),
                    "nonce": secrets.token_urlsafe(8),
                }
            )[:24]
        )
        body = {
            "as_of": instant.isoformat(),
            "examined": len(due),
            "resolved": len(resolved),
            "outcomes": resolved,
            "scores": asdict(scores),
            "notes": notes,
        }
        lines = [
            f"# BharatQuant evening report — {instant.astimezone(IST).date().isoformat()}",
            "",
            f"Resolved {len(resolved)} of {len(due)} due prediction(s).",
            "",
            "## Outcomes",
            "",
        ]
        for item in resolved:
            lines.append(
                f"- **{item['kind']} {item['target']}** — "
                f"{'HIT' if item['occurred'] else 'MISS'} — "
                f"predicted {float(str(item['probability'])) * 100:.0f}%"
            )
        if not resolved:
            lines.append("- No due predictions.")
        lines.extend(
            [
                "",
                f"Brier: {scores.mean_brier if scores.mean_brier is not None else 'n/a'}; "
                f"log loss: {scores.mean_log_loss if scores.mean_log_loss is not None else 'n/a'}; "
                f"outcome rate: {scores.outcome_rate if scores.outcome_rate is not None else 'n/a'}.",
            ]
        )
        if notes:
            lines.extend(["", "## Notes", ""] + [f"- {note}" for note in notes])
        return self.reports.record(
            report_id=report_id,
            report_type="evening",
            report_date=instant.astimezone(IST).date(),
            status="ON_TIME",
            title="Evening accuracy report",
            body_md="\n".join(lines),
            body=body,
        )

    def _resolve(
        self,
        prediction: StoredPrediction,
        detail: PredictionDetail,
        as_of: datetime,
    ) -> tuple[bool, str]:
        target = resolve_symbol(detail.target)
        result = self.router.bars(
            target,
            interval=BarInterval.DAY_1,
            start=prediction.window_start,
            end=min(as_of, prediction.window_end),
            domain=DataDomain.INDICES,
        )
        bars = tuple(
            bar
            for bar in result.data
            if prediction.window_start <= bar.event_at <= prediction.window_end
        )
        if not bars:
            raise ReportEngineError("no bars in the prediction window")
        threshold = prediction.positive_threshold or 0.0
        params = detail.params
        if detail.kind == "DIRECTION":
            reference = float(str(params["reference_close"]))
            occurred = float(bars[-1].close) > reference * (1.0 + threshold)
        elif detail.kind == "RANGE":
            low = float(str(params["low"]))
            high = float(str(params["high"]))
            occurred = (
                min(float(bar.low) for bar in bars) >= low
                and max(float(bar.high) for bar in bars) <= high
            )
        elif detail.kind == "GAP":
            reference = float(str(params["reference_close"]))
            occurred = float(bars[0].open) > reference * (1.0 + threshold)
        elif detail.kind == "VOLATILITY":
            reference = float(str(params["reference_close"]))
            occurred = float(bars[-1].close) > reference
        elif detail.kind == "STANCE":
            reference = float(str(params["reference_close"]))
            deadband = float(str(params["deadband"]))
            forward_return = float(bars[-1].close) / reference - 1.0
            direction = str(params["direction"])
            if direction == "TRADE":
                occurred = forward_return > deadband
            elif direction == "STAND DOWN":
                occurred = forward_return < -deadband
            else:
                occurred = abs(forward_return) <= deadband
        else:
            raise ReportEngineError("unknown prediction kind")
        return occurred, result.source


__all__ = ["ReportEngine", "ReportEngineError"]
