import { Fragment } from "react";

import { AGENT_DOCS, GUIDE_REVIEWED, GUIDE_SECTIONS } from "./howToUseContent";
import type { DocBlock } from "./howToUseContent";

function DocBlockView({ block }: { block: DocBlock }) {
  switch (block.kind) {
    case "p":
      return <p>{block.text}</p>;
    case "note":
      return <p className="panel-note">{block.text}</p>;
    case "subheading":
      return <h3>{block.text}</h3>;
    case "list":
      return (
        <ul className="guide-list">
          {block.items.map((item) => (
            <li key={item}>{item}</li>
          ))}
        </ul>
      );
    case "facts":
      return (
        <dl className="settings-facts">
          {block.rows.map((row) => (
            <Fragment key={row.label}>
              <dt>{row.label}</dt>
              <dd>{row.value}</dd>
            </Fragment>
          ))}
        </dl>
      );
  }
}

export default function HowToUsePanel() {
  return (
    <div className="guide">
      <header className="page-header">
        <div>
          <p className="eyebrow">DOCUMENTATION</p>
          <h1 id="guide-page-title">How to use BharatQuant</h1>
          <p className="intro">
            Every screen, every agent, the routing rules and the guardrails, in one place.
          </p>
        </div>
        <span className="status-badge status-badge--provisional">PAPER ONLY</span>
      </header>

      <section className="mode-banner" aria-label="Guide scope">
        <span aria-hidden="true">i</span>
        <div>
          <strong>Reference guide, not trading advice</strong>
          <p>
            Last reviewed {GUIDE_REVIEWED}. This page describes the paper-only local
            workspace as it is built. It is documentation only: nothing here changes a
            setting, runs an agent or places an order. When this text and the UI disagree,
            the UI is correct.
          </p>
        </div>
      </section>

      <nav className="guide-toc" aria-label="Guide contents">
        {GUIDE_SECTIONS.map((section) => (
          <a key={section.id} href={`#guide-${section.id}`}>
            {section.title}
          </a>
        ))}
        <a href="#guide-agents">Agents reference</a>
      </nav>

      {GUIDE_SECTIONS.map((section) => (
        <section
          key={section.id}
          id={`guide-${section.id}`}
          className="settings-panel guide-section"
          aria-labelledby={`guide-${section.id}-title`}
        >
          <h2 id={`guide-${section.id}-title`}>{section.title}</h2>
          {section.blocks.map((block, index) => (
            <DocBlockView key={`${section.id}-${index}`} block={block} />
          ))}
        </section>
      ))}

      <section
        id="guide-agents"
        className="settings-panel guide-section"
        aria-labelledby="guide-agents-title"
      >
        <h2 id="guide-agents-title">Agents reference</h2>
        <p className="panel-note">
          Every agent and analysis component the workspace can run, what it reads, what it
          produces, which model tier it uses and what it deliberately cannot do. All of
          them are paper-only and advisory; none can place an order.
        </p>
        <div className="guide-agents">
          {AGENT_DOCS.map((agent) => (
            <article
              key={agent.id}
              className="guide-agent"
              aria-labelledby={`guide-agent-${agent.id}`}
            >
              <h3 id={`guide-agent-${agent.id}`}>{agent.name}</h3>
              <p className="eyebrow">{agent.role}</p>
              <p>{agent.summary}</p>
              <dl className="settings-facts">
                <dt>Inputs</dt>
                <dd>
                  <ul className="guide-list guide-list--compact">
                    {agent.inputs.map((item) => (
                      <li key={item}>{item}</li>
                    ))}
                  </ul>
                </dd>
                <dt>Outputs</dt>
                <dd>
                  <ul className="guide-list guide-list--compact">
                    {agent.outputs.map((item) => (
                      <li key={item}>{item}</li>
                    ))}
                  </ul>
                </dd>
                <dt>Model</dt>
                <dd>{agent.model}</dd>
                <dt>Tools</dt>
                <dd>
                  <ul className="guide-list guide-list--compact">
                    {agent.tools.map((item) => (
                      <li key={item}>{item}</li>
                    ))}
                  </ul>
                </dd>
                <dt>Limits</dt>
                <dd>
                  <ul className="guide-list guide-list--compact">
                    {agent.limits.map((item) => (
                      <li key={item}>{item}</li>
                    ))}
                  </ul>
                </dd>
              </dl>
            </article>
          ))}
        </div>
      </section>
    </div>
  );
}
