---
name: clean-software-craftsmanship
description: language-agnostic coding and architecture guidance for reviewing, refactoring, generating, testing, and planning changes to source files, snippets, diffs, pull requests, repositories, and design proposals. use for clean-code reviews, maintainability improvements, technical-debt analysis, api and component design, dependency-boundary decisions, test strategy, repository modernization, or production-code generation using principles synthesized from clean code and clean architecture.
---
# Clean Software Craftsmanship

## Purpose
Produce software that is easy to read, change, test, operate, and replace. Apply the rules as decision aids, not dogma. Preserve externally observable behavior unless the user explicitly requests a behavior change.

This skill contains an original synthesis of widely discussed software-craft principles associated with Robert C. Martin's *Clean Code* and *Clean Architecture*. Do not reproduce book passages. Explain findings in original language and connect each recommendation to concrete evidence in the user's code.

## Operating workflow

1. **Establish scope and constraints**
   - Identify the requested task: review, refactor, generation, architecture, testing, or debt planning.
   - Inspect local files needed to understand build, tests, dependencies, public interfaces, runtime boundaries, and conventions.
   - Detect language and framework from repository evidence; remain language-agnostic in principles.
   - Record explicit constraints such as compatibility, latency, security, deployment model, deadlines, and files that may change.

2. **Build a behavioral model**
   - Determine inputs, outputs, side effects, failure modes, invariants, and public contracts.
   - Identify boundaries between business policy and delivery details.
   - For refactors, establish a safety net before structural changes. Prefer characterization tests when behavior is unclear.

3. **Assess at the right scale**
   - For a snippet or file, focus on names, functions, state, error handling, duplication, tests, and local design.
   - For a diff or pull request, evaluate changed behavior, regression risk, compatibility, tests, and whether complexity increased.
   - For a repository, map modules, dependency direction, change hotspots, cycles, unstable boundaries, duplicated policy, and test seams.
   - For a design proposal, evaluate policy boundaries, use cases, dependency direction, replaceable details, data ownership, and operational qualities.

4. **Prioritize findings**
   - Use severity: **critical**, **high**, **medium**, **low**, **note**.
   - Base severity on user impact, correctness, security, data loss, architectural lock-in, frequency of change, blast radius, and remediation cost.
   - Separate observed defects from design risks and optional improvements.
   - Do not report style preferences as defects unless they materially affect comprehension or change safety.

5. **Recommend the smallest coherent change**
   - Prefer a sequence of safe, reviewable steps over a broad rewrite.
   - Keep behavior-preserving refactors separate from behavior changes.
   - Preserve public contracts or provide a migration path.
   - Avoid speculative abstractions. Introduce a boundary when volatility, policy ownership, testing, or replacement cost justifies it.

6. **Implement and verify**
   - Follow repository-local formatting, naming, testing, and build conventions.
   - Run the most relevant existing checks available locally.
   - Add or update tests at the cheapest stable boundary.
   - Review the resulting diff for accidental scope expansion, dead code, hidden coupling, and misleading comments.

## Required reference loading

Read only the files relevant to the active task:

- Read [references/clean-code.md](references/clean-code.md) for source-level review, naming, functions, classes, errors, comments, formatting, and refactoring.
- Read [references/clean-architecture.md](references/clean-architecture.md) for system structure, components, dependency direction, boundaries, use cases, frameworks, data, and deployment concerns.
- Read [references/review-and-refactoring.md](references/review-and-refactoring.md) for reviews, diffs, repository assessment, severity, refactoring sequencing, and technical-debt planning.
- Read [references/testing-and-verification.md](references/testing-and-verification.md) for test design, characterization, boundary tests, architecture checks, and completion gates.
- Read [references/output-contracts.md](references/output-contracts.md) before producing a formal review, refactor plan, architecture proposal, test plan, or debt roadmap.

## Core rules

- Optimize first for clarity of intent and safe change, then for compactness.
- Make names reveal domain meaning, units, lifecycle, and side-effect expectations.
- Keep each unit at one coherent level of abstraction.
- Separate policy from mechanisms and volatile details.
- Direct source-code dependencies toward stable business rules.
- Encapsulate state; make invalid states difficult to represent.
- Prefer explicit control flow and dependencies over hidden ambient context.
- Eliminate duplication of knowledge, not merely repeated text.
- Treat errors as part of the contract; preserve causal context and recovery semantics.
- Keep comments for rationale, constraints, hazards, and public contracts; rewrite code instead of narrating confusing implementation.
- Design tests as executable specifications of behavior and boundaries.
- Refactor continuously in small verified steps.
- Do not force every system into identical layers, patterns, or class counts.

## Review behavior

For every actionable finding:
- Cite the exact file and line or symbol when available.
- State what is observed, why it matters, when it fails, and the smallest useful correction.
- Provide a concrete code sketch only when it clarifies the remedy.
- Identify any compatibility or migration concern.
- Avoid vague labels such as "not clean," "violates SOLID," or "bad architecture" without evidence.

If no material defect is found, say so and list residual risks or verification gaps. Do not manufacture findings.

## Generation behavior

When generating code:
- Start from use cases, invariants, contracts, and failure behavior.
- Keep domain terminology consistent.
- Make dependencies injectable or replaceable where tests or volatility require it.
- Keep I/O, frameworks, persistence, serialization, and transport at boundaries.
- Include tests for primary behavior, edge cases, and failures.
- Avoid unnecessary patterns, interfaces, factories, layers, or configuration.
- Return complete code for requested files, not fragments disguised as a finished implementation.

## Architecture behavior

- Describe components by responsibility and policy ownership, not by technology alone.
- Show dependency direction separately from runtime data flow.
- Identify entities/domain rules, application use cases, interface adapters, and infrastructure only where those distinctions add real value.
- Treat databases, web frameworks, queues, UI toolkits, cloud services, and ORMs as replaceable details unless the business contract truly depends on them.
- Define boundary contracts and crossing data structures without leaking framework or persistence models inward.
- Consider deployability, observability, security, performance, concurrency, and transactional consistency alongside code structure.

## Completion gate

Before responding, verify:
- The task requested by the user is fully covered.
- Recommendations are grounded in inspected artifacts.
- Behavior changes are distinguished from refactors.
- New abstractions have a stated purpose and cost.
- Tests cover changed risks.
- Commands run and their outcomes are reported accurately.
- Remaining uncertainty, untested behavior, and migration work are explicit.
