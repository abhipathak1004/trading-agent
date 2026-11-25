---
name: graphify
description: Use a local Graphify code graph to trace architecture, relationships, and change impact when that context is useful.
---

# Graphify

Use Graphify for cross-file relationship and impact questions, not for simple exact-string lookups. If a current `graphify-out/graph.json` exists, query it first with `graphify query`, `graphify path`, or `graphify affected`, then verify important conclusions against source files and tests. Treat graph results as navigation evidence, not proof.

If a graph is missing or stale and Graphify CLI is available, a local code-only graph can be built with:

```sh
graphify extract . --code-only
```

Code-only extraction uses local AST parsing and skips semantic LLM extraction. Do not send project source to a hosted service. If the CLI is unavailable or the graph omits needed relationships, continue with the repository's normal search and code-reading tools; do not block work on Graphify.

The CLI is an optional developer tool, not a BharatQuant runtime dependency. Its installation and supported integrations are documented at [Graphify's VS Code guide](https://docs.graphify.com/integrations/vscode.md).
