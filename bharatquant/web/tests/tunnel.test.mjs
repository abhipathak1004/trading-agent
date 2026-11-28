import assert from "node:assert/strict";
import test from "node:test";

import { isBusy, parseTunnelStatus } from "../src/tunnelContract.ts";

function status(state, url = null) {
  return {
    state,
    url,
    started_at: null,
    error: null,
    binary_path: null,
    recent_log: [],
  };
}

test("a stopped tunnel parses without a URL", () => {
  const parsed = parseTunnelStatus(status("stopped"));
  assert.equal(parsed.state, "stopped");
  assert.equal(parsed.url, null);
});

test("a live tunnel parses with a quick-tunnel URL", () => {
  const url = "https://brave-unit-4821.trycloudflare.com";
  const parsed = parseTunnelStatus(status("live", url));
  assert.equal(parsed.state, "live");
  assert.equal(parsed.url, url);
});

test("every state is accepted", () => {
  for (const state of ["downloading", "starting", "stopping", "error"]) {
    const parsed = parseTunnelStatus(status(state));
    assert.equal(parsed.state, state);
  }
});

test("a live tunnel without a URL is rejected", () => {
  assert.throws(() => parseTunnelStatus(status("live", null)));
});

test("a non-trycloudflare URL is rejected", () => {
  assert.throws(() => parseTunnelStatus(status("live", "https://evil.example")));
  assert.throws(() =>
    parseTunnelStatus(status("live", "http://x.trycloudflare.com")),
  );
});

test("malformed payloads are rejected", () => {
  assert.throws(() => parseTunnelStatus({ state: "live" }));
  assert.throws(() => parseTunnelStatus({ ...status("stopped"), state: "gone" }));
  assert.throws(() =>
    parseTunnelStatus({ ...status("stopped"), recent_log: "not a list" }),
  );
  assert.throws(() => parseTunnelStatus({ ...status("stopped"), error: 42 }));
});

test("busy states keep polling", () => {
  for (const state of ["downloading", "starting", "stopping"]) {
    assert.equal(isBusy(parseTunnelStatus(status(state))), true);
  }
});

test("terminal states never poll", () => {
  for (const state of ["stopped", "error"]) {
    assert.equal(isBusy(parseTunnelStatus(status(state))), false);
  }
  const live = parseTunnelStatus(
    status("live", "https://brave-unit-4821.trycloudflare.com"),
  );
  assert.equal(isBusy(live), false);
});
