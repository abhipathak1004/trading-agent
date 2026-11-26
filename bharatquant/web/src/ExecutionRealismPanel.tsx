import { useEffect, useState } from "react";
import type { FormEvent } from "react";
import TableScroll from "./TableScroll";

import { parsePaperRealism } from "./executionContract.ts";
import type { PaperRealism } from "./executionContract.ts";

function formatPaise(value: string): string {
  return `₹${(Number(value) / 100).toLocaleString("en-IN", {
    maximumFractionDigits: 2,
  })}`;
}

function rupeesToPaise(value: string): number | null {
  const amount = Number(value);
  if (!Number.isFinite(amount) || amount <= 0) {
    return null;
  }
  return Math.round(amount * 100);
}

export default function ExecutionRealismPanel({
  accountId,
  csrfToken,
}: {
  accountId: string;
  csrfToken: string;
}) {
  const [realism, setRealism] = useState<PaperRealism | null>(null);
  const [version, setVersion] = useState(0);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [instrument, setInstrument] = useState("");
  const [quantity, setQuantity] = useState("");
  const [stopPrice, setStopPrice] = useState("");
  const [targetPrice, setTargetPrice] = useState("");
  const [product, setProduct] = useState<"CAS" | "MIS">("CAS");

  useEffect(() => {
    const controller = new AbortController();
    async function load() {
      try {
        const response = await fetch(
          `/api/v1/paper/accounts/${encodeURIComponent(accountId)}/realism`,
          { cache: "no-store", signal: controller.signal },
        );
        if (!response.ok) {
          throw new Error("Execution realism is unavailable.");
        }
        setRealism(parsePaperRealism(await response.json()));
      } catch {
        if (!controller.signal.aborted) {
          setError("Execution realism is unavailable.");
        }
      }
    }
    void load();
    return () => controller.abort();
  }, [accountId, version]);

  async function decide(orderId: string, approved: boolean) {
    setError("");
    setMessage("");
    try {
      const response = await fetch(
        `/api/v1/paper/orders/${encodeURIComponent(orderId)}/approval`,
        {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-CSRF-Token": csrfToken,
          },
          body: JSON.stringify({
            approved,
            note: approved ? "Approved in Trade" : "Rejected in Trade",
          }),
        },
      );
      if (!response.ok) {
        const detail = (await response.json()) as { detail?: unknown };
        throw new Error(
          typeof detail.detail === "string"
            ? detail.detail
            : "The approval decision failed.",
        );
      }
      setMessage(approved ? "Order approved." : "Order rejected.");
      setVersion((value) => value + 1);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Approval failed.");
    }
  }

  async function cancelStop(stopId: string) {
    setError("");
    setMessage("");
    try {
      const response = await fetch(
        `/api/v1/paper/accounts/${encodeURIComponent(accountId)}/stops/${encodeURIComponent(stopId)}/cancel`,
        { method: "POST", headers: { "X-CSRF-Token": csrfToken } },
      );
      if (!response.ok) {
        throw new Error("The stop could not be cancelled.");
      }
      setMessage("Stop cancelled.");
      setVersion((value) => value + 1);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Stop failed.");
    }
  }

  async function evaluateStops() {
    setError("");
    setMessage("");
    try {
      const response = await fetch(
        `/api/v1/paper/accounts/${encodeURIComponent(accountId)}/stops/evaluate`,
        { method: "POST", headers: { "X-CSRF-Token": csrfToken } },
      );
      if (!response.ok) {
        const detail = (await response.json()) as { detail?: unknown };
        throw new Error(
          typeof detail.detail === "string"
            ? detail.detail
            : "Stop evaluation failed.",
        );
      }
      const result = (await response.json()) as {
        examined?: unknown;
        triggered?: unknown;
      };
      setMessage(
        `Evaluated ${String(result.examined ?? 0)} stop(s), triggered ${String(result.triggered ?? 0)}.`,
      );
      setVersion((value) => value + 1);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Stop evaluation failed.");
    }
  }

  async function squareOff() {
    setError("");
    setMessage("");
    try {
      const response = await fetch(
        `/api/v1/paper/accounts/${encodeURIComponent(accountId)}/square-off`,
        { method: "POST", headers: { "X-CSRF-Token": csrfToken } },
      );
      if (!response.ok) {
        const detail = (await response.json()) as { detail?: unknown };
        throw new Error(
          typeof detail.detail === "string"
            ? detail.detail
            : "MIS square-off failed.",
        );
      }
      const result = (await response.json()) as {
        examined?: unknown;
        squared_off?: unknown;
      };
      setMessage(
        `Checked ${String(result.examined ?? 0)} MIS position(s), squared off ${String(result.squared_off ?? 0)}.`,
      );
      setVersion((value) => value + 1);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "MIS square-off failed.");
    }
  }

  async function createStop(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError("");
    setMessage("");
    const parsedQuantity = Number(quantity);
    const stopPaise = rupeesToPaise(stopPrice);
    const targetPaise = targetPrice.trim() === "" ? null : rupeesToPaise(targetPrice);
    if (
      instrument.trim() === "" ||
      !Number.isInteger(parsedQuantity) ||
      parsedQuantity < 1 ||
      stopPaise === null ||
      (targetPrice.trim() !== "" && targetPaise === null)
    ) {
      setError("A symbol, positive quantity, stop and valid target are required.");
      return;
    }
    try {
      const response = await fetch(
        `/api/v1/paper/accounts/${encodeURIComponent(accountId)}/stops`,
        {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-CSRF-Token": csrfToken,
          },
          body: JSON.stringify({
            instrument_id: instrument.trim(),
            quantity: parsedQuantity,
            stop_price_paise: stopPaise,
            target_price_paise: targetPaise,
            product,
          }),
        },
      );
      if (!response.ok) {
        const detail = (await response.json()) as { detail?: unknown };
        throw new Error(
          typeof detail.detail === "string" ? detail.detail : "Stop creation failed.",
        );
      }
      setMessage("Server-side stop created.");
      setInstrument("");
      setQuantity("");
      setStopPrice("");
      setTargetPrice("");
      setVersion((value) => value + 1);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Stop creation failed.");
    }
  }

  if (realism === null) {
    return (
      <div className="strategy-result">
        <h3>Execution realism</h3>
        <p className="panel-note">Loading products, lots, approvals and stops...</p>
      </div>
    );
  }

  const pending = realism.approvals.filter((item) => item.status === "PENDING");

  return (
    <div className="strategy-result">
      <h3>Execution realism</h3>
      <p className="panel-note">
        Charges are itemized; CAS sells wait for T+1. Approvals are owner decisions
        and server-side stops are evaluated from bars by the worker.
      </p>
      <dl className="settings-facts">
        <div>
          <dt>Charges paid</dt>
          <dd>{formatPaise(realism.charges_paise)}</dd>
        </div>
        <div>
          <dt>Pending approvals</dt>
          <dd>{pending.length}</dd>
        </div>
      </dl>

      {realism.product_positions.length > 0 && (
        <TableScroll label="Product positions">
          <table className="judgment-table">
            <caption>Product positions</caption>
            <thead>
              <tr>
                <th scope="col">Instrument</th>
                <th scope="col">Product</th>
                <th scope="col">Quantity</th>
              </tr>
            </thead>
            <tbody>
              {realism.product_positions.map((item) => (
                <tr key={`${item.instrument_id}-${item.product}`}>
                  <th scope="row">{item.instrument_id}</th>
                  <td>{item.product}</td>
                  <td>{item.quantity}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </TableScroll>
      )}

      {realism.tax_lots.length > 0 && (
        <TableScroll label="Open tax lots">
          <table className="judgment-table">
            <caption>Open tax lots</caption>
            <thead>
              <tr>
                <th scope="col">Instrument</th>
                <th scope="col">Product</th>
                <th scope="col">Remaining</th>
                <th scope="col">Settles</th>
                <th scope="col">Cost</th>
              </tr>
            </thead>
            <tbody>
              {realism.tax_lots.map((lot) => (
                <tr key={lot.lot_id}>
                  <th scope="row">{lot.instrument_id}</th>
                  <td>{lot.product}</td>
                  <td>
                    {lot.remaining_quantity} / {lot.quantity}
                  </td>
                  <td>{lot.settles_on}</td>
                  <td>{formatPaise(lot.cost_paise)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </TableScroll>
      )}

      {pending.length > 0 && (
        <TableScroll label="Orders waiting for owner approval">
          <table className="judgment-table">
            <caption>Orders waiting for owner approval</caption>
            <thead>
              <tr>
                <th scope="col">Order</th>
                <th scope="col">Reason</th>
                <th scope="col">Decision</th>
              </tr>
            </thead>
            <tbody>
              {pending.map((item) => (
                <tr key={item.approval_id}>
                  <th scope="row">{item.order_id}</th>
                  <td>{item.reason}</td>
                  <td>
                    <button
                      type="button"
                      className="text-button"
                      onClick={() => void decide(item.order_id, true)}
                    >
                      Approve
                    </button>{" "}
                    <button
                      type="button"
                      className="text-button"
                      onClick={() => void decide(item.order_id, false)}
                    >
                      Reject
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </TableScroll>
      )}

      <form className="config-form" onSubmit={(event) => void createStop(event)}>
        <h4>Server-side stop / target</h4>
        <div className="models-grid models-grid--compact">
          <label htmlFor="realism-instrument">
            Instrument
            <input
              id="realism-instrument"
              value={instrument}
              onChange={(event) => setInstrument(event.target.value)}
              placeholder="NSE:RELIANCE"
            />
          </label>
          <label htmlFor="realism-quantity">
            Quantity
            <input
              id="realism-quantity"
              type="number"
              min="1"
              value={quantity}
              onChange={(event) => setQuantity(event.target.value)}
            />
          </label>
          <label htmlFor="realism-stop">
            Stop (₹)
            <input
              id="realism-stop"
              type="number"
              min="0"
              step="0.01"
              value={stopPrice}
              onChange={(event) => setStopPrice(event.target.value)}
            />
          </label>
          <label htmlFor="realism-target">
            Target (₹, optional)
            <input
              id="realism-target"
              type="number"
              min="0"
              step="0.01"
              value={targetPrice}
              onChange={(event) => setTargetPrice(event.target.value)}
            />
          </label>
          <label htmlFor="realism-product">
            Product
            <select
              id="realism-product"
              value={product}
              onChange={(event) => setProduct(event.target.value as "CAS" | "MIS")}
            >
              <option value="CAS">CAS · delivery, T+1</option>
              <option value="MIS">MIS · intraday</option>
            </select>
          </label>
        </div>
        <div className="strategy-examples">
          <button type="submit" className="auth-button">
            Create stop
          </button>
          <button
            type="button"
            className="text-button"
            onClick={() => void evaluateStops()}
          >
            Evaluate stops now
          </button>
          <button
            type="button"
            className="text-button"
            onClick={() => void squareOff()}
          >
            Square off MIS now
          </button>
        </div>
      </form>

      {realism.stops.length > 0 && (
        <TableScroll label="Server-side stops">
          <table className="judgment-table">
            <caption>Server-side stops</caption>
            <thead>
              <tr>
                <th scope="col">Instrument</th>
                <th scope="col">Qty</th>
                <th scope="col">Stop</th>
                <th scope="col">Target</th>
                <th scope="col">Status</th>
                <th scope="col">Action</th>
              </tr>
            </thead>
            <tbody>
              {realism.stops.map((item) => (
                <tr key={item.stop_id}>
                  <th scope="row">{item.instrument_id}</th>
                  <td>{item.quantity}</td>
                  <td>{formatPaise(item.stop_price_paise)}</td>
                  <td>
                    {item.target_price_paise === null
                      ? "—"
                      : formatPaise(item.target_price_paise)}
                  </td>
                  <td>
                    {item.status}
                    {item.trigger_kind === null ? "" : ` · ${item.trigger_kind}`}
                  </td>
                  <td>
                    {item.status === "ACTIVE" ? (
                      <button
                        type="button"
                        className="text-button"
                        onClick={() => void cancelStop(item.stop_id)}
                      >
                        Cancel
                      </button>
                    ) : (
                      "—"
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </TableScroll>
      )}

      {message !== "" && (
        <p className="panel-note" role="status">
          {message}
        </p>
      )}
      {error !== "" && (
        <p className="auth-error" role="alert">
          {error}
        </p>
      )}
    </div>
  );
}
