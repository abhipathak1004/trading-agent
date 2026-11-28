import { isRecord } from "./contracts.ts";

export type AgentCatalogueRoute = {
  tier: string;
  provider: string | null;
  model: string | null;
  configured: boolean;
};

export type AgentCatalogueItem = {
  agent_id: string;
  kind: string;
  label: string;
  description: string;
  tier: string;
  prompt_id: string | null;
  tools: string[];
  read_only: boolean;
  route: AgentCatalogueRoute;
};

function parseRoute(value: unknown): AgentCatalogueRoute {
  if (
    !isRecord(value) ||
    typeof value.tier !== "string" ||
    (value.provider !== null && typeof value.provider !== "string") ||
    (value.model !== null && typeof value.model !== "string") ||
    typeof value.configured !== "boolean"
  ) {
    throw new Error("Invalid agent route");
  }
  return {
    tier: value.tier,
    provider: value.provider as string | null,
    model: value.model as string | null,
    configured: value.configured,
  };
}

function parseItem(value: unknown): AgentCatalogueItem {
  if (
    !isRecord(value) ||
    typeof value.agent_id !== "string" ||
    value.agent_id.length === 0 ||
    typeof value.kind !== "string" ||
    typeof value.label !== "string" ||
    typeof value.description !== "string" ||
    typeof value.tier !== "string" ||
    (value.prompt_id !== null && typeof value.prompt_id !== "string") ||
    !Array.isArray(value.tools) ||
    !value.tools.every((item) => typeof item === "string") ||
    typeof value.read_only !== "boolean"
  ) {
    throw new Error("Invalid agent catalogue item");
  }
  return {
    agent_id: value.agent_id,
    kind: value.kind,
    label: value.label,
    description: value.description,
    tier: value.tier,
    prompt_id: value.prompt_id as string | null,
    tools: value.tools as string[],
    read_only: value.read_only,
    route: parseRoute(value.route),
  };
}

export function parseAgentCatalogue(value: unknown): AgentCatalogueItem[] {
  if (!isRecord(value) || !Array.isArray(value.agents)) {
    throw new Error("Invalid agent catalogue");
  }
  const items = value.agents.map(parseItem);
  const seen = new Set<string>();
  for (const item of items) {
    if (seen.has(item.agent_id)) {
      throw new Error("Invalid agent catalogue");
    }
    seen.add(item.agent_id);
  }
  return items;
}
