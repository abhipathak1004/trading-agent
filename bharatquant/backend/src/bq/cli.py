import argparse
import getpass
import json
import os
import platform
import sqlite3
import sys
import warnings
from dataclasses import asdict
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

import uvicorn

from bq.agent_prompts import PromptStore
from bq.analysis.debate import Debate
from bq.analysis.decision import trade_plan_from_json
from bq.analysis.fundamental import FundamentalEventsAgent
from bq.analysis.news_context import NewsContextAgent
from bq.analysis.pipeline import AnalysisPipeline
from bq.analysis.regime import MarketRegimeAgent
from bq.analysis.risk_team import RiskAnalyst
from bq.analysis.wiring import build_analysis_router
from bq.api import WEB_ROOT, create_app
from bq.auth import OwnerAlreadyConfigured, OwnerAuth
from bq.core import SystemClock
from bq.data.calendar import (
    CalendarSession,
    ExchangeCalendar,
    SessionClass,
    SessionState,
    UncoveredCalendarDate,
)
from bq.data.csv_eod import MAX_BYTES, Adjustment, import_eod_csv
from bq.data.eod_quality import (
    CalendarEodQualityReport,
    EodQualityReport,
    inspect_eod_import,
    inspect_eod_import_with_calendar,
)
from bq.data.freshness import (
    DelayedDataMode,
    freshness_policy_from_seconds,
    inspect_snapshot_freshness,
)
from bq.data.instruments import compare_symbol_sources
from bq.data.records import DataRecordStore, MissingDataImport, MissingDataRecord
from bq.data.symbols import resolve as resolve_symbol
from bq.data.vendors import DataDomain
from bq.data.yahoo import YahooDataClient
from bq.execution.paper_executor import ExecutionContext, PaperExecutor
from bq.infra.database import DatabaseError, check_database, migrate
from bq.infra.model_attempts import ModelAttemptStore
from bq.llm_service import LLMService
from bq.model_router import ModelRouter, RoutedModelCaller
from bq.news import NewsError, NewsStore
from bq.news_agent import NewsResearchAgent
from bq.risk.policy import KillSwitchLevel, RiskLimits
from bq.settings import SettingsStore


def _default_home() -> Path:
    if configured_home := os.environ.get("BQ_HOME"):
        return Path(configured_home).expanduser()
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "BharatQuant"
    raise ValueError("Set BQ_HOME to a local development directory on this platform.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bq")
    actions = parser.add_subparsers(dest="action", required=True)
    actions.add_parser(
        "init", help="Initialize local storage without overwriting data."
    )
    actions.add_parser("doctor", help="Check local storage and platform support.")
    actions.add_parser("serve", help="Run the local development API.")
    auth_actions = actions.add_parser("auth").add_subparsers(
        dest="auth_action", required=True
    )
    auth_actions.add_parser("setup", help="Set the owner password interactively.")
    db_actions = actions.add_parser("db").add_subparsers(
        dest="db_action", required=True
    )
    db_actions.add_parser("migrate", help="Apply pending database migrations.")
    calendar_actions = actions.add_parser("calendar").add_subparsers(
        dest="calendar_action", required=True
    )
    calendar_record = calendar_actions.add_parser(
        "record-session", help="Record an owner-reviewed calendar session."
    )
    calendar_record.add_argument("--exchange-code", required=True)
    calendar_record.add_argument("--date", required=True)
    calendar_record.add_argument(
        "--class",
        dest="session_class",
        required=True,
        choices=[item.value for item in SessionClass],
    )
    calendar_record.add_argument(
        "--state", required=True, choices=[item.value for item in SessionState]
    )
    calendar_record.add_argument("--opens-at")
    calendar_record.add_argument("--closes-at")
    calendar_record.add_argument("--source-url", required=True)
    calendar_record.add_argument("--source-published-at")
    calendar_record.add_argument(
        "--confirm-review",
        action="store_true",
        required=True,
        help="Attest that the source and its applicability were reviewed by the owner.",
    )
    llm_actions = actions.add_parser("llm").add_subparsers(
        dest="llm_action", required=True
    )
    llm_actions.add_parser("usage", help="Show local attempt usage summaries as JSON.")
    llm_ask = llm_actions.add_parser(
        "ask", help="Send one prompt to the configured model and print the reply."
    )
    llm_ask.add_argument("prompt")
    llm_ask.add_argument("--system", default="")
    news_actions = actions.add_parser("news").add_subparsers(
        dest="news_action", required=True
    )
    news_ingest = news_actions.add_parser(
        "ingest", help="Fetch the news feed, store new items and long-term memory."
    )
    news_ingest.add_argument(
        "--source-url",
        default=None,
        help="HTTPS RSS feed URL; defaults to the configured feed (Pulse by Zerodha).",
    )
    news_digest = news_actions.add_parser(
        "digest", help="Print long-term-impact news memories as JSON."
    )
    news_digest.add_argument("--limit", type=int, default=20)
    settings_actions = actions.add_parser("settings").add_subparsers(
        dest="settings_action", required=True
    )
    settings_actions.add_parser(
        "show", help="Print the configured model, feed and API key status as JSON."
    )
    tools_actions = actions.add_parser("tools").add_subparsers(
        dest="tools_action", required=True
    )
    tools_actions.add_parser(
        "list", help="List the news research agent's tools as JSON."
    )
    tools_run = tools_actions.add_parser(
        "run", help="Run one news research agent tool as JSON."
    )
    tools_run.add_argument("name")
    tools_run.add_argument(
        "--args",
        default="{}",
        help="JSON object of tool arguments (default: {}).",
    )
    analyze_action = actions.add_parser(
        "analyze", help="Analyse one symbol and print the structured run as JSON."
    )
    analyze_action.add_argument("symbol")
    analyze_action.add_argument(
        "--timeframe", default="swing", choices=["swing", "intraday"]
    )
    analyze_action.add_argument(
        "--equity-paise", type=int, required=True, help="Account equity in paise."
    )
    analyze_action.add_argument(
        "--cash-paise", type=int, required=True, help="Available cash in paise."
    )
    analyze_action.add_argument("--risk-bps", type=int, default=100)
    paper_actions = actions.add_parser("paper").add_subparsers(
        dest="paper_action", required=True
    )
    paper_execute = paper_actions.add_parser(
        "execute",
        help="Gate a trade-plan JSON through the risk engine and place a paper order.",
    )
    paper_execute.add_argument(
        "plan",
        help="Path to a trade-plan JSON file, or '-' for stdin.",
    )
    paper_execute.add_argument("--account-id", required=True)
    paper_execute.add_argument("--available-cash-paise", type=int, required=True)
    paper_execute.add_argument("--open-positions", type=int, required=True)
    paper_execute.add_argument("--symbol-exposure-paise", type=int, required=True)
    paper_execute.add_argument("--daily-loss-paise", type=int, required=True)
    paper_execute.add_argument("--weekly-loss-paise", type=int, required=True)
    paper_execute.add_argument("--monthly-loss-paise", type=int, required=True)
    paper_execute.add_argument("--orders-this-minute", type=int, default=0)
    paper_execute.add_argument(
        "--session-open",
        choices=["true", "false"],
        required=True,
        help="Whether the request falls inside a known-open trading session.",
    )
    for limit_name in (
        "max-symbol-exposure-paise",
        "max-open-positions",
        "max-daily-loss-paise",
        "max-weekly-loss-paise",
        "max-monthly-loss-paise",
        "max-order-rate-per-minute",
    ):
        paper_execute.add_argument(f"--{limit_name}", type=int, required=True)
    data_actions = actions.add_parser("data").add_subparsers(
        dest="data_action", required=True
    )
    data_actions.add_parser(
        "vendors", help="Print the resolved market-data vendor chains as JSON."
    )
    download_action = data_actions.add_parser(
        "download", help="Download live EOD bars from Yahoo Finance."
    )
    download_action.add_argument("symbol")
    download_action.add_argument("--range", default="1mo")
    download_action.add_argument("--interval", default="1d")
    csv_action = data_actions.add_parser(
        "import-eod-csv", help="Import owner-authorized EOD data from a local CSV."
    )
    csv_action.add_argument("file", type=Path)
    csv_action.add_argument("--import-key", required=True)
    csv_action.add_argument("--source-id", required=True)
    csv_action.add_argument("--source-url", required=True)
    csv_action.add_argument("--review-url", required=True)
    csv_action.add_argument("--reviewed-at", required=True)
    csv_action.add_argument("--currency", required=True)
    csv_action.add_argument(
        "--adjustment", required=True, choices=[item.value for item in Adjustment]
    )
    csv_action.add_argument(
        "--confirm-terms-review",
        required=True,
        action="store_true",
        help="Attest that the source terms were reviewed and this use is permitted.",
    )
    quality_action = data_actions.add_parser(
        "inspect-eod", help="Inspect one CSV import against expected dates."
    )
    quality_action.add_argument("import_id")
    quality_action.add_argument("--source-id", required=True)
    quality_action.add_argument("--instrument-id", required=True)
    quality_action.add_argument(
        "--adjustment", required=True, choices=[item.value for item in Adjustment]
    )
    date_source = quality_action.add_mutually_exclusive_group(required=True)
    date_source.add_argument(
        "--date",
        dest="expected_dates",
        action="append",
        help="Expected YYYY-MM-DD date, repeated in ascending order.",
    )
    date_source.add_argument(
        "--calendar-exchange", help="Exchange code for calendar-derived dates."
    )
    quality_action.add_argument(
        "--calendar-start", help="First calendar date, inclusive."
    )
    quality_action.add_argument("--calendar-end", help="Last calendar date, inclusive.")
    quality_action.add_argument(
        "--assume-open-sessions-expected",
        action="store_true",
        help="Attest that every open session in the range should have an instrument bar.",
    )
    quality_action.add_argument("--as-of", required=True)
    quality_action.add_argument("--max-change-bps", required=True, type=int)
    symbol_action = data_actions.add_parser(
        "inspect-symbol", help="Compare selected unassessed symbol sources."
    )
    symbol_action.add_argument("--exchange-code", required=True)
    symbol_action.add_argument("--symbol", required=True)
    symbol_action.add_argument("--effective-on", required=True)
    symbol_action.add_argument("--as-of", required=True)
    symbol_action.add_argument(
        "--source-id",
        required=True,
        action="append",
        dest="source_ids",
        help="Source identifier; repeat for each source to compare.",
    )
    freshness_action = data_actions.add_parser(
        "inspect-snapshot", help="Inspect a retained snapshot's asserted freshness."
    )
    freshness_action.add_argument("record_id")
    freshness_action.add_argument("--as-of", required=True)
    freshness_action.add_argument("--delayed-after-seconds", required=True, type=int)
    freshness_action.add_argument("--critical-after-seconds", required=True, type=int)
    freshness_action.add_argument(
        "--delayed-mode",
        required=True,
        choices=[item.value for item in DelayedDataMode],
    )
    arguments = list(sys.argv[1:] if argv is None else argv)
    # Opaque IDs are URL-safe and may begin with "-", which argparse would read
    # as an option. Parse a safe placeholder, then restore the real id, so
    # options after the positional still parse normally.
    _ID_POSITIONALS = {"inspect-eod": "import_id", "inspect-snapshot": "record_id"}
    dashed_id: tuple[str, str] | None = None
    if (
        len(arguments) >= 3
        and arguments[0] == "data"
        and arguments[1] in _ID_POSITIONALS
        and arguments[2].startswith("-")
        and arguments[2] != "--"
    ):
        dashed_id = (_ID_POSITIONALS[arguments[1]], arguments[2])
        arguments[2] = "_pending_id"
    args = parser.parse_args(arguments)
    if dashed_id is not None:
        setattr(args, dashed_id[0], dashed_id[1])

    try:
        home = _default_home()
        path = home / "bq.db"
        if args.action == "init":
            home.mkdir(mode=0o700, parents=True, exist_ok=True)
            count = migrate(path, create=True)
            print(f"Initialized {path} ({count} migration(s) applied).")
        elif args.action == "db":
            count = migrate(path)
            print(f"Applied {count} migration(s).")
        else:
            check_database(path)
            if args.action == "doctor":
                print("SQLite migration history, WAL and integrity: OK")
                if (sys.platform, platform.machine()) != ("darwin", "arm64"):
                    print(
                        "Development host only; Apple Silicon macOS target not verified."
                    )
                print(
                    "Market data: Yahoo (delayed) available; Kite read-only when "
                    "enabled. Execution: paper only — no live broker."
                )
            elif args.action == "auth":
                auth = OwnerAuth(path)
                if auth.is_configured():
                    raise OwnerAlreadyConfigured(
                        "Owner password is already configured."
                    )
                with warnings.catch_warnings():
                    warnings.simplefilter("error", getpass.GetPassWarning)
                    password = getpass.getpass("Owner password: ")
                    confirmation = getpass.getpass("Confirm owner password: ")
                if password != confirmation:
                    raise ValueError("Passwords do not match.")
                auth.setup(password)
                print("Owner password configured.")
            elif args.action == "calendar":
                source = urlsplit(args.source_url)
                if (
                    source.scheme != "https"
                    or not source.hostname
                    or source.username is not None
                    or source.password is not None
                    or source.query
                    or source.fragment
                ):
                    raise ValueError(
                        "Calendar source URL must be HTTPS without credentials, query or fragment."
                    )
                session_date = date.fromisoformat(args.date)
                if session_date.isoformat() != args.date:
                    raise ValueError("Calendar date must use YYYY-MM-DD.")
                clock = SystemClock()
                reviewed_at = clock.now()
                session = CalendarSession(
                    exchange_code=args.exchange_code,
                    session_date=session_date,
                    session_class=SessionClass(args.session_class),
                    state=SessionState(args.state),
                    opens_at=datetime.fromisoformat(
                        args.opens_at.replace("Z", "+00:00")
                    )
                    if args.opens_at
                    else None,
                    closes_at=datetime.fromisoformat(
                        args.closes_at.replace("Z", "+00:00")
                    )
                    if args.closes_at
                    else None,
                    source_url=args.source_url,
                    source_published_at=datetime.fromisoformat(
                        args.source_published_at.replace("Z", "+00:00")
                    )
                    if args.source_published_at
                    else None,
                    known_at=reviewed_at,
                    reviewed_by="owner",
                    reviewed_at=reviewed_at,
                )
                session_id = ExchangeCalendar(path, clock).record(session)
                print(
                    f"Calendar session {session_id} recorded with owner review; "
                    "the source is not independently verified."
                )
            elif args.action == "data" and args.data_action == "import-eod-csv":
                with args.file.open("rb") as file:
                    content = file.read(MAX_BYTES + 1)
                result = import_eod_csv(
                    DataRecordStore(path),
                    content,
                    import_key=args.import_key,
                    source_id=args.source_id,
                    source_url=args.source_url,
                    review_url=args.review_url,
                    reviewed_at=datetime.fromisoformat(
                        args.reviewed_at.replace("Z", "+00:00")
                    ),
                    currency=args.currency,
                    adjustment=Adjustment(args.adjustment),
                )
                print(
                    f"CSV import {result.import_id} contains "
                    f"{len(result.record_ids)} record(s); quality is UNASSESSED."
                )
            elif args.action == "data" and args.data_action == "inspect-symbol":
                comparison = compare_symbol_sources(
                    DataRecordStore(path),
                    exchange_code=args.exchange_code,
                    symbol=args.symbol,
                    effective_on=date.fromisoformat(args.effective_on),
                    as_of=datetime.fromisoformat(args.as_of.replace("Z", "+00:00")),
                    source_ids=tuple(args.source_ids),
                )
                print(
                    json.dumps(
                        asdict(comparison),
                        default=lambda value: value.isoformat(),
                        sort_keys=True,
                    )
                )
            elif args.action == "data" and args.data_action == "inspect-snapshot":
                as_of = datetime.fromisoformat(args.as_of.replace("Z", "+00:00"))
                policy = freshness_policy_from_seconds(
                    args.delayed_after_seconds,
                    args.critical_after_seconds,
                    DelayedDataMode(args.delayed_mode),
                )
                version = DataRecordStore(path).version_for(args.record_id, as_of=as_of)
                freshness = inspect_snapshot_freshness(
                    version, as_of=as_of, policy=policy
                )
                print(
                    json.dumps(
                        {
                            "record_id": version.record_id,
                            "source_id": version.record.source_id,
                            "quality_status": version.record.quality_status.value,
                            "as_of": as_of.isoformat(),
                            "status": freshness.status.value,
                            "age_microseconds": (
                                str(freshness.age // timedelta(microseconds=1))
                                if freshness.age is not None
                                else None
                            ),
                            "delayed_after_seconds": args.delayed_after_seconds,
                            "critical_after_seconds": args.critical_after_seconds,
                            "delayed_mode": policy.delayed_mode.value,
                        },
                        sort_keys=True,
                    )
                )
            elif args.action == "data" and args.data_action == "download":
                result = YahooDataClient(path).download(
                    args.symbol, range_=args.range, interval=args.interval
                )
                print(
                    json.dumps(
                        {
                            "import_id": result.import_id,
                            "symbol": result.symbol,
                            "record_count": result.record_count,
                        },
                        sort_keys=True,
                    )
                )
            elif args.action == "data" and args.data_action == "vendors":
                config = SettingsStore(path).load()
                router = build_analysis_router(path, config)
                print(
                    json.dumps(
                        {
                            "kite_enabled": config.kite_enabled,
                            "chains": {
                                domain.value: [
                                    vendor.name for vendor in router.chain(domain)
                                ]
                                for domain in DataDomain
                            },
                        },
                        sort_keys=True,
                    )
                )
            elif args.action == "data":
                as_of = datetime.fromisoformat(args.as_of.replace("Z", "+00:00"))
                store = DataRecordStore(path)
                report: CalendarEodQualityReport | EodQualityReport
                if args.calendar_exchange is not None:
                    if not (
                        args.calendar_start
                        and args.calendar_end
                        and args.assume_open_sessions_expected
                    ):
                        raise ValueError(
                            "Calendar inspection requires start, end and --assume-open-sessions-expected."
                        )
                    report = inspect_eod_import_with_calendar(
                        store,
                        ExchangeCalendar(path),
                        args.import_id,
                        exchange_code=args.calendar_exchange,
                        start=date.fromisoformat(args.calendar_start),
                        end=date.fromisoformat(args.calendar_end),
                        source_id=args.source_id,
                        instrument_id=args.instrument_id,
                        adjustment=Adjustment(args.adjustment),
                        as_of=as_of,
                        max_change_basis_points=args.max_change_bps,
                    )
                else:
                    if (
                        args.calendar_start
                        or args.calendar_end
                        or args.assume_open_sessions_expected
                    ):
                        raise ValueError(
                            "Calendar options require --calendar-exchange."
                        )
                    report = inspect_eod_import(
                        store,
                        args.import_id,
                        expected_dates=tuple(
                            date.fromisoformat(item) for item in args.expected_dates
                        ),
                        source_id=args.source_id,
                        instrument_id=args.instrument_id,
                        adjustment=Adjustment(args.adjustment),
                        as_of=as_of,
                        max_change_basis_points=args.max_change_bps,
                    )
                print(
                    json.dumps(
                        asdict(report),
                        default=lambda value: value.isoformat(),
                        sort_keys=True,
                    )
                )
            elif args.action == "llm" and args.llm_action == "ask":
                result = LLMService(SettingsStore(path)).complete(
                    prompt=args.prompt, system=args.system
                )
                print(
                    json.dumps(
                        {
                            "provider": result.provider.value,
                            "model": result.model,
                            "text": result.text,
                            "reasoning": result.reasoning,
                            "finish_reason": result.finish_reason,
                            "truncated": result.result.truncated,
                            "input_tokens": result.input_tokens,
                            "output_tokens": result.output_tokens,
                        },
                        sort_keys=True,
                    )
                )
            elif args.action == "llm":
                summaries = ModelAttemptStore(path).usage()
                print(
                    json.dumps(
                        {"tasks": [asdict(item) for item in summaries]}, sort_keys=True
                    )
                )
            elif args.action == "news" and args.news_action == "ingest":
                config = SettingsStore(path).load()
                feed_url = args.source_url or config.news_feed_url
                report = NewsResearchAgent(path, feed_url=feed_url).run(
                    memory_ttl_days=config.news_memory_ttl_days
                )
                print(
                    json.dumps(
                        {
                            "source_id": report.source_id,
                            "fetched": report.fetched,
                            "stored": report.stored,
                            "duplicates": report.duplicates,
                            "long_term_memories": report.long_term_memories,
                        },
                        sort_keys=True,
                    )
                )
            elif args.action == "news" and args.news_action == "digest":
                memories = NewsResearchAgent(path).digest(limit=args.limit)
                print(
                    json.dumps(
                        [
                            {
                                "memory_id": item.memory_id,
                                "category": item.category.value,
                                "title": item.title,
                                "body": item.body,
                                "known_at": item.known_at.isoformat(),
                            }
                            for item in memories
                        ],
                        sort_keys=True,
                    )
                )
            elif args.action == "settings":
                config = SettingsStore(path).load()
                print(
                    json.dumps(
                        {
                            "provider": config.provider.value,
                            "model_name": config.model_name,
                            "news_feed_url": config.news_feed_url,
                            "news_memory_ttl_days": config.news_memory_ttl_days,
                            "model_route_configured": config.model_route_configured,
                            "api_keys": [
                                {
                                    "provider": key.provider.value,
                                    "configured": key.configured,
                                    "masked": key.masked,
                                }
                                for key in config.api_keys
                            ],
                            "tiers": {
                                name.value: {
                                    "provider": (
                                        None
                                        if tier.provider is None
                                        else tier.provider.value
                                    ),
                                    "model": tier.model,
                                    "temperature": tier.temperature,
                                    "max_tokens": tier.max_tokens,
                                    "timeout_ms": tier.timeout_ms,
                                    "max_attempts": tier.max_attempts,
                                    "fallback_tier": (
                                        None
                                        if tier.fallback_tier is None
                                        else tier.fallback_tier.value
                                    ),
                                    "configured": config.tier_configured(name),
                                }
                                for name, tier in config.tiers
                            },
                            "agent_routing": {
                                agent_id: {
                                    "tier": route.tier.value,
                                    "provider": (
                                        None
                                        if route.provider is None
                                        else route.provider.value
                                    ),
                                    "model": route.model,
                                }
                                for agent_id, route in config.agent_routing
                            },
                            "decision_policy": {
                                "min_choice_confidence": config.decision_policy.min_choice_confidence,
                                "min_noul_probability": config.decision_policy.min_noul_probability,
                                "abstain_score_band": list(
                                    config.decision_policy.abstain_score_band
                                ),
                                "llm_fallback_size_scale": config.decision_policy.llm_fallback_size_scale,
                                "heuristic_size_scale": config.decision_policy.heuristic_size_scale,
                            },
                            "pipeline": {
                                "debate_rounds": config.pipeline.debate_rounds,
                                "max_tool_rounds_per_agent": config.pipeline.max_tool_rounds_per_agent,
                                "screener_min_score": config.pipeline.screener_min_score,
                                "final_decision_min_prob": config.pipeline.final_decision_min_prob,
                                "execution_mode": config.pipeline.execution_mode,
                                "decision_fallback_order": [
                                    item.value
                                    for item in config.pipeline.decision_fallback_order
                                ],
                            },
                            "decision_route": config.decision_route.value,
                            "configured_tiers": [
                                name.value for name in config.configured_tiers
                            ],
                            "decision_preview": config.decision_preview,
                        },
                        sort_keys=True,
                    )
                )
            elif args.action == "tools" and args.tools_action == "list":
                runner = NewsResearchAgent(path).runner()
                print(
                    json.dumps(
                        {
                            "agent_id": "news-research",
                            "read_only": not runner.policy.allows_mutation,
                            "tools": [
                                {
                                    "name": spec.name,
                                    "summary": spec.summary,
                                    "mutating": spec.mutating,
                                    "arguments": [a.name for a in spec.arguments],
                                }
                                for spec in runner.permitted_specs()
                            ],
                        },
                        sort_keys=True,
                    )
                )
            elif args.action == "tools" and args.tools_action == "run":
                try:
                    tool_args = json.loads(args.args)
                except json.JSONDecodeError as exc:
                    raise ValueError("--args must be a JSON object.") from exc
                if not isinstance(tool_args, dict):
                    raise ValueError("--args must be a JSON object.")
                config = SettingsStore(path).load()
                agent = NewsResearchAgent(path, feed_url=config.news_feed_url)
                result = agent.runner().run(args.name, tool_args)
                print(json.dumps(result, sort_keys=True, default=str))
            elif args.action == "analyze":
                settings_store = SettingsStore(path)
                config = settings_store.load()
                router = build_analysis_router(path, config)
                prompts = PromptStore(path)
                news_store = NewsStore(path)
                if config.any_tier_configured:
                    caller = RoutedModelCaller(
                        ModelRouter(settings_store, prompts=prompts)
                    )
                    pipeline = AnalysisPipeline(
                        router,
                        debate=Debate(caller, prompts=prompts),
                        risk_analyst=RiskAnalyst(caller, prompts=prompts),
                        routing=caller,
                        news=NewsContextAgent(news_store),
                        regime=MarketRegimeAgent(router),
                        fundamental=FundamentalEventsAgent(news_store),
                    )
                else:
                    service = (
                        LLMService(settings_store)
                        if config.model_route_configured
                        else None
                    )
                    pipeline = AnalysisPipeline(
                        router,
                        debate=(
                            Debate(service, prompts=prompts)
                            if service is not None
                            else None
                        ),
                        risk_analyst=(
                            RiskAnalyst(service, prompts=prompts)
                            if service is not None
                            else None
                        ),
                        news=NewsContextAgent(news_store),
                        regime=MarketRegimeAgent(router),
                        fundamental=FundamentalEventsAgent(news_store),
                    )
                analysis = pipeline.run(
                    symbol=resolve_symbol(args.symbol),
                    timeframe=args.timeframe,
                    equity_paise=args.equity_paise,
                    available_cash_paise=args.cash_paise,
                    risk_basis_points=args.risk_bps,
                )
                print(json.dumps(analysis.to_dict(), sort_keys=True, default=str))
            elif args.action == "paper" and args.paper_action == "execute":
                raw = (
                    sys.stdin.read()
                    if args.plan == "-"
                    else Path(args.plan).read_text("utf-8")
                )
                plan = trade_plan_from_json(json.loads(raw))
                limits = RiskLimits(
                    max_symbol_exposure_paise=args.max_symbol_exposure_paise,
                    max_open_positions=args.max_open_positions,
                    max_daily_loss_paise=args.max_daily_loss_paise,
                    max_weekly_loss_paise=args.max_weekly_loss_paise,
                    max_monthly_loss_paise=args.max_monthly_loss_paise,
                    max_order_rate_per_minute=args.max_order_rate_per_minute,
                )
                executor = PaperExecutor(path, limits=limits)
                execution = executor.execute(
                    plan,
                    context=ExecutionContext(
                        account_id=args.account_id,
                        available_cash_paise=args.available_cash_paise,
                        open_positions=args.open_positions,
                        symbol_exposure_paise=args.symbol_exposure_paise,
                        daily_loss_paise=args.daily_loss_paise,
                        weekly_loss_paise=args.weekly_loss_paise,
                        monthly_loss_paise=args.monthly_loss_paise,
                        orders_this_minute=args.orders_this_minute,
                        kill_switch=KillSwitchLevel.NONE,
                        instrument_valid=True,
                        session_open=args.session_open == "true",
                    ),
                    # A deterministic id makes re-running the same plan
                    # idempotent. The paper engine allows only
                    # [A-Za-z0-9._:-], so the timestamp's "+offset" is
                    # rendered as a trailing Z instead.
                    client_order_id=(
                        f"paper-{plan.symbol}-{plan.as_of.strftime('%Y%m%dT%H%M%SZ')}"
                    ),
                )
                print(
                    json.dumps(
                        {
                            "state": execution.decision.state.value,
                            "vetoes": list(execution.decision.vetoes),
                            "data_freshness": execution.freshness.label,
                            "order": (
                                None
                                if execution.order is None
                                else {
                                    "order_id": execution.order.order_id,
                                    "status": execution.order.status.value,
                                    "quantity": execution.order.quantity,
                                    "limit_price_paise": execution.order.limit_price_paise,
                                }
                            ),
                        },
                        sort_keys=True,
                    )
                )
            else:
                if not (WEB_ROOT / "index.html").is_file():
                    raise ValueError(
                        f"Frontend build missing at {WEB_ROOT}; run "
                        "'npm ci && npm run build' from the web directory."
                    )
                uvicorn.run(
                    create_app(database_path=path),
                    host="127.0.0.1",
                    port=8000,
                    proxy_headers=False,
                )
    except (
        DatabaseError,
        MissingDataImport,
        MissingDataRecord,
        NewsError,
        OSError,
        OwnerAlreadyConfigured,
        sqlite3.Error,
        UncoveredCalendarDate,
        ValueError,
    ) as exc:
        print(f"bq: {exc}", file=sys.stderr)
        return 1
    except (EOFError, KeyboardInterrupt):
        print("\nbq: Password setup cancelled.", file=sys.stderr)
        return 1
    except getpass.GetPassWarning:
        print(
            "bq: Password setup requires a terminal with echo disabled.",
            file=sys.stderr,
        )
        return 1
    return 0
