"""Strict, versioned strategy DSL shared by every backtest engine."""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from typing import Mapping

from bq.core import IDENTIFIER, canonical_digest, parse_timestamp
from bq.strategies import StrategyError, StrategyKind


class EntryRule(StrEnum):
    SMA_CROSS = "SMA_CROSS"
    DONCHIAN_BREAKOUT = "DONCHIAN_BREAKOUT"


class ExitRule(StrEnum):
    SMA_CROSS_DOWN = "SMA_CROSS_DOWN"
    DONCHIAN_EXIT = "DONCHIAN_EXIT"


ALLOWED_KEYS = {
    "strategy_id",
    "version",
    "name",
    "kind",
    "timeframe",
    "universe",
    "entry_rule",
    "exit_rule",
    "lookback",
    "atr_period",
    "stop_atr_multiple",
    "target_atr_multiple",
    "risk_per_trade_bps",
    "slippage_bps",
    "max_positions",
    "knowledge_cutoff",
}


@dataclass(frozen=True, slots=True)
class StrategyDefinition:
    strategy_id: str
    version: str
    name: str
    kind: StrategyKind
    timeframe: str
    universe: tuple[str, ...]
    entry_rule: EntryRule
    exit_rule: ExitRule
    lookback: int = 20
    atr_period: int = 14
    stop_atr_multiple: float = 1.5
    target_atr_multiple: float = 2.5
    risk_per_trade_bps: int = 100
    slippage_bps: int = 5
    max_positions: int = 5
    knowledge_cutoff: datetime | None = None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.strategy_id, str)
            or IDENTIFIER.fullmatch(self.strategy_id) is None
        ):
            raise StrategyError("Invalid strategy identifier.")
        if (
            not isinstance(self.version, str)
            or IDENTIFIER.fullmatch(self.version) is None
        ):
            raise StrategyError("Invalid strategy version.")
        if not isinstance(self.name, str) or not 1 <= len(self.name) <= 128:
            raise StrategyError("Strategy name must contain 1-128 characters.")
        if not isinstance(self.kind, StrategyKind):
            raise StrategyError("Invalid strategy kind.")
        if self.timeframe not in {"swing", "intraday"}:
            raise StrategyError("Strategy timeframe must be swing or intraday.")
        if (
            not isinstance(self.universe, tuple)
            or not 1 <= len(self.universe) <= 50
            or any(
                not isinstance(item, str) or IDENTIFIER.fullmatch(item) is None
                for item in self.universe
            )
        ):
            raise StrategyError("Strategy universe must contain 1-50 symbols.")
        if not isinstance(self.entry_rule, EntryRule):
            raise StrategyError("Invalid entry rule.")
        if not isinstance(self.exit_rule, ExitRule):
            raise StrategyError("Invalid exit rule.")
        for label, value, low, high in (
            ("lookback", self.lookback, 2, 200),
            ("atr_period", self.atr_period, 2, 100),
            ("risk_per_trade_bps", self.risk_per_trade_bps, 1, 500),
            ("slippage_bps", self.slippage_bps, 0, 100),
            ("max_positions", self.max_positions, 1, 50),
        ):
            if isinstance(value, bool) or not isinstance(value, int):
                raise StrategyError(f"{label} must be an integer.")
            if not low <= value <= high:
                raise StrategyError(f"{label} is outside its bound.")
        for label, numeric in (
            ("stop_atr_multiple", self.stop_atr_multiple),
            ("target_atr_multiple", self.target_atr_multiple),
        ):
            if isinstance(numeric, bool) or not isinstance(numeric, int | float):
                raise StrategyError(f"{label} must be numeric.")
            if not 0.1 <= float(numeric) <= 20:
                raise StrategyError(f"{label} is outside its bound.")
        if float(self.target_atr_multiple) <= float(self.stop_atr_multiple):
            raise StrategyError("Target multiple must exceed the stop multiple.")
        if self.kind is not StrategyKind.RULE:
            if self.knowledge_cutoff is None:
                raise StrategyError(
                    "LLM-dependent strategies require a knowledge cutoff."
                )
            if self.knowledge_cutoff.utcoffset() is None:
                raise StrategyError("Knowledge cutoff must be timezone-aware.")

    def to_dict(self) -> dict[str, object]:
        return {
            "strategy_id": self.strategy_id,
            "version": self.version,
            "name": self.name,
            "kind": self.kind.value,
            "timeframe": self.timeframe,
            "universe": list(self.universe),
            "entry_rule": self.entry_rule.value,
            "exit_rule": self.exit_rule.value,
            "lookback": self.lookback,
            "atr_period": self.atr_period,
            "stop_atr_multiple": self.stop_atr_multiple,
            "target_atr_multiple": self.target_atr_multiple,
            "risk_per_trade_bps": self.risk_per_trade_bps,
            "slippage_bps": self.slippage_bps,
            "max_positions": self.max_positions,
            "knowledge_cutoff": (
                self.knowledge_cutoff.isoformat()
                if self.knowledge_cutoff is not None
                else None
            ),
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))

    @property
    def content_hash(self) -> str:
        return canonical_digest(self.to_dict())

    def with_lifecycle(self, version: str) -> StrategyDefinition:
        return replace(self, version=version)


def from_dict(value: Mapping[str, object]) -> StrategyDefinition:
    if not isinstance(value, Mapping):
        raise StrategyError("Strategy definition must be an object.")
    unknown = set(value) - ALLOWED_KEYS
    if unknown:
        raise StrategyError("Strategy definition contains unknown fields.")
    try:
        raw_universe = value["universe"]
        if not isinstance(raw_universe, list):
            raise StrategyError("Strategy universe must be a list.")
        raw_cutoff = value.get("knowledge_cutoff")
        cutoff = None
        if raw_cutoff is not None:
            if not isinstance(raw_cutoff, str):
                raise StrategyError("Knowledge cutoff must be a timestamp.")
            cutoff = parse_timestamp(raw_cutoff)
        return StrategyDefinition(
            strategy_id=str(value["strategy_id"]),
            version=str(value["version"]),
            name=str(value["name"]),
            kind=StrategyKind(str(value["kind"])),
            timeframe=str(value["timeframe"]),
            universe=tuple(str(item) for item in raw_universe),
            entry_rule=EntryRule(str(value["entry_rule"])),
            exit_rule=ExitRule(str(value["exit_rule"])),
            lookback=int(str(value.get("lookback", 20))),
            atr_period=int(str(value.get("atr_period", 14))),
            stop_atr_multiple=float(str(value.get("stop_atr_multiple", 1.5))),
            target_atr_multiple=float(str(value.get("target_atr_multiple", 2.5))),
            risk_per_trade_bps=int(str(value.get("risk_per_trade_bps", 100))),
            slippage_bps=int(str(value.get("slippage_bps", 5))),
            max_positions=int(str(value.get("max_positions", 5))),
            knowledge_cutoff=cutoff,
        )
    except (KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, StrategyError):
            raise
        raise StrategyError("Invalid strategy definition.") from exc


__all__ = [
    "ALLOWED_KEYS",
    "EntryRule",
    "ExitRule",
    "StrategyDefinition",
    "from_dict",
]
