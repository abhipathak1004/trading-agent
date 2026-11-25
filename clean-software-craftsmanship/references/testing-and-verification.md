# Testing and Verification

## Contents
1. Test strategy
2. Test design
3. Characterization and refactoring
4. Boundary and architecture tests
5. Non-functional verification
6. Completion checklist

## 1. Test strategy
Build a portfolio with fast deterministic tests for policy, focused integration tests for adapters and contracts, and a small number of end-to-end tests for critical journeys. Choose the lowest stable boundary that proves the risk.

Tests should support change, not mirror implementation. Avoid a fixed pyramid mandate when the system's risks demand a different mix.

## 2. Test design
- Name tests by behavior and condition.
- Use arrange-act-assert or an equivalent clear narrative.
- Keep one primary behavioral reason for failure per test.
- Assert externally meaningful outcomes rather than incidental calls when possible.
- Use test doubles at owned boundaries; do not mock every collaborator.
- Prefer real value objects and pure collaborators over mocks.
- Make time, randomness, identity, and external I/O controllable through seams.
- Cover normal behavior, boundaries, invalid input, failure propagation, recovery, and important state transitions.
- Avoid sleeps, order dependence, shared mutable fixtures, and reliance on external services in fast suites.
- Keep test data small and intention-revealing. Builders and factories should expose meaningful differences.

## 3. Characterization and refactoring
When behavior is poorly documented:
1. Identify observable inputs and outputs.
2. Capture current behavior, including surprising behavior, without declaring it correct.
3. Refactor behind the characterization tests.
4. Add specification tests for intended behavior.
5. Change behavior in a separate step and update expectations deliberately.

Golden-master tests are useful for broad legacy output but should be narrowed over time. Review snapshots so they do not become automatic approval mechanisms.

## 4. Boundary and architecture tests
Test:
- Serialization and schema compatibility.
- Adapter mapping and error translation.
- Database queries and transaction behavior.
- Authorization at trust boundaries.
- Idempotency, retries, duplicate delivery, and ordering for messages.
- Dependency rules, forbidden imports, and component cycles where tooling supports them.
- Public contracts through consumer-driven or provider contract tests when teams or processes evolve independently.

Architecture tests complement design review; they do not prove the architecture is useful.

## 5. Non-functional verification
Select checks based on risk:
- Performance benchmarks for known hot paths using representative inputs.
- Load, soak, and capacity tests for services with throughput or resource constraints.
- Race detection and stress tests for concurrent code.
- Fault injection for dependency loss, timeout, partial failure, and restart behavior.
- Security analysis for input handling, authorization, secrets, dependency exposure, and auditability.
- Migration rehearsal, rollback, reconciliation, and backup restore for data changes.
- Observability checks that verify useful logs, metrics, traces, and correlation without leaking sensitive data.

Do not claim performance, scalability, or thread safety solely from code inspection.

## 6. Completion checklist
- Baseline behavior is known.
- Tests target the changed risk.
- Focused checks pass.
- Broader available checks pass or failures are explained.
- Public contracts and migrations are covered.
- Error and rollback paths are exercised where material.
- No flaky timing assumptions were added.
- Test failures remain diagnosable.
- Unverified claims and environmental limitations are explicit.
