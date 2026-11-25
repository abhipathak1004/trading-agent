import pytest

from bq.agents import AgentDAG, AgentDAGError, AgentNode


def test_dag_runs_roots_then_dependent_nodes() -> None:
    nodes = (
        AgentNode("research", (), lambda context: "facts"),
        AgentNode("portfolio", (), lambda context: "positions"),
        AgentNode(
            "risk",
            ("research", "portfolio"),
            lambda context: "risk-approved",
        ),
        AgentNode(
            "execute",
            ("risk",),
            lambda context: "executed",
        ),
    )
    dag = AgentDAG(nodes)
    result = dag.run("cycle-1", object())
    assert result.cycle_id == "cycle-1"
    assert [node_id for node_id, _ in result.outputs] == [
        "portfolio",
        "research",
        "risk",
        "execute",
    ]
    assert result.skipped == ()


def test_optional_failure_is_skipped_but_required_failure_stops() -> None:
    nodes = (
        AgentNode("research", (), lambda context: "facts"),
        AgentNode(
            "debate",
            ("research",),
            lambda context: (_ for _ in ()).throw(RuntimeError("timeout")),
            optional=True,
        ),
        AgentNode("risk", ("debate",), lambda context: "approved"),
    )
    dag = AgentDAG(nodes)
    result = dag.run(
        "cycle-2",
        object(),
        optional_failures=frozenset({"debate"}),
    )
    assert result.skipped == ("debate", "risk")
    assert result.optional_failures == ("debate",)

    with pytest.raises(AgentDAGError, match="cannot be optional"):
        AgentDAG(
            (
                AgentNode(
                    "risk",
                    (),
                    lambda context: (_ for _ in ()).throw(RuntimeError("failed")),
                    optional=True,
                ),
            )
        )


def test_execution_requires_required_risk_node() -> None:
    with pytest.raises(AgentDAGError, match="depend on the risk"):
        AgentDAG(
            (
                AgentNode("execute", (), lambda context: "execute"),
                AgentNode("risk", (), lambda context: "allow"),
            )
        )
