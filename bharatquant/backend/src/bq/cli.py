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
from bq.infra.database import DatabaseError, check_database, migrate
from bq.infra.model_attempts import ModelAttemptStore
from bq.news import PULSE_FEED_URL, NewsError
from bq.news_agent import NewsResearchAgent


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
    news_actions = actions.add_parser("news").add_subparsers(
        dest="news_action", required=True
    )
    news_ingest = news_actions.add_parser(
        "ingest", help="Fetch the news feed, store new items and long-term memory."
    )
    news_ingest.add_argument(
        "--source-url",
        default=PULSE_FEED_URL,
        help="HTTPS RSS feed URL (default: Pulse by Zerodha).",
    )
    news_digest = news_actions.add_parser(
        "digest", help="Print long-term-impact news memories as JSON."
    )
    news_digest.add_argument("--limit", type=int, default=20)
    data_actions = actions.add_parser("data").add_subparsers(
        dest="data_action", required=True
    )
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
    args = parser.parse_args(argv)

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
                print("Live market data, LLMs and paper trading are not implemented.")
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
            elif args.action == "llm":
                summaries = ModelAttemptStore(path).usage()
                print(
                    json.dumps(
                        {"tasks": [asdict(item) for item in summaries]}, sort_keys=True
                    )
                )
            elif args.action == "news" and args.news_action == "ingest":
                report = NewsResearchAgent(path, feed_url=args.source_url).run()
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
