-- Owner-managed configuration for the model route and the research agent.
--
-- `app_settings` holds non-secret values (provider choice, model name, feed URL,
-- memory retention). `provider_api_keys` holds secrets. Neither is returned in
-- plaintext to the browser; the API only reports a masked form.
--
-- Both tables are mutable: this is configuration, not a financial record. Every
-- change still appends an audit event (without secret material).

CREATE TABLE app_settings (
    setting_key TEXT PRIMARY KEY NOT NULL CHECK (length(setting_key) BETWEEN 1 AND 128),
    setting_value TEXT NOT NULL CHECK (length(setting_value) <= 4096),
    updated_at TEXT NOT NULL
) STRICT;

CREATE TABLE provider_api_keys (
    provider TEXT PRIMARY KEY NOT NULL CHECK (
        provider IN ('GEMINI', 'DEEPSEEK', 'OPENAI', 'CUSTOM')
    ),
    api_key TEXT NOT NULL CHECK (length(api_key) BETWEEN 8 AND 2048),
    updated_at TEXT NOT NULL
) STRICT;
