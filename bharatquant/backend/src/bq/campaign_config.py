"""Validated configuration for the daily catalyst campaign.

Kept free of project imports so both :mod:`bq.settings` (storage) and
:mod:`bq.campaign` (orchestration) can use it without an import cycle.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, fields
from datetime import date, time
from urllib.parse import urlsplit

IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_CLOCK = re.compile(r"([01][0-9]|2[0-3]):([0-5][0-9])")

MIN_TOP_N = 5
MAX_TOP_N = 10
MAX_CAMPAIGN_PAISE = 10**13
MAX_SKIP_DATES = 60
MAX_STALENESS_SECONDS = 7200
MIN_STALENESS_SECONDS = 60
MAX_LOOKBACK_HOURS = 168
MAX_CHASE_BPS = 500
MAX_HORIZON_DAYS = 20
TRIGGER_MODES = ("breakout", "pullback")
ANALYSIS_DEPTHS = ("deterministic", "full")
MAX_FEED_URLS = 5
MAX_FEED_URL_CHARS = 4096

PULSE_FEED = "https://pulse.zerodha.com/feed.xml"
ORDER_WIN_FEED = (
    "https://news.google.com/rss/search?q=(%22order%20win%22%20OR%20"
    "%22wins%20order%22%20OR%20%22bags%20order%22%20OR%20%22order%20worth%22)"
    "%20India%20when:7d&hl=en-IN&gl=IN&ceid=IN:en"
)
RESULTS_FEED = (
    "https://news.google.com/rss/search?q=(%22Q2%20results%22%20OR%20"
    "%22net%20profit%20rises%22%20OR%20%22PAT%20jumps%22%20OR%20"
    "%22revenue%20jumps%22)%20India%20when:7d&hl=en-IN&gl=IN&ceid=IN:en"
)
DEFAULT_FEED_URLS = (PULSE_FEED, ORDER_WIN_FEED, RESULTS_FEED)


class CampaignConfigError(ValueError):
    """A campaign setting is malformed or outside its bound."""


def _int(value: object, name: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise CampaignConfigError(f"{name} must be an integer.")
    if not minimum <= value <= maximum:
        raise CampaignConfigError(f"{name} must be between {minimum} and {maximum}.")
    return value


def _clock_value(value: object, name: str) -> str:
    if not isinstance(value, str) or _CLOCK.fullmatch(value) is None:
        raise CampaignConfigError(f"{name} must be HH:MM.")
    hour, minute = (int(part) for part in value.split(":"))
    if not (time(9, 15) <= time(hour, minute) <= time(15, 15)):
        raise CampaignConfigError(f"{name} must be between 09:15 and 15:15.")
    return value


@dataclass(frozen=True, slots=True)
class CampaignConfig:
    enabled: bool = True
    top_n: int = 8
    equity_paise: int = 100_000_000
    available_cash_paise: int = 90_000_000
    risk_basis_points: int = 100
    max_open_positions: int = 4
    trigger_mode: str = "breakout"
    max_chase_bps: int = 50
    valid_until: str = "14:30"
    min_catalyst_strength: int = 3
    min_price_paise: int = 2000
    min_avg_volume: int = 100_000
    analysis_depth: str = "deterministic"
    news_lookback_hours: int = 96
    trigger_staleness_seconds: int = 1800
    feed_urls: tuple[str, ...] = DEFAULT_FEED_URLS
    skip_dates: tuple[str, ...] = ()
    account_id: str = "paper-main"
    horizon_days: int = 5

    def __post_init__(self) -> None:
        _int(self.top_n, "top_n", MIN_TOP_N, MAX_TOP_N)
        _int(self.equity_paise, "equity_paise", 1, MAX_CAMPAIGN_PAISE)
        _int(
            self.available_cash_paise,
            "available_cash_paise",
            0,
            MAX_CAMPAIGN_PAISE,
        )
        if self.available_cash_paise > self.equity_paise:
            raise CampaignConfigError(
                "available_cash_paise cannot exceed equity_paise."
            )
        _int(self.risk_basis_points, "risk_basis_points", 0, 10_000)
        _int(self.max_open_positions, "max_open_positions", 1, 10)
        if self.trigger_mode not in TRIGGER_MODES:
            raise CampaignConfigError(
                f"trigger_mode must be one of {', '.join(TRIGGER_MODES)}."
            )
        _int(self.max_chase_bps, "max_chase_bps", 0, MAX_CHASE_BPS)
        _clock_value(self.valid_until, "valid_until")
        _int(self.min_catalyst_strength, "min_catalyst_strength", 1, 5)
        _int(self.min_price_paise, "min_price_paise", 0, 10**9)
        _int(self.min_avg_volume, "min_avg_volume", 0, 10**9)
        if self.analysis_depth not in ANALYSIS_DEPTHS:
            raise CampaignConfigError(
                f"analysis_depth must be one of {', '.join(ANALYSIS_DEPTHS)}."
            )
        _int(self.news_lookback_hours, "news_lookback_hours", 1, MAX_LOOKBACK_HOURS)
        _int(
            self.trigger_staleness_seconds,
            "trigger_staleness_seconds",
            MIN_STALENESS_SECONDS,
            MAX_STALENESS_SECONDS,
        )
        _int(self.horizon_days, "horizon_days", 1, MAX_HORIZON_DAYS)
        if (
            not isinstance(self.feed_urls, tuple)
            or not 1 <= len(self.feed_urls) <= MAX_FEED_URLS
        ):
            raise CampaignConfigError(
                f"feed_urls must contain 1 to {MAX_FEED_URLS} URLs."
            )
        for url in self.feed_urls:
            if not isinstance(url, str) or len(url) > MAX_FEED_URL_CHARS:
                raise CampaignConfigError("A feed URL is missing or too long.")
            parts = urlsplit(url)
            if parts.scheme != "https" or not parts.hostname:
                raise CampaignConfigError("Feed URLs must be HTTPS.")
            if parts.username or parts.password:
                raise CampaignConfigError("Feed URLs must not carry credentials.")
        if not isinstance(self.skip_dates, tuple):
            raise CampaignConfigError("skip_dates must be a tuple of ISO dates.")
        if len(self.skip_dates) > MAX_SKIP_DATES:
            raise CampaignConfigError(
                f"skip_dates can hold at most {MAX_SKIP_DATES} dates."
            )
        for value in self.skip_dates:
            if not isinstance(value, str):
                raise CampaignConfigError("skip_dates must contain ISO dates.")
            try:
                date.fromisoformat(value)
            except ValueError as exc:
                raise CampaignConfigError(
                    f"skip_dates entry '{value}' is not an ISO date."
                ) from exc
        if (
            not isinstance(self.account_id, str)
            or IDENTIFIER.fullmatch(self.account_id) is None
        ):
            raise CampaignConfigError("account_id is not a valid identifier.")

    @property
    def valid_until_time(self) -> time:
        hour, minute = (int(part) for part in self.valid_until.split(":"))
        return time(hour, minute)

    @property
    def equity(self) -> int:
        return self.equity_paise

    def to_dict(self) -> dict[str, object]:
        return {
            "enabled": self.enabled,
            "top_n": self.top_n,
            "equity_paise": self.equity_paise,
            "available_cash_paise": self.available_cash_paise,
            "risk_basis_points": self.risk_basis_points,
            "max_open_positions": self.max_open_positions,
            "trigger_mode": self.trigger_mode,
            "max_chase_bps": self.max_chase_bps,
            "valid_until": self.valid_until,
            "min_catalyst_strength": self.min_catalyst_strength,
            "min_price_paise": self.min_price_paise,
            "min_avg_volume": self.min_avg_volume,
            "analysis_depth": self.analysis_depth,
            "news_lookback_hours": self.news_lookback_hours,
            "trigger_staleness_seconds": self.trigger_staleness_seconds,
            "feed_urls": list(self.feed_urls),
            "skip_dates": list(self.skip_dates),
            "account_id": self.account_id,
            "horizon_days": self.horizon_days,
        }

    @classmethod
    def from_mapping(cls, value: object) -> CampaignConfig:
        if value is None:
            return cls()
        if not isinstance(value, Mapping):
            raise CampaignConfigError("Campaign settings must be a JSON object.")
        allowed = {field.name for field in fields(cls)}
        unknown = set(value) - allowed
        if unknown:
            raise CampaignConfigError(
                "Unknown campaign setting(s): " + ", ".join(sorted(unknown))
            )
        data = dict(value)
        if "enabled" in data and not isinstance(data["enabled"], bool):
            raise CampaignConfigError("enabled must be a boolean.")
        if "feed_urls" in data:
            raw = data["feed_urls"]
            if not isinstance(raw, (list, tuple)):
                raise CampaignConfigError("feed_urls must be a list.")
            data["feed_urls"] = tuple(raw)
        if "skip_dates" in data:
            raw = data["skip_dates"]
            if not isinstance(raw, (list, tuple)):
                raise CampaignConfigError("skip_dates must be a list.")
            data["skip_dates"] = tuple(raw)
        return cls(**data)


__all__ = [
    "ANALYSIS_DEPTHS",
    "DEFAULT_FEED_URLS",
    "MAX_FEED_URLS",
    "CampaignConfig",
    "CampaignConfigError",
    "MAX_TOP_N",
    "MIN_TOP_N",
    "TRIGGER_MODES",
]
