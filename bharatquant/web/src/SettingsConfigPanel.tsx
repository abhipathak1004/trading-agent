import { useEffect, useState } from "react";
import type { FormEvent } from "react";

import {
  ALL_PROVIDERS,
  MAX_MEMORY_TTL_DAYS,
  MAX_MODEL_NAME_CHARS,
  MIN_MEMORY_TTL_DAYS,
  PROVIDERS,
  parseConfig,
} from "./settingsContract";
import type { AppConfig, KeyProvider, Provider } from "./settingsContract";

type LoadState =
  | { state: "loading" }
  | { state: "error" }
  | { state: "ready"; config: AppConfig };

type SaveState =
  | { state: "idle" | "saving" }
  | { state: "saved" }
  | { state: "failed"; message: string };

export default function SettingsConfigPanel({
  csrfToken,
}: {
  csrfToken: string;
}) {
  const [load, setLoad] = useState<LoadState>({ state: "loading" });
  const [retry, setRetry] = useState(0);
  const [provider, setProvider] = useState<Provider>("GEMINI");
  const [modelName, setModelName] = useState("");
  const [feedUrl, setFeedUrl] = useState("");
  const [ttlDays, setTtlDays] = useState("365");
  const [save, setSave] = useState<SaveState>({ state: "idle" });
  const [keyProvider, setKeyProvider] = useState<KeyProvider>("GEMINI");
  const [apiKey, setApiKey] = useState("");
  const [keySave, setKeySave] = useState<SaveState>({ state: "idle" });

  useEffect(() => {
    const controller = new AbortController();
    async function loadConfig() {
      try {
        const response = await fetch("/api/v1/settings/config", {
          cache: "no-store",
          signal: controller.signal,
        });
        if (!response.ok) {
          throw new Error("Configuration unavailable");
        }
        const value: unknown = await response.json();
        const config = parseConfig(value);
        setLoad({ state: "ready", config });
        setProvider(config.provider);
        setModelName(config.model_name);
        setFeedUrl(config.news_feed_url);
        setTtlDays(String(config.news_memory_ttl_days));
      } catch {
        if (!controller.signal.aborted) {
          setLoad({ state: "error" });
        }
      }
    }
    void loadConfig();
    return () => controller.abort();
  }, [retry]);

  async function submitConfig(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setSave({ state: "saving" });
    const ttl = Number(ttlDays);
    if (!Number.isInteger(ttl)) {
      setSave({ state: "failed", message: "Retention must be a whole number." });
      return;
    }
    try {
      const response = await fetch("/api/v1/settings/config", {
        method: "PUT",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken },
        body: JSON.stringify({
          provider,
          model_name: modelName,
          news_feed_url: feedUrl,
          news_memory_ttl_days: ttl,
        }),
      });
      if (!response.ok) {
        let message = "Saving configuration failed.";
        try {
          const body = (await response.json()) as { detail?: unknown };
          if (typeof body.detail === "string") {
            message = body.detail;
          }
        } catch {
          // Keep the generic message.
        }
        setSave({ state: "failed", message });
        return;
      }
      const config = parseConfig(await response.json());
      setLoad({ state: "ready", config });
      setSave({ state: "saved" });
    } catch {
      setSave({
        state: "failed",
        message: "Saving configuration failed. Check the local service.",
      });
    }
  }

  async function saveKey(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!apiKey) {
      return;
    }
    setKeySave({ state: "saving" });
    try {
      const response = await fetch(
        `/api/v1/settings/api-keys/${keyProvider}`,
        {
          method: "PUT",
          headers: {
            "Content-Type": "application/json",
            "X-CSRF-Token": csrfToken,
          },
          body: JSON.stringify({ api_key: apiKey }),
        },
      );
      if (!response.ok) {
        let message = "Saving the API key failed.";
        try {
          const body = (await response.json()) as { detail?: unknown };
          if (typeof body.detail === "string") {
            message = body.detail;
          }
        } catch {
          // Keep the generic message.
        }
        setKeySave({ state: "failed", message });
        return;
      }
      setLoad({ state: "ready", config: parseConfig(await response.json()) });
      setApiKey("");
      setKeySave({ state: "saved" });
    } catch {
      setKeySave({
        state: "failed",
        message: "Saving the API key failed. Check the local service.",
      });
    }
  }

  async function clearKey(target: KeyProvider) {
    setKeySave({ state: "saving" });
    try {
      const response = await fetch(`/api/v1/settings/api-keys/${target}`, {
        method: "DELETE",
        headers: { "X-CSRF-Token": csrfToken },
      });
      if (!response.ok) {
        setKeySave({ state: "failed", message: "Clearing the API key failed." });
        return;
      }
      setLoad({ state: "ready", config: parseConfig(await response.json()) });
      setKeySave({ state: "saved" });
    } catch {
      setKeySave({
        state: "failed",
        message: "Clearing the API key failed. Check the local service.",
      });
    }
  }

  if (load.state === "loading") {
    return (
      <section className="settings-panel" aria-label="Configuration">
        <p role="status">Loading configuration...</p>
      </section>
    );
  }

  if (load.state === "error") {
    return (
      <section className="settings-panel" aria-label="Configuration">
        <p role="alert">
          Configuration is unavailable.{" "}
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
      </section>
    );
  }

  const config = load.config;
  const configuredKeys = config.api_keys.filter((key) => key.configured);

  return (
    <>
      <section className="settings-panel" aria-labelledby="config-title">
        <h2 id="config-title">Model &amp; research configuration</h2>
        <p className="panel-note">
          Values are stored in the local database. API keys are never returned to
          the browser — only a masked hint. No model provider is called by this
          build.
        </p>
        <form className="config-form" onSubmit={(event) => void submitConfig(event)}>
          <label htmlFor="config-provider">Model provider</label>
          <select
            id="config-provider"
            value={provider}
            onChange={(event) => setProvider(event.target.value as Provider)}
            disabled={save.state === "saving"}
          >
            {PROVIDERS.map((item) => (
              <option key={item} value={item}>
                {item}
              </option>
            ))}
          </select>

          <label htmlFor="config-model">Model name</label>
          <input
            id="config-model"
            type="text"
            value={modelName}
            maxLength={MAX_MODEL_NAME_CHARS}
            placeholder="e.g. gemini-2.5-pro"
            onChange={(event) => setModelName(event.target.value)}
            disabled={save.state === "saving"}
          />

          <label htmlFor="config-feed">News feed URL (HTTPS)</label>
          <input
            id="config-feed"
            type="url"
            value={feedUrl}
            placeholder="https://pulse.zerodha.com/feed.xml"
            onChange={(event) => setFeedUrl(event.target.value)}
            disabled={save.state === "saving"}
          />

          <label htmlFor="config-ttl">Long-term memory retention (days)</label>
          <input
            id="config-ttl"
            type="number"
            min={MIN_MEMORY_TTL_DAYS}
            max={MAX_MEMORY_TTL_DAYS}
            value={ttlDays}
            onChange={(event) => setTtlDays(event.target.value)}
            disabled={save.state === "saving"}
          />

          <button
            type="submit"
            className="auth-button"
            disabled={save.state === "saving"}
          >
            {save.state === "saving" ? "Saving..." : "Save configuration"}
          </button>
        </form>
        {save.state === "saved" && (
          <p className="config-feedback" role="status">
            Configuration saved.
          </p>
        )}
        {save.state === "failed" && (
          <p className="auth-error" role="alert">
            {save.message}
          </p>
        )}
      </section>

      <section className="settings-panel" aria-labelledby="keys-title">
        <h2 id="keys-title">Provider API keys</h2>
        <p className="panel-note">
          Keys are stored in plaintext in the local database. That is acceptable
          for a single-owner, loopback-only app, but it is not vault-grade.
        </p>

        {config.api_keys.length === 0 ? (
          <p>No API keys configured.</p>
        ) : (
          <ul className="task-list">
            {config.api_keys.map((key) => (
              <li key={key.provider}>
                <strong>{key.provider}</strong>
                <div>
                  {key.configured ? `Configured · ${key.masked}` : "Not configured"}
                </div>
                {key.configured && (
                  <button
                    type="button"
                    className="text-button"
                    onClick={() => void clearKey(key.provider)}
                    disabled={keySave.state === "saving"}
                  >
                    Clear key
                  </button>
                )}
              </li>
            ))}
          </ul>
        )}

        <form className="config-form" onSubmit={(event) => void saveKey(event)}>
          <label htmlFor="key-provider">Provider</label>
          <select
            id="key-provider"
            value={keyProvider}
            onChange={(event) => setKeyProvider(event.target.value as KeyProvider)}
            disabled={keySave.state === "saving"}
          >
            {ALL_PROVIDERS.map((item) => (
              <option key={item} value={item}>
                {item}
              </option>
            ))}
          </select>

          <label htmlFor="key-value">API key</label>
          <input
            id="key-value"
            type="password"
            autoComplete="off"
            value={apiKey}
            onChange={(event) => setApiKey(event.target.value)}
            disabled={keySave.state === "saving"}
            placeholder="Paste the provider key"
          />

          <button
            type="submit"
            className="auth-button"
            disabled={keySave.state === "saving" || apiKey.length === 0}
          >
            {keySave.state === "saving" ? "Saving..." : "Save API key"}
          </button>
        </form>
        {keySave.state === "saved" && (
          <p className="config-feedback" role="status">
            API key saved.
          </p>
        )}
        {keySave.state === "failed" && (
          <p className="auth-error" role="alert">
            {keySave.message}
          </p>
        )}
        <p className="panel-note">
          {config.model_route_configured
            ? "Model route configured: a model name and a key for the selected provider are set."
            : "Model route incomplete: set a model name and a key for the selected provider."}
          {configuredKeys.length === 0 && " No provider key is configured yet."}
        </p>
      </section>
    </>
  );
}
