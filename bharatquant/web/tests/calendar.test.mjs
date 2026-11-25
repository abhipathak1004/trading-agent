import assert from "node:assert/strict";
import test from "node:test";

import { dateMillis, parseResult } from "../src/calendarContract.ts";

const asOf = "2026-11-22T10:00:00.000Z";
const start = "2026-11-22";
const end = "2026-11-24";

function fixture() {
  const timestamp = "2026-11-21T10:00:00Z";
  return {
    exchange_code: "TEST",
    as_of: asOf,
    days: [
      {
        session_date: start, state: "UNCOVERED", session_class: null,
        session_id: null, opens_at: null, closes_at: null, known_at: null,
        reviewed_at: null, recorded_at: null,
      },
      {
        session_date: "2026-11-23", state: "OPEN", session_class: "REGULAR",
        session_id: "fixture", opens_at: "2026-11-23T03:00:00Z",
        closes_at: "2026-11-23T10:00:00Z", known_at: timestamp,
        reviewed_at: timestamp, recorded_at: timestamp,
      },
      {
        session_date: end, state: "CLOSED", session_class: "HOLIDAY",
        session_id: "fixture2", opens_at: null, closes_at: null,
        known_at: timestamp, reviewed_at: timestamp, recorded_at: timestamp,
      },
    ],
  };
}

function parse(value) {
  return parseResult(value, "TEST", start, end, asOf);
}

test("calendar response retains open, closed and uncovered states", () => {
  const result = parse(fixture());
  assert.deepEqual(result.days.map((item) => item.state), ["UNCOVERED", "OPEN", "CLOSED"]);
  assert.equal(result.days[1].opensAt, "2026-11-23T03:00:00Z");
  assert.equal(result.days[0].opensAt, null);
});

test("calendar rejects wrong date ranges, timestamps and leaked uncovered metadata", () => {
  assert.ok(Number.isNaN(dateMillis("2026-02-30")));
  const wrongRange = fixture();
  wrongRange.days[1].session_date = "2026-11-24";
  assert.throws(() => parse(wrongRange), /Invalid calendar date/);
  const future = fixture();
  future.days[1].recorded_at = "2026-11-22T10:00:00.001Z";
  assert.throws(() => parse(future), /Invalid calendar session/);
  const uncovered = fixture();
  uncovered.days[0].session_id = "unexpected";
  assert.throws(() => parse(uncovered), /Invalid uncovered date/);
});

test("calendar rejects mismatched query identity and invalid sessions", () => {
  const differentExchange = fixture();
  differentExchange.exchange_code = "OTHER";
  assert.throws(() => parse(differentExchange), /Invalid calendar coverage/);
  const differentAsOf = fixture();
  differentAsOf.as_of = "2026-11-22T09:00:00Z";
  assert.throws(() => parse(differentAsOf), /Invalid calendar coverage/);
  const invalidWindow = fixture();
  invalidWindow.days[1].closes_at = invalidWindow.days[1].opens_at;
  assert.throws(() => parse(invalidWindow), /Invalid calendar session/);
  const openHoliday = fixture();
  openHoliday.days[1].session_class = "HOLIDAY";
  assert.throws(() => parse(openHoliday), /Invalid calendar session/);
  const incompleteSession = fixture();
  incompleteSession.days[2].reviewed_at = null;
  assert.throws(() => parse(incompleteSession), /Invalid calendar session/);
});
