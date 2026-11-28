import { isRecord } from "./contracts.ts";

export const TUNNEL_STATES = [
  "stopped",
  "downloading",
  "starting",
  "live",
  "stopping",
  "error",
] as const;

export type TunnelState = (typeof TUNNEL_STATES)[number];

export type TunnelStatus = {
  state: TunnelState;
  url: string | null;
  started_at: string | null;
  error: string | null;
  binary_path: string | null;
  recent_log: string[];
};

const TUNNEL_URL = /^https:\/\/[a-z0-9-]+\.trycloudflare\.com$/;

export const POLL_MS = 1500;

export function isBusy(status: TunnelStatus): boolean {
  return (
    status.state === "downloading" ||
    status.state === "starting" ||
    status.state === "stopping"
  );
}

function isState(value: unknown): value is TunnelState {
  return (
    typeof value === "string" &&
    (TUNNEL_STATES as readonly string[]).includes(value)
  );
}

function isUrlOrNull(value: unknown): value is string | null {
  if (value === null) {
    return true;
  }
  return typeof value === "string" && TUNNEL_URL.test(value);
}

function isTextOrNull(value: unknown): value is string | null {
  return value === null || typeof value === "string";
}

export function parseTunnelStatus(value: unknown): TunnelStatus {
  if (!isRecord(value) || !isState(value.state)) {
    throw new Error("Invalid tunnel status");
  }
  if (!isUrlOrNull(value.url)) {
    throw new Error("Invalid tunnel status");
  }
  if (!isTextOrNull(value.started_at) || !isTextOrNull(value.error)) {
    throw new Error("Invalid tunnel status");
  }
  if (!isTextOrNull(value.binary_path)) {
    throw new Error("Invalid tunnel status");
  }
  if (
    !Array.isArray(value.recent_log) ||
    !value.recent_log.every((line) => typeof line === "string")
  ) {
    throw new Error("Invalid tunnel status");
  }
  if (value.state === "live" && value.url === null) {
    throw new Error("A live tunnel must report a URL");
  }
  return {
    state: value.state,
    url: value.url,
    started_at: value.started_at,
    error: value.error,
    binary_path: value.binary_path,
    recent_log: value.recent_log,
  };
}
