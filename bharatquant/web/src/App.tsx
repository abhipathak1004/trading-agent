import { useEffect, useState } from "react";
import type { FormEvent } from "react";

import DataStatus from "./DataStatus";
import CalendarCoverage from "./CalendarCoverage";
import SnapshotFreshness from "./SnapshotFreshness";
import ScheduleStatus from "./ScheduleStatus";
import UsageStatus from "./UsageStatus";

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
    <main className="shell">
      <header className="topbar">
        <a className="wordmark" href="/" aria-label="BharatQuant home">
          <span className="wordmark-mark" aria-hidden="true">
            BQ
          </span>
          <span>BharatQuant</span>
        </a>
        <span className="local-label">
          <span className="local-dot" aria-hidden="true" />
          LOCAL WORKSPACE
        </span>
      </header>

      <section className="welcome" aria-labelledby="welcome-title">
        <p className="eyebrow">INDIAN MARKETS · RESEARCH · PAPER ONLY</p>
        <h1 id="welcome-title">
          Research with
          <br />
          <span>discipline.</span>
        </h1>
        <p className="intro">
          A local-first workspace for market research and paper trading. Real
          money is never at risk.
        </p>

        <div className={`service-status service-status--${serviceState}`}>
          <span className="status-indicator" aria-hidden="true" />
          <span role="status" aria-live="polite">
            {statusText}
          </span>
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

        {serviceState === "online" && (
          <section className="auth-panel" aria-labelledby="auth-title">
            <h2 id="auth-title">Owner access</h2>
            {session.state === "checking" && <p>Checking owner access...</p>}
            {session.state === "setup_required" && (
              <p>
                Set the owner password in a terminal with{" "}
                <code>uv run --locked bq auth setup</code>, then{" "}
                <button
                  type="button"
                  className="text-button"
                  onClick={() => {
                    setSession({ state: "checking" });
                    setSessionRetry((value) => value + 1);
                  }}
                >
                  check again
                </button>
                .
              </p>
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
            {session.state === "signed_in" && (
              <div className="signed-in">
                <p>Signed in as owner.</p>
                <button
                  type="button"
                  className="auth-button"
                  onClick={() => void signOut()}
                  disabled={pending}
                >
                  {pending ? "Signing out..." : "Sign out"}
                </button>
              </div>
            )}
            {error && (
              <p className="auth-error" role="alert">
                {error}
              </p>
            )}
          </section>
        )}
        {serviceState === "online" && session.state === "signed_in" && <DataStatus />}
        {serviceState === "online" && session.state === "signed_in" && <SnapshotFreshness />}
        {serviceState === "online" && session.state === "signed_in" && <CalendarCoverage />}
        {serviceState === "online" && session.state === "signed_in" && <ScheduleStatus />}
        {serviceState === "online" && session.state === "signed_in" && <UsageStatus />}
      </section>

      <footer className="footer">
        <span>Local-first · Built for one owner</span>
        <span>Paper environment · No broker connection</span>
      </footer>
    </main>
  );
}
