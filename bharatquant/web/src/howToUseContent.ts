export type DocBlock =
  | { kind: "p"; text: string }
  | { kind: "note"; text: string }
  | { kind: "subheading"; text: string }
  | { kind: "list"; items: string[] }
  | { kind: "facts"; rows: { label: string; value: string }[] };

export type DocSection = {
  id: string;
  title: string;
  blocks: DocBlock[];
};

export type AgentDoc = {
  id: string;
  name: string;
  role: string;
  summary: string;
  inputs: string[];
  outputs: string[];
  model: string;
  tools: string[];
  limits: string[];
};

export const GUIDE_REVIEWED = "2026-11-26";

export const GUIDE_SECTIONS: DocSection[] = [
  {
    id: "overview",
    title: "Welcome & operating model",
    blocks: [
      {
        kind: "p",
        text: "BharatQuant is a local-first research workspace for Indian equities. It runs on your machine, is reached over loopback only, and is built for a single owner. Every screen is marked PAPER ONLY: the app records analysis and paper-account information, but it is not connected to a broker and cannot place a live order.",
      },
      {
        kind: "p",
        text: "The product is organised around the workspace tabs: Today, Research, Technical, Agent Lab, Strategy, Settings and How to use. This guide documents each of them, then explains every agent in detail, and finishes with the model-routing rules, a glossary and the guardrails.",
      },
      {
        kind: "note",
        text: `Documentation snapshot. This guide is static and was last reviewed ${GUIDE_REVIEWED}. If the app changes, the UI itself is always the source of truth. Nothing here is investment advice.`,
      },
      {
        kind: "facts",
        rows: [
          { label: "Mode", value: "Paper only. No broker connection, no live orders, no short selling or margin." },
          { label: "Access", value: "Single owner, password-gated, loopback-only local service." },
          { label: "Data", value: "Local SQLite stores plus read-only market-data vendors (Yahoo by default; Kite optionally, read-only)." },
          { label: "Cost", value: "Only the model-backed agents spend tokens. Diagnostics, scheduling, sizing and the technical analyst are deterministic." },
        ],
      },
    ],
  },
  {
    id: "getting-started",
    title: "Getting started",
    blocks: [
      { kind: "subheading", text: "Service health" },
      {
        kind: "p",
        text: "The status strip at the top of the workspace shows whether the local service is reachable. If it says the service is unavailable, start BharatQuant and press Retry. No data is shown while the service is offline, and the workspace tabs stay hidden until the check succeeds.",
      },
      { kind: "subheading", text: "First-run owner password" },
      {
        kind: "p",
        text: "On first launch you create the owner password. It must be at least 12 characters; the form shows a live strength estimate (weak, fair or strong) and refuses to submit while the two entries differ. The password is stored by the local service; keep it with your other local secrets.",
      },
      { kind: "subheading", text: "Signing in and out" },
      {
        kind: "list",
        items: [
          "Wrong password shows an incorrect-password message (HTTP 401).",
          "Too many attempts shows a lockout message and asks you to wait before retrying (HTTP 429).",
          "If the service reports that setup is required, you are sent back to the create-password form (HTTP 409).",
          "Sign out is in the status strip. Every mutating request the UI makes carries a CSRF token issued with your session; the browser never stores it anywhere else.",
        ],
      },
      {
        kind: "note",
        text: "There is no multi-user mode, no email reset and no remote access. Lose the local database and you lose the workspace; back it up like any other local file.",
      },
    ],
  },
  {
    id: "today",
    title: "Today tab",
    blocks: [
      {
        kind: "p",
        text: "Today is the read-only command centre. It shows the paper account, the data pipelines, the schedule status and three diagnostics. None of the cards on this tab place orders or run agents.",
      },
      { kind: "subheading", text: "Paper account" },
      {
        kind: "p",
        text: "Shows the paper-main account: name, status (ACTIVE, PAUSED or ARCHIVED), opening balance, deposits, withdrawals and current balance. Amounts are shown in paise exactly as the service stores them. If no paper account exists yet, the card says it is not configured. This is paper simulation only; no broker connection or live order is enabled.",
      },
      { kind: "subheading", text: "Data sources" },
      {
        kind: "p",
        text: "Summarises imported CSV data: how many imports and records exist, whether a live market-data provider is configured, and up to 20 recent imports. Imported data quality is explicitly unassessed. A provider being configured here does not mean its prices are tradable or real-time.",
      },
      { kind: "subheading", text: "Schedules" },
      {
        kind: "p",
        text: "Shows the schedule definitions and the last recorded wake decisions. Schedules are daily local times in an IANA timezone, with optional intraday intervals, weekday filters, an operating window and a wake policy. This build has no job handlers registered: wake decisions are recorded (eligible, skipped by policy, wake window expired, outside operating window) but no work is executed, and the previews do not apply market calendars, dependencies or wake policies.",
      },
      { kind: "subheading", text: "Diagnostics: snapshot freshness" },
      {
        kind: "p",
        text: "Inspects one retained snapshot by record ID at a point in time. You supply a delayed-after threshold, a critical-after threshold and a delayed-data mode (Warn, Block or Allow). The result is a status, the source and quality, the age in seconds, and the event, known and recorded timestamps. The lookup is point-in-time: it uses the snapshot version visible as of the requested instant, not whatever was written last. Source, quality and timestamps are assertions made by the data pipeline, and this diagnostic is not trading readiness.",
      },
      {
        kind: "facts",
        rows: [
          { label: "FRESH", value: "Age is at or below the delayed threshold." },
          { label: "DELAYED_WARN / DELAYED_BLOCK / DELAYED_ALLOW", value: "Age is past the delayed threshold but not past critical; the mode decides how it should be handled." },
          { label: "STALE_BLOCK", value: "Age is past the critical threshold." },
          { label: "QUALITY_BLOCK", value: "Snapshot quality is not ACCEPTABLE. This overrides the age result." },
          { label: "UNKNOWN_EVENT_TIME_BLOCK", value: "The snapshot has no event timestamp, so age cannot be computed." },
        ],
      },
      { kind: "subheading", text: "Diagnostics: calendar coverage" },
      {
        kind: "p",
        text: "Checks one exchange over an inclusive date range of 1 to 31 dates. Each date is reported as uncovered, or with its session state and session class plus open and close times when both are known. An uncovered date is unknown, not automatically open or closed, and owner review is not independent verification.",
      },
      { kind: "subheading", text: "Diagnostics: model usage" },
      {
        kind: "p",
        text: "Groups recorded model attempts by task, task version and model. For each group it shows attempt count, incomplete attempts, cache hits, and input/output tokens split into provider-reported, locally estimated and unknown. Incomplete attempts count as unknown usage; an absent figure is never treated as zero.",
      },
    ],
  },
  {
    id: "research",
    title: "Research tab",
    blocks: [
      {
        kind: "p",
        text: "Research combines three cards: a model-driven research agent, a single-symbol technical analysis form, and the deterministic news feed. All three are read-only with respect to the brokerage; quotes are unassessed and nothing here is investment advice.",
      },
      { kind: "subheading", text: "Research agent" },
      {
        kind: "p",
        text: "Ask a question in plain language, for example about long-term news risks, RBI decisions or a specific ticker. The agent is a bounded tool loop: it chooses a tool, reads the result, and repeats until it writes a final answer. The card shows the provider and model in use, the tool list (write-capable tools are tagged), the answer, token usage, the number of turns and a full step trace of every tool call and its output.",
      },
      {
        kind: "list",
        items: [
          "Tools include news search, news memory, news digest and ingest, stock quotes, historical bar download, and HTTPS-only web fetch.",
          "Mutation is allowed for ingest and download; there is no order or broker tool anywhere in the registry, so the agent structurally cannot trade.",
          "The UI caps the task at 4,000 characters and requests at most 10 turns; the runtime enforces its own hard turn and history limits.",
          "If no provider, key or model is configured, the input is disabled and you are sent to Settings. With a model configured, this agent does make real provider calls.",
          "The final answer is rendered as plain text; the step trace is the auditable record of how it got there.",
        ],
      },
      { kind: "subheading", text: "Technical analysis (single symbol)" },
      {
        kind: "p",
        text: "Runs the full analysis pipeline for one symbol. You choose the symbol, timeframe (swing uses daily bars, intraday uses 15-minute bars), equity and cash in rupees; the risk budget is fixed at 1% and is not editable here. The result shows data provenance, the four technical sub-scores, the evidence facts, the validated trade plan (entry, stop, target, reward-to-risk, quantity, invalidation) and any vetoes. It records a plan; it never places an order.",
      },
      { kind: "subheading", text: "News feed" },
      {
        kind: "p",
        text: "Ingest now pulls the configured HTTPS feed, stores only stories it has not seen, and promotes long-term-impact items into agent memory. This path is fully deterministic and makes zero model calls; full articles are never fetched. The digest lists up to 20 recent stories and up to 20 long-term memories, and these are exactly the stories the research agent can search.",
      },
    ],
  },
  {
    id: "technical",
    title: "Technical tab",
    blocks: [
      {
        kind: "p",
        text: "The Technical tab runs the multi-agent analysis across several symbols in one batch. Enter up to 10 comma-separated symbols, choose swing or intraday, and set equity and cash in rupees. Risk is fixed at 1%.",
      },
      {
        kind: "list",
        items: [
          "Batch header: how many symbols were analysed, how many failed, and the timeframe.",
          "Per symbol: the signal badge (BUY gets the allow styling, otherwise HOLD-like), the composite score, and the Trend, Momentum, Volatility and Structure sub-scores.",
          "Per symbol plan: entry, stop, target, reward-to-risk and quantity, or a clear veto line explaining why there is no plan.",
          "Provenance: whether the data was real-time or delayed, and whether the run was rules-only or included model debate.",
          "Show agents expands the full trace: one row per pipeline node with its status (ok, degraded, failed) and a human-readable summary, followed by the evidence facts.",
          "A failed symbol is isolated: it is shown as not analysed with its error, and the other symbols still complete.",
        ],
      },
      {
        kind: "note",
        text: "Symbols are processed sequentially, not concurrently, to stay polite to the vendor rate limits. Indices are rejected for trade plans. The model only argues; every number in the plan is computed in code.",
      },
    ],
  },
  {
    id: "agent-lab",
    title: "Agent Lab tab",
    blocks: [
      {
        kind: "p",
        text: "Agent Lab is where you test the decision agent and edit the prompts used by the analysis agents. Testing and refining only; it never trades.",
      },
      { kind: "subheading", text: "Decision agent preview" },
      {
        kind: "p",
        text: "Choose a symbol and timeframe and run the Stage 0 screener battery on live cached data. The app first builds a deterministic decision state, then asks the decision ladder for answers: TypeSafe Jev when configured, otherwise the configured language-model fallbacks, otherwise the deterministic heuristic. The result shows the verdict, the reason, a table of every battery question with its answer and confidence, the route actually used (tier, provider, model, latency, model mismatch, state hash) and the exact decision state the model saw.",
      },
      {
        kind: "note",
        text: "The preview runs the full analysis pipeline first, using dummy money, before it throws the trade plan away. It is therefore not free of model spend. A fallback source is labelled, never hidden.",
      },
      { kind: "subheading", text: "Agent prompts" },
      {
        kind: "p",
        text: "Edits the instructions for six agent prompts: the bull advocate, the bear advocate, the research judge, the risk analyst, the Super Agent (CIO) and the decision screener. Each prompt has a system message and, where applicable, a user template with required placeholders. Saving appends an immutable new version; resetting restores the code default as a new version. The JSON contracts, numeric thresholds and risk gates stay in code and cannot be changed from the editor.",
      },
      {
        kind: "facts",
        rows: [
          { label: "Bull advocate", value: "Placeholders: symbol, timeframe, as_of, facts." },
          { label: "Bear advocate", value: "Placeholders: symbol, timeframe, as_of, facts." },
          { label: "Research judge", value: "Placeholders: symbol, timeframe, as_of, confidence_ceiling, facts, bull, bear." },
          { label: "Risk analyst", value: "Placeholders: symbol, timeframe, as_of, facts." },
          { label: "Super Agent (CIO)", value: "Placeholders: strategy, timeframe, constraints, candidates." },
          { label: "Decision screener", value: "System prompt only. The question set and answer types are code-owned." },
        ],
      },
    ],
  },
  {
    id: "strategy",
    title: "Strategy tab",
    blocks: [
      {
        kind: "p",
        text: "Strategy passes an owner-written brief to the Super Agent. Enter up to eight symbols, a timeframe, equity and cash in rupees, risk in basis points and a position cap. The coordinator runs the existing per-symbol agents, then the Super Agent ranks the results.",
      },
      {
        kind: "list",
        items: [
          "Only a deterministic BUY plan becomes a candidate. The coordinator accepts candidates until the position cap or available cash is reached, and never changes the sizer's quantity.",
          "The Super Agent may downgrade a BUY to HOLD or AVOID and write the portfolio view. It cannot create a BUY the deterministic plan did not produce, invent a symbol or alter a number.",
          "With no reasoning model the run still completes: a rules ranking by composite and confidence is labelled source rules with a fallback note.",
          "Accepted plans are research output only. No broker call and no paper order is made.",
          "Per-symbol agent traces show which nodes ran, their status and a one-line summary, so a degraded debate is visible rather than hidden.",
        ],
      },
      {
        kind: "note",
        text: "At most eight symbols and ten positions per run; execution is sequential. The Super Agent prompt is editable in Agent Lab, but the cash gate, position cap, sizing and risk rules are code-owned.",
      },
    ],
  },
  {
    id: "settings",
    title: "Settings tab",
    blocks: [
      {
        kind: "p",
        text: "Settings consolidates configuration: model and research defaults, provider API keys, the three model tiers and decision policy, per-agent routing, an overview, schedules and usage.",
      },
      { kind: "subheading", text: "Model & research configuration" },
      {
        kind: "list",
        items: [
          "Model provider: Gemini, DeepSeek, OpenAI or a custom OpenAI-compatible provider.",
          "Model name: the model identifier passed to that provider.",
          "News feed URL: must be HTTPS and must not contain embedded credentials.",
          "Long-term memory retention: 1 to 3,650 days; memories older than this are eligible for pruning.",
          "Provider API keys: one slot per provider, including TypeSafe Jev. Keys are stored in plaintext in the local database, which is acceptable only for a single-owner, loopback-only app; they are never returned to the browser, only a masked hint and the last four characters.",
        ],
      },
      { kind: "subheading", text: "Model tiers, decision policy & routing" },
      {
        kind: "p",
        text: "Three tiers decide which model does what. The decision tier is reserved for the decision battery and is the only tier that accepts TypeSafe Jev. The reasoning tier handles debate and judgement. The flash tier handles cheap extraction and monitoring. Each tier has a provider, model, temperature, max tokens, timeout, attempt count and an optional fallback tier; a fallback must move down the decision, flash, reasoning ladder.",
      },
      {
        kind: "facts",
        rows: [
          { label: "Decision tier", value: "Default TypeSafe Jev, 3,000 ms timeout, one attempt, falls back to flash. Temperature and max tokens are fixed to 0 and 1." },
          { label: "Reasoning tier", value: "Default temperature 0.2, 4,096 max tokens, 60 s timeout, 3 attempts, no fallback tier by default." },
          { label: "Flash tier", value: "Default temperature 0.1, 2,048 max tokens, 60 s timeout, 3 attempts, falls back to reasoning." },
        ],
      },
      {
        kind: "p",
        text: "The same form exposes the decision fallback order (default flash, reasoning, heuristic), decision policy thresholds (minimum choice confidence 0.55, minimum noul probability 0.60, abstain band 2 to 3, LLM-fallback size scale 0.75, heuristic size scale 0.50), pipeline settings (debate rounds 1 to 3, max tool rounds 1 to 20, screener minimum score 0.60, final minimum probability 0.65, execution mode), the decision route (auto, TypeSafe Jev, or LLM structured fallback) and a per-agent tier dropdown.",
      },
      {
        kind: "facts",
        rows: [
          { label: "Decision-tier agents", value: "watchlist_screener, news_triage, evidence_verifier, final_decision_gate." },
          { label: "Reasoning-tier agents", value: "bull_debater, bear_debater, research_judge, super_agent_cio, reflection_writer." },
          { label: "Flash-tier agents", value: "news_research, fundamental_events, market_regime, technical_analyst, risk_analyst." },
        ],
      },
      {
        kind: "note",
        text: "Thresholds live in configuration, never in a prompt. The execution-mode setting is a configuration value, but this build has no broker path, so it cannot turn into a live order.",
      },
      { kind: "subheading", text: "Configuration overview" },
      {
        kind: "p",
        text: "A read-only summary of registered task definitions, schedules, model usage groups, the resolved model route, configured API-key count and the research-agent feed and retention. The task-definition list is empty unless tasks have been registered in the backend; choosing models for individual tasks is not implemented in this UI.",
      },
    ],
  },
  {
    id: "routing",
    title: "Model routing & the decision ladder",
    blocks: [
      {
        kind: "p",
        text: "Every model-backed agent is mapped to a tier, and the tier supplies the provider and model. The debate agents receive a routed caller that maps their task ids to agent ids, so the run ledger can record each call. If no tier is configured but a single legacy provider and model are set, the analysis agents fall back to that single model with no per-call routing.",
      },
      {
        kind: "p",
        text: "The decision battery is different. It is the only path that walks a ladder, and each step has a size multiplier: Jev native 1.0, LLM fallback 0.75, heuristic 0.5, and rules-only 0.0. If nothing is available, the decision is reported as unavailable and sizing collapses to zero rather than inventing a verdict.",
      },
      {
        kind: "list",
        items: [
          "1. TypeSafe Jev, only when the decision route allows it, the decision tier is configured for Jev, and a Jev key exists.",
          "2. The configured fallback order, by default flash, then reasoning, answering the strict decision schema without prose.",
          "3. The deterministic heuristic judge, which refuses questions it cannot answer honestly.",
          "4. Rules-only: no decision, zero size, explicitly labelled.",
        ],
      },
      {
        kind: "note",
        text: "A model can only lower confidence, never raise it above the deterministic ceiling. It can also only downgrade a BUY to HOLD; the code decides whether a BUY is even eligible before the model is consulted.",
      },
    ],
  },
  {
    id: "glossary",
    title: "Concepts & glossary",
    blocks: [
      {
        kind: "facts",
        rows: [
          { label: "Quality: UNASSESSED", value: "No quality judgement has been made. It is not the same as good." },
          { label: "Quality: ACCEPTABLE", value: "The only quality state that lets a snapshot pass the freshness quality gate." },
          { label: "Quality: DEGRADED", value: "Known to be worse than acceptable; blocks freshness." },
          { label: "Quality: REJECTED", value: "Known to be unusable; blocks freshness." },
          { label: "real-time Kite", value: "Prices served through the read-only Kite integration." },
          { label: "delayed Yahoo", value: "Delayed prices from Yahoo, the default when Kite is disabled or unavailable." },
          { label: "rules only", value: "The deterministic path ran; no model contributed to the verdict." },
          { label: "with model debate", value: "The bull, bear and judge calls completed and were used." },
          { label: "clamped", value: "The judge asked for more confidence than the deterministic ceiling allows, so it was lowered." },
          { label: "rules-only floor", value: "The decision ladder found no source; size scale 0.0." },
        ],
      },
    ],
  },
  {
    id: "guardrails",
    title: "Guardrails: what BharatQuant will not do",
    blocks: [
      {
        kind: "list",
        items: [
          "It will not place a live order. There is no broker connection in this build.",
          "It will not short sell or use margin; only long BUY plans are representable.",
          "It will not let a model compute or change a number: indicators, sizing, stops, targets and reward-to-risk are deterministic code.",
          "It will not let a model approve or veto risk: the risk analyst is advisory only and the real gate is the risk policy and sizer.",
          "It will not execute scheduled jobs; no job handlers are registered.",
          "It will not treat missing values as zero; unknown stays unknown.",
          "It will not return API keys to the browser; only masked hints.",
          "It will not present delayed or unassessed data as real-time or verified.",
          "It will not serve more than one owner or accept non-loopback traffic.",
        ],
      },
      {
        kind: "note",
        text: "The recurring phrase in the UI, 'not trading readiness', is deliberate. A diagnostic result, a battery answer or a plan is an input to your judgement, not an instruction to trade.",
      },
    ],
  },
  {
    id: "troubleshooting",
    title: "Troubleshooting",
    blocks: [
      {
        kind: "facts",
        rows: [
          { label: "Service offline", value: "Start the local BharatQuant service, then press Retry in the status strip." },
          { label: "Research agent disabled", value: "Set a provider, an API key and a model name in Settings. Without all three the agent cannot run." },
          { label: "Snapshot unavailable", value: "Check the record ID exists and was retained at the requested as-of time. Invalid thresholds or inconsistent timestamps return a validation error." },
          { label: "Decision preview falls back", value: "No Jev key, the decision route does not allow Jev, or the decision tier is not configured for Jev. The route panel names the source actually used." },
          { label: "Prompt save rejected", value: "A required placeholder is missing, the template does not format, or it contains control characters. Restore the required placeholders and try again." },
          { label: "No schedules or usage", value: "Schedules are read-only and optional; usage is empty until a model-backed agent has actually run." },
          { label: "Paper account not configured", value: "The paper-main account has not been created in the local database yet." },
        ],
      },
    ],
  },
];

export const AGENT_DOCS: AgentDoc[] = [
  {
    id: "orchestrator",
    name: "Analysis orchestrator",
    role: "Deterministic pipeline (no model)",
    summary:
      "Runs the nine-node analysis graph for one symbol and produces either a validated trade plan or an explicit no-plan result. It is the backbone behind the Technical tab, the single-symbol analysis and the decision preview.",
    inputs: [
      "Symbol, timeframe (swing or intraday), equity and available cash in paise, risk in basis points.",
      "Optional as-of instant for point-in-time runs.",
    ],
    outputs: [
      "Technical report, optional trade plan, mode (FULL or RULES_ONLY), per-node trace, vetoes, notes and model-routing ledger.",
      "A degraded node is recorded rather than raised, so one model outage cannot skip sizing or decision.",
    ],
    model: "No model. It orchestrates deterministic nodes and optional model-backed nodes.",
    tools: [
      "Read-only vendor router: daily bars, live quotes, intraday bars and indices.",
      "News is a placeholder node today and contributes no facts.",
    ],
    limits: [
      "Emits a plan only; never places an order.",
      "Only long BUY is supported.",
      "The news-context node is not wired in this build.",
    ],
  },
  {
    id: "technical-analyst",
    name: "Technical analyst",
    role: "Deterministic scoring (no model)",
    summary:
      "Turns raw bars into four transparent scores from -1 to +1: trend, momentum, volatility and structure. It also emits the plain-English evidence facts that every downstream model is allowed to see.",
    inputs: ["Bars for the symbol and timeframe, plus an optional as-of instant; bars after as-of are dropped twice."],
    outputs: [
      "Sub-scores with evidence, a full indicator snapshot, an unweighted composite of available dimensions, and a facts list.",
    ],
    model: "No model, by design. The model never computes a number; it only interprets this report later.",
    tools: ["Pure arithmetic over the indicator library. No database, no HTTP, no model."],
    limits: [
      "Unknown is kept distinct from neutral; None is not zero.",
      "Minimum bar counts apply per dimension, so thin history leaves dimensions unavailable.",
      "The composite is deliberately unweighted.",
    ],
  },
  {
    id: "bull-debater",
    name: "Bull advocate",
    role: "Reasoning tier, model-backed",
    summary:
      "Argues the strongest evidence-based case for the setup using only the technical facts, in a bounded format.",
    inputs: ["The technical report facts, symbol, timeframe and as-of. The prompt is editable in Agent Lab."],
    outputs: ["A case of at most 700 characters and up to four short points."],
    model: "Reasoning tier by default, with the tier's fallback. Uses the configured prompt override if one exists.",
    tools: ["None. It cannot fetch prices, news or a web page, which is what prevents it inventing numbers."],
    limits: ["Facts only.", "Malformed output fails the debate closed instead of being repaired creatively."],
  },
  {
    id: "bear-debater",
    name: "Bear advocate",
    role: "Reasoning tier, model-backed",
    summary:
      "Argues the strongest evidence-based case against the setup, under the same contract as the bull advocate.",
    inputs: ["The same technical facts, symbol, timeframe and as-of; prompt editable in Agent Lab."],
    outputs: ["A case of at most 700 characters and up to four short points."],
    model: "Reasoning tier by default, with fallback.",
    tools: ["None. Facts only."],
    limits: ["Facts only.", "Malformed output fails closed."],
  },
  {
    id: "research-judge",
    name: "Research judge",
    role: "Reasoning tier, model-backed",
    summary:
      "Resolves the bull and bear cases into BUY or HOLD and a confidence, which it can lower but never raise above the deterministic ceiling.",
    inputs: ["The technical facts, both cases, and the deterministic confidence ceiling."],
    outputs: ["Verdict BUY or HOLD, short reasoning, confidence, the ceiling and a clamped flag."],
    model: "Reasoning tier by default, with fallback.",
    tools: ["None. It cannot create a BUY on its own: the code checks the composite and trend first."],
    limits: [
      "Confidence is clamped to the ceiling computed from coverage and strength.",
      "It can only downgrade, never upgrade the deterministic signal.",
      "Malformed output fails closed.",
    ],
  },
  {
    id: "super-agent",
    name: "Super Agent (CIO)",
    role: "Reasoning tier, portfolio synthesis",
    summary:
      "Receives the owner's strategy, hard constraints and one compact payload per candidate, then ranks the candidates and writes the portfolio and risk notes. It is a bounded synthesis step, not the coordinator.",
    inputs: [
      "The strategy brief, timeframe, constraints and per-symbol summaries from the specialist agents; the prompt is editable in Agent Lab.",
    ],
    outputs: [
      "A strategy summary, ranked actions with an action and size scale, portfolio notes and risk notes. Unknown symbols are ignored.",
    ],
    model: "Reasoning tier by default. If no reasoning answer is available, the run falls back to a rules ranking labelled source rules.",
    tools: ["None. It only sees the bounded candidate payload."],
    limits: [
      "It can downgrade a deterministic BUY, never create one.",
      "It cannot change prices, quantities, R:R or the position/cash gate.",
      "No orders are placed by this agent or the endpoint that calls it.",
    ],
  },
  {
    id: "risk-analyst",
    name: "Risk analyst",
    role: "Flash tier, model-backed",
    summary:
      "Describes what would invalidate the setup and what could go wrong early. Purely advisory: it can neither approve nor block a trade.",
    inputs: ["The technical report facts; prompt editable in Agent Lab."],
    outputs: ["An invalidation reason, up to five early warnings and a volatility flag, always marked advisory_only."],
    model: "Flash tier by default, with reasoning as the tier fallback.",
    tools: ["None. Facts only."],
    limits: [
      "Advisory only; the real gate is the risk policy and sizer.",
      "Falls back to a deterministic invalidation derived from support, trend and ATR.",
    ],
  },
  {
    id: "news-research",
    name: "News research agent",
    role: "Deterministic, zero model calls",
    summary:
      "Fetches the configured HTTPS news feed, stores only unseen stories, classifies them and promotes long-term-impact items into agent memory.",
    inputs: ["The configured feed URL and memory retention; optionally pre-fetched bytes."],
    outputs: ["A run report with fetched, stored, duplicate and long-term-memory counts, plus digest and memory recalls."],
    model: "No model calls at all. Summarisation is intentionally not part of this path.",
    tools: ["News search, memory recall, digest and a mutating ingest tool."],
    limits: [
      "Full articles are never fetched.",
      "Deduplicates by story identity.",
      "Memory retention is configurable from 1 to 3,650 days.",
    ],
  },
  {
    id: "research-agent",
    name: "Research agent",
    role: "Tool-using model loop",
    summary:
      "Answers a research question by calling tools one turn at a time and writing a final answer, while exposing every step it took.",
    inputs: ["A task of up to 4,000 characters, and up to 10 turns requested from the UI."],
    outputs: ["Final text, a list of steps, tool calls, stop reason and token usage."],
    model: "The single configured research model, not the tier router. No deterministic fallback: unconfigured returns 409, provider failure returns 502.",
    tools: [
      "News search, memory, digest and ingest.",
      "Quote and historical download.",
      "HTTPS-only web fetch, capped at 2 MB and 12,000 characters with scripts and styles stripped.",
    ],
    limits: [
      "The loop is hard-bounded and folds history deterministically instead of summarising it.",
      "No order or broker tool exists, so it cannot trade.",
      "Quotes are unassessed and nothing it says is investment advice.",
    ],
  },
  {
    id: "decision-screener",
    name: "Decision screener & battery",
    role: "Decision tier with a fallback ladder",
    summary:
      "Answers a fixed battery of machine-checkable questions about a setup, such as direction, quality, whether deeper research is warranted and event risk. The first battery is the screener; news triage, evidence and the final gate are implemented but not yet exposed by a route.",
    inputs: ["A compact deterministic decision state of at most 16 KiB, and the questions for the battery."],
    outputs: ["Typed answers with confidence, the source actually used, the route and the state hash."],
    model: "Ladder: Jev native, then flash, then reasoning, then the heuristic, then rules-only. Each source has a size multiplier.",
    tools: ["No tools. It only sees the bounded numeric state."],
    limits: [
      "Thresholds and answer types are code-owned, never prompt-owned.",
      "A decision cannot exceed the state it was given.",
      "Only the screener battery is reachable from the UI today.",
    ],
  },
  {
    id: "heuristic-judge",
    name: "Heuristic judge",
    role: "Deterministic fallback",
    summary:
      "Provides deterministic answers when no model is available, so the decision path degrades honestly instead of failing silently.",
    inputs: ["The decision state and the battery questions."],
    outputs: ["Typed answers with a fixed confidence of 0.5, labelled heuristic."],
    model: "No model.",
    tools: ["Pure rules."],
    limits: [
      "Refuses the evidence battery, which it cannot answer honestly.",
      "Its 0.5 confidence is uncalibrated, so policy applies a 0.5 size scale.",
    ],
  },
  {
    id: "sizing-tradeplan",
    name: "Sizing & trade plan",
    role: "Deterministic code (not an agent)",
    summary:
      "Turns a verdict into a validated long-only plan. Quantity is recomputed from equity, risk fraction and stop distance; it is never a field the model fills in.",
    inputs: ["Signal, entry, stop, target, equity, cash and risk basis points."],
    outputs: ["Entry, stop, target, reward-to-risk, quantity, invalidation and freshness provenance."],
    model: "No model.",
    tools: ["Arithmetic."],
    limits: [
      "LIMIT orders only, NSE or BSE, MIS or CNC.",
      "Reward-to-risk has a floor of 1.5.",
      "Rejects a quantity that disagrees with the sizer, a stop on the wrong side, or a target inside the stop.",
    ],
  },
  {
    id: "batch-analyst",
    name: "Batch analyst",
    role: "Deterministic orchestration",
    summary:
      "Runs the single-symbol pipeline across several symbols with per-symbol isolation, so one bad ticker does not waste the batch.",
    inputs: ["A parsed symbol list of up to 10, timeframe, equity, cash and risk."],
    outputs: ["One result per requested symbol in the requested order, with success or error for each."],
    model: "Indirectly model-backed, because each symbol is a full pipeline run.",
    tools: ["Whatever the wrapped pipeline uses."],
    limits: [
      "Sequential, not concurrent, to respect vendor rate limits.",
      "Rejects indices before spending any tokens.",
      "Model spend scales with the number of symbols.",
    ],
  },
];
