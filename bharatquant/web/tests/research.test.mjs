import assert from "node:assert/strict";
import test from "node:test";

import { parseClaims, parseDossier, parseNotes } from "../src/researchContract.ts";

const note = {
  note_id: "note_1",
  instrument_id: "NSE:RELIANCE",
  source_type: "YOUTUBE",
  source_id: "owner-youtube",
  url: "https://youtube.com/watch?v=test",
  title: "Management interview",
  body: "Capital allocation stayed disciplined.",
  published_at: "2026-11-25T00:00:00Z",
  known_at: "2026-11-25T10:00:00Z",
  note_hash: "abc",
  created_at: "2026-11-25T10:00:00Z",
};

const claim = {
  claim_id: "claim_1",
  instrument_id: "NSE:RELIANCE",
  statement: "Deleveraging is on track.",
  note_id: "note_1",
  claim_hash: "def",
  created_at: "2026-11-25T10:00:00Z",
  status: "PENDING",
  review_note: null,
};

const snapshot = {
  snapshot_id: "fundamental_1",
  instrument_id: "NSE:RELIANCE",
  source: "XBRL",
  period_end: "2026-09-30T00:00:00Z",
  known_at: "2026-11-25T10:00:00Z",
  revenue_paise: "10000000",
  ebitda_paise: "2000000",
  net_income_paise: "1000000",
  total_debt_paise: "2000000",
  cash_paise: "500000",
  shares_outstanding: 100000,
  book_value_paise: "5000000",
  fcf_paise: "800000",
  snapshot_hash: "ghi",
  created_at: "2026-11-25T10:00:00Z",
};

test("notes and claims parse", () => {
  assert.equal(parseNotes({ notes: [note] })[0].source_type, "YOUTUBE");
  assert.equal(parseClaims({ claims: [claim] })[0].status, "PENDING");
});

test("a dossier parses evidence and valuation", () => {
  const dossier = parseDossier({
    instrument_id: "NSE:RELIANCE",
    as_of: "2026-11-26T10:00:00Z",
    notes: [note],
    filings: [],
    theses: [],
    fundamentals: [snapshot],
    ratios: { roe_bps: 2000 },
    dcf: { per_share_paise: "14000", upside_bps: 1000 },
    claims: [claim],
  });
  assert.equal(dossier.notes[0].title, "Management interview");
  assert.equal(dossier.fundamentals[0].shares_outstanding, 100000);
  assert.equal(dossier.claims[0].status, "PENDING");
});

test("malformed dossiers are rejected", () => {
  assert.throws(() => parseDossier({ instrument_id: "x" }));
  assert.throws(() =>
    parseNotes({ notes: [{ ...note, published_at: "yesterday" }] }),
  );
});
