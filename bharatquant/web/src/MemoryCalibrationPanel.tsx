import { useEffect, useState } from "react";

import { parseMemoryCalibration } from "./memoryContract";
import type { MemoryCalibration } from "./memoryContract";

type LoadState =
  | { state: "loading" | "error" }
  | { state: "ready"; calibration: MemoryCalibration };

function format(value: number | null, digits = 3): string {
  return value === null ? "n/a" : value.toFixed(digits);
}

export default function MemoryCalibrationPanel() {
  const [load, setLoad] = useState<LoadState>({ state: "loading" });
  const [retry, setRetry] = useState(0);

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

  return (
    <section className="data-panel" aria-labelledby="memory-calibration-title">
      <h2 id="memory-calibration-title">Memory calibration</h2>
      <p className="panel-note">
        Brier scores for resolved shadow trades, separated by decision source and
        model. Unresolved or skipped decisions are not scored.
      </p>
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
        ))}
    </section>
  );
}
