import { isCount, isRecord } from "./contracts.ts";

export type RiskLimitsPreset = {
  configured: boolean;
  max_symbol_exposure_paise: number | null;
  max_open_positions: number | null;
  max_daily_loss_paise: number | null;
  max_weekly_loss_paise: number | null;
  max_monthly_loss_paise: number | null;
  max_order_rate_per_minute: number | null;
};

const FIELDS = [
  "max_symbol_exposure_paise",
  "max_open_positions",
  "max_daily_loss_paise",
  "max_weekly_loss_paise",
  "max_monthly_loss_paise",
  "max_order_rate_per_minute",
] as const;

export function parseRiskLimitsPreset(value: unknown): RiskLimitsPreset {
  if (!isRecord(value) || typeof value.configured !== "boolean") {
    throw new Error("Invalid risk limits preset");
  }
  if (!value.configured) {
    return {
      configured: false,
      max_symbol_exposure_paise: null,
      max_open_positions: null,
      max_daily_loss_paise: null,
      max_weekly_loss_paise: null,
      max_monthly_loss_paise: null,
      max_order_rate_per_minute: null,
    };
  }
  const parsed = { configured: true } as RiskLimitsPreset;
  for (const field of FIELDS) {
    if (!isCount(value[field])) {
      throw new Error("Invalid risk limits preset");
    }
    parsed[field] = value[field] as number;
  }
  return parsed;
}
