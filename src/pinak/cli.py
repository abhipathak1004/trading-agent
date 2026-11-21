"""Pinak CLI."""

from __future__ import annotations

import sys
from datetime import date

import typer

from pinak import __version__
from pinak.agents.regime import RegimeAgent
from pinak.agents.regime import write_report as write_regime_report
from pinak.agents.universe import UniverseAgent, write_report
from pinak.core import logging as pinak_logging
from pinak.core.config import ROOT, load_config
from pinak.core.errors import DataUnavailableError
from pinak.data.providers.nse_bhavcopy import BhavcopyError, NseBhavcopyProvider
from pinak.data.providers.nse_indices import NseIndicesProvider

app = typer.Typer(
    help="Pinak — intraday signal desk for Indian cash equities.",
    no_args_is_help=True,
    add_completion=False,
)


def _table(headers: list[str], rows: list[list[str]]) -> str:
    widths = [len(h) for h in headers]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(cell))
    head = "  ".join(h.ljust(widths[i]) for i, h in enumerate(headers))
    rule = "  ".join("-" * w for w in widths)
    body = [
        "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row))
        for row in rows
    ]
    return "\n".join([head, rule, *body])


@app.command()
def doctor() -> None:
    """Check environment and data health."""
    pinak_logging.configure()
    config = load_config()
    cache = ROOT / "data" / "raw" / "nse" / "bhavcopy"
    cache.mkdir(parents=True, exist_ok=True)
    cached = sorted(cache.glob("*.parquet"))

    checks = [
        ("pinak version", __version__),
        ("python", sys.version.split()[0]),
        ("config", "loaded" if config else "defaults"),
        ("data dir", str(ROOT / "data")),
        ("bhavcopy cache", f"{len(cached)} days"),
        ("universe top_n", str(config.universe.top_n)),
        ("min ADV ₹Cr", str(config.universe.min_adv_cr)),
        ("last entry", config.session.last_entry.isoformat()),
    ]
    for name, value in checks:
        typer.echo(f"  {name:<18} {value}")
    typer.echo("\nOK")


@app.command()
def universe(
    as_of: str | None = typer.Option(None, help="YYYY-MM-DD (default: today)"),
    top: int | None = typer.Option(None, help="Override selected top N"),
    show: int = typer.Option(25, help="Rows to print"),
) -> None:
    """A1 — build today's tradable universe from NSE bhavcopy."""
    pinak_logging.configure()
    config = load_config()
    if top is not None:
        config.universe.top_n = top

    provider = NseBhavcopyProvider(ROOT / "data" / "raw" / "nse" / "bhavcopy")
    agent = UniverseAgent(provider, config.universe)

    try:
        result = agent.run(date.fromisoformat(as_of) if as_of else None)
    except BhavcopyError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    parquet_path, markdown_path = write_report(result, ROOT / "reports" / "universe")

    rows = [
        [
            str(row["rank"]),
            row["symbol"],
            f"{row['close']:.2f}",
            f"{row['adv20_cr']:.1f}",
            f"{row['turnover_cr']:.1f}",
            f"{row['atr_pct']:.2f}",
            "—" if row["delivery_pct"] is None else f"{row['delivery_pct']:.1f}",
        ]
        for row in result.selected.head(show).iter_rows(named=True)
    ]

    typer.echo(
        f"\nUniverse as of {result.as_of} — "
        f"{result.frame.height} symbols, {result.tradable.height} tradable, "
        f"{result.selected.height} selected\n"
    )
    typer.echo(_table(["#", "SYMBOL", "CLOSE", "ADV20", "TURNOVER", "ATR%", "DELIV%"], rows))
    typer.echo(f"\nparquet  {parquet_path}")
    typer.echo(f"report   {markdown_path}")


@app.command()
def regime(
    as_of: str | None = typer.Option(None, help="YYYY-MM-DD (default: today)"),
) -> None:
    """A2 — market regime, daily verdict and size multiplier."""
    pinak_logging.configure()
    config = load_config()
    indices = NseIndicesProvider(ROOT / "data" / "raw" / "nse" / "indices")
    bhavcopy = NseBhavcopyProvider(ROOT / "data" / "raw" / "nse" / "bhavcopy")
    agent = RegimeAgent(indices, bhavcopy, config.regime)

    try:
        state = agent.run(date.fromisoformat(as_of) if as_of else None)
    except (DataUnavailableError, ValueError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    json_path, markdown_path = write_regime_report(state, ROOT / "reports" / "regime")

    typer.echo(f"\nRegime {state.as_of} — {state.label.value} / {state.verdict.value}\n")
    typer.echo(f"  size             {state.size_multiplier:.2f}x")
    typer.echo(f"  max new          {state.max_new_positions}")
    typer.echo(f"  NIFTY 50         {state.nifty_close:.2f}")
    typer.echo(f"  SMA50 / SMA200   {state.sma_fast:.2f} / {state.sma_slow:.2f}")
    typer.echo(
        f"  vol 20d / 120d   {state.realized_vol:.1f}% / {state.baseline_vol:.1f}% "
        f"({state.vol_ratio:.2f}x)"
    )
    typer.echo(f"  India VIX        {'—' if state.vix is None else f'{state.vix:.2f}'}")
    typer.echo(f"  breadth          {state.breadth:.0%} advancing")
    typer.echo(f"  flags            {state.caution} caution / {state.favorable} favourable")

    typer.echo("\n  reasons")
    for reason in state.reasons:
        typer.echo(f"    · {reason}")

    typer.echo(f"\njson     {json_path}")
    typer.echo(f"report   {markdown_path}")


if __name__ == "__main__":
    app()
