"""Code-owned scheduler catalogue.

A job is only advertised here when its handler is actually registered. Default
times are starting points, not hard-coded policy; the owner can edit them.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class JobDefinition:
    job_id: str
    label: str
    description: str
    resource_class: str
    timezone: str
    local_time: str
    window_start: str
    window_end: str
    enabled_by_default: bool = False
    max_attempts: int = 2
    wake_policy: str = "ALWAYS_RUN"


JOB_CATALOGUE: tuple[JobDefinition, ...] = (
    JobDefinition(
        job_id="memory.resolve",
        label="Resolve memory outcomes",
        description="Resolve due shadow episodes and refresh Brier calibration.",
        resource_class="LIGHT",
        timezone="Asia/Kolkata",
        local_time="18:00",
        window_start="17:00",
        window_end="19:00",
        max_attempts=2,
    ),
    JobDefinition(
        job_id="news.ingest",
        label="Ingest news",
        description="Fetch the configured news feed, deduplicate and update memory.",
        resource_class="LIGHT",
        timezone="Asia/Kolkata",
        local_time="07:45",
        window_start="07:30",
        window_end="09:00",
        max_attempts=3,
    ),
    JobDefinition(
        job_id="campaign.research",
        label="Build today's campaign",
        description=(
            "Extract news catalysts, shortlist 5-10 names and freeze their "
            "deterministic trade plans and breakout trigger levels."
        ),
        resource_class="LIGHT",
        timezone="Asia/Kolkata",
        local_time="08:05",
        window_start="07:50",
        window_end="09:10",
        max_attempts=2,
    ),
    JobDefinition(
        job_id="campaign.triggers",
        label="Scan campaign triggers",
        description=(
            "Every 5 minutes from 09:15: fill a triggered breakout through the "
            "paper risk gate and manage campaign stop/target exits."
        ),
        resource_class="LIGHT",
        timezone="Asia/Kolkata",
        local_time="09:15",
        window_start="09:15",
        window_end="15:15",
        max_attempts=1,
    ),
    JobDefinition(
        job_id="report.morning",
        label="Morning report",
        description="Freeze the day's predictions and save the morning research report.",
        resource_class="LIGHT",
        timezone="Asia/Kolkata",
        local_time="08:00",
        window_start="07:45",
        window_end="09:00",
        max_attempts=2,
    ),
    JobDefinition(
        job_id="report.evening_accuracy",
        label="Evening accuracy report",
        description="Resolve due predictions and save the accuracy report.",
        resource_class="LIGHT",
        timezone="Asia/Kolkata",
        local_time="17:00",
        window_start="16:45",
        window_end="17:30",
        max_attempts=2,
    ),
    JobDefinition(
        job_id="paper.stops",
        label="Evaluate paper stops",
        description="Trigger active server-side stop/target exits from daily bars.",
        resource_class="LIGHT",
        timezone="Asia/Kolkata",
        local_time="15:20",
        window_start="09:20",
        window_end="15:25",
        max_attempts=3,
    ),
    JobDefinition(
        job_id="paper.square_off",
        label="Square off MIS",
        description="Close every open MIS paper position at the daily close.",
        resource_class="LIGHT",
        timezone="Asia/Kolkata",
        local_time="15:25",
        window_start="15:10",
        window_end="15:35",
        max_attempts=2,
    ),
    JobDefinition(
        job_id="aihf.fundamentals.ingest",
        label="Ingest fund fundamentals",
        description=(
            "Read quarterly fundamentals, company facts and earnings surprises "
            "for the AI Hedge Fund's traded names from Yahoo, and store them "
            "point-in-time. Disabled until the owner picks a universe, because "
            "it makes one request per symbol."
        ),
        resource_class="LIGHT",
        timezone="Asia/Kolkata",
        local_time="18:30",
        window_start="18:00",
        window_end="20:00",
        enabled_by_default=False,
        max_attempts=2,
    ),
    JobDefinition(
        job_id="logs.flush",
        label="Flush operational logs",
        description=(
            "Move buffered LLM-call and decision logs into dated files and prune "
            "files past the configured retention window."
        ),
        resource_class="LIGHT",
        timezone="Asia/Kolkata",
        local_time="00:00",
        window_start="00:00",
        window_end="23:59",
        enabled_by_default=True,
        max_attempts=2,
    ),
)

CATALOGUE_BY_ID = {item.job_id: item for item in JOB_CATALOGUE}
AVAILABLE_JOBS = frozenset(CATALOGUE_BY_ID)


__all__ = [
    "AVAILABLE_JOBS",
    "CATALOGUE_BY_ID",
    "JOB_CATALOGUE",
    "JobDefinition",
]
