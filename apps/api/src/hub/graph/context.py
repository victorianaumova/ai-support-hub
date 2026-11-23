"""What every node gets besides the state (LangGraph runtime context, P6-01).

The context is per run and is never checkpointed: it holds the pipeline (database, gateway,
prompts) and the stream of the request that started the run.

`step` times a node and makes it a span of the run's trace (P9-01). The steps of one invoke are
collected here and stored on the run (`runs.steps`) when the invoke ends, so the console's Run
tab shows per-step durations with tracing switched off too.
"""

import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal

from hub.domain.enums import RouteReason
from hub.graph.pipeline import STEP_CHECKING, STEP_SEARCHING, Emit, Pipeline
from hub.graph.state import TicketState

# `paused`: the node stopped for the operator (`human_review`, P6-03) and runs again on resume.
type StepOutcome = Literal["ok", "error", "timeout", "paused"]

ERROR_REASONS = [RouteReason.SYSTEM_ERROR.value]
BUDGET_REASONS = [RouteReason.BUDGET_EXCEEDED.value]


@dataclass
class Step:
    """One node of one invoke (`runs.steps`). `outcome` is set by the node wrapper."""

    node: str
    started_at: datetime
    duration_ms: int = 0
    outcome: StepOutcome = "ok"

    def stored(self) -> dict[str, object]:
        return {
            "node": self.node,
            "started_at": self.started_at.isoformat(),
            "duration_ms": self.duration_ms,
            "outcome": self.outcome,
        }


_TRACE_LEVELS = {"ok": "DEFAULT", "paused": "DEFAULT", "timeout": "WARNING", "error": "ERROR"}


@dataclass
class GraphContext:
    pipeline: Pipeline
    emit: Emit
    # The step the stream showed last; the run sends `understanding` itself before the graph.
    last_step: str | None = None
    # The nodes this invoke ran, in order (P9-01).
    steps: list[Step] = field(default_factory=list)

    @contextmanager
    def step(self, node: str) -> Iterator[Step]:
        """Time a node and trace it as a span its model and tool calls nest under."""
        step = Step(node=node, started_at=datetime.now(UTC))
        started = time.perf_counter()
        with self.pipeline.tracer.observe(node) as span:
            try:
                yield step
            finally:
                step.duration_ms = max(0, round((time.perf_counter() - started) * 1000))
                self.steps.append(step)
                span.update(level=_TRACE_LEVELS[step.outcome], metadata={"outcome": step.outcome})

    async def status(self, step: str) -> None:
        """`run.status` when a node starts (api.md §4). A step is sent once, even when two nodes
        share it (intake and triage are both `understanding`); B0 has no search and no check,
        so it never shows them."""
        if step == self.last_step:
            return
        if not self.pipeline.verifies and step in (STEP_SEARCHING, STEP_CHECKING):
            return
        self.last_step = step
        await self.emit("run.status", {"step": step})


def ids(state: TicketState) -> tuple[uuid.UUID, uuid.UUID]:
    """The ticket and run ids of the state."""
    return uuid.UUID(state["ticket_id"]), uuid.UUID(state["run_id"])


def failure_reasons(errors: list[str]) -> list[str]:
    """`budget_exceeded` when a budget stop (G8) ended the run, `system_error` otherwise
    (ai-pipeline §6, row 2)."""
    if any(error.endswith(":BudgetExceededError") for error in errors):
        return list(BUDGET_REASONS)
    return list(ERROR_REASONS)


def error_class(errors: list[str]) -> str | None:
    """The error class of the first failure, for `runs.error` (never an error message: it can
    quote customer text)."""
    return errors[0].split(":", 1)[1] if errors else None
