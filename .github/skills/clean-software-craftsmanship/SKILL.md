---
name: clean-software-craftsmanship
description: Apply maintainability, architecture, and verification guidance to BharatQuant coding and review tasks.
---

# Clean Software Craftsmanship

Use the repository's full guidance at [clean-software-craftsmanship/SKILL.md](../../../clean-software-craftsmanship/SKILL.md) as the canonical source. Read only the reference files relevant to the active task.

Before changing code, understand its behavior, boundaries, invariants, failure modes, and affected callers. Prefer the smallest coherent change that preserves existing contracts. Keep policy independent of infrastructure where that boundary improves testability; avoid speculative layers and abstractions.

Treat errors, security, and data integrity as part of the contract. Add focused tests for changed behavior and risks, run the narrowest relevant project checks, and inspect the final diff for accidental scope expansion.
