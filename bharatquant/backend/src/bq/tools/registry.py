"""Tool registry and per-agent access policy.

Adapted from how Agent Zero separates *tool discovery* from *tool policy*: a
tool existing does not mean every agent may run it. BharatQuant needs this
because the plan requires that research agents cannot place orders and that
execution cannot change limits.

Design choices for this project:

* Registration is explicit and static. There is no import-by-path, so a
  misconfigured directory cannot execute arbitrary code.
* Policy is a denylist applied on top of an allowlist-free registry. A tool must
  be registered *and* permitted for the calling agent profile.
* ``read_only`` callers are refused every ``mutating`` tool, which is the
  coarse safety net; per-agent ``blocked`` sets refine it.
"""

from collections.abc import Iterable, Mapping

from bq.tools.base import (
    Tool,
    ToolBlocked,
    ToolNotFound,
    ToolSpec,
    ToolValidationError,
    render_tools_prompt,
)


class ToolRegistry:
    """Holds the tools this process knows about."""

    def __init__(self, tools: Iterable[Tool] = ()) -> None:
        self._tools: dict[str, Tool] = {}
        for tool in tools:
            self.register(tool)

    def register(self, tool: Tool) -> None:
        if not isinstance(tool, Tool):
            raise ToolValidationError("Registry entries must be Tool instances.")
        name = tool.spec.name
        if name in self._tools:
            raise ToolValidationError(f"Duplicate tool registration: {name}.")
        self._tools[name] = tool

    def get(self, name: str) -> Tool:
        try:
            return self._tools[name]
        except KeyError as exc:
            raise ToolNotFound(f"Unknown tool: {name}.") from exc

    def specs(self) -> tuple[ToolSpec, ...]:
        return tuple(self._tools[name].spec for name in sorted(self._tools))

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and name in self._tools

    def __len__(self) -> int:
        return len(self._tools)


class AgentToolPolicy:
    """What one agent profile may run.

    ``blocked`` names are always refused. ``read_only`` refuses every mutating
    tool even if it is not named in ``blocked``.
    """

    def __init__(
        self,
        *,
        profile: str,
        blocked: frozenset[str] = frozenset(),
        read_only: bool = True,
    ) -> None:
        if not isinstance(profile, str) or not profile:
            raise ToolValidationError("Policy profile is required.")
        if not isinstance(blocked, frozenset) or any(
            not isinstance(name, str) for name in blocked
        ):
            raise ToolValidationError("Blocked tools must be a frozenset of names.")
        if not isinstance(read_only, bool):
            raise ToolValidationError("read_only must be a boolean.")
        self.profile = profile
        self.blocked = blocked
        self.read_only = read_only

    def check(self, spec: ToolSpec) -> None:
        if spec.name in self.blocked:
            raise ToolBlocked(
                f"Tool '{spec.name}' is not permitted for profile '{self.profile}'."
            )
        if self.read_only and spec.mutating:
            raise ToolBlocked(
                f"Profile '{self.profile}' is read-only and cannot run "
                f"mutating tool '{spec.name}'."
            )

    @property
    def allows_mutation(self) -> bool:
        return not self.read_only


class ToolRunner:
    """Runs tools for one agent profile, enforcing its policy."""

    def __init__(self, registry: ToolRegistry, policy: AgentToolPolicy) -> None:
        if not isinstance(registry, ToolRegistry):
            raise ToolValidationError("A ToolRegistry is required.")
        if not isinstance(policy, AgentToolPolicy):
            raise ToolValidationError("An AgentToolPolicy is required.")
        self.registry = registry
        self.policy = policy

    def permitted_specs(self) -> tuple[ToolSpec, ...]:
        allowed = []
        for spec in self.registry.specs():
            try:
                self.policy.check(spec)
            except ToolBlocked:
                continue
            allowed.append(spec)
        return tuple(allowed)

    def tools_prompt(self) -> str:
        return render_tools_prompt(self.permitted_specs())

    def run(
        self, name: str, args: Mapping[str, object] | None = None
    ) -> dict[str, object]:
        tool = self.registry.get(name)
        self.policy.check(tool.spec)
        result = tool.run(args or {})
        return {
            "tool": tool.spec.name,
            "message": result.message,
            "break_loop": result.break_loop,
            "additional": dict(result.additional),
        }
