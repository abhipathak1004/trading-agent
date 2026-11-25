import { useEffect, useState } from "react";
import type { FormEvent } from "react";

import DataStatus from "./DataStatus";
import CalendarCoverage from "./CalendarCoverage";
import SnapshotFreshness from "./SnapshotFreshness";
import ScheduleStatus from "./ScheduleStatus";
import UsageStatus from "./UsageStatus";
import PaperAccountStatus from "./PaperAccountStatus";

type ServiceState = "checking" | "online" | "offline";
type SessionState =
  | { state: "checking" | "unavailable" | "setup_required" | "signed_out" }
  | { state: "signed_in"; csrfToken: string };

function isHealthyResponse(value: unknown): value is { status: "ok" } {
  return (
    typeof value === "object" &&
    value !== null &&
    "status" in value &&
    value.status === "ok"
  );
}

function passwordStrength(value: string): "weak" | "fair" | "strong" {
  if (value.length < 12) {
    return "weak";
  }
  const classes = [
    /[a-z]/.test(value),
    /[A-Z]/.test(value),
    /\d/.test(value),
    /[^A-Za-z0-9]/.test(value),
  ].filter(Boolean).length;
  if (value.length >= 16 && classes >= 3) {
    return "strong";
  }
  if (classes >= 2) {
    return "fair";
  }
  return "weak";
}

function parseSession(value: unknown): SessionState {
  if (typeof value !== "object" || value === null || !("state" in value)) {
    throw new Error("Invalid session response");
  }
  if (value.state === "setup_required" || value.state === "signed_out") {
    return { state: value.state };
  }
  if (
    value.state === "signed_in" &&
    "csrf_token" in value &&
    typeof value.csrf_token === "string" &&
    value.csrf_token.length > 0
  ) {
    return { state: "signed_in", csrfToken: value.csrf_token };
  }
  throw new Error("Invalid session response");
}

export default function App() {
  const [serviceState, setServiceState] = useState<ServiceState>("checking");
  const [retry, setRetry] = useState(0);
  const [session, setSession] = useState<SessionState>({ state: "checking" });
  const [sessionRetry, setSessionRetry] = useState(0);
  const [password, setPassword] = useState("");
  const [confirmation, setConfirmation] = useState("");
  const [pending, setPending] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    const controller = new AbortController();

    async function checkService() {
      try {
        const response = await fetch("/api/v1/system/health", {
          cache: "no-store",
          signal: controller.signal,
        });
        if (!response.ok) {
          throw new Error(`Health request returned ${response.status}`);
        }
        const body: unknown = await response.json();
        if (!isHealthyResponse(body)) {
          throw new Error("Health response was invalid");
        }
        setServiceState("online");
      } catch {
        if (!controller.signal.aborted) {
          setServiceState("offline");
        }
      }
    }

    void checkService();
    return () => controller.abort();
  }, [retry]);

  useEffect(() => {
    if (serviceState !== "online") {
      return;
    }
    const controller = new AbortController();

    async function checkSession() {
      try {
        const response = await fetch("/api/v1/auth/session", {
          cache: "no-store",
          signal: controller.signal,
        });
        if (!response.ok) {
          throw new Error("Session request failed");
        }
        const body: unknown = await response.json();
        setSession(parseSession(body));
      } catch {
        if (!controller.signal.aborted) {
          setSession({ state: "unavailable" });
        }
      }
    }

    void checkSession();
    return () => controller.abort();
  }, [serviceState, sessionRetry]);

  async function setOwnerPassword(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setPending(true);
    setError("");
    const submittedPassword = password;
    const submittedConfirmation = confirmation;
    setPassword("");
    setConfirmation("");
    if (submittedPassword !== submittedConfirmation) {
      setError("Passwords do not match.");
      setPending(false);
      return;
    }
    try {
      const response = await fetch("/api/v1/auth/setup", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          password: submittedPassword,
          confirmation: submittedConfirmation,
        }),
      });
      if (!response.ok) {
        let message = "Password setup failed.";
        try {
          const body = await response.json() as { detail?: unknown };
          if (typeof body.detail === "string") {
            message = body.detail;
          }
        } catch {
          // Keep the generic setup error.
        }
        setError(message);
        if (response.status === 409) {
          setSession({ state: "signed_out" });
        }
        return;
      }
      setSession({ state: "signed_out" });
    } catch {
      setError("Password setup failed. Check the local service and try again.");
    } finally {
      setPending(false);
    }
  }

  async function signIn(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setPending(true);
    setError("");
    const submittedPassword = password;
    setPassword("");
    try {
      const response = await fetch("/api/v1/auth/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ password: submittedPassword }),
      });
      if (!response.ok) {
        if (response.status === 401) {
          setError("Incorrect password.");
        } else if (response.status === 429) {
          setError("Too many attempts. Try again after 15 minutes.");
        } else if (response.status === 409) {
          setSession({ state: "setup_required" });
        } else {
          setError("Sign-in failed. Check the local service and try again.");
        }
        return;
      }
      const body: unknown = await response.json();
      const nextSession = parseSession(body);
      if (nextSession.state !== "signed_in") {
        throw new Error("Login did not return an authenticated session");
      }
      setSession(nextSession);
    } catch {
      setError("Sign-in failed. Check the local service and try again.");
    } finally {
      setPending(false);
    }
  }

  async function signOut() {
    if (session.state !== "signed_in") {
      return;
    }
    setPending(true);
    setError("");
    try {
      const response = await fetch("/api/v1/auth/logout", {
        method: "POST",
        headers: { "X-CSRF-Token": session.csrfToken },
      });
      if (response.status === 401) {
        setSession({ state: "signed_out" });
      } else if (!response.ok) {
        throw new Error("Sign-out failed");
      } else {
        setSession({ state: "signed_out" });
      }
    } catch {
      setError("Sign-out failed. Check the local service and try again.");
    } finally {
      setPending(false);
    }
  }

  const statusText = {
    checking: "Checking local service",
    online: "Local service online",
    offline: "Local service unavailable",
  }[serviceState];

  return (
    <main className="app-shell">
      <aside className="sidebar" aria-label="Primary navigation">
        <a className="wordmark" href="/" aria-label="BharatQuant home">
          <span className="wordmark-mark" aria-hidden="true">BQ</span>
          <span>BharatQuant</span>
        </a>

        {session.state === "signed_in" && (
          <nav className="sidebar-nav" aria-label="Workspace">
            <p className="nav-label">Workspace</p>
            <a className="nav-item nav-item--active" href="#today">
              <span aria-hidden="true">◫</span>Today
            </a>
            <a className="nav-item" href="#data">
              <span aria-hidden="true">▦</span>Data
            </a>
            <a className="nav-item" href="#schedules">
              <span aria-hidden="true">◷</span>Schedules
            </a>
            <a className="nav-item" href="#diagnostics">
              <span aria-hidden="true">⌁</span>Diagnostics
            </a>
            <p className="nav-label nav-label--secondary">Not available yet</p>
            <span className="nav-item nav-item--muted" aria-disabled="true">
              <span aria-hidden="true">◇</span>Trade
            </span>
            <span className="nav-item nav-item--muted" aria-disabled="true">
              <span aria-hidden="true">◈</span>Agent Lab
            </span>
            <span className="nav-item nav-item--muted" aria-disabled="true">
              <span aria-hidden="true">⚙</span>Settings
            </span>
          </nav>
        )}

        <div className="sidebar-footer">
          <span className="local-dot" aria-hidden="true" />
          <div>
            <strong>Local workspace</strong>
            <span>Loopback only</span>
          </div>
        </div>
      </aside>

      <div className="workspace">
        <header className="status-strip" aria-label="System status">
          <div className="status-strip__identity">
            <span className="breadcrumb">Today</span>
            <span className="mode-label">PAPER ONLY</span>
          </div>
          <div className={`service-status service-status--${serviceState}`}>
            <span className="status-indicator" aria-hidden="true" />
            <span role="status" aria-live="polite">{statusText}</span>
            {serviceState === "offline" && (
              <button
                className="retry-button"
                onClick={() => {
                  setServiceState("checking");
                  setRetry((value) => value + 1);
                }}
                type="button"
              >
                Retry
              </button>
            )}
          </div>
          {session.state === "signed_in" && (
            <button
              type="button"
              className="text-button sign-out-button"
              onClick={() => void signOut()}
              disabled={pending}
            >
              {pending ? "Signing out..." : "Sign out"}
            </button>
          )}
        </header>

        {session.state === "signed_in" && error && (
          <p className="auth-error session-error" role="alert">{error}</p>
        )}

        {serviceState === "online" && session.state !== "signed_in" && (
          <section className="auth-workspace" aria-labelledby="auth-title">
            <div className="auth-heading">
              <p className="eyebrow">PRIVATE LOCAL WORKSPACE</p>
              <h1 id="auth-title">
                {session.state === "setup_required" ? "Set up owner access" : "Welcome back"}
              </h1>
              <p>Research with discipline. Paper only; no broker connection.</p>
            </div>
            <section className="auth-card">
              {session.state === "checking" && <p>Checking owner access...</p>}
              {session.state === "setup_required" && (
                <form className="login-form setup-form" onSubmit={(event) => void setOwnerPassword(event)}>
                  <label htmlFor="setup-password">Create owner password</label>
                  <input
                    id="setup-password"
                    type="password"
                    autoComplete="new-password"
                    minLength={12}
                    maxLength={256}
                    value={password}
                    onChange={(event) => setPassword(event.target.value)}
                    disabled={pending}
                    required
                  />
                  <label htmlFor="setup-confirmation">Confirm password</label>
                  <input
                    id="setup-confirmation"
                    type="password"
                    autoComplete="new-password"
                    minLength={12}
                    maxLength={256}
                    value={confirmation}
                    onChange={(event) => setConfirmation(event.target.value)}
                    disabled={pending}
                    required
                  />
                  <p className={`password-strength password-strength--${passwordStrength(password)}`}>
                    Strength: {passwordStrength(password)}
                  </p>
                  {confirmation && password !== confirmation && (
                    <p className="password-match">Passwords do not match.</p>
                  )}
                  <button
                    type="submit"
                    className="auth-button"
                    disabled={pending || password.length < 12 || password !== confirmation}
                  >
                    {pending ? "Setting up..." : "Set owner password"}
                  </button>
                </form>
              )}
              {session.state === "unavailable" && (
                <p>
                  Owner access is unavailable.{" "}
                  <button
                    type="button"
                    className="text-button"
                    onClick={() => {
                      setSession({ state: "checking" });
                      setSessionRetry((value) => value + 1);
                    }}
                  >
                    Retry
                  </button>
                </p>
              )}
              {session.state === "signed_out" && (
                <form className="login-form" onSubmit={(event) => void signIn(event)}>
                  <label htmlFor="owner-password">Owner password</label>
                  <div className="login-row">
                    <input
                      id="owner-password"
                      type="password"
                      autoComplete="current-password"
                      maxLength={256}
                      value={password}
                      onChange={(event) => setPassword(event.target.value)}
                      disabled={pending}
                      required
                    />
                    <button type="submit" className="auth-button" disabled={pending}>
                      {pending ? "Signing in..." : "Sign in"}
                    </button>
                  </div>
                </form>
              )}
              {error && <p className="auth-error" role="alert">{error}</p>}
            </section>
          </section>
        )}

        {serviceState !== "online" && (
          <section className="offline-workspace" aria-labelledby="workspace-status-title">
            <p className="eyebrow">SYSTEM STATUS</p>
            <h1 id="workspace-status-title">
              {serviceState === "checking" ? "Checking local service" : "Local service unavailable"}
            </h1>
            <p>
              {serviceState === "checking"
                ? "Waiting for the loopback workspace..."
                : "Start the BharatQuant service and retry. No data is being shown."}
            </p>
          </section>
        )}

        {serviceState === "online" && session.state === "signed_in" && (
          <section className="dashboard" id="today" aria-labelledby="today-title">
            <header className="page-header">
              <div>
                <p className="eyebrow">COMMAND CENTER</p>
                <h1 id="today-title">Today</h1>
                <p className="intro">Research with discipline. Your local paper-only workspace.</p>
              </div>
              <span className="status-badge status-badge--provisional">FOUNDATION</span>
            </header>

            <section className="mode-banner" aria-label="Current mode">
              <span aria-hidden="true">i</span>
              <div>
                <strong>Operating in paper-only foundation mode</strong>
                <p>
                  Trading, live research, and agent workflows are not enabled.
                  Existing local diagnostics remain available below.
                </p>
              </div>
            </section>

            <div className="dashboard-grid" id="data">
              <PaperAccountStatus />
              <DataStatus />
              <div id="schedules">
                <ScheduleStatus />
              </div>
            </div>

            <div className="dashboard-grid dashboard-grid--diagnostics" id="diagnostics">
              <SnapshotFreshness />
              <CalendarCoverage />
              <UsageStatus />
            </div>
          </section>
        )}

        <footer className="footer">
          <span>Local-first · Built for one owner</span>
          <span>Paper environment · No broker connection</span>
        </footer>
      </div>
    </main>
  );
}
