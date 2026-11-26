"""Append-only setup base-rate records used by the confidence engine."""

from __future__ import annotations

import secrets
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from bq.compute.setup_study import SetupStudy
from bq.core import (
    IDENTIFIER,
    Clock,
    SystemClock,
    open_connection,
    parse_timestamp,
    timestamp,
    utc,
)
from bq.infra.audit import append_event


class SetupRateError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class SetupBaseRate:
    rate_id: str
    setup_id: str
    symbol: str
    regime: str
    horizon_days: int
    sample_count: int
    win_count: int
    win_rate: float
    data_as_of: datetime
    computed_at: datetime

    def to_dict(self) -> dict[str, object]:
        return {
            "setup_id": self.setup_id,
            "symbol": self.symbol,
            "regime": self.regime,
            "horizon_days": self.horizon_days,
            "sample_count": self.sample_count,
            "win_count": self.win_count,
            "win_rate": self.win_rate,
            "data_as_of": self.data_as_of.isoformat(),
            "computed_at": self.computed_at.isoformat(),
        }


class SetupBaseRateStore:
    def __init__(self, path: Path, clock: Clock | None = None) -> None:
        self.path = path
        self.clock = clock or SystemClock()

    def _connect(self) -> sqlite3.Connection:
        return open_connection(self.path, foreign_keys=True)

    def record(
        self,
        study: SetupStudy,
        *,
        regime: str = "ALL",
    ) -> SetupBaseRate:
        if not isinstance(study, SetupStudy):
            raise SetupRateError("A setup study is required.")
        if not isinstance(regime, str) or not regime:
            raise SetupRateError("A regime label is required.")
        if study.sample_count < 0 or study.win_count < 0:
            raise SetupRateError("Setup counts must be nonnegative.")
        computed_at = utc(self.clock.now())
        rate_id = "sbr_" + secrets.token_urlsafe(18)
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            with connection:
                connection.execute(
                    "INSERT INTO setup_base_rates "
                    "(rate_id, setup_id, symbol, regime, horizon_days, sample_count, "
                    "win_count, win_rate, data_as_of, computed_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        rate_id,
                        study.setup_id,
                        study.symbol,
                        regime,
                        study.horizon_days,
                        study.sample_count,
                        study.win_count,
                        study.win_rate,
                        timestamp(study.data_as_of),
                        timestamp(computed_at),
                    ),
                )
                append_event(
                    connection,
                    occurred_at=computed_at,
                    actor="system",
                    action="confidence.base_rate.recorded",
                    object_type="setup_base_rate",
                    object_id=rate_id,
                    payload={
                        "setup_id": study.setup_id,
                        "symbol": study.symbol,
                        "samples": str(study.sample_count),
                    },
                )
        return SetupBaseRate(
            rate_id=rate_id,
            setup_id=study.setup_id,
            symbol=study.symbol,
            regime=regime,
            horizon_days=study.horizon_days,
            sample_count=study.sample_count,
            win_count=study.win_count,
            win_rate=study.win_rate,
            data_as_of=study.data_as_of,
            computed_at=computed_at,
        )

    def latest(
        self,
        *,
        setup_id: str,
        symbol: str,
        horizon_days: int,
        regime: str = "ALL",
        as_of: datetime,
    ) -> SetupBaseRate | None:
        if IDENTIFIER.fullmatch(setup_id) is None:
            raise SetupRateError("Invalid setup identifier.")
        if IDENTIFIER.fullmatch(symbol) is None:
            raise SetupRateError("Invalid setup symbol.")
        if (
            not isinstance(horizon_days, int)
            or isinstance(horizon_days, bool)
            or horizon_days < 1
        ):
            raise SetupRateError("Invalid setup horizon.")
        if as_of.utcoffset() is None:
            raise SetupRateError("as_of must be timezone-aware.")
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT rate_id, setup_id, symbol, regime, horizon_days, "
                "sample_count, win_count, win_rate, data_as_of, computed_at "
                "FROM setup_base_rates "
                "WHERE setup_id = ? AND symbol = ? AND regime = ? "
                "AND horizon_days = ? AND data_as_of <= ? "
                "ORDER BY data_as_of DESC, computed_at DESC LIMIT 1",
                (
                    setup_id,
                    symbol,
                    regime,
                    horizon_days,
                    timestamp(utc(as_of)),
                ),
            ).fetchone()
        if row is None:
            return None
        return SetupBaseRate(
            rate_id=str(row[0]),
            setup_id=str(row[1]),
            symbol=str(row[2]),
            regime=str(row[3]),
            horizon_days=int(row[4]),
            sample_count=int(row[5]),
            win_count=int(row[6]),
            win_rate=float(row[7]),
            data_as_of=parse_timestamp(str(row[8])),
            computed_at=parse_timestamp(str(row[9])),
        )


__all__ = ["SetupBaseRate", "SetupBaseRateStore", "SetupRateError"]
