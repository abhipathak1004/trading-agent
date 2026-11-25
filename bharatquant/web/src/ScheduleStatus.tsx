import { useEffect, useState } from "react";

import { parseInterval, parseWeekdays } from "./scheduleContract";

type Schedule = {
  schedule_id: string;
  job_id: string;
  enabled: boolean;
  timezone: string;
  local_time: string;
  interval_minutes: number | null;
  window_end: string | null;
  weekdays: number[];
  preview_state: "available" | "disabled" | "pending_change" | "unavailable";
  next_occurrences: string[];
};

type WakeDecision = "RUN" | "SKIPPED_BY_POLICY" | "WAKE_WINDOW_EXPIRED" | "OUTSIDE_OPERATING_WINDOW";
type Wake = {
  schedule_id: string;
  scheduled_at: string;
  observed_at: string;
  schedule_version: number;
  decision: WakeDecision;
};

type Summary = {
  latest_version: number;
  observed_at: string;
  scheduler_running: boolean;
  schedules: Schedule[];
  decision_count: number;
  recent_decisions: Wake[];
} & (
  | { effective_state: "available"; effective_version: number; effective_at: string; schedule_count: number }
  | { effective_state: "not_configured" | "unknown"; effective_version: null; effective_at: null; schedule_count: null }
);

type Status = { state: "loading" | "error" } | { state: "ready"; summary: Summary };

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function isCount(value: unknown): value is number {
  return typeof value === "number" && Number.isSafeInteger(value) && value >= 0;
}

function isTimestamp(value: unknown): value is string {
  return typeof value === "string" && Number.isFinite(Date.parse(value));
}

function parseSchedule(value: unknown): Schedule {
  if (
    !isRecord(value) ||
    typeof value.schedule_id !== "string" || value.schedule_id.length === 0 ||
    typeof value.job_id !== "string" || value.job_id.length === 0 ||
    typeof value.enabled !== "boolean" ||
    typeof value.timezone !== "string" || value.timezone.length === 0 ||
    typeof value.local_time !== "string" || !/^(?:[01]\d|2[0-3]):[0-5]\d$/.test(value.local_time) ||
    (value.preview_state !== "available" && value.preview_state !== "disabled" &&
      value.preview_state !== "pending_change" && value.preview_state !== "unavailable") ||
    !Array.isArray(value.next_occurrences) || !value.next_occurrences.every(isTimestamp) ||
    (value.preview_state === "available" ? value.next_occurrences.length !== 5 : value.next_occurrences.length !== 0) ||
    (value.preview_state === "disabled") !== !value.enabled
  ) {
    throw new Error("Invalid schedule");
  }
  return {
    schedule_id: value.schedule_id, job_id: value.job_id, enabled: value.enabled,
    timezone: value.timezone, local_time: value.local_time,
    ...parseInterval(value),
    weekdays: parseWeekdays(value.weekdays),
    preview_state: value.preview_state, next_occurrences: value.next_occurrences,
  };
}

function parseWake(value: unknown): Wake {
  if (
    !isRecord(value) ||
    typeof value.schedule_id !== "string" || value.schedule_id.length === 0 ||
    !isTimestamp(value.scheduled_at) || !isTimestamp(value.observed_at) ||
    !isCount(value.schedule_version) || value.schedule_version === 0 ||
    (value.decision !== "RUN" && value.decision !== "SKIPPED_BY_POLICY" &&
      value.decision !== "WAKE_WINDOW_EXPIRED" && value.decision !== "OUTSIDE_OPERATING_WINDOW")
  ) {
    throw new Error("Invalid wake decision");
  }
  return {
    schedule_id: value.schedule_id, scheduled_at: value.scheduled_at, observed_at: value.observed_at,
    schedule_version: value.schedule_version, decision: value.decision,
  };
}

function parseSummary(value: unknown): Summary {
  if (
    !isRecord(value) || !isCount(value.latest_version) || !isTimestamp(value.observed_at) ||
    typeof value.scheduler_running !== "boolean" ||
    (value.effective_state !== "not_configured" && value.effective_state !== "available" &&
      value.effective_state !== "unknown") ||
    !Array.isArray(value.schedules) || value.schedules.length > 20 ||
    !isCount(value.decision_count) || !Array.isArray(value.recent_decisions) ||
    value.recent_decisions.length > 20 || value.recent_decisions.length > value.decision_count
  ) {
    throw new Error("Invalid schedule status");
  }
  const common = {
    latest_version: value.latest_version,
    observed_at: value.observed_at,
    scheduler_running: value.scheduler_running,
    schedules: value.schedules.map(parseSchedule),
    decision_count: value.decision_count,
    recent_decisions: value.recent_decisions.map(parseWake),
  };
  if (value.effective_state === "available") {
    if (
      !isCount(value.effective_version) || value.effective_version === 0 ||
      value.effective_version > value.latest_version || !isTimestamp(value.effective_at) ||
      !isCount(value.schedule_count) || value.schedules.length > value.schedule_count
    ) {
      throw new Error("Invalid effective schedule");
    }
    return {
      ...common,
      effective_state: value.effective_state,
      effective_version: value.effective_version,
      effective_at: value.effective_at,
      schedule_count: value.schedule_count,
    };
  }
  if (
    value.effective_version !== null || value.effective_at !== null ||
    value.schedule_count !== null || value.schedules.length !== 0 ||
    (value.effective_state === "not_configured" && value.latest_version !== 0) ||
    (value.effective_state === "unknown" && value.latest_version === 0)
  ) {
    throw new Error("Invalid unavailable schedule");
  }
  return {
    ...common,
    effective_state: value.effective_state,
    effective_version: null,
    effective_at: null,
    schedule_count: null,
  };
}

function utc(value: string): string {
  return new Date(value).toISOString().replace("T", " ").replace("Z", " UTC");
}

const decisionText: Record<WakeDecision, string> = {
  RUN: "Wake policy eligible; not dispatched",
  SKIPPED_BY_POLICY: "Skipped by wake policy",
  WAKE_WINDOW_EXPIRED: "Wake window expired",
  OUTSIDE_OPERATING_WINDOW: "Outside operating window",
};
const weekdays = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

export default function ScheduleStatus() {
  const [status, setStatus] = useState<Status>({ state: "loading" });
  const [retry, setRetry] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    async function load() {
      try {
        const response = await fetch("/api/v1/schedules/status", {
          cache: "no-store",
          signal: controller.signal,
        });
        if (!response.ok) {
          throw new Error("Schedule status unavailable");
        }
        const value: unknown = await response.json();
        setStatus({ state: "ready", summary: parseSummary(value) });
      } catch {
        if (!controller.signal.aborted) {
          setStatus({ state: "error" });
        }
      }
    }
    void load();
    return () => controller.abort();
  }, [retry]);

  return (
    <section className="data-panel schedule-panel" aria-labelledby="schedule-status-title">
      <h2 id="schedule-status-title">Schedules</h2>
      {status.state === "loading" && <p role="status">Checking schedules...</p>}
      {status.state === "error" && (
        <p role="alert">
          Schedule status is unavailable.{" "}
          <button type="button" className="text-button" onClick={() => {
            setStatus({ state: "loading" });
            setRetry((value) => value + 1);
          }}>
            Retry
          </button>
        </p>
      )}
      {status.state === "ready" && (
        <>
          <p>
            {status.summary.scheduler_running
              ? "Scheduler discovery loop is running."
              : "Scheduler discovery loop is stopped."}{" "}
            Wake decisions do not execute jobs; no job handlers are registered.
          </p>
          {status.summary.effective_state === "not_configured" && <p>No schedules configured.</p>}
          {status.summary.effective_state === "unknown" && (
            <p>The effective schedule is unavailable: activation may be pending or a historical boundary may be missing.</p>
          )}
          {status.summary.effective_state === "available" && (
            <>
              <p>
                Effective version {status.summary.effective_version}, since{" "}
                <time dateTime={status.summary.effective_at}>
                  {utc(status.summary.effective_at)}
                </time>; latest version {status.summary.latest_version}.
              </p>
              <p>Showing {status.summary.schedules.length} of {status.summary.schedule_count} schedules.</p>
              <p>
                Previews after <time dateTime={status.summary.observed_at}>{utc(status.summary.observed_at)}</time>
                {" "}assume the current version stays unchanged. They do not apply market calendars,
                dependencies or wake policies and do not schedule jobs.
              </p>
              {status.summary.schedules.length > 0 && (
                <ul>
                  {status.summary.schedules.map((schedule) => (
                    <li key={schedule.schedule_id}>
                      {schedule.schedule_id} · {schedule.job_id}: {schedule.local_time} {schedule.timezone}
                      {schedule.interval_minutes !== null &&
                        ` · every ${schedule.interval_minutes} min until ${schedule.window_end}`}
                      {schedule.weekdays.length === 7
                        ? " · Every day"
                        : ` · ${schedule.weekdays.map((day) => weekdays[day]).join(", ")}`}
                      {" "}({schedule.enabled ? "enabled" : "disabled"})
                      {schedule.preview_state === "available" && (
                        <ul>
                          {schedule.next_occurrences.map((occurrence) => (
                            <li key={occurrence}><time dateTime={occurrence}>{utc(occurrence)}</time></li>
                          ))}
                        </ul>
                      )}
                      {schedule.preview_state === "pending_change" && " · Preview unavailable: a later version is pending."}
                      {schedule.preview_state === "unavailable" && " · Preview unavailable: the local time cannot be resolved."}
                    </li>
                  ))}
                </ul>
              )}
            </>
          )}
          <p>
            Showing {status.summary.recent_decisions.length} of {status.summary.decision_count} recorded wake decisions.
            No automatic wake checks are running.
          </p>
          {status.summary.recent_decisions.length > 0 && (
            <ul>
              {status.summary.recent_decisions.map((wake) => (
                <li key={JSON.stringify([wake.schedule_id, wake.scheduled_at])}>
                  {wake.schedule_id} · <time dateTime={wake.scheduled_at}>{utc(wake.scheduled_at)}</time>
                  {" "}· {decisionText[wake.decision]} · version {wake.schedule_version}
                  {" "}· observed <time dateTime={wake.observed_at}>{utc(wake.observed_at)}</time>
                </li>
              ))}
            </ul>
          )}
        </>
      )}
    </section>
  );
}
