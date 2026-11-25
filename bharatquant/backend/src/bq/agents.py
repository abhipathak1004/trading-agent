from dataclasses import dataclass
from graphlib import TopologicalSorter
from typing import Callable, Generic, TypeVar

TContext = TypeVar("TContext")
TOutput = TypeVar("TOutput")


class AgentDAGError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class AgentNode(Generic[TContext, TOutput]):
    node_id: str
    after: tuple[str, ...]
    run: Callable[[TContext], TOutput]
    optional: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.node_id, str) or not self.node_id:
            raise AgentDAGError("Agent node identifier is required.")
        if not isinstance(self.after, tuple) or any(
            not isinstance(item, str) or not item for item in self.after
        ):
            raise AgentDAGError("Agent dependencies must be a tuple of IDs.")
        if not callable(self.run):
            raise AgentDAGError("Agent node requires a callable.")
        if not isinstance(self.optional, bool):
            raise AgentDAGError("Agent optional flag must be boolean.")


@dataclass(frozen=True, slots=True)
class AgentCycleResult(Generic[TOutput]):
    cycle_id: str
    outputs: tuple[tuple[str, TOutput], ...]
    skipped: tuple[str, ...]
    optional_failures: tuple[str, ...]


class AgentDAG(Generic[TContext, TOutput]):
    def __init__(self, nodes: tuple[AgentNode[TContext, TOutput], ...]) -> None:
        if not isinstance(nodes, tuple) or not nodes:
            raise AgentDAGError("Agent DAG requires at least one node.")
        if len({node.node_id for node in nodes}) != len(nodes):
            raise AgentDAGError("Agent node identifiers must be unique.")
        node_ids = {node.node_id for node in nodes}
        by_id = {node.node_id: node for node in nodes}
        if "risk" in by_id and by_id["risk"].optional:
            raise AgentDAGError("The risk node cannot be optional.")
        if "execute" in by_id and "risk" not in set(by_id["execute"].after):
            raise AgentDAGError("Execution must depend on the risk node.")
        for node in nodes:
            if any(dependency not in node_ids for dependency in node.after):
                raise AgentDAGError("Agent DAG dependency is missing.")
            if len(set(node.after)) != len(node.after):
                raise AgentDAGError("Agent DAG dependencies must be unique.")
        graph = {node.node_id: set(node.after) for node in nodes}
        try:
            tuple(TopologicalSorter(graph).static_order())
        except Exception as exc:
            raise AgentDAGError("Agent DAG contains a cycle.") from exc
        self.nodes = {node.node_id: node for node in nodes}
        level: dict[str, int] = {}
        remaining = set(node_ids)
        while remaining:
            ready = {
                node_id
                for node_id in remaining
                if all(dependency in level for dependency in graph[node_id])
            }
            if not ready:
                raise AgentDAGError("Agent DAG contains a cycle.")
            for node_id in ready:
                dependencies = graph[node_id]
                level[node_id] = max(
                    (level[dependency] + 1 for dependency in dependencies), default=0
                )
            remaining -= ready
        self.order = tuple(
            tuple(node_id for node_id in sorted(node_ids) if level[node_id] == index)
            for index in range(max(level.values()) + 1)
        )

    def run(
        self,
        cycle_id: str,
        context: TContext,
        *,
        optional_failures: frozenset[str] = frozenset(),
    ) -> AgentCycleResult[TOutput]:
        if not isinstance(cycle_id, str) or not cycle_id:
            raise AgentDAGError("Agent cycle identifier is required.")
        if not isinstance(optional_failures, frozenset):
            raise AgentDAGError("Optional failures must be a frozenset.")
        outputs: list[tuple[str, TOutput]] = []
        skipped: list[str] = []
        failures: list[str] = []
        completed: set[str] = set()
        for node_id in (item for layer in self.order for item in layer):
            node = self.nodes[node_id]
            if any(dependency not in completed for dependency in node.after):
                skipped.append(node_id)
                continue
            if node.optional and node_id in optional_failures:
                skipped.append(node_id)
                failures.append(node_id)
                continue
            try:
                output = node.run(context)
            except Exception as exc:
                if node.optional:
                    skipped.append(node_id)
                    failures.append(node_id)
                    continue
                raise AgentDAGError(
                    f"Agent node '{node_id}' failed: {type(exc).__name__}"
                ) from exc
            outputs.append((node_id, output))
            completed.add(node_id)
        return AgentCycleResult(
            cycle_id=cycle_id,
            outputs=tuple(outputs),
            skipped=tuple(skipped),
            optional_failures=tuple(failures),
        )
