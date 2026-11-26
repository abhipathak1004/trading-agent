-- Admit TypeSafe Jev as a provider key (ADR-016, ADR-017).
--
-- provider_api_keys has a CHECK constraint on the provider name. SQLite cannot
-- alter a CHECK, so the table is rebuilt. No foreign key references it, and the
-- table has no triggers, so copy/drop/rename preserves every stored key.
-- The migration runner wraps this file in one transaction.

CREATE TABLE provider_api_keys_new (
    provider TEXT PRIMARY KEY NOT NULL CHECK (
        provider IN ('GEMINI', 'DEEPSEEK', 'OPENAI', 'CUSTOM', 'TYPESAFE_JEV')
    ),
    api_key TEXT NOT NULL CHECK (length(api_key) BETWEEN 8 AND 2048),
    updated_at TEXT NOT NULL
) STRICT;

INSERT INTO provider_api_keys_new (provider, api_key, updated_at)
SELECT provider, api_key, updated_at FROM provider_api_keys;

DROP TABLE provider_api_keys;

ALTER TABLE provider_api_keys_new RENAME TO provider_api_keys;
