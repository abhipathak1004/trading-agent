"""NSE daily index closes (Nifty 50, India VIX, sectors).

One CSV per trading day at ``ind_close_all_DDMMYYYY.csv``. Cached as Parquet.
"""

from __future__ import annotations

import io
from datetime import date
from pathlib import Path

import httpx
import polars as pl
import structlog

from pinak.core.calendar import resolve_trading_days
from pinak.core.errors import DataUnavailableError

log = structlog.get_logger(__name__)

_URLS = (
    "https://nsearchives.nseindia.com/content/indices/ind_close_all_{stamp}.csv",
    "https://archives.nseindia.com/content/indices/ind_close_all_{stamp}.csv",
)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
    ),
    "Accept": "text/csv,*/*",
}


def _number(column: str) -> pl.Expr:
    return pl.col(column).str.strip_chars().cast(pl.Float64, strict=False)


class NseIndicesProvider:
    """Daily close for every NSE index, including India VIX."""

    def __init__(
        self,
        cache_dir: Path,
        client: httpx.Client | None = None,
        timeout: float = 20.0,
    ) -> None:
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._client = client
        self._timeout = timeout

    def path_for(self, trading_day: date) -> Path:
        return self.cache_dir / f"{trading_day.isoformat()}.parquet"

    def fetch(self, trading_day: date) -> pl.DataFrame:
        cached = self.path_for(trading_day)
        if cached.exists():
            return pl.read_parquet(cached)
        if trading_day.weekday() >= 5:
            raise DataUnavailableError(f"{trading_day} is a weekend")
        frame = self._download(trading_day)
        frame.write_parquet(cached)
        return frame

    def history(self, index_name: str, end: date, count: int) -> pl.DataFrame:
        """Closes for one index over the last ``count`` trading days."""
        days = resolve_trading_days(self.fetch, end, count, minimum=count)
        frames = [
            self.fetch(day)
            .filter(pl.col("index") == index_name)
            .select("date", "close", "change_pct")
            for day in days
        ]
        frame = pl.concat(frames, how="vertical_relaxed").sort("date")
        if frame.height < count:
            raise DataUnavailableError(
                f"{index_name}: only {frame.height} closes, expected {count}"
            )
        return frame

    def _download(self, trading_day: date) -> pl.DataFrame:
        stamp = trading_day.strftime("%d%m%Y")
        owns_client = self._client is None
        client = self._client or httpx.Client(
            timeout=self._timeout, headers=_HEADERS, follow_redirects=True
        )
        last_error = "unknown"
        try:
            for template in _URLS:
                url = template.format(stamp=stamp)
                try:
                    response = client.get(url)
                except httpx.HTTPError as exc:
                    last_error = f"{type(exc).__name__}: {exc}"
                    continue
                if response.status_code == 200 and response.content:
                    return self._parse(response.content, trading_day)
                last_error = f"HTTP {response.status_code}"
            raise DataUnavailableError(f"no index closes for {trading_day} ({last_error})")
        finally:
            if owns_client:
                client.close()

    @staticmethod
    def _parse(content: bytes, trading_day: date) -> pl.DataFrame:
        raw = pl.read_csv(
            io.BytesIO(content),
            infer_schema_length=0,
            truncate_ragged_lines=True,
            ignore_errors=True,
        )
        raw.columns = [column.strip() for column in raw.columns]
        if "Index Name" not in raw.columns or "Closing Index Value" not in raw.columns:
            raise DataUnavailableError("unexpected index file schema")

        change = (
            _number("Change(%)")
            if "Change(%)" in raw.columns
            else pl.lit(None, dtype=pl.Float64)
        )

        frame = raw.select(
            pl.col("Index Name").str.strip_chars().alias("index"),
            _number("Closing Index Value").alias("close"),
            change.alias("change_pct"),
        ).with_columns(pl.lit(trading_day).alias("date"))

        frame = frame.filter(pl.col("close").is_not_null() & (pl.col("close") > 0))
        if frame.height == 0:
            raise DataUnavailableError(f"index file for {trading_day} had no closes")
        return frame
