import { isCount, isRecord, isTimestamp } from "./contracts.ts";

export type WorkingMemory = {
  run_id: string;
  symbol: string;
  source: string;
  as_of: string;
  schema_version: number;
  state: Record<string, unknown>;
  state_hash: string;
  recorded_at: string;
};

export type WorkingMemoryResponse = {
  run_id: string;
  memories: WorkingMemory[];
};

export function parseWorkingMemory(value: unknown): WorkingMemoryResponse {
  if (
    !isRecord(value) ||
    typeof value.run_id !== "string" ||
    !Array.isArray(value.memories) ||
    value.memories.length === 0 ||
    value.memories.length > 64
  ) {
    throw new Error("Invalid working memory response");
  }
  return {
    run_id: value.run_id,
    memories: value.memories.map((item) => {
      if (
        !isRecord(item) ||
        typeof item.run_id !== "string" ||
        typeof item.symbol !== "string" ||
        typeof item.source !== "string" ||
        !isTimestamp(item.as_of) ||
        !isCount(item.schema_version) ||
        !isRecord(item.state) ||
        typeof item.state_hash !== "string" ||
        item.state_hash.length === 0 ||
        !isTimestamp(item.recorded_at)
      ) {
        throw new Error("Invalid working memory");
      }
      return {
        run_id: item.run_id,
        symbol: item.symbol,
        source: item.source,
        as_of: item.as_of,
        schema_version: item.schema_version,
        state: item.state,
        state_hash: item.state_hash,
        recorded_at: item.recorded_at,
      };
    }),
  };
}
