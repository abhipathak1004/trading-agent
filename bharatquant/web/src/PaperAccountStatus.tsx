import { useEffect, useState } from "react";

type AccountStatus = {
  account_id: string;
  name: string;
  status: "ACTIVE" | "PAUSED" | "ARCHIVED";
  opening_balance_paise: string;
  deposited_paise: string;
  withdrawn_paise: string;
  balance_paise: string;
};

type State =
  | { state: "loading" | "error" }
  | { state: "ready"; account: AccountStatus };

export default function PaperAccountStatus() {
  const [state, setState] = useState<State>({ state: "loading" });

  useEffect(() => {
    const controller = new AbortController();
    async function load() {
      try {
        const response = await fetch("/api/v1/paper/accounts/paper-main", {
          cache: "no-store",
          signal: controller.signal,
        });
        if (response.status === 404) {
          setState({ state: "error" });
          return;
        }
        if (!response.ok) {
          throw new Error("Paper account unavailable");
        }
        setState({ state: "ready", account: await response.json() as AccountStatus });
      } catch {
        if (!controller.signal.aborted) {
          setState({ state: "error" });
        }
      }
    }
    void load();
    return () => controller.abort();
  }, []);

  return (
    <section className="data-panel" aria-labelledby="paper-account-title">
      <h2 id="paper-account-title">Paper account</h2>
      {state.state === "loading" && <p role="status">Checking paper account...</p>}
      {state.state === "error" && <p role="status">Paper-Main not configured.</p>}
      {state.state === "ready" && (
        <>
          <dl>
            <dt>Name</dt><dd>{state.account.name}</dd>
            <dt>Status</dt><dd>{state.account.status}</dd>
            <dt>Opening balance</dt><dd>{state.account.opening_balance_paise} paise</dd>
            <dt>Deposits</dt><dd>{state.account.deposited_paise} paise</dd>
            <dt>Withdrawals</dt><dd>{state.account.withdrawn_paise} paise</dd>
            <dt>Current balance</dt><dd>{state.account.balance_paise} paise</dd>
          </dl>
          <p>Paper simulation only. No broker connection or live order is enabled.</p>
        </>
      )}
    </section>
  );
}
