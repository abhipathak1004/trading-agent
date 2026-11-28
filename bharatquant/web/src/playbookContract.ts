import { isCount, isRecord } from "./contracts.ts";

export type Playbook = {
  playbook_id: string;
  name: string;
  description: string;
  content: string;
  version: number;
  updated_at: string | null;
  is_default: boolean;
};

function parsePlaybook(value: unknown): Playbook {
  if (
    !isRecord(value) ||
    typeof value.playbook_id !== "string" ||
    value.playbook_id.length === 0 ||
    typeof value.name !== "string" ||
    typeof value.description !== "string" ||
    typeof value.content !== "string" ||
    !isCount(value.version) ||
    typeof value.is_default !== "boolean" ||
    (value.updated_at !== null &&
      (typeof value.updated_at !== "string" ||
        !Number.isFinite(Date.parse(value.updated_at))))
  ) {
    throw new Error("Invalid playbook");
  }
  return {
    playbook_id: value.playbook_id,
    name: value.name,
    description: value.description,
    content: value.content,
    version: value.version,
    updated_at: value.updated_at as string | null,
    is_default: value.is_default,
  };
}

export function parsePlaybookResponse(value: unknown): Playbook {
  return parsePlaybook(value);
}

export function parsePlaybookList(value: unknown): Playbook[] {
  if (!isRecord(value) || !Array.isArray(value.playbooks)) {
    throw new Error("Invalid playbook list");
  }
  const playbooks = value.playbooks.map(parsePlaybook);
  if (new Set(playbooks.map((item) => item.playbook_id)).size !== playbooks.length) {
    throw new Error("Invalid playbook list");
  }
  return playbooks;
}
