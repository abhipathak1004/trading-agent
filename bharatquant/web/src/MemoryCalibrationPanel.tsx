import { useEffect, useState } from "react";

import { parseMemoryCalibration } from "./memoryContract";
import TableScroll from "./TableScroll";
import type { MemoryCalibration } from "./memoryContract";
import { parseMemorySchedule } from "./memoryScheduleContract";
import type { MemorySchedule } from "./memoryScheduleContract";

type LoadState =
  | { state: "loading" | "error" }
  | { state: "ready"; calibration: MemoryCalibration };

function format(value: number | null, digits = 3): string {
  return value === null ? "n/a" : value.toFixed(digits);
}

export default function MemoryCalibrationPanel({
  csrfToken,
}: {
  csrfToken: string;
}) {
  const [load, setLoad] = useState<LoadState>({ state: "loading" });
  const [retry, setRetry] = useState(0);
  const [resolving, setResolving] = useState(false);
  const [resolveMessage, setResolveMessage] = useState<string | null>(null);
  const [schedule, setSchedule] = useState<MemorySchedule | null>(null);
  const [scheduleSaving, setScheduleSaving] = useState(false);

  useEffect(() => {
    const controller = new AbortController();
    async function loadCalibration() {
      try {
        const response = await fetch("/api/v1/memory/calibration", {
          cache: "no-store",
          signal: controller.signal,
        });
        if (!response.ok) {
          throw new Error("Calibration unavailable");
        }
        setLoad({
          state: "ready",
          calibration: parseMemoryCalibration(await response.json()),
        });
      } catch {
        if (!controller.signal.aborted) {
          setLoad({ state: "error" });
        }
      }
    }
    void loadCalibration();
    return () => controller.abort();
  }, [retry]);

  useEffect(() => {
    const controller = new AbortController();
    async function loadSchedule() {
      try {
        const response = await fetch("/api/v1/schedules/memory-resolve", {
          cache: "no-store",
          signal: controller.signal,
        });
        if (response.ok) {
          setSchedule(parseMemorySchedule(await response.json()));
        }
      } catch {
        // The schedule toggle is optional; calibration still works.
      }
    }
    void loadSchedule();
    return () => controller.abort();
  }, [retry]);

  async function toggleSchedule(enabled: boolean) {
    setScheduleSaving(true);
    try {
      const response = await fetch("/api/v1/schedules/memory-resolve", {
        method: "PUT",
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": csrfToken,
        },
        body: JSON.stringify({ enabled }),
      });
      if (response.ok) {
        setSchedule(parseMemorySchedule(await response.json()));
      }
    } catch {
      // Leave the displayed state unchanged on failure.
    } finally {
      setScheduleSaving(false);
    }
  }

  async function resolveDue() {
    setResolving(true);
    setResolveMessage(null);
    try {
      const response = await fetch("/api/v1/memory/resolve", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": csrfToken,
        },
        body: JSON.stringify({ horizon_days: 5, max_episodes: 50 }),
      });
      if (!response.ok) {
        let message = "Resolving outcomes failed.";
        try {
          const body = (await response.json()) as { detail?: unknown };
          if (typeof body.detail === "string") {
            message = body.detail;
          }
        } catch {
          // Keep the generic message.
        }
        setResolveMessage(message);
        return;
      }
      const report = (await response.json()) as {
        examined?: unknown;
        resolved?: unknown;
      };
      const examined = typeof report.examined === "number" ? report.examined : 0;
      const resolved = typeof report.resolved === "number" ? report.resolved : 0;
      setResolveMessage(`Resolved ${resolved} of ${examined} due episode(s).`);
      setLoad({ state: "loading" });
      setRetry((value) => value + 1);
    } catch {
      setResolveMessage("Resolving outcomes failed. Check the local service.");
    } finally {
      setResolving(false);
    }
  }

  return (
    <section className="data-panel" aria-labelledby="memory-calibration-title">
      <h2 id="memory-calibration-title">Memory calibration</h2>
      <p className="panel-note">
        Brier scores for resolved shadow trades, separated by decision source and
        model. Unresolved or skipped decisions are not scored.
      </p>
      <button
        type="button"
        className="auth-button auth-button--compact"
        onClick={() => void resolveDue()}
        disabled={resolving}
      >
        {resolving ? "Resolving..." : "Resolve due episodes"}
      </button>
      {resolveMessage !== null && (
        <p className="config-feedback" role="status">
          {resolveMessage}
        </p>
      )}
      {schedule !== null && (
        <p className="panel-note">
          <label>
            <input
              type="checkbox"
              checked={schedule.enabled}
              disabled={scheduleSaving}
              onChange={(event) => void toggleSchedule(event.target.checked)}
            />{" "}
            Daily resolver
          </label>
          {" — "}
          {schedule.enabled
            ? `enabled at ${schedule.local_time ?? "18:00"} ${schedule.timezone ?? "Asia/Kolkata"}`
            : "disabled; enable to let the worker resolve due outcomes automatically"}
          .
        </p>
      )}
      {load.state === "loading" && <p role="status">Loading calibration...</p>}
      {load.state === "error" && (
        <p role="alert">
          Calibration is unavailable.{" "}
          <button
            type="button"
            className="text-button"
            onClick={() => {
              setLoad({ state: "loading" });
              setRetry((value) => value + 1);
            }}
          >
            Retry
          </button>
        </p>
      )}
      {load.state === "ready" &&
        (load.calibration.buckets.length === 0 ? (
          <p>No resolved shadow episodes yet.</p>
        ) : (
          <TableScroll label="Calibration by source">
            <table className="judgment-table">
              <thead>
                <tr>
                  <th scope="col">Source</th>
                  <th scope="col">Model</th>
                  <th scope="col">n</th>
                  <th scope="col">Brier</th>
                  <th scope="col">ECE</th>
                  <th scope="col">Outcome</th>
                </tr>
              </thead>
              <tbody>
                {load.calibration.buckets.map((bucket) => (
                  <tr key={`${bucket.decision_source}-${bucket.model}`}>
                    <th scope="row">{bucket.decision_source}</th>
                    <td>{bucket.model}</td>
                    <td>{bucket.count}</td>
                    <td>{format(bucket.mean_brier)}</td>
                    <td>{format(bucket.expected_calibration_error)}</td>
                    <td>{format(bucket.outcome_rate)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </TableScroll>
        ))}
    </section>
  );
}
