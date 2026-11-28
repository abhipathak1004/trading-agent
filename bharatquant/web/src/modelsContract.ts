import { isRecord } from "./contracts.ts";

export const MODEL_KINDS = ["ollama", "openai_compatible"] as const;
export type ModelKind = (typeof MODEL_KINDS)[number];

export type PullState = {
  status: string;
  output: string;
};

export type LocalModel = {
  id: string;
  label: string;
  kind: ModelKind;
  model: string;
  endpoint: string | null;
  installed: boolean | null;
  pull: PullState | null;
};

function parsePullState(value: unknown): PullState | null {
  if (value === null || value === undefined) {
    return null;
  }
  if (!isRecord(value) || typeof value.status !== "string") {
    throw new Error("Invalid pull state");
  }
  return {
    status: value.status,
    output: typeof value.output === "string" ? value.output : "",
  };
}

export function parseLocalModel(value: unknown): LocalModel {
  if (
    !isRecord(value) ||
    typeof value.id !== "string" ||
    value.id.length === 0 ||
    typeof value.label !== "string" ||
    value.label.length === 0 ||
    (value.kind !== "ollama" && value.kind !== "openai_compatible") ||
    typeof value.model !== "string" ||
    value.model.length === 0 ||
    (value.endpoint !== null && typeof value.endpoint !== "string") ||
    (value.installed !== null &&
      value.installed !== undefined &&
      typeof value.installed !== "boolean")
  ) {
    throw new Error("Invalid local model");
  }
  return {
    id: value.id,
    label: value.label,
    kind: value.kind,
    model: value.model,
    endpoint: value.endpoint as string | null,
    installed: value.installed === undefined ? null : (value.installed as boolean | null),
    pull: parsePullState(value.pull),
  };
}

export function parseLocalModels(value: unknown): LocalModel[] {
  if (!isRecord(value) || !Array.isArray(value.models)) {
    throw new Error("Invalid local model list");
  }
  return value.models.map(parseLocalModel);
}

export function isPulling(model: LocalModel): boolean {
  return model.pull?.status === "queued" || model.pull?.status === "running";
}

export function pullTone(status: string | undefined): "ok" | "warn" | "danger" | "muted" {
  if (status === "completed") {
    return "ok";
  }
  if (status === "running" || status === "queued") {
    return "warn";
  }
  if (status === "failed" || status === "cancelled") {
    return "danger";
  }
  return "muted";
}
