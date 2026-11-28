import { useEffect, useState } from "react";
import type { FormEvent } from "react";

import { parsePlaybookList, parsePlaybookResponse } from "./playbookContract";
import type { Playbook } from "./playbookContract";

type LoadState =
  | { state: "loading" | "error" }
  | { state: "ready"; playbooks: Playbook[] };

type SaveState =
  | { state: "idle" | "saving" }
  | { state: "saved"; message: string }
  | { state: "failed"; message: string };

export default function PlaybookPanel({ csrfToken }: { csrfToken: string }) {
  const [load, setLoad] = useState<LoadState>({ state: "loading" });
  const [retry, setRetry] = useState(0);
  const [selectedId, setSelectedId] = useState("momentum_continuation");
  const [content, setContent] = useState("");
  const [save, setSave] = useState<SaveState>({ state: "idle" });

  useEffect(() => {
    const controller = new AbortController();
    async function loadPlaybooks() {
      try {
        const response = await fetch("/api/v1/memory/playbooks", {
          cache: "no-store",
          signal: controller.signal,
        });
        if (!response.ok) {
          throw new Error("Playbooks unavailable");
        }
        const playbooks = parsePlaybookList(await response.json());
        setLoad({ state: "ready", playbooks });
        const selected =
          playbooks.find((item) => item.playbook_id === selectedId) ?? playbooks[0];
        if (selected !== undefined) {
          setSelectedId(selected.playbook_id);
          setContent(selected.content);
        }
      } catch {
        if (!controller.signal.aborted) {
          setLoad({ state: "error" });
        }
      }
    }
    void loadPlaybooks();
    return () => controller.abort();
    // selectedId is read once per load; the dropdown drives later changes.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [retry]);

  const playbooks = load.state === "ready" ? load.playbooks : [];
  const selected =
    playbooks.find((item) => item.playbook_id === selectedId) ?? null;

  function selectPlaybook(next: string) {
    const playbook = playbooks.find((item) => item.playbook_id === next);
    setSelectedId(next);
    if (playbook !== undefined) {
      setContent(playbook.content);
    }
    setSave({ state: "idle" });
  }

  function replace(playbook: Playbook) {
    if (load.state !== "ready") {
      return;
    }
    setLoad({
      state: "ready",
      playbooks: load.playbooks.map((item) =>
        item.playbook_id === playbook.playbook_id ? playbook : item,
      ),
    });
    setContent(playbook.content);
  }

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (selected === null) {
      return;
    }
    setSave({ state: "saving" });
    try {
      const response = await fetch(
        `/api/v1/memory/playbooks/${encodeURIComponent(selected.playbook_id)}`,
        {
          method: "PUT",
          headers: {
            "Content-Type": "application/json",
            "X-CSRF-Token": csrfToken,
          },
          body: JSON.stringify({ content }),
        },
      );
      if (!response.ok) {
        let message = "Saving the playbook failed.";
        try {
          const body = (await response.json()) as { detail?: unknown };
          if (typeof body.detail === "string") {
            message = body.detail;
          }
        } catch {
          // Keep the generic message.
        }
        setSave({ state: "failed", message });
        return;
      }
      const saved = parsePlaybookResponse(await response.json());
      replace(saved);
      setSave({ state: "saved", message: `Saved version ${saved.version}.` });
    } catch {
      setSave({
        state: "failed",
        message: "Saving the playbook failed. Check the local service.",
      });
    }
  }

  async function reset() {
    if (selected === null) {
      return;
    }
    setSave({ state: "saving" });
    try {
      const response = await fetch(
        `/api/v1/memory/playbooks/${encodeURIComponent(selected.playbook_id)}/reset`,
        { method: "POST", headers: { "X-CSRF-Token": csrfToken } },
      );
      if (!response.ok) {
        setSave({ state: "failed", message: "Resetting the playbook failed." });
        return;
      }
      const resetPlaybook = parsePlaybookResponse(await response.json());
      replace(resetPlaybook);
      setSave({
        state: "saved",
        message: `Reset saved as version ${resetPlaybook.version}.`,
      });
    } catch {
      setSave({
        state: "failed",
        message: "Resetting the playbook failed. Check the local service.",
      });
    }
  }

  return (
    <section className="settings-panel" aria-labelledby="playbook-title">
      <h2 id="playbook-title">Procedural playbooks</h2>
      <p className="panel-note">
        Guidance the Super Agent receives for a strategy run. Playbooks shape
        ranking commentary; thresholds, sizing and risk stay in code. Saves are
        versioned and reset restores the default as a new version.
      </p>
      {load.state === "loading" && <p role="status">Loading playbooks...</p>}
      {load.state === "error" && (
        <p role="alert">
          Playbooks are unavailable.{" "}
          <button
            type="button"
            className="text-button"
            onClick={() => {
              setLoad({ state: "loading" });
              setRetry((value) => value + 1);
            }}
          >
            Retry
          </button>
        </p>
      )}
      {load.state === "ready" && selected !== null && (
        <form className="config-form" onSubmit={(event) => void submit(event)}>
          <label htmlFor="playbook-select">Playbook</label>
          <select
            id="playbook-select"
            value={selectedId}
            onChange={(event) => selectPlaybook(event.target.value)}
          >
            {playbooks.map((playbook) => (
              <option key={playbook.playbook_id} value={playbook.playbook_id}>
                {playbook.name} · {playbook.playbook_id}
              </option>
            ))}
          </select>
          <p className="panel-note">{selected.description}</p>
          <p className="prompt-meta">
            version {selected.version}
            {selected.is_default ? " · default" : " · customized"}
            {selected.updated_at !== null
              ? ` · ${new Date(selected.updated_at).toLocaleString()}`
              : " · never saved"}
          </p>
          <label htmlFor="playbook-content">Guidance</label>
          <textarea
            id="playbook-content"
            rows={10}
            value={content}
            onChange={(event) => setContent(event.target.value)}
          />
          <div className="prompt-actions">
            <button
              type="submit"
              className="auth-button"
              disabled={save.state === "saving" || !content.trim()}
            >
              {save.state === "saving" ? "Saving..." : "Save playbook version"}
            </button>
            <button
              type="button"
              className="text-button"
              onClick={() => void reset()}
              disabled={save.state === "saving"}
            >
              Reset to default
            </button>
          </div>
        </form>
      )}
      {save.state === "saved" && (
        <p className="config-feedback" role="status">
          {save.message}
        </p>
      )}
      {save.state === "failed" && (
        <p className="auth-error" role="alert">
          {save.message}
        </p>
      )}
    </section>
  );
}
