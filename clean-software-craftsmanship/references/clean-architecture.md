# Clean Architecture Reference

## Contents
1. Architectural objective
2. Policy levels and dependency direction
3. Boundaries and components
4. Domain rules and use cases
5. Adapters and infrastructure
6. Component principles
7. Boundary design
8. Data, transactions, and distributed systems
9. Framework and deployment decisions
10. Architecture failure modes

## 1. Architectural objective
Architecture should preserve options and reduce the cost of changing high-value policy. A good structure allows business rules to be understood and tested without starting the full application or depending on databases, web servers, user interfaces, vendors, or delivery mechanisms.

Do not judge architecture by diagram symmetry or layer count. Judge it by change locality, dependency direction, testability, deployability, operational clarity, and the cost of replacing details.

## 2. Policy levels and dependency direction
- High-level policy defines business meaning and use-case decisions.
- Low-level mechanisms perform details such as persistence, transport, serialization, rendering, scheduling, and vendor integration.
- Source dependencies should point toward the more stable, higher-level policy.
- Runtime control flow may cross outward and back inward; source dependency direction is controlled using interfaces, callbacks, ports, messages, or equivalent language constructs.
- Define abstractions on the side that owns the policy, not automatically beside the implementation.
- Keep inward code independent of outward frameworks and schemas.

Check for violations:
- Business logic imports an ORM, web controller, cloud SDK, UI toolkit, or message-broker client.
- Domain types inherit framework base classes.
- Use cases accept transport request objects or return database records.
- Tests of policy require network, filesystem, database, or framework startup.

## 3. Boundaries and components
Create a boundary when it protects a meaningful policy, volatility, ownership area, security zone, deployment concern, or test seam. Boundaries have costs: mappings, interfaces, versioning, latency, duplicated models, and operational complexity.

Use full boundaries when independent change or replacement is likely and valuable. Use partial boundaries when direction needs protection but independent deployment is unnecessary. Defer a boundary when evidence is weak, while keeping code organized so separation remains affordable.

A component should have:
- A coherent responsibility and vocabulary.
- An explicit public contract.
- Controlled dependencies.
- Clear data and lifecycle ownership.
- Tests aligned with its responsibility.

## 4. Domain rules and use cases
- Represent enterprise-wide rules and durable invariants in domain concepts that do not depend on delivery details.
- Represent application-specific workflows as use cases that coordinate entities, authorization decisions, gateways, and output.
- Keep use cases explicit enough to reveal system capabilities.
- Make input and output models specific to the use case when shared framework models would couple policies to details.
- Put validation at the correct level: syntax at transport boundaries, use-case preconditions in application policy, and invariants in the domain owner.
- Distinguish business rules from orchestration and from technical workflows.

## 5. Adapters and infrastructure
Adapters translate between external formats and internal contracts. Examples include controllers, presenters, CLI handlers, repository implementations, event consumers, serializers, and vendor gateways.

Infrastructure implements outward-facing mechanisms:
- Databases and filesystems.
- Web and messaging frameworks.
- Cloud and vendor SDKs.
- UI and device integrations.
- Schedulers and process hosts.

Keep adapter mappings deliberate. Mapping cost is justified when it prevents unstable schemas from contaminating policy. Avoid reflection-heavy or magical mappings that hide required fields and error behavior.

## 6. Component principles
Use these as balancing forces rather than formulas:

- **Release cohesion:** Put code that must be released together into a coherent unit. A reusable component needs a meaningful release and compatibility story.
- **Common change:** Group code that tends to change for the same reasons and at the same times.
- **Common reuse:** Do not force clients to depend on code they do not use. Split components when consumers and release needs diverge.
- **Acyclic dependencies:** Keep the component dependency graph free of cycles. Break cycles with dependency inversion, extracted shared policy, events, or boundary restructuring.
- **Stable dependencies:** Direct dependencies toward components that are harder or less desirable to change.
- **Stable abstractions:** Highly stable components should expose abstractions appropriate to the extension they require, while concrete volatile details remain replaceable.

Avoid turning stability metrics into targets. Use dependency and change history as evidence.

## 7. Boundary design
For each boundary define:
- Owner of the contract.
- Allowed dependency direction.
- Commands, queries, events, or calls crossing it.
- Data shape, validation, versioning, and compatibility.
- Error translation and retry behavior.
- Identity, authorization, auditing, and privacy obligations.
- Transaction and consistency expectations.
- Observability: correlation, logs, metrics, traces, and alerts.
- Performance budget and failure isolation.

Pass simple boundary data that expresses the contract. Do not pass rich framework, persistence, or UI objects inward.

## 8. Data, transactions, and distributed systems
Clean boundaries do not remove distributed-systems realities.
- Identify the authoritative owner for each datum and invariant.
- Keep transactions within a consistency boundary when possible.
- Across processes, define idempotency, deduplication, ordering, retries, timeouts, poison-message handling, and reconciliation.
- Use eventual consistency only with explicit user-visible and operational semantics.
- Treat events as contracts; distinguish facts that occurred from commands asking for work.
- Avoid shared databases between independently evolving services unless the coupling is intentionally accepted.
- Avoid duplicating the same business rule in producers and consumers without a source of truth.

## 9. Framework and deployment decisions
- Delay irreversible technology choices until sufficient evidence exists, but do not postpone decisions needed to retire material risk.
- Wrap vendor-specific APIs when replacement, testing, or policy isolation has meaningful value.
- Do not wrap stable language or standard-library facilities without a concrete benefit.
- A monolith can have clean internal boundaries; microservices do not guarantee separation.
- Choose deployment units based on scaling, availability, ownership, release cadence, fault isolation, data consistency, and operational maturity.
- Keep the application runnable and observable in its real environment.

## 10. Architecture failure modes
Watch for:
- Layered structures where every change crosses all layers.
- Interfaces with only one implementation and no testing, ownership, or volatility benefit.
- Domain models reduced to data bags while rules live in controllers or services.
- Framework models leaking across the system.
- Generic repositories or service layers that erase domain meaning.
- Circular component dependencies.
- Shared utility modules becoming unowned dependency magnets.
- Premature microservices creating network and operational coupling.
- Event-driven designs without ownership, idempotency, or schema evolution.
- Business logic embedded in SQL, templates, workflow configuration, or infrastructure scripts without deliberate ownership and tests.
