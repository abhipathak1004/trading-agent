"""Loop-control tools.

Agent Zero's loop ends when a tool returns ``break_loop``; the ``response``
tool is how a model delivers its final answer. ``unknown`` catches a
hallucinated tool name and replies with the list of tools that do exist instead
of failing the cycle. Both are reimplemented here without the upstream file
discovery or print styling.
"""

from collections.abc import Mapping

from bq.tools.base import (
    Tool,
    ToolArgument,
    ToolResult,
    ToolSpec,
    bounded_text,
)
from bq.tools.registry import ToolRegistry

MAX_RESPONSE_CHARS = 12_000


class ResponseTool(Tool):
    """Deliver the agent's final answer and end the loop."""

    spec = ToolSpec(
        name="response",
        summary="Deliver the final answer to the caller and end the task loop.",
        arguments=(
            ToolArgument(
                "text",
                "The final answer. Balanced and concise.",
                required=True,
            ),
        ),
        mutating=False,
    )

    def execute(self, args: Mapping[str, object]) -> ToolResult:
        text = bounded_text(args.get("text"), name="text", maximum=MAX_RESPONSE_CHARS)
        return ToolResult(message=text, break_loop=True)


class UnknownTool(Tool):
    """Reply to a tool name that does not exist with the available names."""

    spec = ToolSpec(
        name="unknown",
        summary="Internal fallback for an unrecognized tool name.",
        mutating=False,
    )

    def __init__(self, registry: ToolRegistry) -> None:
        self.registry = registry

    def execute(self, args: Mapping[str, object]) -> ToolResult:
        requested = args.get("tool_name")
        requested_text = requested if isinstance(requested, str) else "(none)"
        available = ", ".join(spec.name for spec in self.registry.specs()) or "none"
        return ToolResult(
            message=(
                f"Unknown tool: {requested_text}. "
                f"Available tools: {available}. "
                "Call one listed tool with the exact name."
            )
        )
