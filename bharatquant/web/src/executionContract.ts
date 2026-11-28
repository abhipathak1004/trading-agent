import { isCount, isRecord, isTimestamp } from "./contracts.ts";

export type PaperAccount = {
  account_id: string;
  name: string;
  status: "ACTIVE" | "PAUSED" | "ARCHIVED";
  opening_balance_paise: string;
  deposited_paise: string;
  withdrawn_paise: string;
  balance_paise: string;
};

export type PaperPosition = {
  instrument_id: string;
  quantity: number;
};

export type PaperOrder = {
  order_id: string;
  account_id: string;
  instrument_id: string;
  client_order_id: string;
  side: string;
  order_type: string;
  quantity: number;
  limit_price_paise: string | null;
  status: string;
  filled_quantity: number;
  created_at: string;
  product: "CAS" | "MIS";
  approval_status: string;
};

export type PaperBook = {
  account: PaperAccount;
  positions: PaperPosition[];
  orders: PaperOrder[];
};

export type PaperFill = {
  fill_id: string;
  order_id: string;
  quantity: number;
  price_paise: string;
  filled_at: string;
  idempotency_key: string;
  realized_pnl_paise: string | null;
  gross_paise: string;
  charges_paise: string;
  net_pnl_paise: string | null;
};

export type PaperFillResult = {
  order: PaperOrder;
  fill: PaperFill;
};

export type PaperReconciliation = {
  account_id: string;
  order_count: number;
  fill_count: number;
  ledger_debit_paise: string;
  ledger_credit_paise: string;
  position_count: number;
  charges_paise: string;
};

export type PaperApproval = {
  approval_id: string;
  order_id: string;
  account_id: string;
  status: string;
  reason: string;
  created_at: string;
  decided_at: string | null;
  decision_note: string | null;
};

export type PaperProductPosition = {
  instrument_id: string;
  product: "CAS" | "MIS";
  quantity: number;
};

export type PaperTaxLot = {
  lot_id: string;
  instrument_id: string;
  product: "CAS" | "MIS";
  quantity: number;
  remaining_quantity: number;
  settles_on: string;
  cost_paise: string;
  charges_paise: string;
};

export type PaperStop = {
  stop_id: string;
  account_id: string;
  instrument_id: string;
  product: "CAS" | "MIS";
  quantity: number;
  stop_price_paise: string;
  target_price_paise: string | null;
  status: string;
  trigger_order_id: string | null;
  trigger_kind: string | null;
  created_at: string;
  triggered_at: string | null;
};

export type PaperRealism = {
  account_id: string;
  product_positions: PaperProductPosition[];
  tax_lots: PaperTaxLot[];
  approvals: PaperApproval[];
  stops: PaperStop[];
  charges_paise: string;
};

export type ExecutionFreshness = {
  usable: boolean;
  label: string;
  age_seconds: number;
};

export type ExecutionContextView = {
  account_id: string;
  account_name: string;
  available_cash_paise: string;
  balance_paise: string;
  open_positions: number;
  symbol_exposure_paise: string;
  daily_loss_paise: string;
  weekly_loss_paise: string;
  monthly_loss_paise: string;
  orders_this_minute: number;
  kill_switch: string;
  instrument_valid: boolean;
  session_open: boolean;
  session_state: string;
  session_source: string;
  notes: string[];
  positions: PaperPosition[];
};

export type ExecutionResult = {
  mode: string;
  state: "ALLOW" | "BLOCK";
  vetoes: string[];
  freshness: ExecutionFreshness;
  context: ExecutionContextView;
  order: PaperOrder | null;
};

function paise(value: unknown): string {
  if (typeof value !== "string" || !/^\d+$/.test(value)) {
    throw new Error("Invalid paise value");
  }
  return value;
}

function stringList(value: unknown): string[] {
  if (!Array.isArray(value) || !value.every((item) => typeof item === "string")) {
    throw new Error("Invalid string list");
  }
  return value;
}

function parseAccount(value: unknown): PaperAccount {
  if (
    !isRecord(value) ||
    typeof value.account_id !== "string" ||
    typeof value.name !== "string" ||
    (value.status !== "ACTIVE" &&
      value.status !== "PAUSED" &&
      value.status !== "ARCHIVED")
  ) {
    throw new Error("Invalid paper account");
  }
  return {
    account_id: value.account_id,
    name: value.name,
    status: value.status,
    opening_balance_paise: paise(value.opening_balance_paise),
    deposited_paise: paise(value.deposited_paise),
    withdrawn_paise: paise(value.withdrawn_paise),
    balance_paise: paise(value.balance_paise),
  };
}

function parsePosition(value: unknown): PaperPosition {
  if (
    !isRecord(value) ||
    typeof value.instrument_id !== "string" ||
    !isCount(value.quantity)
  ) {
    throw new Error("Invalid paper position");
  }
  return {
    instrument_id: value.instrument_id,
    quantity: value.quantity,
  };
}

function parseOrder(value: unknown): PaperOrder {
  if (
    !isRecord(value) ||
    typeof value.order_id !== "string" ||
    typeof value.account_id !== "string" ||
    typeof value.instrument_id !== "string" ||
    typeof value.client_order_id !== "string" ||
    typeof value.side !== "string" ||
    typeof value.order_type !== "string" ||
    !isCount(value.quantity) ||
    (value.limit_price_paise !== null &&
      typeof value.limit_price_paise !== "string") ||
    typeof value.status !== "string" ||
    !isCount(value.filled_quantity) ||
    !isTimestamp(value.created_at) ||
    (value.product !== "CAS" && value.product !== "MIS") ||
    typeof value.approval_status !== "string"
  ) {
    throw new Error("Invalid paper order");
  }
  return {
    order_id: value.order_id,
    account_id: value.account_id,
    instrument_id: value.instrument_id,
    client_order_id: value.client_order_id,
    side: value.side,
    order_type: value.order_type,
    quantity: value.quantity,
    limit_price_paise:
      value.limit_price_paise === null ? null : paise(value.limit_price_paise),
    status: value.status,
    filled_quantity: value.filled_quantity,
    created_at: value.created_at,
    product: value.product,
    approval_status: value.approval_status,
  };
}

export function parsePaperOrder(value: unknown): PaperOrder {
  return parseOrder(value);
}

export function parsePaperAccounts(value: unknown): PaperAccount[] {
  if (!isRecord(value) || !Array.isArray(value.accounts)) {
    throw new Error("Invalid paper account list");
  }
  return value.accounts.map(parseAccount);
}

export function parsePaperBook(value: unknown): PaperBook {
  if (
    !isRecord(value) ||
    !Array.isArray(value.positions) ||
    !Array.isArray(value.orders)
  ) {
    throw new Error("Invalid paper book");
  }
  return {
    account: parseAccount(value.account),
    positions: value.positions.map(parsePosition),
    orders: value.orders.map(parseOrder),
  };
}

function parseContext(value: unknown): ExecutionContextView {
  if (
    !isRecord(value) ||
    typeof value.account_id !== "string" ||
    typeof value.account_name !== "string" ||
    !isCount(value.open_positions) ||
    !isCount(value.orders_this_minute) ||
    typeof value.kill_switch !== "string" ||
    typeof value.instrument_valid !== "boolean" ||
    typeof value.session_open !== "boolean" ||
    typeof value.session_state !== "string" ||
    typeof value.session_source !== "string" ||
    !Array.isArray(value.positions)
  ) {
    throw new Error("Invalid execution context");
  }
  return {
    account_id: value.account_id,
    account_name: value.account_name,
    available_cash_paise: paise(value.available_cash_paise),
    balance_paise: paise(value.balance_paise),
    open_positions: value.open_positions,
    symbol_exposure_paise: paise(value.symbol_exposure_paise),
    daily_loss_paise: paise(value.daily_loss_paise),
    weekly_loss_paise: paise(value.weekly_loss_paise),
    monthly_loss_paise: paise(value.monthly_loss_paise),
    orders_this_minute: value.orders_this_minute,
    kill_switch: value.kill_switch,
    instrument_valid: value.instrument_valid,
    session_open: value.session_open,
    session_state: value.session_state,
    session_source: value.session_source,
    notes: stringList(value.notes),
    positions: value.positions.map(parsePosition),
  };
}

export function parseExecution(value: unknown): ExecutionResult {
  if (
    !isRecord(value) ||
    (value.state !== "ALLOW" && value.state !== "BLOCK") ||
    typeof value.mode !== "string" ||
    !isRecord(value.freshness) ||
    typeof value.freshness.usable !== "boolean" ||
    typeof value.freshness.label !== "string" ||
    typeof value.freshness.age_seconds !== "number"
  ) {
    throw new Error("Invalid execution result");
  }
  return {
    mode: value.mode,
    state: value.state,
    vetoes: stringList(value.vetoes),
    freshness: {
      usable: value.freshness.usable,
      label: value.freshness.label,
      age_seconds: value.freshness.age_seconds,
    },
    context: parseContext(value.context),
    order: value.order === null ? null : parseOrder(value.order),
  };
}

export function parsePaperFill(value: unknown): PaperFillResult {
  if (!isRecord(value)) {
    throw new Error("Invalid paper fill");
  }
  const order = parseOrder(value.order);
  const fill = value.fill;
  if (
    !isRecord(fill) ||
    typeof fill.fill_id !== "string" ||
    typeof fill.order_id !== "string" ||
    !isCount(fill.quantity) ||
    fill.quantity < 1 ||
    !isTimestamp(fill.filled_at) ||
    typeof fill.idempotency_key !== "string" ||
    (fill.realized_pnl_paise !== null &&
      (typeof fill.realized_pnl_paise !== "string" ||
        !/^-?\d+$/.test(fill.realized_pnl_paise))) ||
    (fill.net_pnl_paise !== null &&
      (typeof fill.net_pnl_paise !== "string" ||
        !/^-?\d+$/.test(fill.net_pnl_paise))) ||
    typeof fill.gross_paise !== "string" ||
    typeof fill.charges_paise !== "string"
  ) {
    throw new Error("Invalid paper fill");
  }
  return {
    order,
    fill: {
      fill_id: fill.fill_id,
      order_id: fill.order_id,
      quantity: fill.quantity,
      price_paise: paise(fill.price_paise),
      filled_at: fill.filled_at,
      idempotency_key: fill.idempotency_key,
      realized_pnl_paise: fill.realized_pnl_paise,
      gross_paise: paise(fill.gross_paise),
      charges_paise: paise(fill.charges_paise),
      net_pnl_paise: fill.net_pnl_paise,
    },
  };
}

export function parseReconciliation(value: unknown): PaperReconciliation {
  if (
    !isRecord(value) ||
    typeof value.account_id !== "string" ||
    !isCount(value.order_count) ||
    !isCount(value.fill_count) ||
    !isCount(value.position_count)
  ) {
    throw new Error("Invalid paper reconciliation");
  }
  return {
    account_id: value.account_id,
    order_count: value.order_count,
    fill_count: value.fill_count,
    ledger_debit_paise: paise(value.ledger_debit_paise),
    ledger_credit_paise: paise(value.ledger_credit_paise),
    position_count: value.position_count,
    charges_paise: paise(value.charges_paise),
  };
}

function parseApproval(value: unknown): PaperApproval {
  if (
    !isRecord(value) ||
    typeof value.approval_id !== "string" ||
    typeof value.order_id !== "string" ||
    typeof value.account_id !== "string" ||
    typeof value.status !== "string" ||
    typeof value.reason !== "string" ||
    !isTimestamp(value.created_at) ||
    (value.decided_at !== null && !isTimestamp(value.decided_at)) ||
    (value.decision_note !== null && typeof value.decision_note !== "string")
  ) {
    throw new Error("Invalid paper approval");
  }
  return {
    approval_id: value.approval_id,
    order_id: value.order_id,
    account_id: value.account_id,
    status: value.status,
    reason: value.reason,
    created_at: value.created_at,
    decided_at: value.decided_at,
    decision_note: value.decision_note,
  };
}

function parseStop(value: unknown): PaperStop {
  if (
    !isRecord(value) ||
    typeof value.stop_id !== "string" ||
    typeof value.account_id !== "string" ||
    typeof value.instrument_id !== "string" ||
    (value.product !== "CAS" && value.product !== "MIS") ||
    !isCount(value.quantity) ||
    typeof value.stop_price_paise !== "string" ||
    (value.target_price_paise !== null &&
      typeof value.target_price_paise !== "string") ||
    typeof value.status !== "string" ||
    (value.trigger_order_id !== null &&
      typeof value.trigger_order_id !== "string") ||
    (value.trigger_kind !== null && typeof value.trigger_kind !== "string") ||
    !isTimestamp(value.created_at) ||
    (value.triggered_at !== null && !isTimestamp(value.triggered_at))
  ) {
    throw new Error("Invalid paper stop");
  }
  return {
    stop_id: value.stop_id,
    account_id: value.account_id,
    instrument_id: value.instrument_id,
    product: value.product,
    quantity: value.quantity,
    stop_price_paise: paise(value.stop_price_paise),
    target_price_paise:
      value.target_price_paise === null
        ? null
        : paise(value.target_price_paise),
    status: value.status,
    trigger_order_id: value.trigger_order_id,
    trigger_kind: value.trigger_kind,
    created_at: value.created_at,
    triggered_at: value.triggered_at,
  };
}

export function parsePaperRealism(value: unknown): PaperRealism {
  if (
    !isRecord(value) ||
    typeof value.account_id !== "string" ||
    !Array.isArray(value.product_positions) ||
    !Array.isArray(value.tax_lots) ||
    !Array.isArray(value.approvals) ||
    !Array.isArray(value.stops)
  ) {
    throw new Error("Invalid paper realism");
  }
  return {
    account_id: value.account_id,
    product_positions: value.product_positions.map((item) => {
      if (
        !isRecord(item) ||
        typeof item.instrument_id !== "string" ||
        (item.product !== "CAS" && item.product !== "MIS") ||
        !isCount(item.quantity)
      ) {
        throw new Error("Invalid product position");
      }
      return {
        instrument_id: item.instrument_id,
        product: item.product,
        quantity: item.quantity,
      };
    }),
    tax_lots: value.tax_lots.map((item) => {
      if (
        !isRecord(item) ||
        typeof item.lot_id !== "string" ||
        typeof item.instrument_id !== "string" ||
        (item.product !== "CAS" && item.product !== "MIS") ||
        !isCount(item.quantity) ||
        !isCount(item.remaining_quantity) ||
        typeof item.settles_on !== "string"
      ) {
        throw new Error("Invalid tax lot");
      }
      return {
        lot_id: item.lot_id,
        instrument_id: item.instrument_id,
        product: item.product,
        quantity: item.quantity,
        remaining_quantity: item.remaining_quantity,
        settles_on: item.settles_on,
        cost_paise: paise(item.cost_paise),
        charges_paise: paise(item.charges_paise),
      };
    }),
    approvals: value.approvals.map(parseApproval),
    stops: value.stops.map(parseStop),
    charges_paise: paise(value.charges_paise),
  };
}
