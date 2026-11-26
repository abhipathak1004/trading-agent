import assert from "node:assert/strict";
import test from "node:test";

import {
  parseExecution,
  parsePaperAccounts,
  parsePaperBook,
  parsePaperFill,
  parsePaperOrder,
  parseReconciliation,
} from "../src/executionContract.ts";

const account = {
  account_id: "paper-main",
  name: "Paper Main",
  status: "ACTIVE",
  opening_balance_paise: "1000000",
  deposited_paise: "0",
  withdrawn_paise: "0",
  balance_paise: "945000",
};

const order = {
  order_id: "order-1",
  account_id: "paper-main",
  instrument_id: "NSE:RELIANCE",
  client_order_id: "intent-1",
  side: "BUY",
  order_type: "LIMIT",
  quantity: 10,
  limit_price_paise: "10000",
  status: "PENDING",
  filled_quantity: 0,
  created_at: "2026-11-25T10:00:00Z",
};

const execution = {
  mode: "PAPER",
  state: "ALLOW",
  vetoes: [],
  freshness: { usable: true, label: "delayed_warn", age_seconds: 300 },
  context: {
    account_id: "paper-main",
    account_name: "Paper Main",
    available_cash_paise: "945000",
    balance_paise: "945000",
    open_positions: 1,
    symbol_exposure_paise: "50000",
    daily_loss_paise: "5000",
    weekly_loss_paise: "5000",
    monthly_loss_paise: "5000",
    orders_this_minute: 0,
    kill_switch: "NONE",
    instrument_valid: true,
    session_open: true,
    session_state: "UNCOVERED",
    session_source: "owner_override",
    notes: ["Paper only."],
    positions: [{ instrument_id: "NSE:RELIANCE", quantity: 5 }],
  },
  order: null,
};

test("a paper account list parses", () => {
  const parsed = parsePaperAccounts({ accounts: [account] });
  assert.equal(parsed[0].account_id, "paper-main");
  assert.equal(parsed[0].balance_paise, "945000");
});

test("a paper book parses positions and orders", () => {
  const parsed = parsePaperBook({
    account,
    positions: [{ instrument_id: "NSE:RELIANCE", quantity: 5 }],
    orders: [order],
  });
  assert.equal(parsed.positions[0].quantity, 5);
  assert.equal(parsed.orders[0].limit_price_paise, "10000");
});

test("an execution result parses an ALLOW and a block", () => {
  const allowed = parseExecution(execution);
  assert.equal(allowed.state, "ALLOW");
  assert.equal(allowed.context.session_source, "owner_override");
  assert.equal(allowed.order, null);

  const blocked = parseExecution({
    ...execution,
    state: "BLOCK",
    vetoes: ["session_closed"],
  });
  assert.equal(blocked.state, "BLOCK");
  assert.deepEqual(blocked.vetoes, ["session_closed"]);
});

test("an execution result with a placed order parses", () => {
  const parsed = parseExecution({ ...execution, order });
  assert.equal(parsed.order?.quantity, 10);
  assert.equal(parsed.order?.status, "PENDING");
});

test("bad execution contracts are rejected", () => {
  assert.throws(() => parseExecution({ ...execution, state: "MAYBE" }));
  assert.throws(() =>
    parseExecution({
      ...execution,
      context: { ...execution.context, available_cash_paise: "-1" },
    }),
  );
  assert.throws(() => parsePaperBook({ account, positions: [], orders: [{}] }));
});

test("a fill result parses the updated order and the fill", () => {
  const parsed = parsePaperFill({
    order: {
      ...order,
      status: "PARTIALLY_FILLED",
      filled_quantity: 4,
    },
    fill: {
      fill_id: "fill-1",
      order_id: "order-1",
      quantity: 4,
      price_paise: "10000",
      filled_at: "2026-11-25T10:01:00Z",
      idempotency_key: "fill-1",
    },
  });
  assert.equal(parsed.order.status, "PARTIALLY_FILLED");
  assert.equal(parsed.order.filled_quantity, 4);
  assert.equal(parsed.fill.price_paise, "10000");
});

test("a reconciliation result parses", () => {
  const parsed = parseReconciliation({
    account_id: "paper-main",
    order_count: 1,
    fill_count: 2,
    ledger_debit_paise: "100000",
    ledger_credit_paise: "0",
    position_count: 1,
  });
  assert.equal(parsed.fill_count, 2);
  assert.equal(parsed.ledger_debit_paise, "100000");
});

test("bad fill and reconciliation contracts are rejected", () => {
  assert.throws(() =>
    parsePaperFill({
      order,
      fill: {
        fill_id: "fill-1",
        order_id: "order-1",
        quantity: 0,
        price_paise: "10000",
        filled_at: "2026-11-25T10:01:00Z",
        idempotency_key: "fill-1",
      },
    }),
  );
  assert.throws(() =>
    parseReconciliation({
      account_id: "paper-main",
      order_count: 1,
      fill_count: 2,
      ledger_debit_paise: "-1",
      ledger_credit_paise: "0",
      position_count: 1,
    }),
  );
});

test("a standalone order response parses for cancel and exit", () => {
  const parsed = parsePaperOrder({
    ...order,
    side: "SELL",
    status: "CANCELLED",
  });
  assert.equal(parsed.side, "SELL");
  assert.equal(parsed.status, "CANCELLED");
});
