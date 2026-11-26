"""Code-owned job handlers. No model calls and no broker access."""

from __future__ import annotations

from pathlib import Path
from zoneinfo import ZoneInfo

from bq.analysis.wiring import build_analysis_router
from bq.infra.job_worker import JobHandler
from bq.infra.jobs import JobRun
from bq.memory import EpisodicMemoryStore, OutcomeResolver
from bq.news_agent import NewsResearchAgent
from bq.paper_square_off import PaperSquareOff
from bq.paper_stops import PaperStopStore
from bq.reports.engine import ReportEngine
from bq.reports.store import ReportStore
from bq.settings import SettingsStore

MEMORY_RESOLVE_JOB = "memory.resolve"
NEWS_INGEST_JOB = "news.ingest"
REPORT_MORNING_JOB = "report.morning"
REPORT_EVENING_JOB = "report.evening_accuracy"
PAPER_STOPS_JOB = "paper.stops"
PAPER_SQUARE_OFF_JOB = "paper.square_off"
DEFAULT_RESOLVE_HORIZON_DAYS = 5
DEFAULT_RESOLVE_MAX_EPISODES = 50


def build_job_handlers(
    database_path: Path, settings: SettingsStore
) -> dict[str, JobHandler]:
    def resolve_memory(run: JobRun) -> dict[str, object]:
        config = settings.load()
        router = build_analysis_router(database_path, config)
        resolver = OutcomeResolver(
            EpisodicMemoryStore(database_path),
            router,
            horizon_days=DEFAULT_RESOLVE_HORIZON_DAYS,
            max_episodes=DEFAULT_RESOLVE_MAX_EPISODES,
        )
        return resolver.resolve_due(as_of=run.scheduled_at).to_dict()

    def ingest_news(_run: JobRun) -> dict[str, object]:
        config = settings.load()
        report = NewsResearchAgent(
            database_path,
            feed_url=config.news_feed_url,
        ).run(memory_ttl_days=config.news_memory_ttl_days)
        return {
            "source_id": report.source_id,
            "fetched": report.fetched,
            "stored": report.stored,
            "duplicates": report.duplicates,
            "long_term_memories": report.long_term_memories,
        }

    def record_failure(report_type: str, run: JobRun, exc: Exception) -> None:
        try:
            local_date = run.scheduled_at.astimezone(ZoneInfo("Asia/Kolkata")).date()
            ReportStore(database_path).record(
                report_type=report_type,
                report_date=local_date,
                status="FAILED",
                title=f"{report_type.title()} report failed",
                body_md=(
                    f"# {report_type.title()} report failed\n\n"
                    f"{type(exc).__name__}: report generation did not complete."
                ),
                body={"error": type(exc).__name__, "report_type": report_type},
            )
        except Exception:  # noqa: BLE001 - never hide the original failure
            pass

    def morning_report(run: JobRun) -> dict[str, object]:
        try:
            router = build_analysis_router(database_path, settings.load())
            report = ReportEngine(database_path, router).morning(as_of=run.scheduled_at)
        except Exception as exc:  # noqa: BLE001 - record and let worker mark failure
            record_failure("morning", run, exc)
            raise
        return report.to_dict()

    def evening_report(run: JobRun) -> dict[str, object]:
        try:
            router = build_analysis_router(database_path, settings.load())
            report = ReportEngine(database_path, router).evening(as_of=run.scheduled_at)
        except Exception as exc:  # noqa: BLE001 - record and let worker mark failure
            record_failure("evening", run, exc)
            raise
        return report.to_dict()

    def evaluate_stops(run: JobRun) -> dict[str, object]:
        router = build_analysis_router(database_path, settings.load())
        result = PaperStopStore(database_path).evaluate(router, as_of=run.scheduled_at)
        return {
            "examined": result.examined,
            "triggered": result.triggered,
            "notes": list(result.notes),
        }

    def square_off(run: JobRun) -> dict[str, object]:
        router = build_analysis_router(database_path, settings.load())
        result = PaperSquareOff(database_path).square_off(
            router, as_of=run.scheduled_at
        )
        return {
            "examined": result.examined,
            "squared_off": result.squared_off,
            "notes": list(result.notes),
        }

    return {
        MEMORY_RESOLVE_JOB: resolve_memory,
        NEWS_INGEST_JOB: ingest_news,
        REPORT_MORNING_JOB: morning_report,
        REPORT_EVENING_JOB: evening_report,
        PAPER_STOPS_JOB: evaluate_stops,
        PAPER_SQUARE_OFF_JOB: square_off,
    }


__all__ = [
    "DEFAULT_RESOLVE_HORIZON_DAYS",
    "DEFAULT_RESOLVE_MAX_EPISODES",
    "MEMORY_RESOLVE_JOB",
    "NEWS_INGEST_JOB",
    "PAPER_SQUARE_OFF_JOB",
    "PAPER_STOPS_JOB",
    "REPORT_EVENING_JOB",
    "REPORT_MORNING_JOB",
    "build_job_handlers",
]
