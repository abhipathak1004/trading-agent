import { isCount, isRecord } from "./contracts.ts";
export type AgentToolSpec = {
  name: string;
  summary: string;
  mutating: boolean;
  arguments: string[];
};

export type AgentStatus = {
  agent_id: string;
  configured: boolean;
  provider: string;
  model: string;
  tools: AgentToolSpec[];
};

export type AgentStep = {
  turn: number;
  kind: "tool" | "blocked" | "error";
  tool: string;
  arguments: Record<string, unknown>;
  output: string;
};

export type AgentRun = {
  task: string;
  final_text: string;
  turns: number;
  stopped: boolean;
  tool_calls: string[];
  steps: AgentStep[];
  provider: string;
  model: string;
  input_tokens: number | null;
  output_tokens: number | null;
};

export type AgentRunHistoryItem = {
  run_id: string;
  agent_id: string;
  task: string;
  final_text: string;
  turns: number;
  stopped: boolean;
  provider: string;
  model: string;
  input_tokens: number | null;
  output_tokens: number | null;
  started_at: string;
  finished_at: string;
};

export type AgentRunDetail = AgentRunHistoryItem & { steps: AgentStep[] };

function parseToolSpec(value: unknown): AgentToolSpec {
  if (
    !isRecord(value) ||
    typeof value.name !== "string" ||
    value.name.length === 0 ||
    typeof value.summary !== "string" ||
    typeof value.mutating !== "boolean" ||
    !Array.isArray(value.arguments) ||
    !value.arguments.every((item) => typeof item === "string")
  ) {
    throw new Error("Invalid agent tool spec");
  }
  return {
    name: value.name,
    summary: value.summary,
    mutating: value.mutating,
    arguments: value.arguments as string[],
  };
}

export function parseAgentStatus(value: unknown): AgentStatus {
  if (
    !isRecord(value) ||
    typeof value.agent_id !== "string" ||
    value.agent_id.length === 0 ||
    typeof value.configured !== "boolean" ||
    typeof value.provider !== "string" ||
    typeof value.model !== "string" ||
    !Array.isArray(value.tools)
  ) {
    throw new Error("Invalid agent status");
  }
  return {
    agent_id: value.agent_id,
    configured: value.configured,
    provider: value.provider,
    model: value.model,
    tools: value.tools.map(parseToolSpec),
  };
}

function parseStep(value: unknown): AgentStep {
  if (
    !isRecord(value) ||
    !isCount(value.turn) ||
    typeof value.kind !== "string" ||
    typeof value.tool !== "string" ||
    !isRecord(value.arguments) ||
    typeof value.output !== "string"
  ) {
    throw new Error("Invalid agent step");
  }
  if (!["tool", "blocked", "error"].includes(value.kind)) {
    throw new Error("Invalid agent step kind");
  }
  return {
    turn: value.turn,
    kind: value.kind as AgentStep["kind"],
    tool: value.tool,
    arguments: value.arguments,
    output: value.output,
  };
}

export function parseAgentRun(value: unknown): AgentRun {
  if (
    !isRecord(value) ||
    typeof value.task !== "string" ||
    typeof value.final_text !== "string" ||
    !isCount(value.turns) ||
    typeof value.stopped !== "boolean" ||
    !Array.isArray(value.tool_calls) ||
    !value.tool_calls.every((item) => typeof item === "string") ||
    !Array.isArray(value.steps) ||
    typeof value.provider !== "string" ||
    typeof value.model !== "string" ||
    (value.input_tokens !== null && !isCount(value.input_tokens)) ||
    (value.output_tokens !== null && !isCount(value.output_tokens))
  ) {
    throw new Error("Invalid agent run");
  }
  return {
    task: value.task,
    final_text: value.final_text,
    turns: value.turns,
    stopped: value.stopped,
    tool_calls: value.tool_calls as string[],
    steps: value.steps.map(parseStep),
    provider: value.provider,
    model: value.model,
    input_tokens: value.input_tokens as number | null,
    output_tokens: value.output_tokens as number | null,
  };
}

function parseHistoryItem(value: unknown): AgentRunHistoryItem {
  if (
    !isRecord(value) ||
    typeof value.run_id !== "string" ||
    value.run_id.length === 0 ||
    typeof value.agent_id !== "string" ||
    typeof value.task !== "string" ||
    typeof value.final_text !== "string" ||
    !isCount(value.turns) ||
    typeof value.stopped !== "boolean" ||
    typeof value.provider !== "string" ||
    typeof value.model !== "string" ||
    (value.input_tokens !== null && !isCount(value.input_tokens)) ||
    (value.output_tokens !== null && !isCount(value.output_tokens)) ||
    typeof value.started_at !== "string" ||
    !Number.isFinite(Date.parse(value.started_at)) ||
    typeof value.finished_at !== "string" ||
    !Number.isFinite(Date.parse(value.finished_at))
  ) {
    throw new Error("Invalid agent run history item");
  }
  return {
    run_id: value.run_id,
    agent_id: value.agent_id,
    task: value.task,
    final_text: value.final_text,
    turns: value.turns,
    stopped: value.stopped,
    provider: value.provider,
    model: value.model,
    input_tokens: value.input_tokens as number | null,
    output_tokens: value.output_tokens as number | null,
    started_at: value.started_at,
    finished_at: value.finished_at,
  };
}

export function parseAgentRunHistory(value: unknown): AgentRunHistoryItem[] {
  if (!isRecord(value) || !Array.isArray(value.runs)) {
    throw new Error("Invalid agent run history");
  }
  return value.runs.map(parseHistoryItem);
}

export function parseAgentRunDetail(value: unknown): AgentRunDetail {
  const item = parseHistoryItem(value);
  if (!isRecord(value) || !Array.isArray(value.steps)) {
    throw new Error("Invalid agent run detail");
  }
  return { ...item, steps: value.steps.map(parseStep) };
}
