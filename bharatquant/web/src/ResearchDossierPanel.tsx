import { useEffect, useState } from "react";
import type { FormEvent } from "react";

import { parseDossier } from "./researchContract.ts";
import type { Dossier, ResearchClaim } from "./researchContract.ts";

function formatPaise(value: string): string {
  return `₹${(Number(value) / 100).toLocaleString("en-IN", {
    maximumFractionDigits: 2,
  })}`;
}

function toPaise(value: string): number | null {
  const amount = Number(value);
  return Number.isFinite(amount) && amount >= 0 ? Math.round(amount * 100) : null;
}

function bpsPercent(value: unknown): string {
  return typeof value === "number" ? `${(value / 100).toFixed(2)}%` : "n/a";
}

export default function ResearchDossierPanel({ csrfToken }: { csrfToken: string }) {
  const [instrument, setInstrument] = useState("NSE:RELIANCE");
  const [price, setPrice] = useState("");
  const [dossier, setDossier] = useState<Dossier | null>(null);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);

  const [sourceType, setSourceType] = useState("YOUTUBE");
  const [sourceId, setSourceId] = useState("owner-youtube");
  const [noteTitle, setNoteTitle] = useState("");
  const [noteBody, setNoteBody] = useState("");
  const [noteUrl, setNoteUrl] = useState("");
  const [published, setPublished] = useState(new Date().toISOString().slice(0, 10));

  const [periodEnd, setPeriodEnd] = useState(
    new Date(Date.now() - 30 * 86_400_000).toISOString().slice(0, 10),
  );
  const [revenue, setRevenue] = useState("");
  const [ebitda, setEbitda] = useState("");
  const [netIncome, setNetIncome] = useState("");
  const [debt, setDebt] = useState("");
  const [cash, setCash] = useState("");
  const [shares, setShares] = useState("");
  const [book, setBook] = useState("");
  const [fcf, setFcf] = useState("");
  const [claim, setClaim] = useState("");

  async function loadDossier() {
    setBusy(true);
    setError("");
    try {
      const query = price.trim() === "" ? "" : `?price_paise=${toPaise(price) ?? ""}`;
      const response = await fetch(
        `/api/v1/research/instruments/${encodeURIComponent(instrument)}/dossier${query}`,
        { cache: "no-store" },
      );
      if (!response.ok) {
        const detail = (await response.json()) as { detail?: unknown };
        throw new Error(
          typeof detail.detail === "string" ? detail.detail : "Dossier unavailable.",
        );
      }
      setDossier(parseDossier(await response.json()));
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Dossier unavailable.");
    } finally {
      setBusy(false);
    }
  }

  useEffect(() => {
    void loadDossier();
    // The initial instrument is the only automatic load.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function addNote(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy(true);
    setError("");
    setMessage("");
    try {
      const response = await fetch("/api/v1/research/notes", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": csrfToken,
        },
        body: JSON.stringify({
          instrument_id: instrument,
          source_type: sourceType,
          source_id: sourceId,
          url: noteUrl.trim() === "" ? null : noteUrl.trim(),
          title: noteTitle,
          body: noteBody,
          published_at: new Date(`${published}T00:00:00Z`).toISOString(),
          known_at: new Date().toISOString(),
        }),
      });
      if (!response.ok) {
        const detail = (await response.json()) as { detail?: unknown };
        throw new Error(
          typeof detail.detail === "string" ? detail.detail : "Note could not be saved.",
        );
      }
      setMessage("Point-in-time note saved.");
      setNoteTitle("");
      setNoteBody("");
      setNoteUrl("");
      await loadDossier();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Note could not be saved.");
    } finally {
      setBusy(false);
    }
  }

  async function addFundamentals(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy(true);
    setError("");
    setMessage("");
    const values = {
      revenue_paise: toPaise(revenue),
      ebitda_paise: toPaise(ebitda),
      net_income_paise: toPaise(netIncome),
      total_debt_paise: toPaise(debt),
      cash_paise: toPaise(cash),
      book_value_paise: toPaise(book),
      fcf_paise: toPaise(fcf),
      shares_outstanding: Number(shares),
    };
    if (
      Object.entries(values).some(([key, value]) =>
        key === "net_income_paise" || key === "fcf_paise"
          ? value === null
          : value === null || value < 0,
      ) ||
      !Number.isInteger(values.shares_outstanding) ||
      values.shares_outstanding < 1
    ) {
      setError("Enter valid fundamental values and shares.");
      setBusy(false);
      return;
    }
    try {
      const response = await fetch("/api/v1/research/fundamentals", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": csrfToken,
        },
        body: JSON.stringify({
          instrument_id: instrument,
          source: "XBRL",
          period_end: new Date(`${periodEnd}T00:00:00Z`).toISOString(),
          known_at: new Date().toISOString(),
          ...values,
        }),
      });
      if (!response.ok) {
        const detail = (await response.json()) as { detail?: unknown };
        throw new Error(
          typeof detail.detail === "string"
            ? detail.detail
            : "Fundamentals could not be saved.",
        );
      }
      setMessage("Fundamental snapshot saved.");
      await loadDossier();
    } catch (caught) {
      setError(
        caught instanceof Error ? caught.message : "Fundamentals could not be saved.",
      );
    } finally {
      setBusy(false);
    }
  }

  async function addClaim(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy(true);
    setError("");
    setMessage("");
    try {
      const response = await fetch("/api/v1/research/claims", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": csrfToken,
        },
        body: JSON.stringify({
          instrument_id: instrument,
          statement: claim,
          note_id: dossier?.notes[0]?.note_id ?? null,
        }),
      });
      if (!response.ok) {
        throw new Error("Claim could not be saved.");
      }
      setMessage("Claim queued for owner review.");
      setClaim("");
      await loadDossier();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Claim could not be saved.");
    } finally {
      setBusy(false);
    }
  }

  async function review(claim: ResearchClaim, decision: "ACCEPTED" | "REJECTED") {
    setBusy(true);
    setError("");
    try {
      const response = await fetch(
        `/api/v1/research/claims/${encodeURIComponent(claim.claim_id)}/review`,
        {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-CSRF-Token": csrfToken,
          },
          body: JSON.stringify({
            decision,
            note: decision === "ACCEPTED" ? "Accepted in Research" : "Rejected in Research",
          }),
        },
      );
      if (!response.ok) {
        throw new Error("Claim review failed.");
      }
      setMessage(`Claim ${decision.toLowerCase()}.`);
      await loadDossier();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Claim review failed.");
    } finally {
      setBusy(false);
    }
  }

  const pending = dossier?.claims.filter((item) => item.status === "PENDING") ?? [];

  return (
    <section className="settings-panel" aria-labelledby="research-title">
      <h2 id="research-title">Research dossier</h2>
      <p className="panel-note">
        Notes are point-in-time and append-only. Ratios and DCF come from a
        deterministic snapshot, never from model text. Claims wait for an owner
        decision.
      </p>
      <div className="models-grid models-grid--compact">
        <label htmlFor="research-instrument">
          Instrument
          <input
            id="research-instrument"
            value={instrument}
            onChange={(event) => setInstrument(event.target.value)}
          />
        </label>
        <label htmlFor="research-price">
          Market price (₹, optional)
          <input
            id="research-price"
            type="number"
            min="0"
            step="0.01"
            value={price}
            onChange={(event) => setPrice(event.target.value)}
          />
        </label>
      </div>
      <div className="strategy-examples">
        <button
          type="button"
          className="auth-button"
          disabled={busy}
          onClick={() => void loadDossier()}
        >
          Load dossier
        </button>
      </div>
      {error !== "" && <p className="auth-error" role="alert">{error}</p>}
      {message !== "" && <p className="panel-note" role="status">{message}</p>}

      {dossier !== null && (
        <>
          <dl className="settings-facts">
            <div>
              <dt>Notes</dt>
              <dd>{dossier.notes.length}</dd>
            </div>
            <div>
              <dt>Fundamental snapshots</dt>
              <dd>{dossier.fundamentals.length}</dd>
            </div>
            <div>
              <dt>Ratio / DCF</dt>
              <dd>
                {dossier.ratios === null ? "n/a" : "available"} /{" "}
                {dossier.dcf === null ? "n/a" : "available"}
              </dd>
            </div>
            <div>
              <dt>Pending claims</dt>
              <dd>{pending.length}</dd>
            </div>
          </dl>

          {dossier.ratios !== null && (
            <p className="panel-note">
              ROE {bpsPercent(dossier.ratios.roe_bps)} · Debt/equity{" "}
              {bpsPercent(dossier.ratios.debt_equity_bps)} · EBITDA margin{" "}
              {bpsPercent(dossier.ratios.ebitda_margin_bps)} · Net margin{" "}
              {bpsPercent(dossier.ratios.net_margin_bps)}
            </p>
          )}
          {dossier.dcf !== null && (
            <p className="panel-note">
              DCF per share {formatPaise(String(dossier.dcf.per_share_paise))} · upside{" "}
              {typeof dossier.dcf.upside_bps === "number"
                ? bpsPercent(dossier.dcf.upside_bps)
                : "n/a"}
            </p>
          )}

          {dossier.notes.length > 0 && (
            <table className="judgment-table">
              <caption>Research notes</caption>
              <thead>
                <tr>
                  <th scope="col">Known</th>
                  <th scope="col">Source</th>
                  <th scope="col">Title</th>
                </tr>
              </thead>
              <tbody>
                {dossier.notes.map((note) => (
                  <tr key={note.note_id}>
                    <th scope="row">{note.known_at.slice(0, 10)}</th>
                    <td>{note.source_type}</td>
                    <td>
                      {note.url === null ? (
                        note.title
                      ) : (
                        <a href={note.url} target="_blank" rel="noreferrer">
                          {note.title}
                        </a>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}

          {dossier.theses.length > 0 && (
            <table className="judgment-table">
              <caption>Theses &amp; invalidation evidence</caption>
              <thead>
                <tr>
                  <th scope="col">Thesis</th>
                  <th scope="col">Horizon</th>
                  <th scope="col">Events</th>
                </tr>
              </thead>
              <tbody>
                {dossier.theses.map((thesis) => (
                  <tr key={thesis.thesis_id}>
                    <th scope="row">{thesis.thesis_id}</th>
                    <td>{thesis.horizon}</td>
                    <td>
                      {thesis.events
                        .map((item) =>
                          typeof item.kind === "string" ? item.kind : "NOTE",
                        )
                        .join(", ")}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}

          {pending.length > 0 && (
            <table className="judgment-table">
              <caption>Claims waiting for owner review</caption>
              <thead>
                <tr>
                  <th scope="col">Claim</th>
                  <th scope="col">Decision</th>
                </tr>
              </thead>
              <tbody>
                {pending.map((item) => (
                  <tr key={item.claim_id}>
                    <th scope="row">{item.statement}</th>
                    <td>
                      <button
                        type="button"
                        className="text-button"
                        onClick={() => void review(item, "ACCEPTED")}
                      >
                        Accept
                      </button>{" "}
                      <button
                        type="button"
                        className="text-button"
                        onClick={() => void review(item, "REJECTED")}
                      >
                        Reject
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </>
      )}

      <form className="config-form" onSubmit={(event) => void addNote(event)}>
        <h3>Add point-in-time note</h3>
        <div className="models-grid models-grid--compact">
          <label htmlFor="research-source">
            Source
            <select
              id="research-source"
              value={sourceType}
              onChange={(event) => setSourceType(event.target.value)}
            >
              <option value="YOUTUBE">YouTube</option>
              <option value="NEWS">News</option>
              <option value="FILING">Filing</option>
              <option value="XBRL">XBRL</option>
              <option value="OWNER">Owner</option>
            </select>
          </label>
          <label htmlFor="research-source-id">
            Source id
            <input
              id="research-source-id"
              value={sourceId}
              onChange={(event) => setSourceId(event.target.value)}
            />
          </label>
          <label htmlFor="research-published">
            Published
            <input
              id="research-published"
              type="date"
              value={published}
              onChange={(event) => setPublished(event.target.value)}
            />
          </label>
          <label htmlFor="research-url">
            URL (HTTPS, optional)
            <input
              id="research-url"
              value={noteUrl}
              onChange={(event) => setNoteUrl(event.target.value)}
            />
          </label>
        </div>
        <label htmlFor="research-note-title">
          Title
          <input
            id="research-note-title"
            value={noteTitle}
            onChange={(event) => setNoteTitle(event.target.value)}
          />
        </label>
        <label htmlFor="research-note-body">
          Note
          <textarea
            id="research-note-body"
            rows={4}
            value={noteBody}
            onChange={(event) => setNoteBody(event.target.value)}
          />
        </label>
        <div className="strategy-examples">
          <button type="submit" className="auth-button" disabled={busy}>
            Save note
          </button>
        </div>
      </form>

      <form className="config-form" onSubmit={(event) => void addFundamentals(event)}>
        <h3>XBRL / fundamental snapshot</h3>
        <div className="models-grid models-grid--compact">
          <label htmlFor="research-period">
            Period end
            <input
              id="research-period"
              type="date"
              value={periodEnd}
              onChange={(event) => setPeriodEnd(event.target.value)}
            />
          </label>
          <label htmlFor="research-revenue">
            Revenue (₹)
            <input id="research-revenue" type="number" min="0" value={revenue} onChange={(event) => setRevenue(event.target.value)} />
          </label>
          <label htmlFor="research-ebitda">
            EBITDA (₹)
            <input id="research-ebitda" type="number" min="0" value={ebitda} onChange={(event) => setEbitda(event.target.value)} />
          </label>
          <label htmlFor="research-net">
            Net income (₹)
            <input id="research-net" type="number" value={netIncome} onChange={(event) => setNetIncome(event.target.value)} />
          </label>
          <label htmlFor="research-debt">
            Total debt (₹)
            <input id="research-debt" type="number" min="0" value={debt} onChange={(event) => setDebt(event.target.value)} />
          </label>
          <label htmlFor="research-cash">
            Cash (₹)
            <input id="research-cash" type="number" min="0" value={cash} onChange={(event) => setCash(event.target.value)} />
          </label>
          <label htmlFor="research-shares">
            Shares outstanding
            <input id="research-shares" type="number" min="1" value={shares} onChange={(event) => setShares(event.target.value)} />
          </label>
          <label htmlFor="research-book">
            Book value (₹)
            <input id="research-book" type="number" min="0" value={book} onChange={(event) => setBook(event.target.value)} />
          </label>
          <label htmlFor="research-fcf">
            Free cash flow (₹)
            <input id="research-fcf" type="number" value={fcf} onChange={(event) => setFcf(event.target.value)} />
          </label>
        </div>
        <div className="strategy-examples">
          <button type="submit" className="auth-button" disabled={busy}>
            Save snapshot
          </button>
        </div>
      </form>

      <form className="config-form" onSubmit={(event) => void addClaim(event)}>
        <h3>Queue a research claim</h3>
        <label htmlFor="research-claim">
          Statement
          <textarea
            id="research-claim"
            rows={3}
            value={claim}
            onChange={(event) => setClaim(event.target.value)}
          />
        </label>
        <div className="strategy-examples">
          <button type="submit" className="auth-button" disabled={busy}>
            Queue for review
          </button>
        </div>
      </form>
    </section>
  );
}
