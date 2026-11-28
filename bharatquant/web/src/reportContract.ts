import { isRecord, isTimestamp } from "./contracts.ts";

export type Report = {
  report_id: string;
  report_type: "morning" | "evening";
  report_date: string;
  status: string;
  title: string;
  body_md: string;
  body: Record<string, unknown>;
  content_hash: string;
  file_path: string | null;
  created_at: string;
};

export function parseReport(value: unknown): Report {
  if (
    !isRecord(value) ||
    typeof value.report_id !== "string" ||
    (value.report_type !== "morning" && value.report_type !== "evening") ||
    typeof value.report_date !== "string" ||
    typeof value.status !== "string" ||
    typeof value.title !== "string" ||
    typeof value.body_md !== "string" ||
    !isRecord(value.body) ||
    typeof value.content_hash !== "string" ||
    (typeof value.file_path !== "string" && value.file_path !== null) ||
    !isTimestamp(value.created_at)
  ) {
    throw new Error("Invalid report");
  }
  return {
    report_id: value.report_id,
    report_type: value.report_type,
    report_date: value.report_date,
    status: value.status,
    title: value.title,
    body_md: value.body_md,
    body: value.body,
    content_hash: value.content_hash,
    file_path: value.file_path as string | null,
    created_at: value.created_at,
  };
}

export function parseReports(value: unknown): Report[] {
  if (!isRecord(value) || !Array.isArray(value.reports)) {
    throw new Error("Invalid report list");
  }
  return value.reports.map(parseReport);
}

