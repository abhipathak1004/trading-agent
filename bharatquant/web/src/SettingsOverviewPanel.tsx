import { useEffect, useState } from "react";

import { parseOverview } from "./settingsContract";
import type { SettingsOverview } from "./settingsContract";

type Status =
  | { state: "loading" | "error" }
  | { state: "ready"; overview: SettingsOverview };

export default function SettingsOverviewPanel() {
  const [status, setStatus] = useState<Status>({ state: "loading" });
  const [retry, setRetry] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    async function load() {
      try {
        const response = await fetch("/api/v1/settings/overview", {
          cache: "no-store",
          signal: controller.signal,
        });
        if (!response.ok) {
          throw new Error("Settings overview unavailable");
        }
        const value: unknown = await response.json();
        setStatus({ state: "ready", overview: parseOverview(value) });
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
    <section className="data-panel settings-panel" aria-labelledby="settings-overview-title">
      <h2 id="settings-overview-title">Configuration overview</h2>
      {status.state === "loading" && <p role="status">Checking configuration...</p>}
      {status.state === "error" && (
        <p role="alert">
          Configuration overview is unavailable.{" "}
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
          <dl className="settings-facts">
            <dt>Model tasks</dt>
            <dd>
              {status.overview.task_count === 0
                ? "No task definitions registered."
                : `${status.overview.task_count} task definition(s) registered.`}
            </dd>
            <dt>Schedules</dt>
            <dd>
              {status.overview.schedule_count === 0
                ? "No schedules configured."
                : `${status.overview.schedule_count} schedule(s) effective.`}{" "}
              {status.overview.scheduler_running
                ? "Discovery loop running."
                : "Discovery loop stopped."}
            </dd>
            <dt>Model usage</dt>
            <dd>
              {status.overview.model_usage_groups === 0
                ? "No model attempts recorded."
                : `${status.overview.model_usage_groups} task/model usage group(s).`}
            </dd>
            <dt>Model route</dt>
            <dd>
              {status.overview.provider_connected
                ? `${status.overview.config.provider} · ${status.overview.config.model_name}`
                : "Not configured. Set a model name and provider key below."}
            </dd>
            <dt>API keys</dt>
            <dd>
              {status.overview.api_keys_configured
                ? `${status.overview.config.api_keys.filter((key) => key.configured).length} provider key(s) configured.`
                : "No provider API keys configured."}
            </dd>
            <dt>Research agent</dt>
            <dd>
              {status.overview.agent_configuration_state === "configured"
                ? `Configured. Feed ${status.overview.config.news_feed_url}, memory retained ${status.overview.config.news_memory_ttl_days} day(s).`
                : `Feed ${status.overview.config.news_feed_url}; model route incomplete.`}
            </dd>
          </dl>

          <h3>Registered model tasks (up to 50)</h3>
          {status.overview.tasks.length === 0 ? (
            <p>
              No task definitions are registered. Registering tasks and choosing
              their models is not implemented in the UI yet.
            </p>
          ) : (
            <ul className="task-list">
              {status.overview.tasks.map((task) => (
                <li key={`${task.task_id}:${task.task_version}`}>
                  <strong>{task.task_id} · {task.task_version}</strong>
                  <div>
                    Class {task.task_class}; data {task.allowed_data.join(", ")}; tiers{" "}
                    {task.allowed_tiers.join(", ")}.
                  </div>
                  <div>
                    Prompt {task.prompt_version} · spec {task.spec_version}
                  </div>
                </li>
              ))}
            </ul>
          )}
        </>
      )}
    </section>
  );
}
