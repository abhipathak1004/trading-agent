import assert from "node:assert/strict";
import test from "node:test";

import { parsePlaybookList, parsePlaybookResponse } from "../src/playbookContract.ts";

const playbook = {
  playbook_id: "defensive",
  name: "Capital preservation",
  description: "Favour the cleanest setups.",
  content: "Capital preservation guidance:\n- Prefer the cleanest trend.",
  version: 2,
  updated_at: "2026-11-26T12:00:00+00:00",
  is_default: false,
};

test("a playbook and list are accepted", () => {
  const parsed = parsePlaybookResponse(playbook);
  assert.equal(parsed.playbook_id, "defensive");
  assert.equal(parsed.version, 2);
  const list = parsePlaybookList({ playbooks: [playbook] });
  assert.equal(list.length, 1);
});

test("malformed playbooks are rejected", () => {
  for (const override of [
    { playbook_id: "" },
    { version: -1 },
    { is_default: "yes" },
    { updated_at: "not-a-time" },
    { content: 3 },
  ]) {
    assert.throws(() => parsePlaybookResponse({ ...playbook, ...override }));
  }
  assert.throws(() =>
    parsePlaybookList({ playbooks: [playbook, { ...playbook, version: 3 }] }),
  );
  assert.throws(() => parsePlaybookList({ playbooks: "no" }));
});
