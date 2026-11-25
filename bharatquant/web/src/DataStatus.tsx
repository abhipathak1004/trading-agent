import { useEffect, useState } from "react";

type ImportSummary = {
  import_id: string;
  source_id: string;
  recorded_at: string;
  record_count: number;
};

type DataSummary = {
  import_count: number;
  imported_record_count: number;
  import_quality: "unassessed";
  live_provider: string;
  recent_imports: ImportSummary[];
};

type Status =
  | { state: "loading" | "error" }
  | { state: "ready"; summary: DataSummary };

function parseImport(value: unknown): ImportSummary {
  if (
    typeof value !== "object" ||
    value === null ||
    !("import_id" in value) ||
    typeof value.import_id !== "string" ||
    value.import_id.length === 0 ||
    !("source_id" in value) ||
    typeof value.source_id !== "string" ||
    value.source_id.length === 0 ||
    !("recorded_at" in value) ||
    typeof value.recorded_at !== "string" ||
    !Number.isFinite(Date.parse(value.recorded_at)) ||
    !("record_count" in value) ||
    typeof value.record_count !== "number" ||
    !Number.isSafeInteger(value.record_count) ||
    value.record_count < 1
  ) {
    throw new Error("Invalid import status response");
  }
  return {
    import_id: value.import_id,
    source_id: value.source_id,
    recorded_at: value.recorded_at,
    record_count: value.record_count,
  };
}

function parseSummary(value: unknown): DataSummary {
  if (
    typeof value !== "object" ||
    value === null ||
    !("import_count" in value) ||
    typeof value.import_count !== "number" ||
    !Number.isSafeInteger(value.import_count) ||
    value.import_count < 0 ||
    !("imported_record_count" in value) ||
    typeof value.imported_record_count !== "number" ||
    !Number.isSafeInteger(value.imported_record_count) ||
    value.imported_record_count < 0 ||
    !("import_quality" in value) ||
    value.import_quality !== "unassessed" ||
    !("live_provider" in value) ||
    typeof value.live_provider !== "string" ||
    !("recent_imports" in value) ||
    !Array.isArray(value.recent_imports) ||
    value.recent_imports.length > 20
  ) {
    throw new Error("Invalid data status response");
  }
  return {
    import_count: value.import_count,
    imported_record_count: value.imported_record_count,
    import_quality: value.import_quality,
    live_provider: value.live_provider,
    recent_imports: value.recent_imports.map(parseImport),
  };
}

export default function DataStatus() {
  const [status, setStatus] = useState<Status>({ state: "loading" });
  const [retry, setRetry] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    async function load() {
      try {
        const response = await fetch("/api/v1/data/status", {
          cache: "no-store",
          signal: controller.signal,
        });
        if (!response.ok) {
          throw new Error("Data status unavailable");
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
    <section className="data-panel" aria-labelledby="data-status-title">
      <h2 id="data-status-title">Data sources</h2>
      {status.state === "loading" && <p role="status">Checking data status...</p>}
      {status.state === "error" && (
        <p role="alert">
          Data status is unavailable.{" "}
          <button
            type="button"
            className="text-button"
            onClick={() => {
              setStatus({ state: "loading" });
              setRetry((value) => value + 1);
            }}
          >
            Retry
          </button>
        </p>
      )}
      {status.state === "ready" && (
        <>
          <p>
            {status.summary.import_count.toLocaleString("en-IN")} CSV imports ·{" "}
            {status.summary.imported_record_count.toLocaleString("en-IN")} imported
            records
          </p>
          <p>
            Data quality is unassessed.{" "}
            {status.summary.live_provider === "not_configured"
              ? "No live provider is configured."
              : `Live data source: ${status.summary.live_provider}.`}
          </p>
          {status.summary.recent_imports.length > 0 && (
            <>
              <h3>Recent imports (up to 20)</h3>
              <ul>
                {status.summary.recent_imports.map((item) => (
                  <li key={item.import_id}>
                    {item.source_id}: {item.record_count.toLocaleString("en-IN")}{" "}
                    {item.record_count === 1 ? "record" : "records"} ·{" "}
                    <time dateTime={item.recorded_at}>
                      {new Date(item.recorded_at).toISOString().replace("T", " ").replace("Z", " UTC")}
                    </time>
                  </li>
                ))}
              </ul>
            </>
          )}
        </>
      )}
    </section>
  );
}
