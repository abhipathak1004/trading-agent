import { useEffect, useState } from "react";
import type { FormEvent } from "react";

import {
  parseExecution,
  parsePaperAccounts,
  parsePaperBook,
  parsePaperFill,
  parsePaperOrder,
  parseReconciliation,
} from "./executionContract.ts";
import type {
  ExecutionResult,
  PaperAccount,
  PaperBook,
  PaperOrder,
  PaperPosition,
  PaperReconciliation,
} from "./executionContract.ts";
import ExecutionRealismPanel from "./ExecutionRealismPanel.tsx";
import { parseRiskLimitsPreset } from "./riskLimitsContract.ts";
import type { StrategyPlan } from "./strategyContract.ts";

type PreviewState =
  | { state: "idle" | "loading" }
  | { state: "error"; message: string }
  | { state: "ready"; result: ExecutionResult };

const KILL_SWITCHES = [
  "NONE",
  "PAUSE_STRATEGY",
  "FLATTEN_STRATEGY",
  "PAUSE_HORIZON",
  "HALT_ACCOUNT",
  "FLATTEN_ALL",
] as const;

function rupeesToPaise(value: string): number | null {
  if (value.trim() === "") {
    return null;
  }
  const amount = Number(value);
  if (!Number.isFinite(amount) || amount < 0) {
    return null;
  }
  return Math.round(amount * 100);
}

function positiveInteger(value: string): number | null {
  const amount = Number(value);
  if (!Number.isInteger(amount) || amount < 1) {
    return null;
  }
  return amount;
}

function formatPaise(value: string | number): string {
  const amount = typeof value === "number" ? value : Number(value);
  return `₹${(amount / 100).toLocaleString("en-IN", {
    maximumFractionDigits: 2,
  })}`;
}

function formatSignedPaise(value: string): string {
  const amount = Number(value);
  const sign = amount < 0 ? "-" : "";
  return `${sign}₹${(Math.abs(amount) / 100).toLocaleString("en-IN", {
    maximumFractionDigits: 2,
  })}`;
}

function newClientOrderId(): string {
  return `execution-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
}

export default function TradePanel({
  csrfToken,
  stagedPlan,
}: {
  csrfToken: string;
  stagedPlan: StrategyPlan | null;
}) {
  const [accounts, setAccounts] = useState<PaperAccount[]>([]);
  const [accountId, setAccountId] = useState("");
  const [book, setBook] = useState<PaperBook | null>(null);
  const [planJson, setPlanJson] = useState("");
  const [maxSymbolExposure, setMaxSymbolExposure] = useState("");
  const [maxOpenPositions, setMaxOpenPositions] = useState("");
  const [maxDailyLoss, setMaxDailyLoss] = useState("");
  const [maxWeeklyLoss, setMaxWeeklyLoss] = useState("");
  const [maxMonthlyLoss, setMaxMonthlyLoss] = useState("");
  const [maxOrderRate, setMaxOrderRate] = useState("");
  const [killSwitch, setKillSwitch] = useState("NONE");
  const [sessionOverride, setSessionOverride] = useState("");
  const [preview, setPreview] = useState<PreviewState>({ state: "idle" });
  const [previewBody, setPreviewBody] = useState<Record<string, unknown> | null>(
    null,
  );
  const [clientOrderId, setClientOrderId] = useState("");
  const [product, setProduct] = useState<"CAS" | "MIS">("MIS");
  const [requireApproval, setRequireApproval] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [fillPrice, setFillPrice] = useState("");
  const [reconciliation, setReconciliation] =
    useState<PaperReconciliation | null>(null);
  const [bookRefresh, setBookRefresh] = useState(0);
  const [exitQuantities, setExitQuantities] = useState<Record<string, string>>(
    {},
  );
  const [exitPrices, setExitPrices] = useState<Record<string, string>>({});
  const [presetConfigured, setPresetConfigured] = useState(false);
  const [newAccountId, setNewAccountId] = useState("paper-main");
  const [newAccountName, setNewAccountName] = useState("Paper Main");
  const [newAccountBalance, setNewAccountBalance] = useState("10000");
  const [creating, setCreating] = useState(false);

  useEffect(() => {
    if (stagedPlan !== null) {
      setPlanJson(JSON.stringify(stagedPlan, null, 2));
    }
  }, [stagedPlan]);

  useEffect(() => {
    const controller = new AbortController();
    async function loadRiskPreset() {
      try {
        const response = await fetch("/api/v1/settings/risk-limits", {
          cache: "no-store",
          signal: controller.signal,
        });
        if (!response.ok) {
          return;
        }
        const preset = parseRiskLimitsPreset(await response.json());
        if (!preset.configured) {
          return;
        }
        const asRupees = (value: number | null): string =>
          value === null ? "" : String(value / 100);
        setMaxSymbolExposure(asRupees(preset.max_symbol_exposure_paise));
        setMaxOpenPositions(String(preset.max_open_positions ?? ""));
        setMaxDailyLoss(asRupees(preset.max_daily_loss_paise));
        setMaxWeeklyLoss(asRupees(preset.max_weekly_loss_paise));
        setMaxMonthlyLoss(asRupees(preset.max_monthly_loss_paise));
        setMaxOrderRate(String(preset.max_order_rate_per_minute ?? ""));
        setPresetConfigured(true);
      } catch {
        // A preset is optional; the owner can still fill the fields by hand.
      }
    }
    void loadRiskPreset();
    return () => controller.abort();
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    async function loadAccounts() {
      try {
        const response = await fetch("/api/v1/paper/accounts", {
          cache: "no-store",
          signal: controller.signal,
        });
        if (!response.ok) {
          throw new Error("Paper accounts unavailable");
        }
        const parsed = parsePaperAccounts(await response.json());
        setAccounts(parsed);
        setAccountId((current) => current || parsed[0]?.account_id || "");
      } catch {
        if (!controller.signal.aborted) {
          setError("Paper accounts could not be loaded.");
        }
      }
    }
    void loadAccounts();
    return () => controller.abort();
  }, []);

  useEffect(() => {
    if (accountId === "") {
      setBook(null);
      return;
    }
    const controller = new AbortController();
    async function loadBook() {
      try {
        const response = await fetch(
          `/api/v1/paper/accounts/${encodeURIComponent(accountId)}/book`,
          { cache: "no-store", signal: controller.signal },
        );
        if (!response.ok) {
          throw new Error("Paper book unavailable");
        }
        setBook(parsePaperBook(await response.json()));
      } catch {
        if (!controller.signal.aborted) {
          setBook(null);
        }
      }
    }
    void loadBook();
    return () => controller.abort();
  }, [accountId, preview.state, bookRefresh]);

  useEffect(() => {
    if (accountId === "") {
      setReconciliation(null);
      return;
    }
    const controller = new AbortController();
    async function loadReconciliation() {
      try {
        const response = await fetch(
          `/api/v1/paper/accounts/${encodeURIComponent(accountId)}/reconciliation`,
          { cache: "no-store", signal: controller.signal },
        );
        if (!response.ok) {
          throw new Error("Reconciliation unavailable");
        }
        setReconciliation(parseReconciliation(await response.json()));
      } catch {
        if (!controller.signal.aborted) {
          setReconciliation(null);
        }
      }
    }
    void loadReconciliation();
    return () => controller.abort();
  }, [accountId, bookRefresh]);

  async function fillOrder(order: PaperOrder) {
    const remaining = order.quantity - order.filled_quantity;
    if (remaining <= 0) {
      return;
    }
    let price = order.limit_price_paise === null
      ? null
      : Number(order.limit_price_paise);
    if (fillPrice.trim() !== "") {
      price = rupeesToPaise(fillPrice);
    }
    if (price === null || price <= 0) {
      setError("A positive fill price is required.");
      return;
    }
    setError("");
    setNotice("");
    try {
      const response = await fetch(
        `/api/v1/paper/orders/${encodeURIComponent(order.order_id)}/fill`,
        {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-CSRF-Token": csrfToken,
          },
          body: JSON.stringify({
            quantity: remaining,
            price_paise: price,
            idempotency_key: `fill-${order.order_id}-${order.filled_quantity}`,
          }),
        },
      );
      if (!response.ok) {
        let message = "The fill was rejected.";
        try {
          const detail = (await response.json()) as { detail?: unknown };
          if (typeof detail.detail === "string") {
            message = detail.detail;
          }
        } catch {
          // Keep the generic message.
        }
        setError(message);
        return;
      }
      const result = parsePaperFill(await response.json());
      const realized =
        result.fill.realized_pnl_paise === null
          ? ""
          : ` · gross P&L ${formatSignedPaise(result.fill.realized_pnl_paise)}`;
      const net =
        result.fill.net_pnl_paise === null
          ? ""
          : ` · net ${formatSignedPaise(result.fill.net_pnl_paise)}`;
      setNotice(
        `${result.order.side === "SELL" ? "Sold" : "Bought"} ${
          result.fill.quantity
        } ${result.order.instrument_id} at ${formatPaise(
          result.fill.price_paise,
        )} · charges ${formatPaise(result.fill.charges_paise)}${realized}${net}; order ${result.order.status.toLowerCase()} (${result.order.product}).`,
      );
      setBookRefresh((value) => value + 1);
    } catch {
      setError("The fill was rejected.");
    }
  }

  async function reconcileAccount() {
    if (accountId === "") {
      return;
    }
    setError("");
    try {
      const response = await fetch(
        `/api/v1/paper/accounts/${encodeURIComponent(accountId)}/reconciliation`,
        { cache: "no-store" },
      );
      if (!response.ok) {
        throw new Error("Reconciliation failed");
      }
      setReconciliation(parseReconciliation(await response.json()));
      setNotice("Paper book reconciled.");
    } catch {
      setError("The paper book could not be reconciled.");
    }
  }

  async function cancelOrder(order: PaperOrder) {
    setError("");
    setNotice("");
    try {
      const response = await fetch(
        `/api/v1/paper/orders/${encodeURIComponent(order.order_id)}/cancel`,
        {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-CSRF-Token": csrfToken,
          },
        },
      );
      if (!response.ok) {
        let message = "The order could not be cancelled.";
        try {
          const detail = (await response.json()) as { detail?: unknown };
          if (typeof detail.detail === "string") {
            message = detail.detail;
          }
        } catch {
          // Keep the generic message.
        }
        setError(message);
        return;
      }
      const cancelled = parsePaperOrder(await response.json());
      setNotice(`${cancelled.instrument_id} order cancelled.`);
      setBookRefresh((value) => value + 1);
    } catch {
      setError("The order could not be cancelled.");
    }
  }

  async function exitPosition(position: PaperPosition) {
    const quantity = Number(
      exitQuantities[position.instrument_id] ?? String(position.quantity),
    );
    const price = rupeesToPaise(exitPrices[position.instrument_id] ?? "");
    if (!Number.isInteger(quantity) || quantity < 1) {
      setError("Exit quantity must be a positive integer.");
      return;
    }
    if (quantity > position.quantity) {
      setError("Exit quantity cannot exceed the held position.");
      return;
    }
    if (price === null || price <= 0) {
      setError("An explicit positive exit price is required.");
      return;
    }
    setError("");
    setNotice("");
    try {
      const response = await fetch(
        `/api/v1/paper/accounts/${encodeURIComponent(
          accountId,
        )}/positions/${encodeURIComponent(position.instrument_id)}/exit`,
        {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-CSRF-Token": csrfToken,
          },
          body: JSON.stringify({
            quantity,
            price_paise: price,
            client_order_id: `exit-${position.instrument_id}-${Date.now()}`,
          }),
        },
      );
      if (!response.ok) {
        let message = "The exit order was rejected.";
        try {
          const detail = (await response.json()) as { detail?: unknown };
          if (typeof detail.detail === "string") {
            message = detail.detail;
          }
        } catch {
          // Keep the generic message.
        }
        setError(message);
        return;
      }
      const order = parsePaperOrder(await response.json());
      setExitQuantities((current) => {
        const next = { ...current };
        delete next[position.instrument_id];
        return next;
      });
      setExitPrices((current) => {
        const next = { ...current };
        delete next[position.instrument_id];
        return next;
      });
      setNotice(
        `Sell order for ${order.quantity} ${order.instrument_id} placed at ${formatPaise(
          order.limit_price_paise ?? "0",
        )}; fill it below.`,
      );
      setBookRefresh((value) => value + 1);
    } catch {
      setError("The exit order was rejected.");
    }
  }

  async function createAccount(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const opening = rupeesToPaise(newAccountBalance);
    if (!newAccountId.trim() || !newAccountName.trim() || opening === null || opening <= 0) {
      setError("Account id, name and a positive opening balance are required.");
      return;
    }
    setCreating(true);
    setError("");
    try {
      const response = await fetch("/api/v1/paper/accounts", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": csrfToken,
        },
        body: JSON.stringify({
          account_id: newAccountId.trim(),
          name: newAccountName.trim(),
          opening_balance_paise: opening,
        }),
      });
      if (!response.ok) {
        throw new Error("Paper account could not be created");
      }
      const list = await fetch("/api/v1/paper/accounts", { cache: "no-store" });
      if (list.ok) {
        const parsed = parsePaperAccounts(await list.json());
        setAccounts(parsed);
        setAccountId(newAccountId.trim());
      }
    } catch {
      setError("Paper account could not be created.");
    } finally {
      setCreating(false);
    }
  }

  function buildBody(): Record<string, unknown> | null {
    if (!accountId) {
      setError("Select a paper account first.");
      return null;
    }
    let plan: unknown;
    try {
      plan = JSON.parse(planJson);
    } catch {
      setError("The plan must be valid JSON.");
      return null;
    }
    if (typeof plan !== "object" || plan === null || Array.isArray(plan)) {
      setError("The plan must be a JSON object.");
      return null;
    }
    const exposure = rupeesToPaise(maxSymbolExposure);
    const daily = rupeesToPaise(maxDailyLoss);
    const weekly = rupeesToPaise(maxWeeklyLoss);
    const monthly = rupeesToPaise(maxMonthlyLoss);
    const positions = positiveInteger(maxOpenPositions);
    const rate = positiveInteger(maxOrderRate);
    if (
      exposure === null ||
      daily === null ||
      weekly === null ||
      monthly === null ||
      positions === null ||
      rate === null
    ) {
      setError(
        "Every risk limit is required; paise limits cannot be negative.",
      );
      return null;
    }
    setError("");
    return {
      account_id: accountId,
      plan,
      mode: "PAPER",
      limits: {
        max_symbol_exposure_paise: exposure,
        max_open_positions: positions,
        max_daily_loss_paise: daily,
        max_weekly_loss_paise: weekly,
        max_monthly_loss_paise: monthly,
        max_order_rate_per_minute: rate,
      },
      kill_switch: killSwitch,
      session_open_override:
        sessionOverride === "" ? null : sessionOverride === "true",
    };
  }

  async function runPreview(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const body = buildBody();
    if (body === null) {
      return;
    }
    setPreview({ state: "loading" });
    setError("");
    try {
      const response = await fetch("/api/v1/execution/preview", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": csrfToken,
        },
        body: JSON.stringify(body),
      });
      if (!response.ok) {
        let message = "The gate preview failed.";
        try {
          const detail = (await response.json()) as { detail?: unknown };
          if (typeof detail.detail === "string") {
            message = detail.detail;
          }
        } catch {
          // Keep the generic message.
        }
        setPreview({ state: "error", message });
        return;
      }
      const result = parseExecution(await response.json());
      setPreviewBody(body);
      setClientOrderId(newClientOrderId());
      setPreview({ state: "ready", result });
    } catch {
      setPreview({ state: "error", message: "The gate preview failed." });
    }
  }

  async function placeOrder() {
    if (
      preview.state !== "ready" ||
      preview.result.state !== "ALLOW" ||
      previewBody === null ||
      clientOrderId === ""
    ) {
      return;
    }
    setPreview({ state: "loading" });
    setError("");
    try {
      const response = await fetch("/api/v1/execution/paper", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": csrfToken,
        },
        body: JSON.stringify({
          ...previewBody,
          client_order_id: clientOrderId,
          product,
          require_approval: requireApproval,
        }),
      });
      if (!response.ok) {
        let message = "The paper order was refused.";
        try {
          const detail = (await response.json()) as { detail?: unknown };
          if (typeof detail.detail === "string") {
            message = detail.detail;
          }
        } catch {
          // Keep the generic message.
        }
        setPreview({ state: "error", message });
        return;
      }
      setPreview({ state: "ready", result: parseExecution(await response.json()) });
    } catch {
      setPreview({ state: "error", message: "The paper order was refused." });
    }
  }

  const result = preview.state === "ready" ? preview.result : null;

  return (
    <section className="settings-panel" aria-labelledby="trade-title">
      <h2 id="trade-title">Paper execution gate</h2>
      <p className="panel-note">
        A plan runs through the deterministic pre-trade gate and becomes a paper
        order only on ALLOW. Nothing here reaches a broker; Kite sandbox and live
        modes fail closed.
      </p>

      {accounts.length === 0 ? (
        <form className="config-form" onSubmit={(event) => void createAccount(event)}>
          <h3>Create the first paper account</h3>
          <div className="models-grid models-grid--compact">
            <label htmlFor="trade-new-id">
              Account id
              <input
                id="trade-new-id"
                value={newAccountId}
                onChange={(event) => setNewAccountId(event.target.value)}
              />
            </label>
            <label htmlFor="trade-new-name">
              Name
              <input
                id="trade-new-name"
                value={newAccountName}
                onChange={(event) => setNewAccountName(event.target.value)}
              />
            </label>
            <label htmlFor="trade-new-balance">
              Opening balance (₹)
              <input
                id="trade-new-balance"
                type="number"
                min="1"
                value={newAccountBalance}
                onChange={(event) => setNewAccountBalance(event.target.value)}
              />
            </label>
          </div>
          <button type="submit" className="auth-button" disabled={creating}>
            {creating ? "Creating..." : "Create paper account"}
          </button>
        </form>
      ) : (
        <>
          <div className="models-grid models-grid--compact">
            <label htmlFor="trade-account">
              Paper account
              <select
                id="trade-account"
                value={accountId}
                onChange={(event) => setAccountId(event.target.value)}
              >
                {accounts.map((account) => (
                  <option key={account.account_id} value={account.account_id}>
                    {account.name} ({account.account_id})
                  </option>
                ))}
              </select>
            </label>
          </div>

          {book !== null && (
            <div className="strategy-result">
              <dl className="settings-facts">
                <div>
                  <dt>Cash</dt>
                  <dd>{formatPaise(book.account.balance_paise)}</dd>
                </div>
                <div>
                  <dt>Positions</dt>
                  <dd>{book.positions.length}</dd>
                </div>
                <div>
                  <dt>Orders</dt>
                  <dd>{book.orders.length}</dd>
                </div>
              </dl>
              <p className="panel-note">
                <button
                  type="button"
                  className="text-button"
                  onClick={() => void reconcileAccount()}
                >
                  Reconcile paper book
                </button>
              </p>
              {book.positions.length > 0 && (
                <table className="judgment-table">
                  <thead>
                    <tr>
                      <th scope="col">Instrument</th>
                      <th scope="col">Quantity</th>
                      <th scope="col">Exit qty</th>
                      <th scope="col">Exit price (₹)</th>
                      <th scope="col">Action</th>
                    </tr>
                  </thead>
                  <tbody>
                    {book.positions.map((position) => (
                      <tr key={position.instrument_id}>
                        <th scope="row">{position.instrument_id}</th>
                        <td>{position.quantity}</td>
                        <td>
                          <input
                            aria-label={`Exit quantity for ${position.instrument_id}`}
                            type="number"
                            min="1"
                            max={position.quantity}
                            value={
                              exitQuantities[position.instrument_id] ??
                              String(position.quantity)
                            }
                            onChange={(event) =>
                              setExitQuantities((current) => ({
                                ...current,
                                [position.instrument_id]: event.target.value,
                              }))
                            }
                          />
                        </td>
                        <td>
                          <input
                            aria-label={`Exit price for ${position.instrument_id}`}
                            type="number"
                            min="0"
                            step="0.01"
                            value={exitPrices[position.instrument_id] ?? ""}
                            onChange={(event) =>
                              setExitPrices((current) => ({
                                ...current,
                                [position.instrument_id]: event.target.value,
                              }))
                            }
                          />
                        </td>
                        <td>
                          <button
                            type="button"
                            className="text-button"
                            onClick={() => void exitPosition(position)}
                          >
                            Exit position
                          </button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
              {reconciliation !== null && (
                <p className="panel-note">
                  Reconciled: {reconciliation.order_count} order(s),{" "}
                  {reconciliation.fill_count} fill(s),{" "}
                  {reconciliation.position_count} open position(s), debits{" "}
                  {formatPaise(reconciliation.ledger_debit_paise)}, credits{" "}
                  {formatPaise(reconciliation.ledger_credit_paise)}.{" "}
                  <button
                    type="button"
                    className="text-button"
                    onClick={() => void reconcileAccount()}
                  >
                    Reconcile again
                  </button>
                </p>
              )}
              {book.orders.length > 0 && (
                <>
                  <h3>Recent paper orders</h3>
                  <div className="models-grid models-grid--compact">
                    <label htmlFor="trade-fill-price">
                      Fill price override (₹, optional)
                      <input
                        id="trade-fill-price"
                        type="number"
                        min="0"
                        step="0.01"
                        value={fillPrice}
                        onChange={(event) => setFillPrice(event.target.value)}
                        placeholder="Blank = order limit"
                      />
                    </label>
                  </div>
                  <table className="judgment-table">
                    <thead>
                      <tr>
                        <th scope="col">Instrument</th>
                        <th scope="col">Side</th>
                        <th scope="col">Filled / Qty</th>
                        <th scope="col">Limit</th>
                        <th scope="col">Status</th>
                        <th scope="col">Action</th>
                      </tr>
                    </thead>
                    <tbody>
                      {book.orders.map((order) => (
                        <tr key={order.order_id}>
                          <th scope="row">{order.instrument_id}</th>
                          <td>{order.side}</td>
                          <td>
                            {order.filled_quantity} / {order.quantity}
                          </td>
                          <td>
                            {order.limit_price_paise === null
                              ? "market"
                              : formatPaise(order.limit_price_paise)}
                          </td>
                          <td>{order.status}</td>
                          <td>
                            {order.status === "PENDING" ||
                            order.status === "PARTIALLY_FILLED" ? (
                              <>
                                <button
                                  type="button"
                                  className="text-button"
                                  onClick={() => void fillOrder(order)}
                                >
                                  Fill at limit
                                </button>{" "}
                                <button
                                  type="button"
                                  className="text-button"
                                  onClick={() => void cancelOrder(order)}
                                >
                                  Cancel
                                </button>
                              </>
                            ) : (
                              "—"
                            )}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </>
              )}
              <ExecutionRealismPanel accountId={accountId} csrfToken={csrfToken} />
            </div>
          )}
        </>
      )}

      <form className="config-form" onSubmit={(event) => void runPreview(event)}>
        <label htmlFor="trade-plan">Trade plan JSON</label>
        <textarea
          id="trade-plan"
          rows={12}
          value={planJson}
          onChange={(event) => setPlanJson(event.target.value)}
          placeholder='{"signal":"BUY","symbol":"NSE:RELIANCE", ...}'
        />
        <p className="panel-note">
          Stage an accepted Strategy plan here, or paste a plan from a Technical
          run. The server re-validates every field.
        </p>

        <h3>Risk limits (required)</h3>
        {presetConfigured && (
          <p className="panel-note">
            Limits loaded from Settings. Every field remains editable for this run.
          </p>
        )}
        <div className="models-grid models-grid--compact">
          <label htmlFor="trade-symbol-exposure">
            Max symbol exposure (₹)
            <input
              id="trade-symbol-exposure"
              type="number"
              min="0"
              value={maxSymbolExposure}
              onChange={(event) => setMaxSymbolExposure(event.target.value)}
            />
          </label>
          <label htmlFor="trade-open-positions">
            Max open positions
            <input
              id="trade-open-positions"
              type="number"
              min="1"
              value={maxOpenPositions}
              onChange={(event) => setMaxOpenPositions(event.target.value)}
            />
          </label>
          <label htmlFor="trade-daily-loss">
            Max daily loss (₹)
            <input
              id="trade-daily-loss"
              type="number"
              min="0"
              value={maxDailyLoss}
              onChange={(event) => setMaxDailyLoss(event.target.value)}
            />
          </label>
          <label htmlFor="trade-weekly-loss">
            Max weekly loss (₹)
            <input
              id="trade-weekly-loss"
              type="number"
              min="0"
              value={maxWeeklyLoss}
              onChange={(event) => setMaxWeeklyLoss(event.target.value)}
            />
          </label>
          <label htmlFor="trade-monthly-loss">
            Max monthly loss (₹)
            <input
              id="trade-monthly-loss"
              type="number"
              min="0"
              value={maxMonthlyLoss}
              onChange={(event) => setMaxMonthlyLoss(event.target.value)}
            />
          </label>
          <label htmlFor="trade-order-rate">
            Max orders / minute
            <input
              id="trade-order-rate"
              type="number"
              min="1"
              value={maxOrderRate}
              onChange={(event) => setMaxOrderRate(event.target.value)}
            />
          </label>
          <label htmlFor="trade-kill-switch">
            Kill switch
            <select
              id="trade-kill-switch"
              value={killSwitch}
              onChange={(event) => setKillSwitch(event.target.value)}
            >
              {KILL_SWITCHES.map((value) => (
                <option key={value} value={value}>
                  {value}
                </option>
              ))}
            </select>
          </label>
          <label htmlFor="trade-session">
            Session
            <select
              id="trade-session"
              value={sessionOverride}
              onChange={(event) => setSessionOverride(event.target.value)}
            >
              <option value="">Use reviewed calendar</option>
              <option value="true">Owner override: open</option>
              <option value="false">Owner override: closed</option>
            </select>
          </label>
        </div>

        <div className="models-grid models-grid--compact">
          <label htmlFor="trade-product">
            Paper product
            <select
              id="trade-product"
              value={product}
              onChange={(event) => setProduct(event.target.value as "CAS" | "MIS")}
            >
              <option value="MIS">MIS · intraday, same-day sells</option>
              <option value="CAS">CAS · delivery, T+1 sells</option>
            </select>
          </label>
          <label htmlFor="trade-approval">
            Owner approval
            <span>
              <input
                id="trade-approval"
                type="checkbox"
                checked={requireApproval}
                onChange={(event) => setRequireApproval(event.target.checked)}
              />{" "}
              Require approval before this order can fill
            </span>
          </label>
        </div>

        <div className="strategy-examples">
          <button
            type="submit"
            className="auth-button"
            disabled={preview.state === "loading"}
          >
            {preview.state === "loading" ? "Checking gate..." : "Preview gate"}
          </button>
          <button
            type="button"
            className="auth-button"
            onClick={() => void placeOrder()}
            disabled={
              result === null ||
              result.state !== "ALLOW" ||
              preview.state === "loading"
            }
          >
            Place paper order
          </button>
        </div>
      </form>

      {error !== "" && (
        <p className="auth-error" role="alert">
          {error}
        </p>
      )}
      {notice !== "" && (
        <p className="panel-note" role="status">
          {notice}
        </p>
      )}
      {preview.state === "error" && (
        <p className="auth-error" role="alert">
          {preview.message}
        </p>
      )}

      {result !== null && (
        <div className="strategy-result">
          <p className="decision-preview">
            Gate <strong>{result.state}</strong> · {result.mode} · freshness{" "}
            {result.freshness.label} ({Math.round(result.freshness.age_seconds)}s)
          </p>
          {result.vetoes.length > 0 && (
            <p className="auth-error">Vetoes: {result.vetoes.join(", ")}</p>
          )}
          <dl className="settings-facts">
            <div>
              <dt>Cash</dt>
              <dd>{formatPaise(result.context.available_cash_paise)}</dd>
            </div>
            <div>
              <dt>Open positions</dt>
              <dd>{result.context.open_positions}</dd>
            </div>
            <div>
              <dt>Symbol exposure</dt>
              <dd>{formatPaise(result.context.symbol_exposure_paise)}</dd>
            </div>
            <div>
              <dt>Daily / weekly / monthly loss</dt>
              <dd>
                {formatPaise(result.context.daily_loss_paise)} /{" "}
                {formatPaise(result.context.weekly_loss_paise)} /{" "}
                {formatPaise(result.context.monthly_loss_paise)}
              </dd>
            </div>
            <div>
              <dt>Session</dt>
              <dd>
                {result.context.session_state} ({result.context.session_source})
              </dd>
            </div>
            <div>
              <dt>Kill switch</dt>
              <dd>{result.context.kill_switch}</dd>
            </div>
          </dl>
          <ul className="task-list">
            {result.context.notes.map((note) => (
              <li key={note}>{note}</li>
            ))}
          </ul>
          {result.order !== null && (
            <>
              <h3>Paper order</h3>
              <table className="judgment-table">
                <thead>
                  <tr>
                    <th scope="col">Order</th>
                    <th scope="col">Instrument</th>
                    <th scope="col">Side</th>
                    <th scope="col">Qty</th>
                    <th scope="col">Limit</th>
                    <th scope="col">Status</th>
                  </tr>
                </thead>
                <tbody>
                  <tr>
                    <th scope="row">{result.order.order_id}</th>
                    <td>{result.order.instrument_id}</td>
                    <td>{result.order.side}</td>
                    <td>{result.order.quantity}</td>
                    <td>
                      {result.order.limit_price_paise === null
                        ? "market"
                        : formatPaise(result.order.limit_price_paise)}
                    </td>
                    <td>{result.order.status}</td>
                  </tr>
                </tbody>
              </table>
            </>
          )}
        </div>
      )}
    </section>
  );
}
