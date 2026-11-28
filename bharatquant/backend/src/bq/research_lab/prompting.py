"""Byte-stable prompt prefix and bounded observation compaction.

Prompt caching only pays when the prefix is byte-identical between calls. This
builder therefore keeps three things strictly apart:

* the **static prefix** — instructions, rendered tool specs and skill rules. It
  is built once, is identical on every call, and is what a provider caches;
* the **dynamic turn context** — ``as_of``, active run ids and tool summaries;
* the **stored observations** — compacted into summaries that keep their run and
  artifact references, because the artifacts themselves stay on disk.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Final

MIN_COMPACTION_TOKENS: Final = 256


class PromptError(ValueError):
    """The prompt cannot be assembled as described."""


@dataclass(frozen=True, slots=True)
class PromptTurn:
    system: str
    prompt: str
    compacted: bool
    omitted: int

    def to_dict(self) -> dict[str, object]:
        return {
            "system": self.system,
            "prompt": self.prompt,
            "compacted": self.compacted,
            "omitted": self.omitted,
        }


def _block(text: str, name: str) -> str:
    cleaned = text.strip()
    if not cleaned:
        raise PromptError(f"The {name} block must not be empty.")
    return cleaned


class ResearchPromptBuilder:
    """Assembles a cache-friendly prefix and a per-turn dynamic prompt."""

    def __init__(
        self,
        *,
        instructions: str,
        tools_prompt: str,
        skill_rules: str = "",
    ) -> None:
        self._instructions = _block(instructions, "instruction")
        self._tools = _block(tools_prompt, "tool")
        self._skills = skill_rules.strip()
        self._prefix = self._assemble()

    def _assemble(self) -> str:
        blocks = [self._instructions, self._tools]
        if self._skills:
            blocks.append(self._skills)
        return "\n\n".join(blocks)

    def static_prefix(self) -> str:
        """The identical string on every call, including across processes."""
        return self._prefix

    def build(
        self,
        *,
        as_of: str,
        run_ids: Sequence[str] = (),
        observations: Sequence[Mapping[str, object]] = (),
        compaction_token_threshold: int = 24000,
        count_tokens: Callable[[str], int] | None = None,
    ) -> PromptTurn:
        """Append the dynamic context after the untouched prefix."""
        if not as_of.strip():
            raise PromptError("A turn needs an as_of anchor.")
        counter = count_tokens or (lambda text: max(1, len(text) // 4))
        kept, compacted, omitted = compact_observations(
            observations,
            threshold_tokens=compaction_token_threshold,
            count_tokens=counter,
        )
        lines = [f"as_of: {as_of}"]
        if run_ids:
            lines.append("active runs: " + ", ".join(sorted(set(run_ids))))
        for item in kept:
            lines.append(str(item.get("text", "")))
        return PromptTurn(
            system=self._prefix,
            prompt="\n".join(lines),
            compacted=compacted,
            omitted=omitted,
        )


def compact_observations(
    observations: Sequence[Mapping[str, object]],
    *,
    threshold_tokens: int,
    count_tokens: Callable[[str], int],
) -> tuple[list[dict[str, object]], bool, int]:
    """Keep the newest observations that fit; summarise the rest by reference.

    Returns ``(kept, compacted, omitted)``. The summary keeps the run ids and
    artifact references of everything it replaced, so a later step can re-read
    the full output from disk instead of from the context window.
    """
    if threshold_tokens < MIN_COMPACTION_TOKENS:
        raise PromptError(
            f"The compaction threshold must be at least {MIN_COMPACTION_TOKENS}."
        )
    items = [dict(item) for item in observations]
    total = sum(count_tokens(str(item.get("text", ""))) for item in items)
    if total <= threshold_tokens:
        return items, False, 0

    budget = threshold_tokens // 2
    kept: list[dict[str, object]] = []
    used = 0
    for item in reversed(items):
        cost = count_tokens(str(item.get("text", "")))
        if used + cost > budget:
            break
        kept.append(item)
        used += cost
    kept.reverse()
    omitted = len(items) - len(kept)
    dropped = items[:omitted]
    refs: list[str] = []
    for item in dropped:
        run_id = item.get("run_id")
        if isinstance(run_id, str) and run_id:
            refs.append(run_id)
        artifacts = item.get("artifact_refs")
        if isinstance(artifacts, list):
            refs.extend(str(ref) for ref in artifacts)
    summary: dict[str, object] = {
        "kind": "compaction_summary",
        "run_id": "",
        "artifact_refs": sorted(set(refs)),
        "text": (
            f"[{omitted} earlier observations compacted; stored refs: "
            + (", ".join(sorted(set(refs))) if refs else "none")
            + "]"
        ),
    }
    return [summary, *kept], True, omitted


__all__ = [
    "MIN_COMPACTION_TOKENS",
    "PromptError",
    "PromptTurn",
    "ResearchPromptBuilder",
    "compact_observations",
]
