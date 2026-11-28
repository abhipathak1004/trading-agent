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

from bq.agent_prompts import (
    RESEARCH_SYSTEM,
    RESEARCH_TEMPLATE,
    PromptSource,
    resolve_prompt,
)
from bq.agent_run_store import AgentRunStep, AgentRunStore
from bq.agent_runtime import (
    AgentStep,
    LoopOutcome,
    ModelReply,
    run_agent_loop,
)
from bq.core import Clock, SystemClock, utc
from bq.llm_service import CompletionOutcome
from bq.model_router import ModelRouter, RouteUse
from bq.settings import SettingsStore
from bq.tools.base import Tool
from bq.tools.control import ResponseTool, UnknownTool
from bq.tools.market import market_tools
from bq.tools.news import news_tools
from bq.tools.registry import AgentToolPolicy, ToolRegistry, ToolRunner

AGENT_ID = "research"
MAX_TURNS = 10


class RouterLike(Protocol):
    """The slice of ``ModelRouter`` the agent needs. Tests inject a fake."""

    def complete(
        self,
        agent_id: str,
        *,
        prompt: str,
        system: str = "",
        task_id: str = ...,
        task_version: str = ...,
        timeout: float | None = None,
    ) -> CompletionOutcome: ...

    @property
    def routes_used(self) -> tuple[RouteUse, ...]: ...


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
        router: RouterLike | None = None,
        prompts: PromptSource | None = None,
        run_store: AgentRunStore | None = None,
        transport: object = None,
    ) -> None:
        self.path = path
        self.clock = clock or SystemClock()
        self.feed_url = feed_url
        self.router: RouterLike = router or ModelRouter(SettingsStore(path, self.clock))
        self.prompts = prompts
        self.run_store = run_store
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
            system, template = resolve_prompt(
                self.prompts, AGENT_ID, RESEARCH_SYSTEM, RESEARCH_TEMPLATE
            )
            prompt = template.format(task=task, history="\n\n".join(entries))
            system_text = system + "\n\n" + runner.tools_prompt()
            outcome = self.router.complete(
                AGENT_ID,
                prompt=prompt,
                system=system_text,
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

    def _last_route(self) -> RouteUse | None:
        routes = getattr(self.router, "routes_used", ())
        if not routes:
            return None
        return routes[-1]

    def run(self, task: str, *, max_turns: int = MAX_TURNS) -> ResearchRun:
        runner = self.runner()
        turn = self._model_turn(runner)
        started_at = utc(self.clock.now())
        outcome: LoopOutcome = run_agent_loop(
            task=task, runner=runner, model_turn=turn, max_turns=max_turns
        )
        input_tokens, output_tokens = turn.usage()  # type: ignore[attr-defined]
        route = self._last_route()
        provider = "" if route is None else route.provider
        model = "" if route is None else route.model
        if self.run_store is not None:
            self.run_store.record(
                agent_id=AGENT_ID,
                task=task,
                final_text=outcome.final_text,
                turns=outcome.turns,
                stopped=outcome.stopped,
                provider=provider,
                model=model,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                steps=tuple(
                    AgentRunStep(
                        turn=step.turn,
                        kind=step.kind,
                        tool=step.tool,
                        arguments=dict(step.arguments),
                        output=step.output,
                    )
                    for step in outcome.steps
                ),
                started_at=started_at,
            )
        return ResearchRun(
            task=task,
            final_text=outcome.final_text,
            turns=outcome.turns,
            stopped=outcome.stopped,
            tool_calls=outcome.tool_calls,
            steps=outcome.steps,
            provider=provider,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )


__all__ = [
    "AGENT_ID",
    "ResearchAgent",
    "ResearchRun",
]
