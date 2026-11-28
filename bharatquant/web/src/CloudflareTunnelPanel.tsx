import { useCallback, useEffect, useState } from "react";

import { POLL_MS, isBusy, parseTunnelStatus } from "./tunnelContract.ts";
import type { TunnelStatus } from "./tunnelContract.ts";

type BusyState = "idle" | "starting" | "stopping";

const LABELS: Record<TunnelStatus["state"], string> = {
  stopped: "Stopped",
  downloading: "Downloading cloudflared",
  starting: "Waiting for Cloudflare",
  live: "Live",
  stopping: "Stopping",
  error: "Failed",
};

async function readStatus(response: Response): Promise<TunnelStatus> {
  if (!response.ok) {
    let message = "The tunnel request failed.";
    try {
      const detail = (await response.json()) as { detail?: unknown };
      if (typeof detail.detail === "string") {
        message = detail.detail;
      }
    } catch {
      // Keep the generic message.
    }
    throw new Error(message);
  }
  return parseTunnelStatus(await response.json());
}

export default function CloudflareTunnelPanel({
  csrfToken,
}: {
  csrfToken: string;
}) {
  const [status, setStatus] = useState<TunnelStatus | null>(null);
  const [unavailable, setUnavailable] = useState(false);
  const [tick, setTick] = useState(0);
  const [busy, setBusy] = useState<BusyState>("idle");
  const [notice, setNotice] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const response = await fetch("/api/v1/settings/tunnel", {
        cache: "no-store",
      });
      setStatus(await readStatus(response));
      setUnavailable(false);
    } catch {
      setUnavailable(true);
    }
  }, []);

  useEffect(() => {
    if (status !== null && !unavailable && !isBusy(status)) {
      return;
    }
    const timer = setTimeout(() => {
      setTick((count) => count + 1);
      void refresh();
    }, POLL_MS);
    return () => clearTimeout(timer);
  }, [status, unavailable, tick, refresh]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  async function act(kind: "start" | "stop") {
    setBusy(kind === "start" ? "starting" : "stopping");
    setNotice(null);
    try {
      const response = await fetch(`/api/v1/settings/tunnel/${kind}`, {
        method: "POST",
        headers: { "X-CSRF-Token": csrfToken },
      });
      setStatus(await readStatus(response));
      setUnavailable(false);
    } catch (error) {
      setNotice(
        error instanceof Error ? error.message : "The tunnel request failed.",
      );
    } finally {
      setBusy("idle");
    }
  }

  async function copy(url: string) {
    try {
      await navigator.clipboard.writeText(url);
      setNotice("Link copied. Anyone with it still needs the owner password.");
    } catch {
      setNotice("Copy failed; select the link and copy it manually.");
    }
  }

  if (status === null) {
    return (
      <section className="settings-panel" aria-label="Cloudflare tunnel">
        <p role={unavailable ? "alert" : "status"}>
          {unavailable
            ? "The tunnel status is unavailable."
            : "Loading the tunnel status..."}
        </p>
        {unavailable && (
          <button
            type="button"
            className="text-button"
            onClick={() => void refresh()}
          >
            Retry
          </button>
        )}
      </section>
    );
  }

  const live = status.state === "live" && status.url !== null;
  const working = busy !== "idle" || isBusy(status);
  const displayNotice =
    notice ??
    (unavailable
      ? "Lost contact with the server; showing the last known state."
      : null);

  return (
    <section className="settings-panel" aria-labelledby="tunnel-title">
      <h2 id="tunnel-title">Cloudflare tunnel</h2>
      <p className="panel-note">
        Start a temporary Cloudflare quick tunnel to reach this app from another
        device. The link is public on the internet and still asks for the owner
        password; stopping the tunnel kills it immediately.
      </p>

      <div className="tunnel-status">
        <span
          className={`status-badge${live ? " status-badge--live" : ""}`}
          aria-live="polite"
        >
          {LABELS[status.state]}
        </span>
        {status.error !== null && status.state === "error" && (
          <span className="panel-note" role="alert">
            {status.error}
          </span>
        )}
      </div>

      {live && status.url !== null ? (
        <div className="tunnel-link">
          <input
            id="tunnel-url"
            type="text"
            readOnly
            value={status.url}
            aria-label="Tunnel URL"
            onFocus={(event) => event.currentTarget.select()}
          />
          <button
            type="button"
            className="auth-button"
            onClick={() => void copy(status.url as string)}
          >
            Copy link
          </button>
        </div>
      ) : (
        <p className="panel-note">
          {status.state === "error"
            ? "The tunnel could not start. Check that cloudflared can run, then retry."
            : status.state === "downloading"
              ? "Downloading the official cloudflared build into your BharatQuant home."
              : status.state === "starting"
                ? "Cloudflare is assigning a public URL and registering the connection."
                : "No tunnel is running. Local access stays on 127.0.0.1."}
        </p>
      )}

      {live && (
        <p className="panel-note">
          Cloudflare just created this hostname, so your own network may take a
          minute to resolve it. If the link does not open at first, wait a moment
          and retry.
        </p>
      )}

      <div className="strategy-examples">
        <button
          type="button"
          className="auth-button"
          onClick={() => void act("start")}
          disabled={working || live}
        >
          {status.state === "downloading" || busy === "starting"
            ? "Starting..."
            : "Start tunnel"}
        </button>
        <button
          type="button"
          className="text-button"
          onClick={() => void act("stop")}
          disabled={status.state === "stopped" || status.state === "stopping"}
        >
          {busy === "stopping" ? "Stopping..." : "Stop tunnel"}
        </button>
      </div>

      {displayNotice !== null && (
        <p className="panel-note" role="status">
          {displayNotice}
        </p>
      )}

      {status.recent_log.length > 0 && status.state !== "stopped" && (
        <details className="tunnel-log">
          <summary>Recent cloudflared output</summary>
          <pre>{status.recent_log.join("\n")}</pre>
        </details>
      )}
    </section>
  );
}
