import { isRecord } from "./contracts.ts";

export type SemanticHit = {
  doc_id: string;
  symbol: string;
  source_url: string;
  title: string;
  snippet: string;
  published_at: string;
  known_at: string;
  score: number;
};

export type SemanticSearch = {
  query: string;
  as_of: string;
  hits: SemanticHit[];
};

export type SemanticDocument = {
  doc_id: string;
  symbol: string;
  source_url: string;
  title: string;
  snippet: string;
  published_at: string;
  known_at: string;
  recorded_at: string;
  content_hash: string;
};

function parseHit(value: unknown): SemanticHit {
  if (
    !isRecord(value) ||
    typeof value.doc_id !== "string" ||
    value.doc_id.length === 0 ||
    typeof value.symbol !== "string" ||
    typeof value.source_url !== "string" ||
    typeof value.title !== "string" ||
    typeof value.snippet !== "string" ||
    typeof value.published_at !== "string" ||
    typeof value.known_at !== "string" ||
    typeof value.score !== "number" ||
    !Number.isFinite(value.score)
  ) {
    throw new Error("Invalid semantic hit");
  }
  return {
    doc_id: value.doc_id,
    symbol: value.symbol,
    source_url: value.source_url,
    title: value.title,
    snippet: value.snippet,
    published_at: value.published_at,
    known_at: value.known_at,
    score: value.score,
  };
}

export function parseSemanticSearch(value: unknown): SemanticSearch {
  if (
    !isRecord(value) ||
    typeof value.query !== "string" ||
    typeof value.as_of !== "string" ||
    !Array.isArray(value.hits) ||
    value.hits.length > 50
  ) {
    throw new Error("Invalid semantic search");
  }
  return {
    query: value.query,
    as_of: value.as_of,
    hits: value.hits.map(parseHit),
  };
}

export function parseSemanticDocument(value: unknown): SemanticDocument {
  if (
    !isRecord(value) ||
    typeof value.doc_id !== "string" ||
    value.doc_id.length === 0 ||
    typeof value.symbol !== "string" ||
    typeof value.source_url !== "string" ||
    typeof value.title !== "string" ||
    typeof value.snippet !== "string" ||
    typeof value.published_at !== "string" ||
    typeof value.known_at !== "string" ||
    typeof value.recorded_at !== "string" ||
    typeof value.content_hash !== "string" ||
    value.content_hash.length === 0
  ) {
    throw new Error("Invalid semantic document");
  }
  return {
    doc_id: value.doc_id,
    symbol: value.symbol,
    source_url: value.source_url,
    title: value.title,
    snippet: value.snippet,
    published_at: value.published_at,
    known_at: value.known_at,
    recorded_at: value.recorded_at,
    content_hash: value.content_hash,
  };
}
