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
import { parseDataKey } from "./hedgeFundContract";

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
  const [dataKey, setDataKey] = useState("");
  const [dataKeyShow, setDataKeyShow] = useState(false);
  const [dataKeySave, setDataKeySave] = useState<SaveState>({ state: "idle" });
  const [dataKeyStatus, setDataKeyStatus] = useState<{ configured: boolean; masked: string | null }>({
    configured: false,
    masked: null,
  });
  const [retry, setRetry] = useState(0);
  const [provider, setProvider] = useState<Provider>("GEMINI");
  const [modelName, setModelName] = useState("");
  const [feedUrl, setFeedUrl] = useState("");
  const [ttlDays, setTtlDays] = useState("365");
  const [bedrockRegion, setBedrockRegion] = useState("");
  const [regionSave, setRegionSave] = useState<SaveState>({ state: "idle" });
  const [applySave, setApplySave] = useState<SaveState>({ state: "idle" });
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
        setBedrockRegion(config.bedrock_region ?? "");
        // The AI Hedge Fund's data key is its own row, not a provider key.
        const keyResponse = await fetch("/api/v1/aihf/settings", {
          cache: "no-store",
          signal: controller.signal,
        });
        if (keyResponse.ok) {
          setDataKeyStatus(parseDataKey(await keyResponse.json()));
        }
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

  async function applyBedrock() {
    setApplySave({ state: "saving" });
    try {
      const response = await fetch("/api/v1/settings/bedrock/apply", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken },
        body: JSON.stringify({ model: modelName.trim(), region: bedrockRegion }),
      });
      if (!response.ok) {
        let message = "Applying Bedrock failed.";
        try {
          const body = (await response.json()) as { detail?: unknown };
          if (typeof body.detail === "string") {
            message = body.detail;
          }
        } catch {
          // Keep the generic message.
        }
        setApplySave({ state: "failed", message });
        return;
      }
      const config = parseConfig(await response.json());
      setLoad({ state: "ready", config });
      setApplySave({ state: "saved" });
    } catch {
      setApplySave({
        state: "failed",
        message: "Applying Bedrock failed. Check the local service.",
      });
    }
  }

  async function saveRegion() {
    setRegionSave({ state: "saving" });
    try {
      const response = await fetch("/api/v1/settings/bedrock-region", {
        method: "PUT",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken },
        body: JSON.stringify({ region: bedrockRegion }),
      });
      if (!response.ok) {
        let message = "Saving the Bedrock region failed.";
        try {
          const body = (await response.json()) as { detail?: unknown };
          if (typeof body.detail === "string") {
            message = body.detail;
          }
        } catch {
          // Keep the generic message.
        }
        setRegionSave({ state: "failed", message });
        return;
      }
      const config = parseConfig(await response.json());
      setBedrockRegion(config.bedrock_region ?? "");
      setRegionSave({ state: "saved" });
    } catch {
      setRegionSave({
        state: "failed",
        message: "Saving the Bedrock region failed. Check the local service.",
      });
    }
  }

  async function saveDataKey(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setDataKeySave({ state: "saving" });
    try {
      const response = await fetch("/api/v1/aihf/settings", {
        method: "PUT",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken },
        body: JSON.stringify({ financial_datasets_api_key: dataKey }),
      });
      if (!response.ok) {
        let message = "Saving the Financial Datasets key failed.";
        try {
          const body = (await response.json()) as { detail?: unknown };
          if (typeof body.detail === "string") {
            message = body.detail;
          }
        } catch {
          // Keep the generic message.
        }
        setDataKeySave({ state: "failed", message });
        return;
      }
      setDataKeyStatus(parseDataKey(await response.json()));
      setDataKey("");
      setDataKeySave({ state: "saved" });
    } catch {
      setDataKeySave({
        state: "failed",
        message: "Saving the Financial Datasets key failed. Check the local service.",
      });
    }
  }

  async function clearDataKey() {
    setDataKeySave({ state: "saving" });
    try {
      const response = await fetch("/api/v1/aihf/settings", {
        method: "PUT",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken },
        body: JSON.stringify({ financial_datasets_api_key: "" }),
      });
      if (!response.ok) {
        setDataKeySave({ state: "failed", message: "Clearing the key failed." });
        return;
      }
      setDataKeyStatus(parseDataKey(await response.json()));
      setDataKeySave({ state: "saved" });
    } catch {
      setDataKeySave({
        state: "failed",
        message: "Clearing the key failed. Check the local service.",
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
            placeholder={
              provider === "BEDROCK"
                ? "e.g. us.anthropic.claude-sonnet-4-6"
                : "e.g. gemini-2.5-pro"
            }
            onChange={(event) => setModelName(event.target.value)}
            disabled={save.state === "saving"}
          />

          {provider === "BEDROCK" && (
            <>
              <label htmlFor="config-bedrock-region">Bedrock region</label>
              <input
                id="config-bedrock-region"
                type="text"
                value={bedrockRegion}
                maxLength={32}
                placeholder="us-east-1"
                onChange={(event) => setBedrockRegion(event.target.value)}
                disabled={regionSave.state === "saving"}
              />
              <p className="panel-note">
                A Bedrock model id is namespaced by region, e.g.
                <code> us.anthropic.claude-sonnet-4-6</code>. A short-term Bedrock
                API key only works in the region it was created in.
              </p>
              <button
                type="button"
                className="auth-button auth-button--compact"
                disabled={regionSave.state === "saving"}
                onClick={() => void saveRegion()}
              >
                {regionSave.state === "saving" ? "Saving..." : "Save region"}
              </button>
              {regionSave.state === "saved" && (
                <p className="config-feedback" role="status">
                  Bedrock region saved.
                </p>
              )}
              {regionSave.state === "failed" && (
                <p className="auth-error" role="alert">
                  {regionSave.message}
                </p>
              )}
              <button
                type="button"
                className="auth-button auth-button--compact"
                disabled={applySave.state === "saving" || modelName.trim().length === 0}
                onClick={() => void applyBedrock()}
              >
                {applySave.state === "saving"
                  ? "Applying..."
                  : "Use Bedrock for all text agents"}
              </button>
              <p className="panel-note">
                Points the reasoning and flash tiers at this model. Eleven agent
                roles route through them; the four decision agents stay on TypeSafe Jev.
              </p>
              {applySave.state === "saved" && (
                <p className="config-feedback" role="status">
                  Text agents now route through Bedrock.
                </p>
              )}
              {applySave.state === "failed" && (
                <p className="auth-error" role="alert">
                  {applySave.message}
                </p>
              )}
            </>
          )}

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

      <section className="settings-panel" aria-labelledby="data-providers-title">
        <h2 id="data-providers-title">Data providers</h2>
        <p className="panel-note">
          The AI Hedge Fund tab reads its own data key. It is stored locally, masked
          on read, and injected into that engine&apos;s process environment per run —
          never written to <code>~/.hedge-fund/.env</code>.
        </p>
        <ul className="task-list">
          <li>
            <strong>Financial Datasets</strong>{" "}
            {dataKeyStatus.configured
              ? `Configured · ${dataKeyStatus.masked ?? ""}`
              : "Not configured"}{" "}
            {dataKeyStatus.configured && (
              <button
                type="button"
                className="text-button"
                onClick={() => void clearDataKey()}
                disabled={dataKeySave.state === "saving"}
              >
                Clear key
              </button>
            )}
          </li>
        </ul>
        <form className="config-form" onSubmit={(event) => void saveDataKey(event)}>
          <label htmlFor="data-key-value">Financial Datasets API key</label>
          <div className="ahf-secret">
            <input
              id="data-key-value"
              type={dataKeyShow ? "text" : "password"}
              autoComplete="off"
              spellCheck={false}
              value={dataKey}
              onChange={(event) => setDataKey(event.target.value)}
              disabled={dataKeySave.state === "saving"}
              placeholder="Paste the Financial Datasets key"
            />
            <button
              type="button"
              className="text-button"
              aria-pressed={dataKeyShow}
              onClick={() => setDataKeyShow((value) => !value)}
            >
              {dataKeyShow ? "Hide" : "Show"}
            </button>
          </div>
          <button
            type="submit"
            className="auth-button"
            disabled={dataKeySave.state === "saving" || dataKey.length === 0}
          >
            {dataKeySave.state === "saving" ? "Saving..." : "Save data key"}
          </button>
        </form>
        {dataKeySave.state === "saved" && (
          <p className="config-feedback" role="status">
            Financial Datasets key saved.
          </p>
        )}
        {dataKeySave.state === "failed" && (
          <p className="auth-error" role="alert">
            {dataKeySave.message}
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
