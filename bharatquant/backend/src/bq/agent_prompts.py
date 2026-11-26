"""Editable, versioned prompts for the analysis agents.

Prompts are configuration: the owner can refine them from the Agent Lab without
editing code. Every save appends an immutable version so a change is reviewable,
and the store returns the default when no override exists. Validation keeps a
template's required placeholders intact; otherwise a refinement would fail at
format time during a live run.
"""

from __future__ import annotations

import re
import sqlite3
import string
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol

from bq.analysis.prompts import (
    BEAR_SYSTEM,
    BEAR_TEMPLATE,
    BULL_SYSTEM,
    BULL_TEMPLATE,
    JUDGE_SYSTEM,
    JUDGE_TEMPLATE,
    RISK_SYSTEM,
    RISK_TEMPLATE,
)
from bq.core import (
    Clock,
    SystemClock,
    canonical_digest,
    open_connection,
    parse_timestamp,
    timestamp,
    utc,
)
from bq.infra.audit import append_event

MAX_PROMPT_CHARS = 16_384
MAX_AGENT_ID_CHARS = 64
_AGENT_ID = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")
# Newlines, carriage returns and tabs are legitimate; other controls are not.
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

DECISION_SYSTEM = (
    "You are a typed decision component. Answer the supplied questions about the "
    "supplied state. Do not explain."
)


class PromptError(ValueError):
    pass


class PromptNotFound(PromptError):
    pass


@dataclass(frozen=True, slots=True)
class PromptDefinition:
    agent_id: str
    label: str
    description: str
    system: str
    template: str
    required_fields: tuple[str, ...] = ()


DEFINITIONS: tuple[PromptDefinition, ...] = (
    PromptDefinition(
        agent_id="bull_debater",
        label="Bull advocate",
        description="Argues the strongest honest long case from the supplied facts.",
        system=BULL_SYSTEM,
        template=BULL_TEMPLATE,
        required_fields=("symbol", "timeframe", "as_of", "facts"),
    ),
    PromptDefinition(
        agent_id="bear_debater",
        label="Bear advocate",
        description="Argues the strongest honest case against the long position.",
        system=BEAR_SYSTEM,
        template=BEAR_TEMPLATE,
        required_fields=("symbol", "timeframe", "as_of", "facts"),
    ),
    PromptDefinition(
        agent_id="research_judge",
        label="Research judge",
        description="Weighs bull and bear against the facts and the confidence ceiling.",
        system=JUDGE_SYSTEM,
        template=JUDGE_TEMPLATE,
        required_fields=(
            "symbol",
            "timeframe",
            "as_of",
            "confidence_ceiling",
            "facts",
            "bull",
            "bear",
        ),
    ),
    PromptDefinition(
        agent_id="risk_analyst",
        label="Risk analyst",
        description="Describes invalidation and early warnings; advisory only.",
        system=RISK_SYSTEM,
        template=RISK_TEMPLATE,
        required_fields=("symbol", "timeframe", "as_of", "facts"),
    ),
    PromptDefinition(
        agent_id="decision_screener",
        label="Decision screener (LLM fallback)",
        description=(
            "System prompt used when the decision battery falls back to a text "
            "model. The question set and answer types stay code-owned."
        ),
        system=DECISION_SYSTEM,
        template="",
        required_fields=(),
    ),
)

DEFINITIONS_BY_ID = {definition.agent_id: definition for definition in DEFINITIONS}


@dataclass(frozen=True, slots=True)
class AgentPrompt:
    agent_id: str
    label: str
    description: str
    system: str
    template: str
    required_fields: tuple[str, ...]
    version: int
    updated_at: datetime | None
    is_default: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "agent_id": self.agent_id,
            "label": self.label,
            "description": self.description,
            "system": self.system,
            "template": self.template,
            "required_fields": list(self.required_fields),
            "version": self.version,
            "updated_at": (
                None if self.updated_at is None else self.updated_at.isoformat()
            ),
            "is_default": self.is_default,
        }


class PromptSource(Protocol):
    def get(self, agent_id: str) -> AgentPrompt: ...


def _validate_text(value: object, *, label: str) -> str:
    if not isinstance(value, str):
        raise PromptError(f"{label} must be text.")
    if not value.strip():
        raise PromptError(f"{label} cannot be empty.")
    if len(value) > MAX_PROMPT_CHARS:
        raise PromptError(f"{label} is too long.")
    if _CONTROL.search(value):
        raise PromptError(f"{label} contains an unsupported control character.")
    return value


def validate_agent_id(agent_id: str) -> PromptDefinition:
    if not isinstance(agent_id, str) or _AGENT_ID.fullmatch(agent_id) is None:
        raise PromptNotFound("Unknown agent.")
    definition = DEFINITIONS_BY_ID.get(agent_id)
    if definition is None:
        raise PromptNotFound("Unknown agent.")
    return definition


def validate_template(template: str, definition: PromptDefinition) -> str:
    if definition.required_fields == () and not template.strip():
        return ""
    text = _validate_text(template, label="Template")
    try:
        parsed = list(string.Formatter().parse(text))
    except ValueError as exc:
        raise PromptError("Template has invalid braces.") from exc
    fields: list[str] = []
    for _, field_name, _, _ in parsed:
        if field_name is None:
            continue
        if not field_name or "." in field_name or "[" in field_name:
            raise PromptError("Template fields must be simple placeholder names.")
        fields.append(field_name)
    required = set(definition.required_fields)
    unknown = set(fields) - required
    if unknown:
        raise PromptError(f"Template uses an unknown field: {sorted(unknown)[0]}.")
    missing = required - set(fields)
    if missing:
        raise PromptError(
            f"Template is missing a required field: {sorted(missing)[0]}."
        )
    sample = {
        field: (0.0 if field == "confidence_ceiling" else "sample")
        for field in definition.required_fields
    }
    try:
        text.format(**sample)
    except (KeyError, ValueError, IndexError, AttributeError) as exc:
        raise PromptError("Template does not format with the required fields.") from exc
    return text


class PromptStore:
    """Append-only prompt overrides with the code defaults as the baseline."""

    def __init__(self, path: Path, clock: Clock | None = None) -> None:
        self.path = path
        self.clock = clock or SystemClock()

    def _connect(self) -> sqlite3.Connection:
        return open_connection(self.path, foreign_keys=False)

    def list(self) -> tuple[AgentPrompt, ...]:
        rows = self._latest_rows()
        return tuple(
            self._to_prompt(definition, rows.get(definition.agent_id))
            for definition in DEFINITIONS
        )

    def get(self, agent_id: str) -> AgentPrompt:
        definition = validate_agent_id(agent_id)
        row = self._latest_rows({agent_id}).get(agent_id)
        return self._to_prompt(definition, row)

    def save(self, agent_id: str, *, system: str, template: str) -> AgentPrompt:
        definition = validate_agent_id(agent_id)
        system_text = _validate_text(system, label="System prompt")
        template_text = validate_template(template, definition)
        updated_at = utc(self.clock.now())
        digest = canonical_digest(
            {
                "agent_id": agent_id,
                "system": system_text,
                "template": template_text,
            }
        )
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            with connection:
                row = connection.execute(
                    "SELECT COALESCE(MAX(version), 0) FROM agent_prompts "
                    "WHERE agent_id = ?",
                    (agent_id,),
                ).fetchone()
                version = int(row[0]) + 1
                connection.execute(
                    "INSERT INTO agent_prompts "
                    "(prompt_id, agent_id, version, system, template, updated_at, "
                    "prompt_hash) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        f"{agent_id}:{version}",
                        agent_id,
                        version,
                        system_text,
                        template_text,
                        timestamp(updated_at),
                        digest,
                    ),
                )
                append_event(
                    connection,
                    occurred_at=updated_at,
                    actor="owner",
                    action="agent.prompt.saved",
                    object_type="agent_prompt",
                    object_id=agent_id,
                    payload={
                        "agent_id": agent_id,
                        "version": str(version),
                        "prompt_hash": digest,
                    },
                )
        return self.get(agent_id)

    def reset(self, agent_id: str) -> AgentPrompt:
        definition = validate_agent_id(agent_id)
        return self.save(
            agent_id, system=definition.system, template=definition.template
        )

    def _latest_rows(
        self, agent_ids: set[str] | None = None
    ) -> dict[str, tuple[int, str, str, str]]:
        query = (
            "SELECT p.agent_id, p.version, p.system, p.template, p.updated_at "
            "FROM agent_prompts AS p WHERE p.version = ("
            "SELECT MAX(q.version) FROM agent_prompts AS q "
            "WHERE q.agent_id = p.agent_id)"
        )
        parameters: tuple[object, ...] = ()
        if agent_ids:
            placeholders = ",".join("?" for _ in agent_ids)
            query = (
                "SELECT p.agent_id, p.version, p.system, p.template, p.updated_at "
                "FROM agent_prompts AS p WHERE p.version = ("
                "SELECT MAX(q.version) FROM agent_prompts AS q "
                f"WHERE q.agent_id = p.agent_id) AND p.agent_id IN ({placeholders})"
            )
            parameters = tuple(sorted(agent_ids))
        with closing(self._connect()) as connection:
            rows = connection.execute(query, parameters).fetchall()
        return {
            str(row[0]): (
                int(row[1]),
                str(row[2]),
                str(row[3]),
                str(row[4]),
            )
            for row in rows
        }

    @staticmethod
    def _to_prompt(
        definition: PromptDefinition,
        row: tuple[int, str, str, str] | None,
    ) -> AgentPrompt:
        if row is None:
            return AgentPrompt(
                agent_id=definition.agent_id,
                label=definition.label,
                description=definition.description,
                system=definition.system,
                template=definition.template,
                required_fields=definition.required_fields,
                version=0,
                updated_at=None,
                is_default=True,
            )
        version, system, template, updated = row
        try:
            updated_at = parse_timestamp(updated)
        except ValueError:
            updated_at = None
        return AgentPrompt(
            agent_id=definition.agent_id,
            label=definition.label,
            description=definition.description,
            system=system,
            template=template,
            required_fields=definition.required_fields,
            version=version,
            updated_at=updated_at,
            is_default=(
                system == definition.system and template == definition.template
            ),
        )


def default_prompt(agent_id: str) -> AgentPrompt:
    definition = validate_agent_id(agent_id)
    return PromptStore._to_prompt(definition, None)


def resolve_prompt(
    source: PromptSource | None,
    agent_id: str,
    default_system: str,
    default_template: str,
) -> tuple[str, str]:
    """Resolve a prompt, falling back to the code default on any store failure."""
    if source is None:
        return default_system, default_template
    try:
        prompt = source.get(agent_id)
    except Exception:  # noqa: BLE001 - a prompt store failure must not break a run
        return default_system, default_template
    return prompt.system, prompt.template


__all__ = [
    "AgentPrompt",
    "DECISION_SYSTEM",
    "DEFINITIONS",
    "DEFINITIONS_BY_ID",
    "MAX_PROMPT_CHARS",
    "PromptDefinition",
    "PromptError",
    "PromptNotFound",
    "PromptSource",
    "PromptStore",
    "default_prompt",
    "resolve_prompt",
    "validate_agent_id",
    "validate_template",
]
