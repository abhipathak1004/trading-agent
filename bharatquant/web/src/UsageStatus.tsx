import { useEffect, useState } from "react";
import { isCount, isRecord } from "./contracts";

type TokenUsage = {
  reported_tokens: string;
  reported_count: number;
  estimated_tokens: string;
  estimated_count: number;
  unknown_count: number;
};

type UsageGroup = {
  task_id: string;
  task_version: string;
  model_id: string;
  attempt_count: number;
  incomplete_count: number;
  cache_hits: number;
  input: TokenUsage;
  output: TokenUsage;
};

type UsageSummary = {
  total_groups: number;
  provider_connected: boolean;
  groups: UsageGroup[];
};
type Status =
  | { state: "loading" | "error" }
  | { state: "ready"; summary: UsageSummary };

function parseTokens(value: unknown): TokenUsage {
  if (
    !isRecord(value) ||
    typeof value.reported_tokens !== "string" ||
    !/^(0|[1-9][0-9]*)$/.test(value.reported_tokens) ||
    !isCount(value.reported_count) ||
    typeof value.estimated_tokens !== "string" ||
    !/^(0|[1-9][0-9]*)$/.test(value.estimated_tokens) ||
    !isCount(value.estimated_count) ||
    !isCount(value.unknown_count)
  ) {
    throw new Error("Invalid token usage");
  }
  return {
    reported_tokens: value.reported_tokens,
    reported_count: value.reported_count,
    estimated_tokens: value.estimated_tokens,
    estimated_count: value.estimated_count,
    unknown_count: value.unknown_count,
  };
}

function parseGroup(value: unknown): UsageGroup {
  if (
    !isRecord(value) ||
    typeof value.task_id !== "string" ||
    value.task_id.length === 0 ||
    typeof value.task_version !== "string" ||
    value.task_version.length === 0 ||
    typeof value.model_id !== "string" ||
    value.model_id.length === 0 ||
    !isCount(value.attempt_count) ||
    !isCount(value.incomplete_count) ||
    !isCount(value.cache_hits) ||
    value.incomplete_count + value.cache_hits > value.attempt_count
  ) {
    throw new Error("Invalid model usage group");
  }
  return {
    task_id: value.task_id,
    task_version: value.task_version,
    model_id: value.model_id,
    attempt_count: value.attempt_count,
    incomplete_count: value.incomplete_count,
    cache_hits: value.cache_hits,
    input: parseTokens(value.input),
    output: parseTokens(value.output),
  };
}

function parseSummary(value: unknown): UsageSummary {
  if (
    !isRecord(value) ||
    typeof value.provider_connected !== "boolean" ||
    !isCount(value.total_groups) ||
    !Array.isArray(value.groups) ||
    value.groups.length > 20 ||
    value.groups.length > value.total_groups
  ) {
    throw new Error("Invalid model usage response");
  }
  return {
    total_groups: value.total_groups,
    provider_connected: value.provider_connected,
    groups: value.groups.map(parseGroup),
  };
}

function tokenText(usage: TokenUsage): string {
  return (
    `${BigInt(usage.reported_tokens).toLocaleString("en-IN")} reported ` +
    `(${usage.reported_count} known), ` +
    `${BigInt(usage.estimated_tokens).toLocaleString("en-IN")} estimated ` +
    `(${usage.estimated_count} estimates), ${usage.unknown_count} unknown`
  );
}

export default function UsageStatus() {
  const [status, setStatus] = useState<Status>({ state: "loading" });
  const [retry, setRetry] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    async function load() {
      try {
        const response = await fetch("/api/v1/llm/usage", {
          cache: "no-store",
          signal: controller.signal,
        });
        if (!response.ok) {
          throw new Error("Model usage unavailable");
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
    <section className="data-panel" aria-labelledby="usage-title">
      <h2 id="usage-title">Model usage</h2>
      {status.state === "loading" && <p role="status">Checking model usage...</p>}
      {status.state === "error" && (
        <p role="alert">
          Model usage is unavailable.{" "}
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
            {status.summary.provider_connected
              ? "A model provider is configured."
              : "No model provider is connected."}
          </p>
          {status.summary.total_groups === 0 ? (
            <p>No model attempts recorded.</p>
          ) : (
            <>
              <p>
                Showing {status.summary.groups.length} of {status.summary.total_groups} task/model groups.
                Incomplete attempts count as unknown usage.
              </p>
              <ul>
                {status.summary.groups.map((group) => (
                  <li key={JSON.stringify([group.task_id, group.task_version, group.model_id])}>
                    <strong>{group.task_id} · {group.task_version} · {group.model_id}</strong>
                    <div>
                      Attempts: {group.attempt_count}; incomplete: {group.incomplete_count};
                      cache hits: {group.cache_hits}.
                    </div>
                    <div>Input: {tokenText(group.input)}</div>
                    <div>Output: {tokenText(group.output)}</div>
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
