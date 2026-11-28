import { isCount, isRecord } from "./contracts.ts";

export type AgentPrompt = {
  agent_id: string;
  label: string;
  description: string;
  system: string;
  template: string;
  required_fields: string[];
  version: number;
  updated_at: string | null;
  is_default: boolean;
};

function parsePromptValue(value: unknown): AgentPrompt {
  if (
    !isRecord(value) ||
    typeof value.agent_id !== "string" ||
    value.agent_id.length === 0 ||
    typeof value.label !== "string" ||
    typeof value.description !== "string" ||
    typeof value.system !== "string" ||
    typeof value.template !== "string" ||
    !Array.isArray(value.required_fields) ||
    !value.required_fields.every((item) => typeof item === "string") ||
    !isCount(value.version) ||
    typeof value.is_default !== "boolean" ||
    (value.updated_at !== null &&
      (typeof value.updated_at !== "string" ||
        !Number.isFinite(Date.parse(value.updated_at))))
  ) {
    throw new Error("Invalid agent prompt");
  }
  return {
    agent_id: value.agent_id,
    label: value.label,
    description: value.description,
    system: value.system,
    template: value.template,
    required_fields: value.required_fields as string[],
    version: value.version,
    updated_at: value.updated_at as string | null,
    is_default: value.is_default,
  };
}

export function parsePrompt(value: unknown): AgentPrompt {
  return parsePromptValue(value);
}

export function parsePromptList(value: unknown): AgentPrompt[] {
  if (!isRecord(value) || !Array.isArray(value.prompts)) {
    throw new Error("Invalid prompt list");
  }
  const prompts = value.prompts.map(parsePromptValue);
  const seen = new Set<string>();
  for (const prompt of prompts) {
    if (seen.has(prompt.agent_id)) {
      throw new Error("Invalid prompt list");
    }
    seen.add(prompt.agent_id);
  }
  return prompts;
}
