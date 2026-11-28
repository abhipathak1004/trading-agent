-- Execution realism: product, charges, tax lots, approvals and stops.

ALTER TABLE paper_orders
ADD COLUMN product TEXT NOT NULL DEFAULT 'MIS' CHECK (product IN ('CAS', 'MIS'));

ALTER TABLE paper_orders
ADD COLUMN approval_status TEXT NOT NULL DEFAULT 'APPROVED'
CHECK (approval_status IN ('APPROVED', 'PENDING', 'REJECTED'));

ALTER TABLE paper_fills ADD COLUMN gross_paise INTEGER NOT NULL DEFAULT 0;
ALTER TABLE paper_fills ADD COLUMN charges_paise INTEGER NOT NULL DEFAULT 0;
ALTER TABLE paper_fills ADD COLUMN net_pnl_paise INTEGER;

CREATE TABLE paper_charges (
    charge_id TEXT PRIMARY KEY NOT NULL,
    fill_id TEXT NOT NULL REFERENCES paper_fills(fill_id),
    code TEXT NOT NULL,
    amount_paise INTEGER NOT NULL CHECK (amount_paise >= 0),
    UNIQUE (fill_id, code)
) STRICT;

CREATE TABLE paper_tax_lots (
    lot_id TEXT PRIMARY KEY NOT NULL,
    account_id TEXT NOT NULL REFERENCES paper_accounts(account_id),
    instrument_id TEXT NOT NULL,
    product TEXT NOT NULL CHECK (product IN ('CAS', 'MIS')),
    buy_fill_id TEXT NOT NULL REFERENCES paper_fills(fill_id),
    quantity INTEGER NOT NULL CHECK (quantity > 0),
    remaining_quantity INTEGER NOT NULL CHECK (
        remaining_quantity >= 0 AND remaining_quantity <= quantity
    ),
    cost_paise INTEGER NOT NULL CHECK (cost_paise >= 0),
    charges_paise INTEGER NOT NULL CHECK (charges_paise >= 0),
    acquired_at TEXT NOT NULL,
    settles_on TEXT NOT NULL
) STRICT;

CREATE INDEX paper_tax_lots_by_position
ON paper_tax_lots (account_id, instrument_id, product, settles_on);

CREATE TABLE paper_product_positions (
    account_id TEXT NOT NULL REFERENCES paper_accounts(account_id),
    instrument_id TEXT NOT NULL,
    product TEXT NOT NULL CHECK (product IN ('CAS', 'MIS')),
    quantity INTEGER NOT NULL CHECK (quantity >= 0),
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
    PRIMARY KEY (account_id, instrument_id, product)
) STRICT;

INSERT OR IGNORE INTO paper_product_positions
    (account_id, instrument_id, product, quantity, revision)
SELECT account_id, instrument_id, 'MIS', quantity, revision
FROM paper_positions
WHERE quantity > 0;

CREATE TABLE paper_approvals (
    approval_id TEXT PRIMARY KEY NOT NULL,
    order_id TEXT NOT NULL UNIQUE REFERENCES paper_orders(order_id),
    account_id TEXT NOT NULL REFERENCES paper_accounts(account_id),
    status TEXT NOT NULL CHECK (status IN ('PENDING', 'APPROVED', 'REJECTED')),
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL,
    decided_at TEXT,
    decision_note TEXT
) STRICT;

CREATE INDEX paper_approvals_by_account
ON paper_approvals (account_id, status, created_at);

CREATE TABLE paper_stops (
    stop_id TEXT PRIMARY KEY NOT NULL,
    account_id TEXT NOT NULL REFERENCES paper_accounts(account_id),
    instrument_id TEXT NOT NULL,
    product TEXT NOT NULL CHECK (product IN ('CAS', 'MIS')),
    quantity INTEGER NOT NULL CHECK (quantity > 0),
    stop_price_paise INTEGER NOT NULL CHECK (stop_price_paise > 0),
    target_price_paise INTEGER CHECK (target_price_paise > 0),
    status TEXT NOT NULL CHECK (status IN ('ACTIVE', 'TRIGGERED', 'CANCELLED')),
    trigger_order_id TEXT,
    trigger_kind TEXT CHECK (trigger_kind IN ('STOP', 'TARGET')),
    created_at TEXT NOT NULL,
    triggered_at TEXT
) STRICT;

CREATE INDEX paper_stops_by_account
ON paper_stops (account_id, status, created_at);
