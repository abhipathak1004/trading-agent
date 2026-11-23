# Review and Refactoring Playbook

## Contents
1. Review by artifact type
2. Severity model
3. Finding quality
4. Safe refactoring sequence
5. Repository modernization
6. Technical-debt planning
7. Anti-patterns in reviews

## 1. Review by artifact type

### Snippet or source file
Inspect contract, names, abstraction levels, state mutation, edge cases, errors, duplication, dependencies, and testability. Do not infer repository-wide architecture from one file.

### Diff or pull request
Read the changed code in surrounding context. Determine intended behavior, contract changes, regression paths, migration needs, and tests. Focus on problems introduced or exposed by the change. Distinguish blocking issues from follow-up improvements.

### Repository
Map entry points, packages/modules, build and test commands, dependency graph, persistence, external integrations, configuration, deployment, and observability. Use change history if locally available to identify hotspots. Sample representative paths before generalizing.

### Design proposal
Extract use cases, quality attributes, constraints, data ownership, trust boundaries, deployment topology, and failure modes. Test the design with concrete change scenarios such as replacing persistence, adding a channel, revising a rule, scaling one workload, or handling dependency outages.

## 2. Severity model
- **Critical:** Likely data loss, severe security exposure, unrecoverable corruption, or broad production outage. Must fix before release.
- **High:** Incorrect core behavior, major compatibility break, serious reliability issue, or architectural choice that makes an imminent requirement impractical. Normally blocks release.
- **Medium:** Material maintainability, testability, or localized correctness risk. Fix in the change when reasonably scoped, otherwise create owned follow-up work.
- **Low:** Small clarity, consistency, or resilience improvement with limited immediate risk.
- **Note:** Non-blocking observation, question, or alternative.

Severity depends on probability and impact in the actual context. A code smell alone is not a defect.

## 3. Finding quality
Use this structure:
1. **Location:** file, line range, or symbol.
2. **Observation:** concrete behavior or structure.
3. **Impact:** failure scenario or change cost.
4. **Recommendation:** smallest coherent fix.
5. **Verification:** test or check proving the fix.

Bad: "This violates single responsibility."
Good: "The parser also writes records and retries network failures, so a format change and delivery-policy change modify the same function. Keep parsing pure and move delivery/retry to the caller; verify malformed input and retry exhaustion separately."

## 4. Safe refactoring sequence
1. Establish baseline build and tests.
2. Capture unclear behavior with characterization tests.
3. Make mechanical changes first: rename, extract, move, inline, or introduce a seam.
4. Run focused checks after each coherent step.
5. Separate pure calculations from side effects.
6. Move policy toward its owner and dependencies outward.
7. Simplify interfaces after callers are migrated.
8. Delete dead paths and temporary adapters.
9. Run broader checks and inspect the final diff.

Rules:
- Never combine a large behavior change and broad structural rewrite without a staged reason.
- Keep commits reversible and reviewable when commit control is available.
- Preserve compatibility through adapters, parallel reads/writes, versioned contracts, or deprecation periods as appropriate.
- Stop if tests reveal unknown behavior; update the model before continuing.

## 5. Repository modernization
Prioritize by risk-adjusted value:
- Stabilize fragile tests and builds.
- Isolate high-change business policy from high-volatility infrastructure.
- Break harmful dependency cycles.
- Add seams around external systems.
- Consolidate duplicated rules.
- Reduce oversized modules at change hotspots.
- Retire dead code and obsolete compatibility layers.
- Improve observability before high-risk migrations.

Use incremental replacement for large legacy areas. Place a tested boundary around the old subsystem, route one use case at a time, compare outcomes where possible, and remove the old path when evidence supports cutover.

## 6. Technical-debt planning
For each item record:
- Evidence and affected artifacts.
- User or engineering impact.
- Trigger conditions and frequency.
- Blast radius.
- Proposed intervention.
- Dependencies and migration needs.
- Verification and rollback.
- Estimated size as relative bands, not false precision.
- Owner and target milestone only if the user provides or requests planning context.

Rank debt by expected loss and opportunity cost, not aesthetic displeasure. Include "leave as is" when remediation cost exceeds likely benefit.

Suggested roadmap:
- **Now:** correctness, security, data integrity, broken builds, or blockers to current delivery.
- **Next:** hotspot isolation, missing safety nets, repeated operational failures, and high-cost change paths.
- **Later:** broad consistency improvements and speculative flexibility.

## 7. Anti-patterns in reviews
- Counting function lines as a universal quality metric.
- Requiring one class or interface per concept.
- Treating all duplication as identical knowledge.
- Demanding dependency injection for stable pure helpers.
- Recommending patterns without a change scenario.
- Rewriting idiomatic code into a preferred personal style.
- Reporting hypothetical performance issues without a workload or complexity basis.
- Ignoring operational, data, migration, and compatibility costs.
