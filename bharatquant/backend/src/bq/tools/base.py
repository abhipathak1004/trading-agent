"""Agent tool contract.

A tool is a named capability with a declared argument surface. The patterns
here are adapted from how Agent Zero structures its tools — a per-call object
that returns a bounded message plus a ``break_loop`` flag — but everything in
this module is original code written against BharatQuant's conventions:

* Tools are registered explicitly, never discovered by importing arbitrary
  files from disk. Loading code by path is an avoidable execution surface.
* Every argument and result is bounded before it can reach storage or a prompt.
* Nothing here talks to a model. A tool is a deterministic function of its
  arguments and the local database.
"""

import re
from abc import ABC, abstractmethod
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

TOOL_NAME = re.compile(r"[a-z][a-z0-9_]{0,63}")
MAX_ARGS_BYTES = 64 * 1024
MAX_RESULT_CHARS = 24_000
MAX_ARGUMENT_CHARS = 8_000
MAX_ARGUMENTS = 32


class ToolError(ValueError):
    """A tool could not be run for a reason the caller can act on."""


class ToolValidationError(ToolError):
    """The supplied arguments did not match the tool's declared surface."""


class ToolNotFound(ToolError):
    """No tool is registered under the requested name."""


class ToolBlocked(ToolError):
    """The tool exists but policy does not allow this caller to run it."""


def validate_tool_name(name: object) -> str:
    if not isinstance(name, str) or TOOL_NAME.fullmatch(name) is None:
        raise ToolValidationError("Invalid tool name.")
    return name


@dataclass(frozen=True, slots=True)
class ToolArgument:
    """One declared tool argument, used for validation and the prompt listing."""

    name: str
    description: str
    required: bool = False
    kind: type = str

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not TOOL_NAME.fullmatch(self.name):
            raise ToolValidationError("Invalid tool argument name.")
        if not isinstance(self.description, str) or not self.description:
            raise ToolValidationError("Tool argument description is required.")
        if not isinstance(self.required, bool):
            raise ToolValidationError("Tool argument required flag must be boolean.")
        if self.kind not in (str, int, bool):
            raise ToolValidationError("Tool argument kinds are str, int or bool.")


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """A tool's stable identity and the surface it presents to a caller."""

    name: str
    summary: str
    arguments: tuple[ToolArgument, ...] = ()
    mutating: bool = False

    def __post_init__(self) -> None:
        validate_tool_name(self.name)
        if not isinstance(self.summary, str) or not 1 <= len(self.summary) <= 400:
            raise ToolValidationError("Tool summary must be 1 to 400 characters.")
        if not isinstance(self.mutating, bool):
            raise ToolValidationError("Tool mutating flag must be boolean.")
        names = [argument.name for argument in self.arguments]
        if len(set(names)) != len(names):
            raise ToolValidationError("Tool arguments must be unique.")
        if len(self.arguments) > MAX_ARGUMENTS:
            raise ToolValidationError("Tool has too many arguments.")

    @property
    def argument_names(self) -> frozenset[str]:
        return frozenset(argument.name for argument in self.arguments)

    @property
    def required_arguments(self) -> frozenset[str]:
        return frozenset(
            argument.name for argument in self.arguments if argument.required
        )

    def render(self) -> str:
        lines = [f"### {self.name}", self.summary]
        if self.arguments:
            rendered = []
            for argument in self.arguments:
                suffix = "" if argument.required else " (optional)"
                rendered.append(f"`{argument.name}`{suffix}: {argument.description}")
            lines.append("args: " + "; ".join(rendered))
        else:
            lines.append("args: none")
        if self.mutating:
            lines.append("side effect: writes local state")
        return "\n".join(lines)


@dataclass(frozen=True, slots=True)
class ToolResult:
    """What a tool returns. ``break_loop`` ends the agent's thinking loop."""

    message: str
    break_loop: bool = False
    additional: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.message, str):
            raise ToolValidationError("Tool result message must be text.")
        if not isinstance(self.break_loop, bool):
            raise ToolValidationError("Tool result break_loop must be boolean.")
        if not isinstance(self.additional, Mapping) or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in self.additional.items()
        ):
            raise ToolValidationError("Tool result additional must be a string map.")

    def bounded(self, limit: int = MAX_RESULT_CHARS) -> "ToolResult":
        if len(self.message) <= limit:
            return self
        return ToolResult(
            message=self.message[:limit].rstrip() + "\n\n[truncated]",
            break_loop=self.break_loop,
            additional=self.additional,
        )


def _check_utf8_size(args: Mapping[str, object]) -> None:
    size = 0
    for key, value in args.items():
        if not isinstance(key, str):
            raise ToolValidationError("Tool argument names must be text.")
        size += len(key.encode("utf-8"))
        if isinstance(value, str):
            if len(value) > MAX_ARGUMENT_CHARS:
                raise ToolValidationError(f"Tool argument '{key}' is too long.")
            size += len(value.encode("utf-8"))
        else:
            size += 16
    if size > MAX_ARGS_BYTES:
        raise ToolValidationError("Tool arguments exceed the size limit.")


class Tool(ABC):
    """Base class for a runnable capability."""

    spec: ToolSpec

    def run(self, args: Mapping[str, object] | None = None) -> ToolResult:
        checked = self.validate(args or {})
        result = self.execute(checked)
        if not isinstance(result, ToolResult):
            raise ToolValidationError("Tool did not return a ToolResult.")
        return result.bounded()

    def validate(self, args: Mapping[str, object]) -> dict[str, object]:
        if not isinstance(args, Mapping):
            raise ToolValidationError("Tool arguments must be an object.")
        unknown = set(args) - self.spec.argument_names
        if unknown:
            raise ToolValidationError(
                "Unexpected tool argument(s): " + ", ".join(sorted(unknown))
            )
        _check_utf8_size(args)
        missing = self.spec.required_arguments - set(args)
        if missing:
            raise ToolValidationError(
                "Missing required tool argument(s): " + ", ".join(sorted(missing))
            )
        return dict(args)

    @abstractmethod
    def execute(self, args: Mapping[str, object]) -> ToolResult:
        """Perform the tool's work. Arguments are already validated."""


def decimal_int(value: object, *, name: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ToolValidationError(f"'{name}' must be an integer.")
    if not minimum <= value <= maximum:
        raise ToolValidationError(f"'{name}' must be between {minimum} and {maximum}.")
    return value


def bounded_text(
    value: object, *, name: str, maximum: int, required: bool = True
) -> str:
    if value is None:
        if required:
            raise ToolValidationError(f"'{name}' is required.")
        return ""
    if not isinstance(value, str):
        raise ToolValidationError(f"'{name}' must be text.")
    text = value.strip()
    if required and not text:
        raise ToolValidationError(f"'{name}' cannot be empty.")
    if len(text) > maximum:
        raise ToolValidationError(f"'{name}' must be at most {maximum} characters.")
    return text


def bounded_bool(value: object, *, name: str, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
    raise ToolValidationError(f"'{name}' must be a boolean.")


def render_tools_prompt(specs: Iterable[ToolSpec]) -> str:
    """Render the available-tools block that would precede a model call."""
    ordered = sorted(specs, key=lambda spec: spec.name)
    if not ordered:
        return "## available tools\nNo tools are available to this agent."
    blocks = "\n\n".join(spec.render() for spec in ordered)
    return (
        "## available tools\n"
        "Use only the tools listed here, with the exact names shown. "
        "Arguments are named; unknown arguments are rejected.\n\n"
        f"{blocks}"
    )
