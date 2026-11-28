import { isRecord } from "./contracts.ts";

export type CampaignEventView = {
  event: string;
  strength: number;
  polarity: number;
  title: string;
};

export type CampaignItemView = {
  symbol: string;
  rank: number;
  catalystScore: number;
  events: CampaignEventView[];
  verdict: string;
  status: string;
  plan: Record<string, unknown> | null;
  trigger: Record<string, unknown> | null;
  outcome: Record<string, unknown> | null;
  rMultiple: number | null;
};

export type CampaignConfigView = {
  enabled: boolean;
  top_n: number;
  equity_paise: number;
  available_cash_paise: number;
  risk_basis_points: number;
  max_open_positions: number;
  trigger_mode: string;
  max_chase_bps: number;
  valid_until: string;
  min_catalyst_strength: number;
  min_price_paise: number;
  min_avg_volume: number;
  analysis_depth: string;
  news_lookback_hours: number;
  trigger_staleness_seconds: number;
  feed_urls: string[];
  skip_dates: string[];
  account_id: string;
  horizon_days: number;
};

export type CampaignView = {
  campaignId: string;
  tradeDate: string;
  createdAt: string;
  asOf: string;
  status: string;
  summary: Record<string, unknown>;
  items: CampaignItemView[];
};

export type CampaignEnvelope = {
  campaign: CampaignView | null;
  config: CampaignConfigView;
  masterAsOf: string | null;
};

export type EventStatView = {
  event: string;
  closed: number;
  wins: number;
  hitRate: number;
  avgR: number;
};

export type ReviewView = {
  campaign: CampaignView | null;
  items: CampaignItemView[];
  realizedPaise: number;
  eventStats: EventStatView[];
  weights: Record<string, number>;
  openPositions: { symbol: string; quantity: number }[];
};

export type StageView = {
  key: string;
  label: string;
  state: "done" | "active" | "pending";
  detail: string;
};

function record(value: unknown): Record<string, unknown> | null {
  return isRecord(value) ? value : null;
}

function num(value: unknown, fallback: number): number {
  return typeof value === "number" && Number.isFinite(value) ? value : fallback;
}

function text(value: unknown, fallback: string): string {
  return typeof value === "string" ? value : fallback;
}

function bool(value: unknown, fallback: boolean): boolean {
  return typeof value === "boolean" ? value : fallback;
}

function events(value: unknown): CampaignEventView[] {
  if (!Array.isArray(value)) {
    return [];
  }
  return value.filter(isRecord).map((item) => ({
    event: text(item.event, "UNKNOWN"),
    strength: num(item.strength, 0),
    polarity: num(item.polarity, 0),
    title: text(item.title, ""),
  }));
}

export function parseCampaignConfig(value: unknown): CampaignConfigView {
  const item = record(value) ?? {};
  const feeds = Array.isArray(item.feed_urls)
    ? item.feed_urls.filter((entry): entry is string => typeof entry === "string")
    : [];
  const skip = Array.isArray(item.skip_dates)
    ? item.skip_dates.filter((entry): entry is string => typeof entry === "string")
    : [];
  return {
    enabled: bool(item.enabled, true),
    top_n: num(item.top_n, 8),
    equity_paise: num(item.equity_paise, 100_000_000),
    available_cash_paise: num(item.available_cash_paise, 90_000_000),
    risk_basis_points: num(item.risk_basis_points, 100),
    max_open_positions: num(item.max_open_positions, 4),
    trigger_mode: text(item.trigger_mode, "breakout"),
    max_chase_bps: num(item.max_chase_bps, 50),
    valid_until: text(item.valid_until, "14:30"),
    min_catalyst_strength: num(item.min_catalyst_strength, 3),
    min_price_paise: num(item.min_price_paise, 2000),
    min_avg_volume: num(item.min_avg_volume, 100_000),
    analysis_depth: text(item.analysis_depth, "deterministic"),
    news_lookback_hours: num(item.news_lookback_hours, 36),
    trigger_staleness_seconds: num(item.trigger_staleness_seconds, 1800),
    feed_urls: feeds,
    skip_dates: skip,
    account_id: text(item.account_id, "paper-main"),
    horizon_days: num(item.horizon_days, 5),
  };
}

function parseItem(value: unknown): CampaignItemView | null {
  const item = record(value);
  if (item === null) {
    return null;
  }
  return {
    symbol: text(item.symbol, ""),
    rank: num(item.rank, 0),
    catalystScore: num(item.catalyst_score, 0),
    events: events(item.events),
    verdict: text(item.verdict, "UNKNOWN"),
    status: text(item.status, "UNKNOWN"),
    plan: record(item.plan),
    trigger: record(item.trigger),
    outcome: record(item.outcome),
    rMultiple:
      typeof item.r_multiple === "number" ? item.r_multiple : null,
  };
}

export function parseCampaign(value: unknown): CampaignView | null {
  const item = record(value);
  if (item === null) {
    return null;
  }
  const items = Array.isArray(item.items)
    ? item.items
        .map(parseItem)
        .filter((entry): entry is CampaignItemView => entry !== null)
    : [];
  return {
    campaignId: text(item.campaign_id, ""),
    tradeDate: text(item.trade_date, ""),
    createdAt: text(item.created_at, ""),
    asOf: text(item.as_of, ""),
    status: text(item.status, "UNKNOWN"),
    summary: record(item.summary) ?? {},
    items,
  };
}

export function parseEnvelope(value: unknown): CampaignEnvelope {
  const item = record(value) ?? {};
  return {
    campaign: parseCampaign(item.campaign),
    config: parseCampaignConfig(item.config),
    masterAsOf: typeof item.master_as_of === "string" ? item.master_as_of : null,
  };
}

export function parseReview(value: unknown): ReviewView {
  const item = record(value) ?? {};
  const stats = Array.isArray(item.event_stats)
    ? item.event_stats.filter(isRecord).map((stat) => ({
        event: text(stat.event, ""),
        closed: num(stat.closed, 0),
        wins: num(stat.wins, 0),
        hitRate: num(stat.hit_rate, 0),
        avgR: num(stat.avg_r, 0),
      }))
    : [];
  const weights: Record<string, number> = {};
  if (isRecord(item.weights)) {
    for (const [key, entry] of Object.entries(item.weights)) {
      if (typeof entry === "number") {
        weights[key] = entry;
      }
    }
  }
  const openPositions = Array.isArray(item.open_positions)
    ? item.open_positions.filter(isRecord).map((position) => ({
        symbol: text(position.symbol, ""),
        quantity: num(position.quantity, 0),
      }))
    : [];
  const items = Array.isArray(item.items)
    ? item.items
        .map(parseItem)
        .filter((entry): entry is CampaignItemView => entry !== null)
    : [];
  return {
    campaign: parseCampaign(item.campaign),
    items,
    realizedPaise: num(item.realized_pnl_paise, 0),
    eventStats: stats,
    weights,
    openPositions,
  };
}

export function formatPaise(value: unknown): string {
  const amount =
    typeof value === "number"
      ? value
      : typeof value === "string" && /^-?\d+$/.test(value)
        ? Number(value)
        : null;
  if (amount === null || !Number.isFinite(amount)) {
    return "—";
  }
  return `₹${(amount / 100).toLocaleString("en-IN", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })}`;
}

export function formatR(value: number | null): string {
  return value === null ? "—" : `${value >= 0 ? "+" : ""}${value.toFixed(2)}R`;
}

export function statusTone(status: string): "ok" | "warn" | "danger" | "muted" {
  if (["FILLED", "CLOSED", "ACTIVE", "CANDIDATE"].includes(status)) {
    return "ok";
  }
  if (["WATCHING", "READY", "TRIGGERED", "APPROVAL_PENDING"].includes(status)) {
    return "warn";
  }
  if (
    ["BLOCKED", "MISSED_CHASE", "EXPIRED", "CANCELLED", "ERROR", "REJECTED"].includes(
      status,
    )
  ) {
    return "danger";
  }
  return "muted";
}

export function campaignStages(campaign: CampaignView | null): StageView[] {
  const pending: StageView[] = [
    { key: "news", label: "News & catalysts", state: "pending", detail: "not run" },
    { key: "shortlist", label: "Shortlist", state: "pending", detail: "0 names" },
    { key: "analysis", label: "Technical plans", state: "pending", detail: "0 plans" },
    { key: "watch", label: "Trigger watch", state: "pending", detail: "0 watching" },
    { key: "fills", label: "Paper fills", state: "pending", detail: "0 filled" },
    { key: "review", label: "5pm review", state: "pending", detail: "evening report" },
  ];
  if (campaign === null) {
    return pending;
  }
  const catalysts = num(campaign.summary.catalysts_stored, 0);
  const plans = campaign.items.filter((item) => item.plan !== null).length;
  const watching = campaign.items.filter((item) => item.status === "WATCHING").length;
  const filled = campaign.items.filter((item) =>
    ["FILLED", "CLOSED"].includes(item.status),
  ).length;
  return [
    {
      key: "news",
      label: "News & catalysts",
      state: catalysts > 0 ? "done" : "active",
      detail: `${catalysts} catalyst(s)`,
    },
    {
      key: "shortlist",
      label: "Shortlist",
      state: campaign.items.length > 0 ? "done" : "pending",
      detail: `${campaign.items.length} name(s)`,
    },
    {
      key: "analysis",
      label: "Technical plans",
      state: plans > 0 ? "done" : "pending",
      detail: `${plans} plan(s)`,
    },
    {
      key: "watch",
      label: "Trigger watch",
      state: watching > 0 ? "active" : filled > 0 ? "done" : "pending",
      detail: `${watching} watching`,
    },
    {
      key: "fills",
      label: "Paper fills",
      state: filled > 0 ? "done" : "pending",
      detail: `${filled} filled`,
    },
    {
      key: "review",
      label: "5pm review",
      state: campaign.status === "CLOSED" ? "done" : "pending",
      detail: "evening report",
    },
  ];
}
