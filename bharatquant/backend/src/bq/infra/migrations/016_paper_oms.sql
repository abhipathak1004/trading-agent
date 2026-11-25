-- Paper orders, fills, positions and exact-paise trading ledger.

CREATE TABLE paper_orders (
    order_id TEXT PRIMARY KEY NOT NULL,
    account_id TEXT NOT NULL REFERENCES paper_accounts(account_id),
    instrument_id TEXT NOT NULL,
    client_order_id TEXT NOT NULL,
    side TEXT NOT NULL CHECK (side IN ('BUY', 'SELL')),
    order_type TEXT NOT NULL CHECK (order_type IN ('MARKET', 'LIMIT')),
    quantity INTEGER NOT NULL CHECK (quantity > 0),
    limit_price_paise INTEGER CHECK (limit_price_paise > 0),
    status TEXT NOT NULL CHECK (
        status IN ('PENDING', 'PARTIALLY_FILLED', 'FILLED', 'CANCELLED')
    ),
    filled_quantity INTEGER NOT NULL DEFAULT 0 CHECK (
        filled_quantity >= 0 AND filled_quantity <= quantity
    ),
    created_at TEXT NOT NULL,
    UNIQUE (account_id, client_order_id),
    CHECK ((order_type = 'LIMIT' AND limit_price_paise IS NOT NULL)
        OR (order_type = 'MARKET' AND limit_price_paise IS NULL))
) STRICT;

CREATE TABLE paper_fills (
    fill_id TEXT PRIMARY KEY NOT NULL,
    order_id TEXT NOT NULL REFERENCES paper_orders(order_id),
    quantity INTEGER NOT NULL CHECK (quantity > 0),
    price_paise INTEGER NOT NULL CHECK (price_paise > 0),
    filled_at TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    UNIQUE (order_id, idempotency_key)
) STRICT;

CREATE TABLE paper_positions (
    account_id TEXT NOT NULL REFERENCES paper_accounts(account_id),
    instrument_id TEXT NOT NULL,
    quantity INTEGER NOT NULL CHECK (quantity >= 0),
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
    PRIMARY KEY (account_id, instrument_id)
) STRICT;

CREATE TABLE paper_ledger_entries (
    entry_id TEXT PRIMARY KEY NOT NULL,
    account_id TEXT NOT NULL REFERENCES paper_accounts(account_id),
    kind TEXT NOT NULL CHECK (kind IN ('DEBIT', 'CREDIT')),
    amount_paise INTEGER NOT NULL CHECK (amount_paise > 0),
    occurred_at TEXT NOT NULL,
    reference_type TEXT NOT NULL,
    reference_id TEXT NOT NULL
) STRICT;

CREATE INDEX paper_orders_by_account
ON paper_orders (account_id, created_at);

CREATE INDEX paper_ledger_by_account
ON paper_ledger_entries (account_id, occurred_at);

CREATE TRIGGER paper_orders_no_delete
BEFORE DELETE ON paper_orders
BEGIN
    SELECT RAISE(ABORT, 'paper orders cannot be deleted');
END;

CREATE TRIGGER paper_fills_no_delete
BEFORE DELETE ON paper_fills
BEGIN
    SELECT RAISE(ABORT, 'paper fills cannot be deleted');
END;
