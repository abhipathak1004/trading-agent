# BharatQuant Copilot instructions

Before changing code, inspect the task's relevant source, tests, and project
documentation. Identify affected callers, contracts, and safety requirements.
Use only the focused project skill guidance needed for the task:

- `.github/skills/clean-software-craftsmanship/SKILL.md`
- `.github/skills/ponytail/SKILL.md`
- `.github/skills/graphify/SKILL.md`

For architecture or cross-file impact questions, use the local Graphify graph
when it exists and the CLI is available. Verify graph findings in source and
tests. For exact-string lookups, use normal code search. If the graph is missing
or stale and the CLI is installed, build it locally with
`graphify extract . --code-only`; do not use semantic extraction or upload
project source to a hosted service.

Keep changes small but complete. Preserve validation, security, error handling,
data integrity, accessibility, and existing behavior unless a change is
explicitly requested. Add focused tests for changed behavior and run the
smallest relevant checks. Report verification gaps and remaining risks.

Graphify and Ponytail are optional development tools, not BharatQuant runtime
dependencies. These instructions do not install their plugins or enable
Ponytail slash commands.
