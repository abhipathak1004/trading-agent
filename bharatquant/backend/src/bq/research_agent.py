"""The research agent: a real model driving the tool loop.

This ties together the pieces built so far:

* ``bq.tools`` — what the agent can do (news, market data, web fetch).
* ``bq.agent_runtime`` — the bounded, observable tool loop.
* ``bq.llm_service`` — the configured provider, routed and called.

The agent is given a research brief and runs until it calls ``response`` (or the
turn limit is reached). It returns not only the final answer but the full step
trace, so the UI can show *how* it got there rather than a black-box string.

The model turn is deliberately thin: it renders the running conversation into a
single prompt with a strict output contract, calls the provider, and hands the
parsed tool call back to the loop. No hidden state, no streaming.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from bq.agent_runtime import (
    AgentStep,
    LoopOutcome,
    ModelReply,
    run_agent_loop,
)
from bq.core import Clock, SystemClock
from bq.llm_client import LLMClientError, LLMNotConfigured
from bq.llm_service import CompletionOutcome, LLMService
from bq.settings import SettingsStore
from bq.tools.base import Tool
from bq.tools.control import ResponseTool, UnknownTool
from bq.tools.market import market_tools
from bq.tools.news import news_tools
from bq.tools.registry import AgentToolPolicy, ToolRegistry, ToolRunner

AGENT_ID = "research"
MAX_TURNS = 10

_SYSTEM_PROMPT = """\
You are the BharatQuant research agent for Indian equity markets. You are a \
paper-only research assistant: you never place orders and you never give \
investment advice as a certainty.

You work by calling tools, one turn at a time. On each turn reply with EXACTLY \
one JSON object and nothing else — no prose, no markdown fences:

    {"tool_name": "<tool>", "tool_args": {<arguments>}}

Rules:
- Use only the tools listed below, with the exact names shown.
- Read tool output carefully; it is real data, cite it plainly, do not invent numbers.
- Prefer specific tools over guessing. If a user names a company, resolve its \
  Yahoo symbol (e.g. RELIANCE.NS, TCS.NS, INFY.NS, HDFCBANK.NS).
- When you have enough for a useful, concise answer, call `response` with a \
  tight, well-structured summary. If data is missing or a tool failed, say so \
  rather than fabricating.
- Keep going until you call `response`; do not stop mid-investigation.
"""

_FINAL_TEMPLATE = """\
Task: {task}

Conversation so far:
{history}

Reply with the next single JSON tool call now."""


class ModelService(Protocol):
    """The slice of ``LLMService`` the agent needs. Tests inject a fake."""

    def complete(
        self,
        *,
        prompt: str,
        system: str = "",
        task_id: str = ...,
        task_version: str = ...,
    ) -> CompletionOutcome: ...


@dataclass(frozen=True, slots=True)
class ResearchRun:
    """The result of one agent run, including its step trace."""

    task: str
    final_text: str
    turns: int
    stopped: bool
    tool_calls: tuple[str, ...]
    steps: tuple[AgentStep, ...]
    provider: str
    model: str
    input_tokens: int | None
    output_tokens: int | None


class ResearchAgent:
    """Builds tools and runs the model-driven loop for one database."""

    def __init__(
        self,
        path: Path,
        *,
        clock: Clock | None = None,
        feed_url: str,
        service: ModelService | None = None,
        transport: object = None,
    ) -> None:
        self.path = path
        self.clock = clock or SystemClock()
        self.feed_url = feed_url
        self.service: ModelService = service or LLMService(
            SettingsStore(path, self.clock)
        )
        self.transport = transport

    def tools(self) -> tuple[Tool, ...]:
        return (
            *news_tools(self.path, feed_url=self.feed_url, clock=self.clock),
            *market_tools(self.path, clock=self.clock, transport=self.transport),  # type: ignore[arg-type]
        )

    def registry(self) -> ToolRegistry:
        registry = ToolRegistry(self.tools())
        registry.register(ResponseTool())
        registry.register(UnknownTool(registry))
        return registry

    def runner(self) -> ToolRunner:
        return ToolRunner(
            self.registry(),
            AgentToolPolicy(profile=AGENT_ID, read_only=False),
        )

    def _model_turn(
        self, runner: ToolRunner
    ) -> Callable[[str, Sequence[object]], ModelReply]:
        """Build the ``ModelTurn`` closure that drives the loop.

        Token usage is accumulated in the closure so the run reports the real
        total across every provider call, not just the last one.
        """
        totals = {"input": 0, "output": 0, "seen": False}

        def turn(task: str, history: Sequence[object]) -> ModelReply:
            entries = [
                f"{getattr(entry, 'role', '')}: {getattr(entry, 'content', '')}"
                for entry in history
            ]
            prompt = _FINAL_TEMPLATE.format(task=task, history="\n\n".join(entries))
            system = _SYSTEM_PROMPT + "\n\n" + runner.tools_prompt()
            outcome = self.service.complete(
                prompt=prompt,
                system=system,
                task_id="research.agent",
                task_version="v1",
            )
            if outcome.input_tokens is not None:
                totals["input"] += outcome.input_tokens
                totals["seen"] = True
            if outcome.output_tokens is not None:
                totals["output"] += outcome.output_tokens
                totals["seen"] = True
            text = outcome.text.strip()
            # The provider may already have produced a native tool call.
            if outcome.result.function_calls:
                call = outcome.result.function_calls[0]
                return ModelReply(
                    text=text, tool_name=call.name, tool_args=call.arguments
                )
            return ModelReply(text=text)

        turn.usage = lambda: (  # type: ignore[attr-defined]
            (int(totals["input"]), int(totals["output"]))
            if totals["seen"]
            else (None, None)
        )
        return turn

    def _config(self) -> object:
        store = getattr(self.service, "store", None)
        if store is None:
            return None
        return store.load()

    def run(self, task: str, *, max_turns: int = MAX_TURNS) -> ResearchRun:
        runner = self.runner()
        config = self._config()
        turn = self._model_turn(runner)
        outcome: LoopOutcome = run_agent_loop(
            task=task, runner=runner, model_turn=turn, max_turns=max_turns
        )
        input_tokens, output_tokens = turn.usage()  # type: ignore[attr-defined]
        return ResearchRun(
            task=task,
            final_text=outcome.final_text,
            turns=outcome.turns,
            stopped=outcome.stopped,
            tool_calls=outcome.tool_calls,
            steps=outcome.steps,
            provider=getattr(getattr(config, "provider", None), "value", ""),
            model=str(getattr(config, "model_name", "")),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )


__all__ = [
    "AGENT_ID",
    "LLMClientError",
    "LLMNotConfigured",
    "ResearchAgent",
    "ResearchRun",
]
