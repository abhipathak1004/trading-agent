-- Paper accounts and exact-paise capital movements.

CREATE TABLE paper_accounts (
    account_id TEXT PRIMARY KEY NOT NULL CHECK (length(account_id) BETWEEN 1 AND 128),
    name TEXT NOT NULL CHECK (length(name) BETWEEN 1 AND 128),
    status TEXT NOT NULL CHECK (status IN ('ACTIVE', 'PAUSED', 'ARCHIVED')),
    opening_balance_paise INTEGER NOT NULL CHECK (opening_balance_paise >= 0),
    created_at TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1)
) STRICT;

CREATE TABLE paper_account_movements (
    movement_id TEXT PRIMARY KEY NOT NULL,
    account_id TEXT NOT NULL REFERENCES paper_accounts(account_id),
    kind TEXT NOT NULL CHECK (kind IN ('DEPOSIT', 'WITHDRAWAL')),
    amount_paise INTEGER NOT NULL CHECK (amount_paise > 0),
    occurred_at TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    UNIQUE (account_id, idempotency_key)
) STRICT;

CREATE INDEX paper_account_movements_by_account
ON paper_account_movements (account_id, occurred_at);

CREATE TRIGGER paper_accounts_no_delete
BEFORE DELETE ON paper_accounts
BEGIN
    SELECT RAISE(ABORT, 'paper accounts cannot be deleted');
END;
