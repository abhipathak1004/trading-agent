"""Code-owned job handlers. No model calls and no broker access."""

from __future__ import annotations

from pathlib import Path

from bq.analysis.wiring import build_analysis_router
from bq.infra.job_worker import JobHandler
from bq.infra.jobs import JobRun
from bq.memory import EpisodicMemoryStore, OutcomeResolver
from bq.settings import SettingsStore

MEMORY_RESOLVE_JOB = "memory.resolve"
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

    return {MEMORY_RESOLVE_JOB: resolve_memory}


__all__ = [
    "DEFAULT_RESOLVE_HORIZON_DAYS",
    "DEFAULT_RESOLVE_MAX_EPISODES",
    "MEMORY_RESOLVE_JOB",
    "build_job_handlers",
]
