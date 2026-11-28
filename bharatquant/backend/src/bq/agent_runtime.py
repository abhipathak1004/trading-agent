"""A tiny, deterministic agent loop.

Adapted from Agent Zero's design: a message loop that, each turn, builds a
prompt, asks the model for either a tool call or a final answer, runs the tool,
appends the bounded result to history, and stops when a tool returns
``break_loop``.

Deliberate differences for BharatQuant:

* **No streaming and no provider coupling here.** The loop runs over an
  injected ``ModelTurn`` callable. Wiring a real model is the caller's job
  (see ``bq.research_agent``), so the loop stays testable with a fake model.
* **Bounded history.** History keeps a fixed number of recent turns and a
  running summary string. When it grows past a token budget it drops the oldest
  turns into the summary deterministically, the same *idea* as Agent Zero's
  compressible records but without a summarising model call.
* **Observable.** Every tool call is recorded as an ``AgentStep`` so the UI can
  show what the agent did, not just its final answer.
"""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field

from bq.tools.base import ToolBlocked, ToolError, ToolValidationError
from bq.tools.parsing import ToolCallParseError, parse_tool_call
from bq.tools.registry import ToolRunner

MAX_TURNS = 16
MAX_HISTORY_ENTRIES = 40
MAX_SUMMARY_CHARS = 8_000
MAX_ENTRY_CHARS = 12_000
STEP_PREVIEW_CHARS = 1_200


class AgentLoopError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ModelReply:
    """What a model turn is allowed to return: a tool call or final text.

    ``tool_call_id`` and ``tool_result`` let a native tool protocol be replayed
    on the next turn. Providers such as Bedrock's ``Converse`` require the
    assistant's ``toolUse`` block *and* the matching ``toolResult`` to appear in
    the conversation; without them the model has no record of its own call and
    re-requests the same tool forever.
    """

    text: str
    tool_name: str | None = None
    tool_args: Mapping[str, object] = field(default_factory=dict)
    tool_call_id: str | None = None
    tool_result: str | None = None


ModelTurn = Callable[[str, Sequence["HistoryEntry"]], ModelReply]


@dataclass(frozen=True, slots=True)
class HistoryEntry:
    role: str
    content: str

    def __post_init__(self) -> None:
        if self.role not in {"user", "assistant", "tool", "system"}:
            raise AgentLoopError("Invalid history role.")
        if len(self.content) > MAX_ENTRY_CHARS:
            object.__setattr__(self, "content", self.content[:MAX_ENTRY_CHARS])


@dataclass(slots=True)
class ConversationHistory:
    """Bounded turn history with a deterministic rolling summary."""

    summary: str = ""
    entries: list[HistoryEntry] = field(default_factory=list)

    def add(self, role: str, content: str) -> None:
        self.entries.append(HistoryEntry(role, content))
        self._trim()

    def _trim(self) -> None:
        while len(self.entries) > MAX_HISTORY_ENTRIES:
            oldest = self.entries.pop(0)
            self._fold(oldest)

    def _fold(self, entry: HistoryEntry) -> None:
        line = f"{entry.role}: {entry.content}".strip()
        combined = f"{self.summary}\n{line}".strip() if self.summary else line
        if len(combined) > MAX_SUMMARY_CHARS:
            combined = combined[-MAX_SUMMARY_CHARS:]
        self.summary = combined

    def as_prompt(self) -> str:
        parts = []
        if self.summary:
            parts.append("Earlier (summarized):\n" + self.summary)
        for entry in self.entries:
            parts.append(f"{entry.role}: {entry.content}")
        return "\n\n".join(parts)


@dataclass(frozen=True, slots=True)
class AgentStep:
    """One observable action in the loop, for the UI trace."""

    turn: int
    kind: str  # "tool" | "blocked" | "error"
    tool: str
    arguments: Mapping[str, object]
    output: str

    def to_dict(self) -> dict[str, object]:
        return {
            "turn": self.turn,
            "kind": self.kind,
            "tool": self.tool,
            "arguments": dict(self.arguments),
            "output": self.output,
        }


@dataclass(frozen=True, slots=True)
class LoopOutcome:
    final_text: str
    turns: int
    tool_calls: tuple[str, ...]
    stopped: bool
    steps: tuple[AgentStep, ...] = ()


def _preview(value: object) -> str:
    text = value if isinstance(value, str) else str(value)
    if len(text) <= STEP_PREVIEW_CHARS:
        return text
    return text[:STEP_PREVIEW_CHARS].rstrip() + "…"


def _stage_tool_exchange(
    model_turn: ModelTurn,
    reply: ModelReply,
    name: str,
    args: Mapping[str, object],
    output: str,
) -> None:
    """Offer a just-executed tool call to the closure for native replay.

    Purely optional: a provider without a native tool protocol simply has no
    ``pending`` slot, and the loop keeps working from the text history. A
    provider that has one receives the assistant request and its result, which
    is what stops it re-requesting the same tool on the following turn.
    """
    pending = getattr(model_turn, "pending", None)
    if not isinstance(pending, dict):
        return
    call_id = reply.tool_call_id
    if not call_id:
        # No id means the call came from parsed prose, not a native block, so
        # there is no protocol to replay.
        return
    from bq.llm_client import ToolCallRequest, ToolResult

    pending["call"] = ToolCallRequest(call_id=call_id, name=name, arguments=dict(args))
    pending["result"] = ToolResult(call_id=call_id, name=name, content=output)


def _resolve_reply(
    reply: ModelReply,
) -> tuple[str, Mapping[str, object]]:
    """Pick the tool call from an explicit field or from the reply text."""
    if reply.tool_name:
        return reply.tool_name, reply.tool_args
    try:
        return parse_tool_call(reply.text, recover=True)
    except ToolCallParseError:
        return "", {}


def run_agent_loop(
    *,
    task: str,
    runner: ToolRunner,
    model_turn: ModelTurn,
    max_turns: int = MAX_TURNS,
) -> LoopOutcome:
    """Run the loop until a tool says stop, the model answers, or turns run out."""
    if not isinstance(task, str) or not task.strip():
        raise AgentLoopError("Task text is required.")
    if not 1 <= max_turns <= MAX_TURNS:
        raise AgentLoopError(f"max_turns must be between 1 and {MAX_TURNS}.")

    history = ConversationHistory()
    history.add("system", runner.tools_prompt())
    history.add("user", task.strip())
    tool_calls: list[str] = []
    steps: list[AgentStep] = []

    for turn in range(1, max_turns + 1):
        reply = model_turn(task, tuple(history.entries))
        if not isinstance(reply, ModelReply):
            raise AgentLoopError("Model turn must return a ModelReply.")

        name, args = _resolve_reply(reply)
        if not name:
            # No tool call: treat the reply text as the final answer.
            text = reply.text.strip()
            history.add("assistant", text)
            return LoopOutcome(
                final_text=text or "(empty response)",
                turns=turn,
                tool_calls=tuple(tool_calls),
                stopped=False,
                steps=tuple(steps),
            )

        tool_calls.append(name)
        try:
            output = runner.run(name, args)
        except ToolBlocked as exc:
            history.add("tool", f"{name} blocked: {exc}")
            steps.append(
                AgentStep(turn, "blocked", name, dict(args), _preview(str(exc)))
            )
            continue
        except (ToolValidationError, ToolError) as exc:
            history.add("tool", f"{name} error: {exc}")
            steps.append(AgentStep(turn, "error", name, dict(args), _preview(str(exc))))
            continue

        message = str(output["message"])
        history.add("assistant", f"call {name}")
        history.add("tool", message)
        # Hand the executed call and its output to the model-turn closure, so a
        # provider with a native tool protocol receives the assistant's request
        # and the matching result on the next turn. Without this the model has
        # no record of its own call and re-requests the same tool.
        _stage_tool_exchange(model_turn, reply, name, args, message)
        steps.append(AgentStep(turn, "tool", name, dict(args), _preview(message)))
        if output["break_loop"]:
            return LoopOutcome(
                final_text=message,
                turns=turn,
                tool_calls=tuple(tool_calls),
                stopped=True,
                steps=tuple(steps),
            )

    return LoopOutcome(
        final_text=history.summary or "Turn limit reached with no final answer.",
        turns=max_turns,
        tool_calls=tuple(tool_calls),
        stopped=False,
        steps=tuple(steps),
    )
