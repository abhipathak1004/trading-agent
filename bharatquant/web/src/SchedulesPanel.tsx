import { useEffect, useState } from "react";

import {
  parseJobCatalogue,
  parseJobRun,
  parseScheduleConfig,
  parseScheduleList,
} from "./schedulerContract.ts";
import type {
  JobDefinition,
  ScheduleConfig,
  ScheduledJobRun,
} from "./schedulerContract.ts";

type LoadState =
  | { state: "loading" | "error" }
  | { state: "ready"; version: number; schedules: ScheduleConfig[]; jobs: JobDefinition[] };

type SaveState =
  | { state: "idle" | "saving" }
  | { state: "saved"; message: string }
  | { state: "failed"; message: string };

function scheduleIdFor(job: JobDefinition): string {
  return `${job.job_id.replace(/\./g, "-")}-daily`;
}

function scheduleBody(schedule: ScheduleConfig) {
  return {
    job_id: schedule.job_id,
    enabled: schedule.enabled,
    timezone: schedule.timezone,
    local_time: schedule.local_time,
    window_start: schedule.window_start,
    window_end: schedule.window_end,
    wake_policy: schedule.wake_policy,
    wake_within_seconds: schedule.wake_within_seconds,
    max_attempts: schedule.max_attempts,
    interval_minutes: schedule.interval_minutes,
    weekdays: schedule.weekdays,
    dependencies: schedule.dependencies,
  };
}

export default function SchedulesPanel({ csrfToken }: { csrfToken: string }) {
  const [load, setLoad] = useState<LoadState>({ state: "loading" });
  const [save, setSave] = useState<SaveState>({ state: "idle" });
  const [notice, setNotice] = useState("");
  const [version, setVersion] = useState(0);
  const [history, setHistory] = useState<Record<string, ScheduledJobRun[]>>({});

  useEffect(() => {
    const controller = new AbortController();
    async function loadSchedules() {
      try {
        const [listResponse, catalogueResponse] = await Promise.all([
          fetch("/api/v1/schedules", {
            cache: "no-store",
            signal: controller.signal,
          }),
          fetch("/api/v1/schedules/catalogue", {
            cache: "no-store",
            signal: controller.signal,
          }),
        ]);
        if (!listResponse.ok || !catalogueResponse.ok) {
          throw new Error("Schedules unavailable");
        }
        const list = parseScheduleList(await listResponse.json());
        const jobs = parseJobCatalogue(await catalogueResponse.json());
        setLoad({
          state: "ready",
          version: list.version,
          schedules: list.schedules,
          jobs,
        });
      } catch {
        if (!controller.signal.aborted) {
          setLoad({ state: "error" });
        }
      }
    }
    void loadSchedules();
    return () => controller.abort();
  }, [version]);

  function patch(scheduleId: string, patchValue: Partial<ScheduleConfig>) {
    if (load.state !== "ready") {
      return;
    }
    setLoad({
      ...load,
      schedules: load.schedules.map((item) =>
        item.schedule_id === scheduleId ? { ...item, ...patchValue } : item,
      ),
    });
  }

  async function saveSchedule(schedule: ScheduleConfig) {
    setSave({ state: "saving" });
    setNotice("");
    try {
      const response = await fetch(
        `/api/v1/schedules/${encodeURIComponent(schedule.schedule_id)}`,
        {
          method: "PUT",
          headers: {
            "Content-Type": "application/json",
            "X-CSRF-Token": csrfToken,
          },
          body: JSON.stringify(scheduleBody(schedule)),
        },
      );
      if (!response.ok) {
        let message = "The schedule could not be saved.";
        try {
          const detail = (await response.json()) as { detail?: unknown };
          if (typeof detail.detail === "string") {
            message = detail.detail;
          }
        } catch {
          // Keep the generic message.
        }
        setSave({ state: "failed", message });
        return;
      }
      parseScheduleConfig(await response.json());
      setSave({ state: "saved", message: "Schedule saved." });
      setVersion((value) => value + 1);
    } catch {
      setSave({ state: "failed", message: "The schedule could not be saved." });
    }
  }

  function scheduleFromJob(job: JobDefinition): ScheduleConfig {
    return {
      schedule_id: scheduleIdFor(job),
      job_id: job.job_id,
      enabled: job.enabled_by_default,
      timezone: job.timezone,
      local_time: job.local_time,
      window_start: job.window_start,
      window_end: job.window_end,
      wake_policy: job.wake_policy,
      wake_within_seconds: null,
      max_attempts: job.max_attempts,
      interval_minutes: null,
      weekdays: [0, 1, 2, 3, 4, 5, 6],
      dependencies: [],
      version: load.state === "ready" ? load.version : 0,
      next_occurrences: [],
    };
  }

  async function createFromJob(job: JobDefinition) {
    if (load.state !== "ready") {
      return;
    }
    const schedule = scheduleFromJob(job);
    setLoad({
      ...load,
      schedules: [...load.schedules, schedule],
    });
    await saveSchedule(schedule);
  }

  async function toggle(schedule: ScheduleConfig) {
    setSave({ state: "saving" });
    setNotice("");
    try {
      const response = await fetch(
        `/api/v1/schedules/${encodeURIComponent(schedule.schedule_id)}/enable`,
        {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-CSRF-Token": csrfToken,
          },
          body: JSON.stringify({ enabled: !schedule.enabled }),
        },
      );
      if (!response.ok) {
        throw new Error("Enable failed");
      }
      parseScheduleConfig(await response.json());
      setSave({ state: "saved", message: schedule.enabled ? "Schedule disabled." : "Schedule enabled." });
      setVersion((value) => value + 1);
    } catch {
      setSave({ state: "failed", message: "The schedule could not be toggled." });
    }
  }

  async function runNow(schedule: ScheduleConfig) {
    setSave({ state: "saving" });
    setNotice("");
    try {
      const response = await fetch(
        `/api/v1/schedules/${encodeURIComponent(schedule.schedule_id)}/run`,
        {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-CSRF-Token": csrfToken,
          },
          body: JSON.stringify({ idempotency_key: `ui-${Date.now()}` }),
        },
      );
      if (!response.ok) {
        throw new Error("Run failed");
      }
      const run = parseJobRun(await response.json());
      setNotice(`Queued ${run.job_id} (${run.status.toLowerCase()}).`);
      void loadHistory(schedule);
    } catch {
      setSave({ state: "failed", message: "The job could not be queued." });
    }
  }

  async function dryRun(schedule: ScheduleConfig) {
    setSave({ state: "saving" });
    setNotice("");
    try {
      const response = await fetch(
        `/api/v1/schedules/${encodeURIComponent(schedule.schedule_id)}/dry-run`,
        { method: "POST", headers: { "X-CSRF-Token": csrfToken } },
      );
      if (!response.ok) {
        throw new Error("Dry run failed");
      }
      setSave({ state: "saved", message: "Dry run valid; nothing was queued." });
    } catch {
      setSave({ state: "failed", message: "Dry run failed." });
    }
  }

  async function loadHistory(schedule: ScheduleConfig) {
    try {
      const response = await fetch(
        `/api/v1/schedules/${encodeURIComponent(schedule.schedule_id)}/history`,
        { cache: "no-store" },
      );
      if (response.ok) {
        const values: unknown = await response.json();
        if (Array.isArray(values)) {
          setHistory((current) => ({
            ...current,
            [schedule.schedule_id]: values.map(parseJobRun),
          }));
        }
      }
    } catch {
      // History is optional.
    }
  }

  if (load.state === "loading") {
    return (
      <section className="settings-panel" aria-label="Schedules">
        <p role="status">Loading schedules...</p>
      </section>
    );
  }
  if (load.state === "error") {
    return (
      <section className="settings-panel" aria-label="Schedules">
        <p role="alert">
          Schedules are unavailable.{" "}
          <button
            type="button"
            className="text-button"
            onClick={() => setVersion((value) => value + 1)}
          >
            Retry
          </button>
        </p>
      </section>
    );
  }

  const ready = load as Extract<LoadState, { state: "ready" }>;

  return (
    <section className="settings-panel" aria-labelledby="scheduler-title">
      <h2 id="scheduler-title">Schedules</h2>
      <p className="panel-note">
        Every recurring job is configuration. Saving creates a new immutable
        schedule version; Run now queues one durable job and never executes it in
        the browser request. Disabled schedules do not dispatch.
      </p>

      {save.state === "failed" && (
        <p className="auth-error" role="alert">{save.message}</p>
      )}
      {save.state === "saved" && (
        <p className="panel-note" role="status">{save.message}</p>
      )}
      {notice !== "" && <p className="panel-note" role="status">{notice}</p>}

      <h3>Job catalogue</h3>
      <div className="models-grid models-grid--compact">
        {ready.jobs.map((job) => {
          const existing = ready.schedules.some((item) => item.job_id === job.job_id);
          return (
            <article className="tier-card" key={job.job_id}>
              <h4>{job.label}</h4>
              <p className="panel-note">{job.description}</p>
              <p className="prompt-meta">
                {job.resource_class} · {job.job_id} · {job.wake_policy}
              </p>
              <button
                type="button"
                className="text-button"
                disabled={existing || save.state === "saving"}
                onClick={() => void createFromJob(job)}
              >
                {existing ? "Schedule exists" : "Create from catalogue"}
              </button>
            </article>
          );
        })}
      </div>

      <h3>Schedules</h3>
      {ready.schedules.length === 0 && (
        <p className="panel-note">No schedules configured.</p>
      )}
      {ready.schedules.map((schedule) => (
        <article className="tier-card" key={schedule.schedule_id}>
          <div className="strategy-symbol-head">
            <h4>{schedule.schedule_id}</h4>
            <span
              className={`status-badge ${
                schedule.enabled
                  ? "status-badge--allow"
                  : "status-badge--provisional"
              }`}
            >
              {schedule.enabled ? "enabled" : "disabled"}
            </span>
          </div>
          <p className="prompt-meta">
            {schedule.job_id} · version {schedule.version} ·{" "}
            {schedule.next_occurrences.length} next occurrence(s)
          </p>
          <div className="models-grid models-grid--compact">
            <label>
              Timezone
              <input
                value={schedule.timezone}
                onChange={(event) =>
                  patch(schedule.schedule_id, { timezone: event.target.value })
                }
              />
            </label>
            <label>
              Local time
              <input
                type="time"
                value={schedule.local_time}
                onChange={(event) =>
                  patch(schedule.schedule_id, { local_time: event.target.value })
                }
              />
            </label>
            <label>
              Window start
              <input
                type="time"
                value={schedule.window_start}
                onChange={(event) =>
                  patch(schedule.schedule_id, { window_start: event.target.value })
                }
              />
            </label>
            <label>
              Window end
              <input
                type="time"
                value={schedule.window_end}
                onChange={(event) =>
                  patch(schedule.schedule_id, { window_end: event.target.value })
                }
              />
            </label>
            <label>
              Max attempts
              <input
                type="number"
                min="1"
                max="10"
                value={schedule.max_attempts}
                onChange={(event) =>
                  patch(schedule.schedule_id, {
                    max_attempts: Number(event.target.value),
                  })
                }
              />
            </label>
            <label>
              Interval minutes (optional)
              <input
                type="number"
                min="1"
                max="1440"
                value={schedule.interval_minutes ?? ""}
                onChange={(event) =>
                  patch(schedule.schedule_id, {
                    interval_minutes:
                      event.target.value === "" ? null : Number(event.target.value),
                  })
                }
              />
            </label>
          </div>
          <div className="strategy-examples">
            <button
              type="button"
              className="auth-button"
              disabled={save.state === "saving"}
              onClick={() => void saveSchedule(schedule)}
            >
              Save schedule
            </button>
            <button
              type="button"
              className="text-button"
              disabled={save.state === "saving"}
              onClick={() => void toggle(schedule)}
            >
              {schedule.enabled ? "Disable" : "Enable"}
            </button>
            <button
              type="button"
              className="text-button"
              disabled={save.state === "saving"}
              onClick={() => void dryRun(schedule)}
            >
              Dry run
            </button>
            <button
              type="button"
              className="text-button"
              disabled={save.state === "saving"}
              onClick={() => void runNow(schedule)}
            >
              Run now
            </button>
            <button
              type="button"
              className="text-button"
              onClick={() => void loadHistory(schedule)}
            >
              History
            </button>
          </div>
          {history[schedule.schedule_id] !== undefined && (
            <table className="judgment-table">
              <thead>
                <tr>
                  <th scope="col">Job</th>
                  <th scope="col">Status</th>
                  <th scope="col">Attempts</th>
                  <th scope="col">Failure</th>
                </tr>
              </thead>
              <tbody>
                {history[schedule.schedule_id].map((run) => (
                  <tr key={run.run_id}>
                    <th scope="row">{run.job_id}</th>
                    <td>{run.status}</td>
                    <td>{run.attempts}/{run.max_attempts}</td>
                    <td>{run.failure_code ?? "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </article>
      ))}
    </section>
  );
}
