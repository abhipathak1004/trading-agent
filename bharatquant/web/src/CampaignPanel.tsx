import { useEffect, useState } from "react";

import TableScroll from "./TableScroll";
import {
  campaignStages,
  formatPaise,
  formatR,
  parseCampaign,
  parseCampaignConfig,
  parseEnvelope,
  parseReview,
  statusTone,
} from "./campaignContract.ts";
import type {
  CampaignConfigView,
  CampaignEnvelope,
  CampaignItemView,
  ReviewView,
} from "./campaignContract.ts";

type LoadState =
  | { state: "loading" }
  | { state: "error" }
  | { state: "ready"; envelope: CampaignEnvelope };

type ReviewState =
  | { state: "idle" | "loading" | "error" }
  | { state: "ready"; review: ReviewView };

async function detail(response: Response, fallback: string): Promise<string> {
  try {
    const body = (await response.json()) as { detail?: unknown };
    if (typeof body.detail === "string") {
      return body.detail;
    }
  } catch {
    // Keep the fallback.
  }
  return fallback;
}

function catalystLabel(item: CampaignItemView): string {
  const positive = item.events
    .filter((event) => event.polarity > 0)
    .map((event) => event.event);
  const unique = Array.from(new Set(positive));
  return unique.length === 0 ? "—" : unique.join(", ");
}

export default function CampaignPanel({ csrfToken }: { csrfToken: string }) {
  const [load, setLoad] = useState<LoadState>({ state: "loading" });
  const [review, setReview] = useState<ReviewState>({ state: "idle" });
  const [config, setConfig] = useState<CampaignConfigView | null>(null);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [version, setVersion] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    async function loadLatest() {
      try {
        const response = await fetch("/api/v1/campaign/latest", {
          cache: "no-store",
          signal: controller.signal,
        });
        if (!response.ok) {
          throw new Error("campaign unavailable");
        }
        const envelope = parseEnvelope(await response.json());
        setLoad({ state: "ready", envelope });
        setConfig(envelope.config);
      } catch {
        if (!controller.signal.aborted) {
          setLoad({ state: "error" });
        }
      }
    }
    void loadLatest();
    return () => controller.abort();
  }, [version]);

  async function send(
    path: string,
    method: string,
    body: unknown,
    okMessage: string,
  ): Promise<unknown | null> {
    setBusy(true);
    setMessage("");
    try {
      const response = await fetch(path, {
        method,
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": csrfToken,
        },
        body: body === null ? undefined : JSON.stringify(body),
      });
      if (!response.ok) {
        setMessage(await detail(response, "The request was rejected."));
        return null;
      }
      setMessage(okMessage);
      setVersion((value) => value + 1);
      return await response.json();
    } catch {
      setMessage("The request failed. Check the local service.");
      return null;
    } finally {
      setBusy(false);
    }
  }

  async function refreshMaster() {
    await send(
      "/api/v1/equity-master/refresh",
      "POST",
      null,
      "NSE equity list refreshed.",
    );
  }

  async function buildCampaign() {
    const body = await send(
      "/api/v1/campaign/research",
      "POST",
      { force: true },
      "Campaign built.",
    );
    if (body !== null) {
      const campaign = parseCampaign(body);
      if (campaign !== null) {
        void openReview(campaign.campaignId);
      }
    }
  }

  async function scan(campaignId: string) {
    const body = (await send(
      `/api/v1/campaign/${campaignId}/scan`,
      "POST",
      {},
      "Trigger scan completed.",
    )) as { entries_triggered?: unknown; exits?: unknown } | null;
    if (body !== null) {
      const entries =
        typeof body.entries_triggered === "number" ? body.entries_triggered : 0;
      const exits = typeof body.exits === "number" ? body.exits : 0;
      setMessage(`Scan complete: ${entries} entry action(s), ${exits} exit(s).`);
    }
  }

  async function cancel(campaignId: string) {
    if (!window.confirm("Cancel every watching name in this campaign?")) {
      return;
    }
    await send(
      `/api/v1/campaign/${campaignId}/cancel`,
      "POST",
      {},
      "Campaign cancelled.",
    );
  }

  async function openReview(campaignId: string) {
    setReview({ state: "loading" });
    try {
      const response = await fetch(`/api/v1/campaign/${campaignId}/review`, {
        cache: "no-store",
      });
      if (!response.ok) {
        setReview({ state: "error" });
        return;
      }
      setReview({ state: "ready", review: parseReview(await response.json()) });
    } catch {
      setReview({ state: "error" });
    }
  }

  async function saveConfig() {
    if (config === null) {
      return;
    }
    const body = await send(
      "/api/v1/settings/campaign",
      "PUT",
      { config },
      "Campaign settings saved.",
    );
    if (body !== null && typeof body === "object" && body !== null) {
      const saved = parseCampaignConfig(
        (body as { config?: unknown }).config,
      );
      setConfig(saved);
    }
  }

  if (load.state === "loading") {
    return <p role="status">Loading campaign...</p>;
  }
  if (load.state === "error") {
    return (
      <p role="alert">
        The campaign could not be read.{" "}
        <button
          type="button"
          className="text-button"
          onClick={() => {
            setLoad({ state: "loading" });
            setVersion((value) => value + 1);
          }}
        >
          Retry
        </button>
      </p>
    );
  }

  const { envelope } = load;
  const campaign = envelope.campaign;
  const stages = campaignStages(campaign);

  return (
    <>
      <section className="settings-panel" aria-labelledby="campaign-run-title">
        <h3 id="campaign-run-title">Run the loop</h3>
        <p className="panel-note">
          News catalysts → shortlist → deterministic technical plans → breakout
          levels → paper fills. Prices come from Yahoo 5-minute candles, about 15
          minutes delayed. Paper only; no broker order path.
        </p>
        <div className="strategy-examples">
          <button
            type="button"
            className="auth-button"
            onClick={() => void buildCampaign()}
            disabled={busy}
          >
            {busy ? "Working..." : "Build campaign"}
          </button>
          <button
            type="button"
            className="text-button"
            onClick={() => void refreshMaster()}
            disabled={busy}
          >
            Refresh NSE list
          </button>
          {campaign !== null && (
            <>
              <button
                type="button"
                className="text-button"
                onClick={() => void scan(campaign.campaignId)}
                disabled={busy}
              >
                Scan triggers now
              </button>
              <button
                type="button"
                className="text-button"
                onClick={() => void openReview(campaign.campaignId)}
                disabled={busy}
              >
                Load review
              </button>
              <button
                type="button"
                className="text-button"
                onClick={() => void cancel(campaign.campaignId)}
                disabled={busy}
              >
                Cancel campaign
              </button>
            </>
          )}
        </div>
        {message !== "" && <p role="status">{message}</p>}
        <p className="panel-note">
          Equity master:{" "}
          {envelope.masterAsOf === null
            ? "not loaded — refresh the NSE list first."
            : envelope.masterAsOf}
        </p>
      </section>

      <section className="settings-panel" aria-labelledby="campaign-stage-title">
        <h3 id="campaign-stage-title">Today&apos;s pipeline</h3>
        <div className="campaign-stages">
          {stages.map((stage) => (
            <span
              key={stage.key}
              className={`campaign-stage campaign-stage--${stage.state}`}
            >
              <strong>{stage.label}</strong>
              <span>{stage.detail}</span>
            </span>
          ))}
        </div>
        {campaign === null ? (
          <p className="panel-note">
            No campaign yet. Build one to collect news, shortlist names and freeze
            their trigger levels.
          </p>
        ) : (
          <>
            <p className="panel-note">
              <strong>{campaign.campaignId}</strong> · trade date{" "}
              {campaign.tradeDate} · status {campaign.status} ·{" "}
              {campaign.items.length} name(s)
            </p>
            <TableScroll label="Campaign candidates">
              <table className="judgment-table">
                <thead>
                  <tr>
                    <th>#</th>
                    <th>Symbol</th>
                    <th>Catalyst</th>
                    <th>Signal</th>
                    <th>Status</th>
                    <th>Level</th>
                    <th>Entry</th>
                    <th>Exit</th>
                    <th>R</th>
                  </tr>
                </thead>
                <tbody>
                  {campaign.items.map((item) => {
                    const tone = statusTone(item.status);
                    return (
                      <tr key={item.symbol}>
                        <td>{item.rank}</td>
                        <td>{item.symbol}</td>
                        <td>{catalystLabel(item)}</td>
                        <td>
                          {typeof item.plan?.signal === "string"
                            ? item.plan.signal
                            : "—"}
                        </td>
                        <td>
                          <span className={`status-badge status-badge--${tone}`}>
                            {item.status}
                          </span>
                        </td>
                        <td>{formatPaise(item.trigger?.level_paise)}</td>
                        <td>{formatPaise(item.outcome?.entry_paise)}</td>
                        <td>{formatPaise(item.outcome?.exit_paise)}</td>
                        <td>{formatR(item.rMultiple)}</td>
                      </tr>
                    );
                  })}
                  {campaign.items.length === 0 && (
                    <tr>
                      <td colSpan={9}>No qualifying positive catalyst today.</td>
                    </tr>
                  )}
                </tbody>
              </table>
            </TableScroll>
          </>
        )}
      </section>

      {review.state === "ready" && (
        <section className="settings-panel" aria-labelledby="campaign-review-title">
          <h3 id="campaign-review-title">Review</h3>
          <p className="panel-note">
            Realised P&amp;L {formatPaise(review.review.realizedPaise)} ·{" "}
            {review.review.openPositions.length} open position(s).
          </p>
          <TableScroll label="Catalyst hit rates">
            <table className="judgment-table">
              <thead>
                <tr>
                  <th>Catalyst</th>
                  <th>Closed</th>
                  <th>Hit rate</th>
                  <th>Avg R</th>
                  <th>Weight</th>
                </tr>
              </thead>
              <tbody>
                {review.review.eventStats.map((stat) => (
                  <tr key={stat.event}>
                    <td>{stat.event}</td>
                    <td>{stat.closed}</td>
                    <td>{(stat.hitRate * 100).toFixed(0)}%</td>
                    <td>{formatR(stat.avgR)}</td>
                    <td>
                      {review.review.weights[stat.event]?.toFixed(2) ?? "1.00"}
                    </td>
                  </tr>
                ))}
                {review.review.eventStats.length === 0 && (
                  <tr>
                    <td colSpan={5}>
                      No closed campaign outcomes yet; weights stay neutral.
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </TableScroll>
        </section>
      )}

      {config !== null && (
        <section className="settings-panel" aria-labelledby="campaign-config-title">
          <h3 id="campaign-config-title">Campaign settings</h3>
          <p className="panel-note">
            Every value is a bounded setting. Models are never used to compute a
            level, size or weight.
          </p>
          <label>
            Enabled
            <input
              type="checkbox"
              checked={config.enabled}
              onChange={(event) =>
                setConfig({ ...config, enabled: event.target.checked })
              }
            />
          </label>
          <label>
            Names to shortlist
            <select
              value={config.top_n}
              onChange={(event) =>
                setConfig({ ...config, top_n: Number(event.target.value) })
              }
            >
              {[5, 6, 7, 8, 9, 10].map((value) => (
                <option key={value} value={value}>
                  {value}
                </option>
              ))}
            </select>
          </label>
          <label>
            Equity (₹)
            <input
              type="number"
              min={1}
              step={1000}
              value={config.equity_paise / 100}
              onChange={(event) => {
                const equity = Math.max(1, Math.round(Number(event.target.value) * 100));
                setConfig({
                  ...config,
                  equity_paise: equity,
                  available_cash_paise: Math.floor(equity * 0.9),
                });
              }}
            />
          </label>
          <label>
            Risk per trade (%)
            <input
              type="number"
              min={0}
              max={100}
              step={0.1}
              value={config.risk_basis_points / 100}
              onChange={(event) =>
                setConfig({
                  ...config,
                  risk_basis_points: Math.round(Number(event.target.value) * 100),
                })
              }
            />
          </label>
          <label>
            Maximum open positions
            <input
              type="number"
              min={1}
              max={10}
              value={config.max_open_positions}
              onChange={(event) =>
                setConfig({
                  ...config,
                  max_open_positions: Number(event.target.value),
                })
              }
            />
          </label>
          <label>
            Trigger
            <select
              value={config.trigger_mode}
              onChange={(event) =>
                setConfig({ ...config, trigger_mode: event.target.value })
              }
            >
              <option value="breakout">Breakout above prior high</option>
              <option value="pullback">Pullback to entry</option>
            </select>
          </label>
          <label>
            Maximum chase (%)
            <input
              type="number"
              min={0}
              max={5}
              step={0.1}
              value={config.max_chase_bps / 100}
              onChange={(event) =>
                setConfig({
                  ...config,
                  max_chase_bps: Math.round(Number(event.target.value) * 100),
                })
              }
            />
          </label>
          <label>
            Latest entry time
            <input
              type="time"
              value={config.valid_until}
              onChange={(event) =>
                setConfig({ ...config, valid_until: event.target.value })
              }
            />
          </label>
          <label>
            Minimum catalyst strength
            <select
              value={config.min_catalyst_strength}
              onChange={(event) =>
                setConfig({
                  ...config,
                  min_catalyst_strength: Number(event.target.value),
                })
              }
            >
              {[1, 2, 3, 4, 5].map((value) => (
                <option key={value} value={value}>
                  {value}
                </option>
              ))}
            </select>
          </label>
          <label>
            Analysis depth
            <select
              value={config.analysis_depth}
              onChange={(event) =>
                setConfig({ ...config, analysis_depth: event.target.value })
              }
            >
              <option value="deterministic">Deterministic (no LLM)</option>
              <option value="full">Full debate (LLM)</option>
            </select>
          </label>
          <label>
            News feeds (one HTTPS URL per line)
            <textarea
              rows={3}
              value={config.feed_urls.join("\n")}
              onChange={(event) =>
                setConfig({
                  ...config,
                  feed_urls: event.target.value
                    .split("\n")
                    .map((value) => value.trim())
                    .filter((value) => value !== ""),
                })
              }
            />
          </label>
          <label>
            Paper account
            <input
              type="text"
              value={config.account_id}
              onChange={(event) =>
                setConfig({ ...config, account_id: event.target.value })
              }
            />
          </label>
          <button
            type="button"
            className="auth-button"
            onClick={() => void saveConfig()}
            disabled={busy}
          >
            Save campaign settings
          </button>
        </section>
      )}
    </>
  );
}
