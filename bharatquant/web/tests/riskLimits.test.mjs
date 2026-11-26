import assert from "node:assert/strict";
import test from "node:test";

import { parseRiskLimitsPreset } from "../src/riskLimitsContract.ts";

const configured = {
  configured: true,
  max_symbol_exposure_paise: 500_000,
  max_open_positions: 5,
  max_daily_loss_paise: 50_000,
  max_weekly_loss_paise: 100_000,
  max_monthly_loss_paise: 200_000,
  max_order_rate_per_minute: 10,
};

test("an absent preset parses as unconfigured", () => {
  const parsed = parseRiskLimitsPreset({
    configured: false,
    max_symbol_exposure_paise: null,
    max_open_positions: null,
    max_daily_loss_paise: null,
    max_weekly_loss_paise: null,
    max_monthly_loss_paise: null,
    max_order_rate_per_minute: null,
  });
  assert.equal(parsed.configured, false);
  assert.equal(parsed.max_symbol_exposure_paise, null);
});

test("a configured preset parses", () => {
  const parsed = parseRiskLimitsPreset(configured);
  assert.equal(parsed.configured, true);
  assert.equal(parsed.max_order_rate_per_minute, 10);
});

test("invalid presets are rejected", () => {
  assert.throws(() =>
    parseRiskLimitsPreset({ ...configured, max_symbol_exposure_paise: -1 }),
  );
  assert.throws(() => parseRiskLimitsPreset({ configured: true }));
});
