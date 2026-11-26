import { useEffect, useState } from "react";

export type TodayTarget = "trade" | "data" | "schedules" | "settings";

type Tone = "ok" | "warn" | "danger" | "muted";

type Card = {
  key: string;
  title: string;
  value: string;
  detail: string;
  tone: Tone;
  action: string;
  target: TodayTarget;
};

type State =
  | { state: "loading" }
  | { state: "error" }
  | { state: "ready"; cards: Card[] };

function rupees(value: unknown): string {
  if (typeof value !== "string" || !/^-?\d+$/.test(value)) {
    return "n/a";
  }
  return `₹${(Number(value) / 100).toLocaleString("en-IN", {
    maximumFractionDigits: 2,
  })}`;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

class HttpError extends Error {
  constructor(
    readonly status: number,
    url: string,
  ) {
    super(`${url} returned ${status}`);
  }
}

async function read(url: string): Promise<unknown> {
  const response = await fetch(url, { cache: "no-store" });
  if (!response.ok) {
    throw new HttpError(response.status, url);
  }
  return response.json();
}

function unavailable(key: string, title: string, target: TodayTarget): Card {
  return {
    key,
    title,
    value: "Unavailable",
    detail: "This status could not be read. Check the local service.",
    tone: "danger",
    action: `Open ${title}`,
    target,
  };
}

function paperCard(value: unknown): Card {
  if (!isRecord(value) || typeof value.balance_paise !== "string") {
    return {
      key: "paper",
      title: "Paper account",
      value: "Not configured",
      detail: "Create the paper-main account in Trade.",
      tone: "warn",
      action: "Open Trade",
      target: "trade",
    };
  }
  return {
    key: "paper",
    title: "Paper account",
    value: rupees(value.balance_paise),
    detail: `${typeof value.status === "string" ? value.status : "UNKNOWN"} · cash after deposits and withdrawals`,
    tone: "ok",
    action: "Open Trade",
    target: "trade",
  };
}

function dataCard(value: unknown): Card {
  if (!isRecord(value) || typeof value.import_count !== "number") {
    return {
      key: "data",
      title: "Data",
      value: "Unavailable",
      detail: "Data status could not be read.",
      tone: "danger",
      action: "Open Data",
      target: "data",
    };
  }
  const imports = value.import_count;
  const records =
    typeof value.imported_record_count === "number"
      ? value.imported_record_count
      : 0;
  const provider =
    typeof value.live_provider === "string" ? value.live_provider : "unknown";
  return {
    key: "data",
    title: "Data",
    value: `${imports.toLocaleString("en-IN")} ${imports === 1 ? "import" : "imports"}`,
    detail: `${records.toLocaleString("en-IN")} records · live provider ${provider}`,
    tone: imports === 0 ? "warn" : "muted",
    action: "Open Data",
    target: "data",
  };
}

function schedulesCard(value: unknown): Card {
  if (!isRecord(value) || typeof value.scheduler_running !== "boolean") {
    return {
      key: "schedules",
      title: "Schedules",
      value: "Unavailable",
      detail: "Schedule status could not be read.",
      tone: "danger",
      action: "Open Schedules",
      target: "schedules",
    };
  }
  const running = value.scheduler_running;
  const available = value.effective_state === "available";
  const count = available && typeof value.schedule_count === "number"
    ? value.schedule_count
    : 0;
  const runs = Array.isArray(value.job_runs) ? value.job_runs.length : 0;
  return {
    key: "schedules",
    title: "Schedules",
    value: running ? "Loop running" : "Loop stopped",
    detail: available
      ? `${count} schedule(s) · ${runs} recent job run(s)`
      : "No schedules configured yet.",
    tone: running ? "ok" : "warn",
    action: "Open Schedules",
    target: "schedules",
  };
}

function runtimeCard(value: unknown): Card {
  if (!isRecord(value) || typeof value.status !== "string") {
    return {
      key: "runtime",
      title: "Runtime",
      value: "Unavailable",
      detail: "Runtime health could not be read.",
      tone: "danger",
      action: "Open Settings",
      target: "settings",
    };
  }
  const issues = Array.isArray(value.issues) ? value.issues.length : 0;
  const jobs = isRecord(value.jobs) ? value.jobs : {};
  const stale = typeof jobs.stale === "number" ? jobs.stale : 0;
  const failed = typeof jobs.failed_24h === "number" ? jobs.failed_24h : 0;
  return {
    key: "runtime",
    title: "Runtime",
    value: value.status.toUpperCase(),
    detail: `${issues} issue(s) · ${stale} stale job(s) · ${failed} failed (24h)`,
    tone:
      value.status === "ok" && issues === 0
        ? "ok"
        : issues > 0
          ? "danger"
          : "warn",
    action: "Open Settings",
    target: "settings",
  };
}

export default function TodayOverview({
  onNavigate,
}: {
  onNavigate: (target: TodayTarget) => void;
}) {
  const [state, setState] = useState<State>({ state: "loading" });
  const [retry, setRetry] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    async function load() {
      const [paper, data, schedules, runtime] = await Promise.allSettled([
        read("/api/v1/paper/accounts/paper-main"),
        read("/api/v1/data/status"),
        read("/api/v1/schedules/status"),
        read("/api/v1/system/status"),
      ]);
      if (controller.signal.aborted) {
        return;
      }
      const paperFailure =
        paper.status === "rejected" &&
        paper.reason instanceof HttpError &&
        paper.reason.status === 404
          ? paperCard(null)
          : unavailable("paper", "Paper account", "trade");
      const cards = [
        paper.status === "fulfilled" ? paperCard(paper.value) : paperFailure,
        data.status === "fulfilled"
          ? dataCard(data.value)
          : unavailable("data", "Data", "data"),
        schedules.status === "fulfilled"
          ? schedulesCard(schedules.value)
          : unavailable("schedules", "Schedules", "schedules"),
        runtime.status === "fulfilled"
          ? runtimeCard(runtime.value)
          : unavailable("runtime", "Runtime", "settings"),
      ];
      setState({ state: "ready", cards });
    }
    void load().catch(() => {
      if (!controller.signal.aborted) {
        setState({ state: "error" });
      }
    });
    return () => controller.abort();
  }, [retry]);

  if (state.state === "loading") {
    return (
      <section className="overview-grid" aria-label="Today overview">
        {[0, 1, 2, 3].map((key) => (
          <div className="overview-card" key={key}>
            <p role="status">Checking status...</p>
          </div>
        ))}
      </section>
    );
  }

  if (state.state === "error") {
    return (
      <section className="overview-grid" aria-label="Today overview">
        <div className="overview-card">
          <p role="alert">
            The overview is unavailable.{" "}
            <button
              type="button"
              className="text-button"
              onClick={() => {
                setState({ state: "loading" });
                setRetry((value) => value + 1);
              }}
            >
              Retry
            </button>
          </p>
        </div>
      </section>
    );
  }

  return (
    <section className="overview-grid" aria-label="Today overview">
      {state.cards.map((card) => (
        <article className="overview-card" key={card.key}>
          <p className="eyebrow">{card.title}</p>
          <p className={`overview-card__value overview-card__value--${card.tone}`}>
            {card.value}
          </p>
          <p className="overview-card__detail">{card.detail}</p>
          <button
            type="button"
            className="text-button"
            onClick={() => onNavigate(card.target)}
          >
            {card.action}
          </button>
        </article>
      ))}
    </section>
  );
}
