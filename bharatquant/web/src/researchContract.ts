import { isCount, isRecord, isTimestamp } from "./contracts.ts";

export type ResearchNote = {
  note_id: string;
  instrument_id: string | null;
  source_type: string;
  source_id: string;
  url: string | null;
  title: string;
  body: string;
  published_at: string;
  known_at: string;
  note_hash: string;
  created_at: string;
};

export type FundamentalSnapshot = {
  snapshot_id: string;
  instrument_id: string;
  source: string;
  period_end: string;
  known_at: string;
  revenue_paise: string;
  ebitda_paise: string;
  net_income_paise: string;
  total_debt_paise: string;
  cash_paise: string;
  shares_outstanding: number;
  book_value_paise: string;
  fcf_paise: string;
  snapshot_hash: string;
  created_at: string;
};

export type ResearchClaim = {
  claim_id: string;
  instrument_id: string;
  statement: string;
  note_id: string | null;
  claim_hash: string;
  created_at: string;
  status: string;
  review_note: string | null;
};

export type ThesisView = {
  thesis_id: string;
  instrument_id: string;
  horizon: string;
  state: string;
  created_at: string;
  thesis_hash: string;
  events: Record<string, unknown>[];
};

export type Dossier = {
  instrument_id: string;
  as_of: string;
  notes: ResearchNote[];
  filings: Record<string, unknown>[];
  theses: ThesisView[];
  fundamentals: FundamentalSnapshot[];
  ratios: Record<string, unknown> | null;
  dcf: Record<string, unknown> | null;
  claims: ResearchClaim[];
};

function signedInteger(value: unknown): string {
  if (typeof value !== "string" || !/^-?\d+$/.test(value)) {
    throw new Error("Invalid integer field");
  }
  return value;
}

function parseNote(value: unknown): ResearchNote {
  if (
    !isRecord(value) ||
    typeof value.note_id !== "string" ||
    (value.instrument_id !== null && typeof value.instrument_id !== "string") ||
    typeof value.source_type !== "string" ||
    typeof value.source_id !== "string" ||
    (value.url !== null && typeof value.url !== "string") ||
    typeof value.title !== "string" ||
    typeof value.body !== "string" ||
    !isTimestamp(value.published_at) ||
    !isTimestamp(value.known_at) ||
    typeof value.note_hash !== "string" ||
    !isTimestamp(value.created_at)
  ) {
    throw new Error("Invalid research note");
  }
  return {
    note_id: value.note_id,
    instrument_id: value.instrument_id,
    source_type: value.source_type,
    source_id: value.source_id,
    url: value.url,
    title: value.title,
    body: value.body,
    published_at: value.published_at,
    known_at: value.known_at,
    note_hash: value.note_hash,
    created_at: value.created_at,
  };
}

function parseSnapshot(value: unknown): FundamentalSnapshot {
  if (
    !isRecord(value) ||
    typeof value.snapshot_id !== "string" ||
    typeof value.instrument_id !== "string" ||
    typeof value.source !== "string" ||
    !isTimestamp(value.period_end) ||
    !isTimestamp(value.known_at) ||
    !isCount(value.shares_outstanding) ||
    typeof value.snapshot_hash !== "string" ||
    !isTimestamp(value.created_at)
  ) {
    throw new Error("Invalid fundamental snapshot");
  }
  return {
    snapshot_id: value.snapshot_id,
    instrument_id: value.instrument_id,
    source: value.source,
    period_end: value.period_end,
    known_at: value.known_at,
    revenue_paise: signedInteger(value.revenue_paise),
    ebitda_paise: signedInteger(value.ebitda_paise),
    net_income_paise: signedInteger(value.net_income_paise),
    total_debt_paise: signedInteger(value.total_debt_paise),
    cash_paise: signedInteger(value.cash_paise),
    shares_outstanding: value.shares_outstanding,
    book_value_paise: signedInteger(value.book_value_paise),
    fcf_paise: signedInteger(value.fcf_paise),
    snapshot_hash: value.snapshot_hash,
    created_at: value.created_at,
  };
}

function parseClaim(value: unknown): ResearchClaim {
  if (
    !isRecord(value) ||
    typeof value.claim_id !== "string" ||
    typeof value.instrument_id !== "string" ||
    typeof value.statement !== "string" ||
    (value.note_id !== null && typeof value.note_id !== "string") ||
    typeof value.claim_hash !== "string" ||
    !isTimestamp(value.created_at) ||
    typeof value.status !== "string" ||
    (value.review_note !== null && typeof value.review_note !== "string")
  ) {
    throw new Error("Invalid research claim");
  }
  return {
    claim_id: value.claim_id,
    instrument_id: value.instrument_id,
    statement: value.statement,
    note_id: value.note_id,
    claim_hash: value.claim_hash,
    created_at: value.created_at,
    status: value.status,
    review_note: value.review_note,
  };
}

function parseThesis(value: unknown): ThesisView {
  if (
    !isRecord(value) ||
    typeof value.thesis_id !== "string" ||
    typeof value.instrument_id !== "string" ||
    typeof value.horizon !== "string" ||
    typeof value.state !== "string" ||
    !isTimestamp(value.created_at) ||
    typeof value.thesis_hash !== "string" ||
    !Array.isArray(value.events)
  ) {
    throw new Error("Invalid thesis");
  }
  return {
    thesis_id: value.thesis_id,
    instrument_id: value.instrument_id,
    horizon: value.horizon,
    state: value.state,
    created_at: value.created_at,
    thesis_hash: value.thesis_hash,
    events: value.events.filter(isRecord),
  };
}

export function parseDossier(value: unknown): Dossier {
  if (
    !isRecord(value) ||
    typeof value.instrument_id !== "string" ||
    !isTimestamp(value.as_of) ||
    !Array.isArray(value.notes) ||
    !Array.isArray(value.filings) ||
    !Array.isArray(value.theses) ||
    !Array.isArray(value.fundamentals) ||
    !Array.isArray(value.claims) ||
    (value.ratios !== null && !isRecord(value.ratios)) ||
    (value.dcf !== null && !isRecord(value.dcf))
  ) {
    throw new Error("Invalid dossier");
  }
  return {
    instrument_id: value.instrument_id,
    as_of: value.as_of,
    notes: value.notes.map(parseNote),
    filings: value.filings.filter(isRecord),
    theses: value.theses.map(parseThesis),
    fundamentals: value.fundamentals.map(parseSnapshot),
    ratios: value.ratios,
    dcf: value.dcf,
    claims: value.claims.map(parseClaim),
  };
}

export function parseNotes(value: unknown): ResearchNote[] {
  if (!isRecord(value) || !Array.isArray(value.notes)) {
    throw new Error("Invalid research note list");
  }
  return value.notes.map(parseNote);
}

export function parseClaims(value: unknown): ResearchClaim[] {
  if (!isRecord(value) || !Array.isArray(value.claims)) {
    throw new Error("Invalid research claim list");
  }
  return value.claims.map(parseClaim);
}
