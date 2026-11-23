# Output Contracts

## Contents
1. Code review
2. Refactoring plan
3. Implemented refactor
4. Architecture proposal
5. Test plan
6. Technical-debt roadmap

## 1. Code review
Use:

### Summary
State the change reviewed, overall risk, and whether material blockers exist.

### Findings
Order by severity. For each finding include severity, location, observation, impact scenario, correction, and verification.

### Positive design choices
Mention only choices that materially reduce risk or improve changeability.

### Verification gaps
List checks not run, missing environment, ambiguous requirements, or untested paths.

Do not bury blocking findings in a long checklist.

## 2. Refactoring plan
Include:
- Goal and non-goals.
- Behavior to preserve.
- Current pain and evidence.
- Target structure.
- Ordered safe steps.
- Tests before, during, and after.
- Compatibility and migration.
- Rollback strategy.
- Risks and stopping conditions.

## 3. Implemented refactor
Report:
- Files changed.
- Behavioral contract preserved or intentionally changed.
- Structural improvements.
- Tests added or changed.
- Commands run and outcomes.
- Remaining risks or follow-up work.

Do not claim success from code generation alone.

## 4. Architecture proposal
Include:
- Context, constraints, and quality attributes.
- Use cases and durable business rules.
- Component responsibilities.
- Source dependency direction.
- Boundary contracts and crossing data.
- Data ownership and consistency.
- Runtime/deployment view.
- Security and observability.
- Alternatives and trade-offs.
- Evolution and migration plan.
- Validation scenarios.

Use ASCII or Mermaid diagrams only if requested or clearly useful. Label runtime flow separately from source dependencies.

## 5. Test plan
Include:
- Risks and behaviors under test.
- Test levels and why each is appropriate.
- Representative cases covering success, boundaries, failures, and recovery.
- Fixtures, doubles, and environment needs.
- Non-functional tests where justified.
- Entry/exit criteria.
- Known exclusions.

## 6. Technical-debt roadmap
Use a table with:
- Item.
- Evidence.
- Impact.
- Priority.
- Proposed action.
- Verification.
- Relative effort.
- Dependencies.

Follow with sequencing rationale and explicit items not recommended for remediation.
