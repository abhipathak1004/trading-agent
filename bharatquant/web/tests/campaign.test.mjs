import assert from "node:assert/strict";
import test from "node:test";

import {
  campaignStages,
  formatPaise,
  formatR,
  parseCampaignConfig,
  parseEnvelope,
  parseReview,
  statusTone,
} from "../src/campaignContract.ts";

function item(overrides = {}) {
  return {
    symbol: "RELIANCE",
    rank: 1,
    catalyst_score: 5,
    events: [
      { event: "ORDER_WIN", strength: 5, polarity: 1, title: "wins order" },
    ],
    verdict: "CANDIDATE",
    status: "WATCHING",
    plan: { signal: "BUY", entry_price: "1715.00" },
    trigger: { level_paise: 171800, stop_paise: 169850 },
    outcome: null,
    r_multiple: null,
    ...overrides,
  };
}

function campaign(overrides = {}) {
  return {
    campaign_id: "camp_20261126",
    trade_date: "2026-11-26",
    created_at: "2026-11-26T03:45:00Z",
    as_of: "2026-11-26T03:45:00Z",
    status: "ACTIVE",
    summary: { catalysts_stored: 3 },
    items: [item()],
    ...overrides,
  };
}

test("an envelope with no campaign still yields defaults", () => {
  const parsed = parseEnvelope({ campaign: null, config: {} });
  assert.equal(parsed.campaign, null);
  assert.equal(parsed.config.top_n, 8);
  assert.equal(parsed.config.trigger_mode, "breakout");
  assert.deepEqual(parsed.config.skip_dates, []);
  assert.deepEqual(parsed.config.feed_urls, []);
  assert.equal(parsed.masterAsOf, null);
});

test("a campaign and its items parse field by field", () => {
  const parsed = parseEnvelope({
    campaign: campaign(),
    config: {
      top_n: 6,
      max_chase_bps: 80,
      feed_urls: ["https://pulse.zerodha.com/feed.xml"],
    },
    master_as_of: "2026-11-26",
  });
  assert.equal(parsed.campaign?.campaignId, "camp_20261126");
  assert.equal(parsed.campaign?.items[0].symbol, "RELIANCE");
  assert.equal(parsed.campaign?.items[0].events[0].event, "ORDER_WIN");
  assert.equal(parsed.campaign?.items[0].trigger?.level_paise, 171800);
  assert.equal(parsed.config.top_n, 6);
  assert.equal(parsed.config.max_chase_bps, 80);
  assert.deepEqual(parsed.config.feed_urls, [
    "https://pulse.zerodha.com/feed.xml",
  ]);
  assert.equal(parsed.masterAsOf, "2026-11-26");
});

test("config parsing keeps defaults for malformed fields", () => {
  const config = parseCampaignConfig({
    top_n: "eight",
    equity_paise: -1,
    valid_until: 1430,
    skip_dates: ["2026-01-26", 7],
  });
  assert.equal(config.top_n, 8);
  assert.equal(config.valid_until, "14:30");
  assert.deepEqual(config.skip_dates, ["2026-01-26"]);
});

test("stages reflect the campaign's progress", () => {
  const pending = campaignStages(null);
  assert.equal(pending.every((stage) => stage.state === "pending"), true);

  const watched = campaignStages(
    parseEnvelope({ campaign: campaign(), config: {} }).campaign,
  );
  assert.equal(watched[0].state, "done");
  assert.equal(watched[1].detail, "1 name(s)");
  assert.equal(watched[3].state, "active");
});

test("paise, R and status helpers format deterministically", () => {
  assert.equal(formatPaise(171800), "₹1,718.00");
  assert.equal(formatPaise("100000"), "₹1,000.00");
  assert.equal(formatPaise(null), "—");
  assert.equal(formatR(1.25), "+1.25R");
  assert.equal(formatR(-1), "-1.00R");
  assert.equal(formatR(null), "—");
  assert.equal(statusTone("FILLED"), "ok");
  assert.equal(statusTone("WATCHING"), "warn");
  assert.equal(statusTone("BLOCKED"), "danger");
  assert.equal(statusTone("SOMETHING"), "muted");
});

test("review parsing tolerates missing history", () => {
  const review = parseReview({
    campaign: campaign(),
    items: [item({ status: "CLOSED", r_multiple: 1.5 })],
    realized_pnl_paise: 9000,
    event_stats: [
      { event: "ORDER_WIN", closed: 2, wins: 1, hit_rate: 0.5, avg_r: 0.25 },
    ],
    weights: { ORDER_WIN: 1.1 },
    open_positions: [{ symbol: "RELIANCE", quantity: 10 }],
  });
  assert.equal(review.realizedPaise, 9000);
  assert.equal(review.eventStats[0].hitRate, 0.5);
  assert.equal(review.weights.ORDER_WIN, 1.1);
  assert.equal(review.openPositions[0].quantity, 10);

  const empty = parseReview({});
  assert.equal(empty.campaign, null);
  assert.deepEqual(empty.eventStats, []);
});
