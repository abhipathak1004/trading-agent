"""Execution adapter seam. Paper is enabled; every other mode fails closed."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from bq.analysis.decision import TradePlan
from bq.execution.paper_executor import (
    ExecutionContext,
    PaperExecution,
    PaperExecutor,
)
from bq.paper_trading import PaperOrder
from bq.risk.policy import RiskDecision, RiskLimits


class ExecutionMode(StrEnum):
    PAPER = "PAPER"
    KITE_SANDBOX = "KITE_SANDBOX"
    LIVE_APPROVAL = "LIVE_APPROVAL"


class ExecutionDisabled(RuntimeError):
    """The selected execution mode is not enabled in this build."""


@dataclass(frozen=True, slots=True)
class ExecutionOutcome:
    mode: ExecutionMode
    execution: PaperExecution

    @property
    def decision(self) -> RiskDecision:
        return self.execution.decision

    @property
    def order(self) -> PaperOrder | None:
        return self.execution.order


class ExecutionAdapter(Protocol):
    mode: ExecutionMode

    def preview(
        self, plan: TradePlan, *, context: ExecutionContext
    ) -> ExecutionOutcome: ...

    def place(
        self,
        plan: TradePlan,
        *,
        context: ExecutionContext,
        client_order_id: str,
    ) -> ExecutionOutcome: ...


class PaperExecutionAdapter:
    mode = ExecutionMode.PAPER

    def __init__(self, path: Path, *, limits: RiskLimits) -> None:
        self.executor = PaperExecutor(path, limits=limits)

    def preview(
        self, plan: TradePlan, *, context: ExecutionContext
    ) -> ExecutionOutcome:
        return ExecutionOutcome(
            mode=self.mode,
            execution=self.executor.preview(plan, context=context),
        )

    def place(
        self,
        plan: TradePlan,
        *,
        context: ExecutionContext,
        client_order_id: str,
    ) -> ExecutionOutcome:
        return ExecutionOutcome(
            mode=self.mode,
            execution=self.executor.execute(
                plan, context=context, client_order_id=client_order_id
            ),
        )


class DisabledExecutionAdapter:
    def __init__(self, mode: ExecutionMode) -> None:
        self.mode = mode

    def preview(
        self, plan: TradePlan, *, context: ExecutionContext
    ) -> ExecutionOutcome:
        raise ExecutionDisabled(
            f"{self.mode.value} execution is not enabled; only PAPER is available."
        )

    def place(
        self,
        plan: TradePlan,
        *,
        context: ExecutionContext,
        client_order_id: str,
    ) -> ExecutionOutcome:
        raise ExecutionDisabled(
            f"{self.mode.value} execution is not enabled; only PAPER is available."
        )


def build_execution_adapter(
    mode: ExecutionMode, path: Path, *, limits: RiskLimits
) -> ExecutionAdapter:
    if mode is ExecutionMode.PAPER:
        return PaperExecutionAdapter(path, limits=limits)
    if mode in (ExecutionMode.KITE_SANDBOX, ExecutionMode.LIVE_APPROVAL):
        return DisabledExecutionAdapter(mode)
    raise ExecutionDisabled("Unknown execution mode.")


__all__ = [
    "DisabledExecutionAdapter",
    "ExecutionAdapter",
    "ExecutionDisabled",
    "ExecutionMode",
    "ExecutionOutcome",
    "PaperExecutionAdapter",
    "build_execution_adapter",
]
