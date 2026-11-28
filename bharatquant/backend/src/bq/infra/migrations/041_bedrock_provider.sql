-- Admit AWS Bedrock as a provider key (ADR-051).
--
-- provider_api_keys has a CHECK constraint on the provider name, and SQLite
-- cannot alter a CHECK, so the table is rebuilt — the same copy/drop/rename as
-- 026. No foreign key references it and it has no triggers, so every stored key
-- survives. The migration runner wraps this file in one transaction.
--
-- Bedrock authenticates with a bearer token (AWS_BEARER_TOKEN_BEDROCK), so the
-- stored key is that token; it is not an AWS secret access key and no SigV4
-- signing happens in this application.

CREATE TABLE provider_api_keys_new (
    provider TEXT PRIMARY KEY NOT NULL CHECK (
        provider IN ('GEMINI', 'DEEPSEEK', 'OPENAI', 'CUSTOM', 'TYPESAFE_JEV', 'BEDROCK')
    ),
    api_key TEXT NOT NULL CHECK (length(api_key) BETWEEN 8 AND 2048),
    updated_at TEXT NOT NULL
) STRICT;

INSERT INTO provider_api_keys_new (provider, api_key, updated_at)
SELECT provider, api_key, updated_at FROM provider_api_keys;

DROP TABLE provider_api_keys;

ALTER TABLE provider_api_keys_new RENAME TO provider_api_keys;
