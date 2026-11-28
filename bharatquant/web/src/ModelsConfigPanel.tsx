import { useEffect, useState } from "react";
import type { FormEvent } from "react";

import {
  DECISION_FALLBACKS,
  PROVIDERS,
  TIER_NAMES,
  parseModelsConfig,
} from "./settingsContract";
import type {
  AgentRoute,
  DecisionFallback,
  DecisionPolicy,
  KeyProvider,
  ModelsConfig,
  PipelineConfig,
  TierConfig,
  TierName,
} from "./settingsContract";
import { parseLocalModels } from "./modelsContract";
import type { LocalModel } from "./modelsContract";

type LoadState =
  | { state: "loading" }
  | { state: "error" }
  | { state: "ready"; models: ModelsConfig };

type SaveState =
  | { state: "idle" | "saving" }
  | { state: "saved" }
  | { state: "failed"; message: string };

const PREVIEW_LABELS: Record<ModelsConfig["decision_preview"], string> = {
  jev: "TypeSafe Jev",
  flash: "Flash tier",
  reasoning: "Reasoning tier",
  heuristic: "deterministic heuristic",
  rules_only: "rules only",
};

function numberValue(event: { currentTarget: { valueAsNumber: number } }): number {
  const value = event.currentTarget.valueAsNumber;
  return Number.isFinite(value) ? value : 0;
}

export default function ModelsConfigPanel({ csrfToken }: { csrfToken: string }) {
  const [load, setLoad] = useState<LoadState>({ state: "loading" });
  const [retry, setRetry] = useState(0);
  const [save, setSave] = useState<SaveState>({ state: "idle" });
  const [localModels, setLocalModels] = useState<LocalModel[]>([]);

  useEffect(() => {
    const controller = new AbortController();
    async function loadModels() {
      try {
        const [configResponse, modelsResponse] = await Promise.all([
          fetch("/api/v1/settings/config", {
            cache: "no-store",
            signal: controller.signal,
          }),
          fetch("/api/v1/models", {
            cache: "no-store",
            signal: controller.signal,
          }),
        ]);
        if (!configResponse.ok) {
          throw new Error("Models configuration unavailable");
        }
        const value: unknown = await configResponse.json();
        setLoad({ state: "ready", models: parseModelsConfig(value) });
        if (modelsResponse.ok) {
          try {
            setLocalModels(parseLocalModels(await modelsResponse.json()));
          } catch {
            setLocalModels([]);
          }
        }
      } catch {
        if (!controller.signal.aborted) {
          setLoad({ state: "error" });
        }
      }
    }
    void loadModels();
    return () => controller.abort();
  }, [retry]);

  if (load.state === "loading") {
    return (
      <section className="settings-panel" aria-label="Model tiers">
        <p role="status">Loading model tiers...</p>
      </section>
    );
  }
  if (load.state === "error") {
    return (
      <section className="settings-panel" aria-label="Model tiers">
        <p role="alert">
          Model tiers are unavailable.{" "}
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
      </section>
    );
  }

  const models = load.models;

  function updateTier(name: TierName, patch: Partial<TierConfig>) {
    setLoad({
      state: "ready",
      models: {
        ...models,
        tiers: { ...models.tiers, [name]: { ...models.tiers[name], ...patch } },
      },
    });
  }

  function updatePolicy(patch: Partial<DecisionPolicy>) {
    setLoad({
      state: "ready",
      models: {
        ...models,
        decision_policy: { ...models.decision_policy, ...patch },
      },
    });
  }

  function updatePipeline(patch: Partial<PipelineConfig>) {
    setLoad({
      state: "ready",
      models: { ...models, pipeline: { ...models.pipeline, ...patch } },
    });
  }

  function updateRoute(agentId: string, tier: TierName) {
    const route = models.agent_routing[agentId];
    const next: AgentRoute = { ...route, tier };
    setLoad({
      state: "ready",
      models: {
        ...models,
        agent_routing: { ...models.agent_routing, [agentId]: next },
      },
    });
  }

  function fallbackSlots(): (DecisionFallback | "none")[] {
    const order: (DecisionFallback | "none")[] = [...models.pipeline.decision_fallback_order];
    while (order.length < 3) {
      order.push("none");
    }
    return order.slice(0, 3);
  }

  function setFallbackSlot(index: number, value: string) {
    const slots = fallbackSlots();
    slots[index] = value === "none" ? "none" : (value as DecisionFallback);
    updatePipeline({
      decision_fallback_order: slots.filter(
        (item): item is DecisionFallback => item !== "none",
      ),
    });
  }

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setSave({ state: "saving" });
    const order = models.pipeline.decision_fallback_order;
    if (order.length === 0 || new Set(order).size !== order.length) {
      setSave({
        state: "failed",
        message: "The fallback order needs at least one source and no duplicates.",
      });
      return;
    }
    const body = {
      tiers: Object.fromEntries(
        TIER_NAMES.map((name) => {
          const tier = models.tiers[name];
          return [
            name,
            {
              provider: tier.provider,
              model: tier.model,
              temperature: tier.temperature,
              max_tokens: tier.max_tokens,
              timeout_ms: tier.timeout_ms,
              max_attempts: tier.max_attempts,
              fallback_tier: tier.fallback_tier,
            },
          ];
        }),
      ),
      agent_routing: Object.fromEntries(
        Object.entries(models.agent_routing).map(([agentId, route]) => [
          agentId,
          { tier: route.tier, provider: route.provider, model: route.model },
        ]),
      ),
      decision_policy: models.decision_policy,
      pipeline: models.pipeline,
      decision_route: models.decision_route,
    };
    try {
      const response = await fetch("/api/v1/settings/models", {
        method: "PUT",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken },
        body: JSON.stringify(body),
      });
      if (!response.ok) {
        let message = "Saving model tiers failed.";
        try {
          const detail = (await response.json()) as { detail?: unknown };
          if (typeof detail.detail === "string") {
            message = detail.detail;
          }
        } catch {
          // Keep the generic message.
        }
        setSave({ state: "failed", message });
        return;
      }
      setLoad({ state: "ready", models: parseModelsConfig(await response.json()) });
      setSave({ state: "saved" });
    } catch {
      setSave({
        state: "failed",
        message: "Saving model tiers failed. Check the local service.",
      });
    }
  }

  const slots = fallbackSlots();

  return (
    <section className="settings-panel" aria-labelledby="models-title">
      <h2 id="models-title">Model tiers, decision policy &amp; routing</h2>
      <p className="panel-note">
        Decision questions use TypeSafe Jev when it is configured and reachable.
        Otherwise they fall back in the order below, ending in a deterministic
        heuristic. Thresholds live in this configuration, never in a prompt.
      </p>
      <p className="decision-preview" role="status">
        Decision will run on: <strong>{PREVIEW_LABELS[models.decision_preview]}</strong>
      </p>

      <form className="config-form" onSubmit={(event) => void submit(event)}>
        <datalist id="bq-local-models">
          {localModels.map((model) => (
            <option key={model.id} value={model.model}>
              {model.label}
            </option>
          ))}
        </datalist>
        <div className="models-grid">
          {TIER_NAMES.map((name) => {
            const tier = models.tiers[name];
            return (
              <article className="tier-card" key={name}>
                <h3>
                  {name}
                  {name === "decision" ? " · Jev is optional" : ""}
                </h3>
                <label htmlFor={`tier-${name}-provider`}>Provider</label>
                <select
                  id={`tier-${name}-provider`}
                  value={tier.provider ?? ""}
                  onChange={(event) =>
                    updateTier(name, {
                      provider:
                        event.target.value === ""
                          ? null
                          : (event.target.value as KeyProvider),
                    })
                  }
                >
                  <option value="">None</option>
                  {(name === "decision" ? ["TYPESAFE_JEV"] : PROVIDERS).map(
                    (provider) => (
                      <option key={provider} value={provider}>
                        {provider}
                      </option>
                    ),
                  )}
                </select>

                <label htmlFor={`tier-${name}-model`}>Model</label>
                <input
                  id={`tier-${name}-model`}
                  type="text"
                  list="bq-local-models"
                  value={tier.model}
                  onChange={(event) => {
                    const value = event.target.value;
                    const match = localModels.find((item) => item.model === value);
                    updateTier(
                      name,
                      match === undefined
                        ? { model: value }
                        : { model: value, provider: "CUSTOM" },
                    );
                  }}
                />

                <label htmlFor={`tier-${name}-temperature`}>Temperature</label>
                <input
                  id={`tier-${name}-temperature`}
                  type="number"
                  step="0.05"
                  min="0"
                  max="2"
                  value={tier.temperature}
                  onChange={(event) =>
                    updateTier(name, { temperature: numberValue(event) })
                  }
                />

                <label htmlFor={`tier-${name}-tokens`}>Max tokens</label>
                <input
                  id={`tier-${name}-tokens`}
                  type="number"
                  min="1"
                  value={tier.max_tokens}
                  onChange={(event) =>
                    updateTier(name, { max_tokens: numberValue(event) })
                  }
                />

                <label htmlFor={`tier-${name}-timeout`}>Timeout (ms)</label>
                <input
                  id={`tier-${name}-timeout`}
                  type="number"
                  min="100"
                  value={tier.timeout_ms}
                  onChange={(event) =>
                    updateTier(name, { timeout_ms: numberValue(event) })
                  }
                />

                <label htmlFor={`tier-${name}-attempts`}>Attempts</label>
                <input
                  id={`tier-${name}-attempts`}
                  type="number"
                  min="1"
                  max="3"
                  value={tier.max_attempts}
                  onChange={(event) =>
                    updateTier(name, { max_attempts: numberValue(event) })
                  }
                />

                <label htmlFor={`tier-${name}-fallback`}>Fallback tier</label>
                <select
                  id={`tier-${name}-fallback`}
                  value={tier.fallback_tier ?? ""}
                  onChange={(event) =>
                    updateTier(name, {
                      fallback_tier:
                        event.target.value === ""
                          ? null
                          : (event.target.value as TierName),
                    })
                  }
                >
                  <option value="">None</option>
                  {TIER_NAMES.filter((item) => item !== name).map((item) => (
                    <option key={item} value={item}>
                      {item}
                    </option>
                  ))}
                </select>
              </article>
            );
          })}
        </div>

        <p className="panel-note">
          Choosing a registered local model switches that tier to CUSTOM. Saving
          derives the local endpoint from the model; only one local endpoint can
          be routed at a time.
        </p>

        <h3>Decision fallback order</h3>
        <div className="fallback-row">
          {[0, 1, 2].map((index) => (
            <select
              key={index}
              aria-label={`Fallback ${index + 1}`}
              value={slots[index]}
              onChange={(event) => setFallbackSlot(index, event.target.value)}
            >
              <option value="none">None</option>
              {DECISION_FALLBACKS.map((item) => (
                <option key={item} value={item}>
                  {item}
                </option>
              ))}
            </select>
          ))}
        </div>

        <h3>Decision policy</h3>
        <div className="models-grid models-grid--compact">
          <label htmlFor="policy-confidence">
            Minimum choice confidence
            <input
              id="policy-confidence"
              type="number"
              step="0.05"
              min="0"
              max="1"
              value={models.decision_policy.min_choice_confidence}
              onChange={(event) =>
                updatePolicy({ min_choice_confidence: numberValue(event) })
              }
            />
          </label>
          <label htmlFor="policy-noul">
            Minimum noul probability
            <input
              id="policy-noul"
              type="number"
              step="0.05"
              min="0"
              max="1"
              value={models.decision_policy.min_noul_probability}
              onChange={(event) =>
                updatePolicy({ min_noul_probability: numberValue(event) })
              }
            />
          </label>
          <label htmlFor="policy-band-low">
            Abstain band low
            <input
              id="policy-band-low"
              type="number"
              min="0"
              max="4"
              value={models.decision_policy.abstain_score_band[0]}
              onChange={(event) =>
                updatePolicy({
                  abstain_score_band: [
                    numberValue(event),
                    models.decision_policy.abstain_score_band[1],
                  ],
                })
              }
            />
          </label>
          <label htmlFor="policy-band-high">
            Abstain band high
            <input
              id="policy-band-high"
              type="number"
              min="0"
              max="4"
              value={models.decision_policy.abstain_score_band[1]}
              onChange={(event) =>
                updatePolicy({
                  abstain_score_band: [
                    models.decision_policy.abstain_score_band[0],
                    numberValue(event),
                  ],
                })
              }
            />
          </label>
          <label htmlFor="policy-llm-scale">
            LLM fallback size scale
            <input
              id="policy-llm-scale"
              type="number"
              step="0.05"
              min="0"
              max="1"
              value={models.decision_policy.llm_fallback_size_scale}
              onChange={(event) =>
                updatePolicy({ llm_fallback_size_scale: numberValue(event) })
              }
            />
          </label>
          <label htmlFor="policy-heuristic-scale">
            Heuristic size scale
            <input
              id="policy-heuristic-scale"
              type="number"
              step="0.05"
              min="0"
              max="1"
              value={models.decision_policy.heuristic_size_scale}
              onChange={(event) =>
                updatePolicy({ heuristic_size_scale: numberValue(event) })
              }
            />
          </label>
        </div>

        <h3>Pipeline</h3>
        <div className="models-grid models-grid--compact">
          <label htmlFor="pipeline-debate">
            Debate rounds
            <input
              id="pipeline-debate"
              type="number"
              min="1"
              max="3"
              value={models.pipeline.debate_rounds}
              onChange={(event) =>
                updatePipeline({ debate_rounds: numberValue(event) })
              }
            />
          </label>
          <label htmlFor="pipeline-tools">
            Max tool rounds
            <input
              id="pipeline-tools"
              type="number"
              min="1"
              max="20"
              value={models.pipeline.max_tool_rounds_per_agent}
              onChange={(event) =>
                updatePipeline({ max_tool_rounds_per_agent: numberValue(event) })
              }
            />
          </label>
          <label htmlFor="pipeline-screener">
            Screener minimum score
            <input
              id="pipeline-screener"
              type="number"
              step="0.05"
              min="0"
              max="1"
              value={models.pipeline.screener_min_score}
              onChange={(event) =>
                updatePipeline({ screener_min_score: numberValue(event) })
              }
            />
          </label>
          <label htmlFor="pipeline-final">
            Final minimum probability
            <input
              id="pipeline-final"
              type="number"
              step="0.05"
              min="0"
              max="1"
              value={models.pipeline.final_decision_min_prob}
              onChange={(event) =>
                updatePipeline({ final_decision_min_prob: numberValue(event) })
              }
            />
          </label>
          <label htmlFor="pipeline-execution">
            Execution mode
            <select
              id="pipeline-execution"
              value={models.pipeline.execution_mode}
              onChange={(event) =>
                updatePipeline({ execution_mode: event.target.value })
              }
            >
              <option value="PAPER">PAPER</option>
              <option value="SANDBOX">SANDBOX</option>
              <option value="LIVE_APPROVAL">LIVE_APPROVAL</option>
            </select>
          </label>
          <label htmlFor="pipeline-route">
            Decision route
            <select
              id="pipeline-route"
              value={models.decision_route}
              onChange={(event) =>
                setLoad({
                  state: "ready",
                  models: { ...models, decision_route: event.target.value },
                })
              }
            >
              <option value="auto">auto</option>
              <option value="typesafe_jev">typesafe_jev</option>
              <option value="llm_structured_fallback">llm_structured_fallback</option>
            </select>
          </label>
        </div>

        <h3>Agent routing</h3>
        <div className="routing-grid">
          {Object.entries(models.agent_routing)
            .sort(([left], [right]) => left.localeCompare(right))
            .map(([agentId, route]) => (
              <label key={agentId} htmlFor={`route-${agentId}`} className="routing-row">
                <span>{agentId}</span>
                <select
                  id={`route-${agentId}`}
                  value={route.tier}
                  onChange={(event) =>
                    updateRoute(agentId, event.target.value as TierName)
                  }
                >
                  {TIER_NAMES.map((tier) => (
                    <option key={tier} value={tier}>
                      {tier}
                    </option>
                  ))}
                </select>
              </label>
            ))}
        </div>

        <button
          type="submit"
          className="auth-button"
          disabled={save.state === "saving"}
        >
          {save.state === "saving" ? "Saving..." : "Save model tiers"}
        </button>
      </form>
      {save.state === "saved" && (
        <p className="config-feedback" role="status">
          Model tiers saved.
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
