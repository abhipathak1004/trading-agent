import { isCount, isRecord, isTimestamp } from "./contracts.ts";
export type NewsCategory =
  | "INDIA_MARKET"
  | "GLOBAL_MARKET"
  | "GEOPOLITICAL"
  | "COMMODITY"
  | "RATES"
  | "CORPORATE"
  | "REGULATORY"
  | "OTHER";

const NEWS_CATEGORIES: readonly NewsCategory[] = [
  "INDIA_MARKET",
  "GLOBAL_MARKET",
  "GEOPOLITICAL",
  "COMMODITY",
  "RATES",
  "CORPORATE",
  "REGULATORY",
  "OTHER",
];

export type NewsItem = {
  news_id: string;
  source_id: string;
  url: string;
  title: string;
  summary: string;
  category: NewsCategory;
  long_term_impact: boolean;
  published_at: string;
};

export type NewsMemory = {
  memory_id: string;
  category: NewsCategory;
  title: string;
  body: string;
  known_at: string;
};

export type NewsDigest = {
  source_id: string;
  item_count: number;
  items: NewsItem[];
  memory_count: number;
  memories: NewsMemory[];
};

export type NewsIngestResult = {
  source_id: string;
  fetched: number;
  stored: number;
  duplicates: number;
  long_term_memories: number;
};

function parseCategory(value: unknown): NewsCategory {
  if (
    typeof value !== "string" ||
    !(NEWS_CATEGORIES as readonly string[]).includes(value)
  ) {
    throw new Error("Invalid news category");
  }
  return value as NewsCategory;
}

function parseItem(value: unknown): NewsItem {
  if (
    !isRecord(value) ||
    typeof value.news_id !== "string" ||
    value.news_id.length === 0 ||
    typeof value.source_id !== "string" ||
    value.source_id.length === 0 ||
    typeof value.url !== "string" ||
    !value.url.startsWith("https://") ||
    typeof value.title !== "string" ||
    value.title.length === 0 ||
    typeof value.summary !== "string" ||
    typeof value.long_term_impact !== "boolean" ||
    !isTimestamp(value.published_at)
  ) {
    throw new Error("Invalid news item");
  }
  return {
    news_id: value.news_id,
    source_id: value.source_id,
    url: value.url,
    title: value.title,
    summary: value.summary,
    category: parseCategory(value.category),
    long_term_impact: value.long_term_impact,
    published_at: value.published_at,
  };
}

function parseMemory(value: unknown): NewsMemory {
  if (
    !isRecord(value) ||
    typeof value.memory_id !== "string" ||
    value.memory_id.length === 0 ||
    typeof value.title !== "string" ||
    value.title.length === 0 ||
    typeof value.body !== "string" ||
    !isTimestamp(value.known_at)
  ) {
    throw new Error("Invalid news memory");
  }
  return {
    memory_id: value.memory_id,
    category: parseCategory(value.category),
    title: value.title,
    body: value.body,
    known_at: value.known_at,
  };
}

export function parseDigest(value: unknown): NewsDigest {
  if (
    !isRecord(value) ||
    typeof value.source_id !== "string" ||
    value.source_id.length === 0 ||
    !isCount(value.item_count) ||
    !Array.isArray(value.items) ||
    value.items.length > value.item_count ||
    !isCount(value.memory_count) ||
    !Array.isArray(value.memories) ||
    value.memories.length > value.memory_count
  ) {
    throw new Error("Invalid news digest");
  }
  return {
    source_id: value.source_id,
    item_count: value.item_count,
    items: value.items.map(parseItem),
    memory_count: value.memory_count,
    memories: value.memories.map(parseMemory),
  };
}

export function parseIngestResult(value: unknown): NewsIngestResult {
  if (
    !isRecord(value) ||
    typeof value.source_id !== "string" ||
    value.source_id.length === 0 ||
    !isCount(value.fetched) ||
    !isCount(value.stored) ||
    !isCount(value.duplicates) ||
    !isCount(value.long_term_memories) ||
    value.fetched !== value.stored + value.duplicates
  ) {
    throw new Error("Invalid news ingest result");
  }
  return {
    source_id: value.source_id,
    fetched: value.fetched,
    stored: value.stored,
    duplicates: value.duplicates,
    long_term_memories: value.long_term_memories,
  };
}
