"""NSE security-wise bhavcopy provider.

Downloads the daily ``sec_bhavdata_full_DDMMYYYY.csv`` file, normalises it to a
typed Polars frame, and caches each day as Parquet so a backfill only ever hits
the network once.
"""

from __future__ import annotations

import io
from datetime import date
from pathlib import Path

import httpx
import polars as pl
import structlog

from pinak.core.errors import DataUnavailableError

log = structlog.get_logger(__name__)

_URLS = (
    "https://nsearchives.nseindia.com/products/content/sec_bhavdata_full_{stamp}.csv",
    "https://archives.nseindia.com/products/content/sec_bhavdata_full_{stamp}.csv",
)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
    ),
    "Accept": "text/csv,application/csv,*/*",
    "Accept-Language": "en-US,en;q=0.9",
}

_REQUIRED = {
    "SYMBOL",
    "SERIES",
    "OPEN_PRICE",
    "HIGH_PRICE",
    "LOW_PRICE",
    "CLOSE_PRICE",
    "TTL_TRD_QNTY",
}


def _number(column: str) -> pl.Expr:
    """NSE pads numeric fields with spaces; strip before casting."""
    return pl.col(column).str.strip_chars().cast(pl.Float64, strict=False)


class BhavcopyError(DataUnavailableError):
    """Raised when a day's bhavcopy cannot be obtained or parsed."""


class NseBhavcopyProvider:
    """Daily EQ bhavcopy with a Parquet cache keyed by trading date."""

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

    def available(self, trading_day: date) -> bool:
        return self.path_for(trading_day).exists()

    def fetch(self, trading_day: date) -> pl.DataFrame:
        cached = self.path_for(trading_day)
        if cached.exists():
            return pl.read_parquet(cached)
        if trading_day.weekday() >= 5:
            raise BhavcopyError(f"{trading_day} is a weekend")
        frame = self._download(trading_day)
        frame.write_parquet(cached)
        log.info("bhavcopy.cached", day=str(trading_day), rows=frame.height)
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
                except httpx.HTTPError as exc:  # network-level failure
                    last_error = f"{type(exc).__name__}: {exc}"
                    continue
                if response.status_code == 200 and response.content:
                    return self._parse(response.content, trading_day)
                last_error = f"HTTP {response.status_code}"
            raise BhavcopyError(f"no bhavcopy for {trading_day} ({last_error})")
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
        raw.columns = [column.strip().upper() for column in raw.columns]
        missing = _REQUIRED - set(raw.columns)
        if missing:
            raise BhavcopyError(f"unexpected bhavcopy schema, missing {sorted(missing)}")

        delivery = (
            _number("DELIV_PER") if "DELIV_PER" in raw.columns else pl.lit(None, dtype=pl.Float64)
        )

        # NSE has shipped turnover in both rupees (TURNOVER_LTS) and lakhs
        # (TURNOVER_LACS). Normalise everything to rupees.
        if "TURNOVER_LTS" in raw.columns:
            turnover = _number("TURNOVER_LTS")
        elif "TURNOVER_LACS" in raw.columns:
            turnover = _number("TURNOVER_LACS") * 1e5
        else:
            raise BhavcopyError("bhavcopy has no turnover column")

        frame = raw.select(
            pl.col("SYMBOL").str.strip_chars().alias("symbol"),
            pl.col("SERIES").str.strip_chars().alias("series"),
            _number("OPEN_PRICE").alias("open"),
            _number("HIGH_PRICE").alias("high"),
            _number("LOW_PRICE").alias("low"),
            _number("CLOSE_PRICE").alias("close"),
            _number("TTL_TRD_QNTY").alias("volume"),
            turnover.alias("turnover"),
            delivery.alias("delivery_pct"),
        ).with_columns(pl.lit(trading_day).alias("date"))

        frame = frame.filter(pl.col("series") == "EQ")
        frame = frame.filter(pl.col("close").is_not_null() & (pl.col("close") > 0))
        if frame.height == 0:
            raise BhavcopyError(f"bhavcopy for {trading_day} contained no EQ rows")
        return frame
