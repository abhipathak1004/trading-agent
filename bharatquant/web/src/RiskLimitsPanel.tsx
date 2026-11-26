import { useEffect, useState } from "react";
import type { FormEvent } from "react";

import { parseRiskLimitsPreset } from "./riskLimitsContract.ts";
import type { RiskLimitsPreset } from "./riskLimitsContract.ts";

type LoadState =
  | { state: "loading" | "error" }
  | { state: "ready"; preset: RiskLimitsPreset };

type SaveState =
  | { state: "idle" | "saving" }
  | { state: "saved"; message: string }
  | { state: "failed"; message: string };

type Fields = {
  maxSymbolExposure: string;
  maxOpenPositions: string;
  maxDailyLoss: string;
  maxWeeklyLoss: string;
  maxMonthlyLoss: string;
  maxOrderRate: string;
};

const EMPTY: Fields = {
  maxSymbolExposure: "",
  maxOpenPositions: "",
  maxDailyLoss: "",
  maxWeeklyLoss: "",
  maxMonthlyLoss: "",
  maxOrderRate: "",
};

function rupees(paise: number | null): string {
  if (paise === null) {
    return "";
  }
  return String(paise / 100);
}

function fromPreset(preset: RiskLimitsPreset): Fields {
  if (!preset.configured) {
    return EMPTY;
  }
  return {
    maxSymbolExposure: rupees(preset.max_symbol_exposure_paise),
    maxOpenPositions: String(preset.max_open_positions),
    maxDailyLoss: rupees(preset.max_daily_loss_paise),
    maxWeeklyLoss: rupees(preset.max_weekly_loss_paise),
    maxMonthlyLoss: rupees(preset.max_monthly_loss_paise),
    maxOrderRate: String(preset.max_order_rate_per_minute),
  };
}

function toPaise(value: string): number | null {
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

export default function RiskLimitsPanel({ csrfToken }: { csrfToken: string }) {
  const [load, setLoad] = useState<LoadState>({ state: "loading" });
  const [fields, setFields] = useState<Fields>(EMPTY);
  const [save, setSave] = useState<SaveState>({ state: "idle" });

  useEffect(() => {
    const controller = new AbortController();
    async function loadPreset() {
      try {
        const response = await fetch("/api/v1/settings/risk-limits", {
          cache: "no-store",
          signal: controller.signal,
        });
        if (!response.ok) {
          throw new Error("Risk limits unavailable");
        }
        const preset = parseRiskLimitsPreset(await response.json());
        setLoad({ state: "ready", preset });
        setFields(fromPreset(preset));
      } catch {
        if (!controller.signal.aborted) {
          setLoad({ state: "error" });
        }
      }
    }
    void loadPreset();
    return () => controller.abort();
  }, []);

  function update(patch: Partial<Fields>) {
    setFields({ ...fields, ...patch });
  }

  function body(): Record<string, number> | null {
    const exposure = toPaise(fields.maxSymbolExposure);
    const daily = toPaise(fields.maxDailyLoss);
    const weekly = toPaise(fields.maxWeeklyLoss);
    const monthly = toPaise(fields.maxMonthlyLoss);
    const positions = positiveInteger(fields.maxOpenPositions);
    const rate = positiveInteger(fields.maxOrderRate);
    if (
      exposure === null ||
      daily === null ||
      weekly === null ||
      monthly === null ||
      positions === null ||
      rate === null
    ) {
      setSave({
        state: "failed",
        message: "Every limit is required and must be a valid non-negative value.",
      });
      return null;
    }
    return {
      max_symbol_exposure_paise: exposure,
      max_open_positions: positions,
      max_daily_loss_paise: daily,
      max_weekly_loss_paise: weekly,
      max_monthly_loss_paise: monthly,
      max_order_rate_per_minute: rate,
    };
  }

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const payload = body();
    if (payload === null) {
      return;
    }
    setSave({ state: "saving" });
    try {
      const response = await fetch("/api/v1/settings/risk-limits", {
        method: "PUT",
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": csrfToken,
        },
        body: JSON.stringify(payload),
      });
      if (!response.ok) {
        let message = "Saving risk limits failed.";
        try {
          const detail = (await response.json()) as { detail?: unknown };
          if (typeof detail.detail === "string") {
            message = detail.detail;
          }
        } catch {
          // Keep the generic message.
        }
        setSave({ state: "failed", message });
        return;
      }
      const preset = parseRiskLimitsPreset(await response.json());
      setLoad({ state: "ready", preset });
      setFields(fromPreset(preset));
      setSave({ state: "saved", message: "Risk limits saved." });
    } catch {
      setSave({ state: "failed", message: "Saving risk limits failed." });
    }
  }

  async function reset() {
    setSave({ state: "saving" });
    try {
      const response = await fetch("/api/v1/settings/risk-limits", {
        method: "DELETE",
        headers: { "X-CSRF-Token": csrfToken },
      });
      if (!response.ok) {
        throw new Error("Reset failed");
      }
      const preset = parseRiskLimitsPreset(await response.json());
      setLoad({ state: "ready", preset });
      setFields(EMPTY);
      setSave({ state: "saved", message: "Risk limits reset." });
    } catch {
      setSave({ state: "failed", message: "Resetting risk limits failed." });
    }
  }

  if (load.state === "loading") {
    return (
      <section className="settings-panel" aria-label="Risk limits">
        <p role="status">Loading risk limits...</p>
      </section>
    );
  }
  if (load.state === "error") {
    return (
      <section className="settings-panel" aria-label="Risk limits">
        <p role="alert">Risk limits are unavailable.</p>
      </section>
    );
  }

  const configured = "preset" in load && load.preset.configured;

  return (
    <section className="settings-panel" aria-labelledby="risk-limits-title">
      <h2 id="risk-limits-title">Paper execution risk limits</h2>
      <p className="panel-note">
        A saved preset pre-fills the Trade tab. Every preview and placement still
        sends these limits explicitly, and the owner can override them for one run.
        {configured ? " A preset is configured." : " No preset is configured."}
      </p>

      <form className="config-form" onSubmit={(event) => void submit(event)}>
        <div className="models-grid models-grid--compact">
          <label htmlFor="risk-symbol-exposure">
            Max symbol exposure (₹)
            <input
              id="risk-symbol-exposure"
              type="number"
              min="0"
              value={fields.maxSymbolExposure}
              onChange={(event) => update({ maxSymbolExposure: event.target.value })}
            />
          </label>
          <label htmlFor="risk-open-positions">
            Max open positions
            <input
              id="risk-open-positions"
              type="number"
              min="1"
              value={fields.maxOpenPositions}
              onChange={(event) => update({ maxOpenPositions: event.target.value })}
            />
          </label>
          <label htmlFor="risk-daily-loss">
            Max daily loss (₹)
            <input
              id="risk-daily-loss"
              type="number"
              min="0"
              value={fields.maxDailyLoss}
              onChange={(event) => update({ maxDailyLoss: event.target.value })}
            />
          </label>
          <label htmlFor="risk-weekly-loss">
            Max weekly loss (₹)
            <input
              id="risk-weekly-loss"
              type="number"
              min="0"
              value={fields.maxWeeklyLoss}
              onChange={(event) => update({ maxWeeklyLoss: event.target.value })}
            />
          </label>
          <label htmlFor="risk-monthly-loss">
            Max monthly loss (₹)
            <input
              id="risk-monthly-loss"
              type="number"
              min="0"
              value={fields.maxMonthlyLoss}
              onChange={(event) => update({ maxMonthlyLoss: event.target.value })}
            />
          </label>
          <label htmlFor="risk-order-rate">
            Max orders / minute
            <input
              id="risk-order-rate"
              type="number"
              min="1"
              value={fields.maxOrderRate}
              onChange={(event) => update({ maxOrderRate: event.target.value })}
            />
          </label>
        </div>

        <div className="strategy-examples">
          <button
            type="submit"
            className="auth-button"
            disabled={save.state === "saving"}
          >
            {save.state === "saving" ? "Saving..." : "Save risk limits"}
          </button>
          <button
            type="button"
            className="text-button"
            onClick={() => void reset()}
            disabled={save.state === "saving" || !configured}
          >
            Reset preset
          </button>
        </div>
        {save.state === "failed" && (
          <p className="auth-error" role="alert">
            {save.message}
          </p>
        )}
        {save.state === "saved" && (
          <p className="panel-note" role="status">
            {save.message}
          </p>
        )}
      </form>
    </section>
  );
}
