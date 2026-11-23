# Clean Code Reference

## Contents
1. Names
2. Functions and methods
3. Data and objects
4. Classes and modules
5. Comments and documentation
6. Formatting
7. Error handling
8. Duplication and abstraction
9. Concurrency
10. Refactoring heuristics

## 1. Names
- Choose names that expose intent, domain role, units, scope, and lifecycle.
- Avoid names whose meaning depends on type inspection or surrounding comments.
- Use one term per concept. Do not alternate among synonyms such as fetch/load/read unless they denote different contracts.
- Distinguish commands from queries. A name that sounds observational should not mutate hidden state.
- Prefer searchable, pronounceable names over abbreviations. Keep conventional short names only in tiny, obvious scopes.
- Encode units and representation where confusion is costly: `timeout_ms`, `gross_amount`, `utc_timestamp`.
- Avoid type encodings that become false after refactoring.
- Name booleans as propositions that read naturally at call sites.
- Use domain language for business rules and technical language for mechanisms.
- Rename aggressively when the code's meaning changes.

Review questions:
- Can a reader predict behavior from the name and signature?
- Could two similarly named concepts be confused?
- Does the name conceal side effects, expensive work, blocking, retries, or remote I/O?

## 2. Functions and methods
- Give each function one coherent responsibility at one abstraction level.
- Keep orchestration readable as a sequence of domain operations.
- Extract when a block has a meaningful name, repeats knowledge, mixes abstraction levels, or needs independent testing.
- Do not extract trivial fragments that make control flow harder to follow.
- Minimize parameters. Group parameters only when they form a real concept, not to hide a long list.
- Avoid boolean mode flags that select unrelated behaviors. Prefer distinct operations or a strategy when variation is real.
- Make side effects explicit in names, return types, boundaries, or command objects.
- Avoid output parameters and surprising mutation.
- Separate queries that return information from commands that change state when practical.
- Keep validation near the boundary that owns the contract.
- Prefer guard clauses when they reduce nesting and expose exceptional paths.
- Keep exception handling from obscuring the primary algorithm; extract boundary-specific recovery when needed.

Signs of mixed responsibility:
- The function changes for unrelated reasons.
- It performs policy decisions and low-level I/O.
- Tests require many unrelated fixtures.
- Its name contains multiple independent verbs.
- Some callers need only half of its behavior.

## 3. Data and objects
- Use objects to protect behavior and invariants; use plain data structures for transparent transfer when appropriate.
- Do not expose internal representation unnecessarily.
- Avoid hybrid types that expose fields while also pretending to enforce behavioral encapsulation.
- Make illegal states hard to construct through validated constructors, factories, value objects, or types.
- Prefer immutable values where mutation adds no benefit.
- Keep data-transfer structures at boundaries; do not let transport or database schemas become the domain model by default.
- Avoid long navigation chains that reveal internal structure and increase coupling.
- Place behavior near the data and invariant it governs.

## 4. Classes and modules
- Give each module a narrow, cohesive reason to change.
- Keep public surfaces smaller than internal implementation.
- Group code by business capability or policy when that improves change locality; avoid organizing solely by technical category.
- Depend on abstractions at volatile boundaries, not everywhere.
- Combine tiny types whose separation adds navigation cost without independent change value.
- Split large modules when distinct responsibilities, ownership, release cadence, or dependency needs emerge.
- Protect invariants inside the owning type or module.
- Prefer composition over inheritance when variation is behavioral and substitutability is uncertain.
- If inheritance is used, ensure derived types honor base contracts without special-case caller logic.
- Make clients depend only on the operations they need; split broad interfaces by client role.

## 5. Comments and documentation
Useful comments explain:
- Why a non-obvious decision exists.
- Constraints, hazards, compatibility requirements, and trade-offs.
- Public contracts, units, ownership, thread safety, and failure semantics.
- Temporary workarounds that include an exit condition or issue reference.

Avoid comments that:
- Restate obvious syntax.
- Compensate for misleading names or structure.
- Preserve deleted code.
- Claim behavior the implementation does not guarantee.
- Add decorative headings or noise.

Update documentation in the same change as the behavior it describes.

## 6. Formatting
- Use formatting to reveal conceptual groups and reading order.
- Place tightly related declarations near one another unless project conventions dictate otherwise.
- Keep files navigable and avoid unrelated declarations sharing a module merely for convenience.
- Follow automatic formatters and local conventions; do not create style-only churn in functional changes.
- Keep the most important public concepts easy to discover.

## 7. Error handling
- Model expected failures explicitly when callers can take meaningful action.
- Use exceptions or error values consistently with the ecosystem and repository.
- Never swallow failures without a deliberate fallback and observability.
- Preserve the original cause when translating errors across a boundary.
- Add context once, at the layer that knows what operation was attempted.
- Avoid returning sentinel values that are easily ignored when absence or failure matters.
- Distinguish validation errors, conflicts, unavailable dependencies, authorization failures, corruption, and programmer defects.
- Define retry semantics. Retry only operations that are safe or made idempotent, using bounded attempts and backoff where appropriate.
- Keep cleanup deterministic through language-native resource management constructs.
- Do not expose infrastructure-specific errors through stable business interfaces unless intentionally part of the contract.

## 8. Duplication and abstraction
- Remove duplicated knowledge: the same business rule, mapping, invariant, or decision maintained in multiple places.
- Do not merge code merely because its current text looks similar if the concepts change independently.
- Prefer the simplest abstraction that gives one authoritative home to a rule.
- Wait for evidence before generalizing. Two examples may be coincidence; repeated change patterns are stronger evidence.
- Keep abstractions honest: names, contracts, and supported variation must match actual use.
- Delete dead code rather than commenting it out or preserving speculative hooks.

## 9. Concurrency
- Minimize shared mutable state.
- Make ownership, lifecycle, synchronization, and cancellation explicit.
- Keep concurrent coordination separate from business calculations where possible.
- Define ordering, idempotency, reentrancy, and duplicate-delivery behavior.
- Test races and time-dependent behavior with deterministic seams, not arbitrary sleeps.
- Treat timeouts, cancellation, partial completion, and resource exhaustion as first-class failure modes.

## 10. Refactoring heuristics
Consider refactoring when encountering:
- Long functions with mixed abstraction levels.
- Deep nesting or repeated condition trees.
- Primitive values carrying domain meaning without validation.
- Feature envy, where behavior repeatedly reaches into another object's data.
- Widespread edits for one business change.
- Shotgun surgery or divergent responsibilities.
- Temporal coupling and required call order that is not encoded.
- Hidden dependencies on globals, clocks, randomness, environment, or static state.
- Repeated switches on type or mode that change together.
- Comments explaining what confusing code does.

Refactor in small steps: establish tests, rename, extract, move, simplify, rerun checks, and commit coherent stages.
