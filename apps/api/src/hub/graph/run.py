"""One run of the workflow graph for one customer message (P6-01; the linear run of P3-05 to
P5-04 moved into `hub.graph.build`).

Called by `POST /tickets` for a new ticket, and by the evaluation runner (`hub_evals.runner`,
with `context = eval`, the evaluation database and its own cost limit as `budget`).

1. `run.status` `understanding` is sent at once; the run row is created and committed before the
   graph starts (`store.start_run`). No run starts when the ticket is no longer the AI's or the
   message is not its own: the stream then ends with the ticket's status.
2. The graph runs with the ticket id as its checkpoint thread (ai-pipeline §1) and this run's
   identity and nothing else as input (`state.run_input`). Nodes send `run.status` as they start.
3. The stream ends with `ticket.status` (the status the last node that changed the ticket left
   it in), with `error` before it when a node failed and the ticket was handed off.

The route decides how the run ends (ai-pipeline §6, P6-02): `pending_approval` with a draft,
`handed_off` with a notice (also after a failed node: `system_error`, or `budget_exceeded` after a
G8 budget stop), or `answered` with an auto-reply when a lead turned it on.

**Pause and resume (P6-03, ADR-0008).** On the approval and handoff routes the run pauses in
`human_review` (`runs.status = interrupted`); `run_ticket` then ends the stream with the ticket's
status. `resume_run` continues it with the operator's `ReviewDecision`, after the console has
written the decision (owner decision): the run ends in `respond` with the decision's message as
its final message. The checkpoint is in Postgres, so a paused run survives a restart.

**Last resort.** When the graph itself fails (a node that does not fail into a handoff, the
checkpointer, the database), the run is `failed` and the ticket goes to a human with
`system_error` (`store.fail`). When even that cannot be saved, the ticket is still `processing`:
it waits in the operator queue (a claim hands it off) and recovery (`hub.graph.recover`) picks
the run up.
The stream says so instead of claiming a handoff. Logs and the run row name only the error class:
an error message can quote customer text.

**Trace and steps (P9-01).** Every invoke of the graph (a new run, a resume, a recovery) goes
through `invoke`: it opens the run's trace (`hub.observability.tracing`; the same trace for all
three, its id is derived from the run id) and stores the invoke's per-step durations on the run
(`runs.steps`), also when the graph fails.

An operator may claim the ticket while the run is going (`processing → handed_off`, reason
`operator_takeover`). Then the run still finishes, but its draft is stored as `superseded` and the
ticket is left as the operator has it.
"""

import asyncio
import logging
import uuid
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.types import Command

from hub.domain.enums import TicketStatus
from hub.domain.review import ReviewDecision
from hub.graph.build import Graph, compile_graph
from hub.graph.context import ERROR_REASONS, GraphContext
from hub.graph.pipeline import (
    GRAPH_VERSION,
    STEP_UNDERSTANDING,
    Emit,
    Pipeline,
    RunOutcome,
    run_failed_problem,
)
from hub.graph.state import run_input
from hub.graph.store import current_status, fail, record_steps, start_run

logger = logging.getLogger(__name__)


# How long a resume waits for the run to reach its pause: the console can act in the moment
# between `human_review`'s commit and the checkpoint of the pause.
RESUME_WAIT_S = 5.0
RESUME_POLL_S = 0.1
# LangGraph's key for the pause in the result of an invoke.
INTERRUPT_KEY = "__interrupt__"


class RunIncompleteError(Exception):
    """The graph ended without a node that settled the ticket's status."""


def thread(ticket_id: uuid.UUID) -> RunnableConfig:
    """The checkpoint thread of a ticket (ai-pipeline §1: `thread_id` = ticket id)."""
    return {"configurable": {"thread_id": str(ticket_id)}}


async def ignore(_name: str, _data: dict[str, Any]) -> None:
    """The stream of a resumed or recovered run: no client listens."""


def run_summary(final: Any) -> dict[str, Any]:  # noqa: ANN401
    """What the trace shows as the outcome of an invoke: route, reasons and the like, no text."""
    if not isinstance(final, dict):
        return {}
    triage = final.get("triage")
    return {
        "paused": INTERRUPT_KEY in final,
        "intent": getattr(triage, "intent", None),
        "route": final.get("route"),
        "route_reasons": final.get("route_reasons"),
        "confidence": final.get("confidence"),
        "ticket_status": final.get("ticket_status"),
        "errors": final.get("errors"),
    }


async def invoke(
    pipeline: Pipeline,
    graph: Graph,
    payload: Any,  # noqa: ANN401 - a run input or a LangGraph Command
    *,
    ticket_id: uuid.UUID,
    run_id: uuid.UUID,
    context: GraphContext,
    how: str,
) -> Any:  # noqa: ANN401 - what LangGraph returns
    """One invoke of the graph inside the run's trace; its steps are stored on the run whatever
    happens. `how`: `start`, `resume` or `recover` (the trace shows each as a span of its own)."""
    with pipeline.tracer.run(
        run_id,
        ticket_id=ticket_id,
        context=pipeline.context.value,
        name=f"run.{how}",
        attributes={
            "run_id": str(run_id),
            "ticket_id": str(ticket_id),
            "context": pipeline.context.value,
            "graph_version": GRAPH_VERSION,
            "variant": pipeline.variant,
            "prompt_versions": ",".join(
                f"{node}:{version}" for node, version in sorted(pipeline.prompt_versions.items())
            ),
        },
    ) as root:
        try:
            final = await graph.ainvoke(payload, config=thread(ticket_id), context=context)
        except Exception as exc:
            root.update(level="ERROR", status_message=type(exc).__name__)
            raise
        finally:
            await record_steps(pipeline, run_id, context.steps)
        summary = run_summary(final)
        root.update(output=summary, level="ERROR" if summary.get("errors") else "DEFAULT")
        return final


async def outcome_status(pipeline: Pipeline, ticket_id: uuid.UUID, final: Any) -> TicketStatus:  # noqa: ANN401
    """The ticket's status after an invoke. A paused run did not return from `human_review`, so
    its status is read from the ticket; otherwise the last node that changed it says."""
    if not isinstance(final, dict):
        raise RunIncompleteError
    if INTERRUPT_KEY in final:
        return await current_status(pipeline, ticket_id)
    shown = final.get("ticket_status")
    if shown is None:
        raise RunIncompleteError
    return TicketStatus(shown)


async def run_ticket(
    pipeline: Pipeline, *, ticket_id: uuid.UUID, message_id: uuid.UUID, emit: Emit
) -> RunOutcome:
    """One run for the customer message `message_id` of a ticket in `processing`. Never raises
    for a failed step: the failure is recorded and the ticket handed off. Emits the stream
    events `run.status`, then `ticket.status` (and `error` before it when the run failed)."""
    run_id: uuid.UUID | None = None
    try:
        await emit("run.status", {"step": STEP_UNDERSTANDING})
        started = await start_run(pipeline, ticket_id, message_id)
        if isinstance(started, TicketStatus):
            # Nothing to do: the ticket is no longer the AI's, or the message is not its own.
            # The stream still ends with the ticket's status (api.md §4).
            await emit("ticket.status", {"status": started.value})
            return RunOutcome(run_id=None, ticket_status=started, failed=False)
        run_id, customer_id = started
        pipeline.live.claim(run_id)
        try:
            graph = await pipeline.checkpoints.graph(compile_graph)
            final = await invoke(
                pipeline,
                graph,
                run_input(
                    ticket_id=str(ticket_id),
                    run_id=str(run_id),
                    message_id=str(message_id),
                    customer_ref=str(customer_id),
                ),
                ticket_id=ticket_id,
                run_id=run_id,
                context=GraphContext(pipeline=pipeline, emit=emit, last_step=STEP_UNDERSTANDING),
                how="start",
            )
            status = await outcome_status(pipeline, ticket_id, final)
        finally:
            pipeline.live.release(run_id)
        failed = bool(final.get("errors"))
    except Exception as exc:
        error = type(exc).__name__
        logger.warning("run failed", extra={"run_id": str(run_id), "error": error})
        try:
            failed_status: TicketStatus | None = await fail(
                pipeline, ticket_id, run_id, error, {}, list(ERROR_REASONS)
            )
        except Exception as second:
            logger.error(
                "run failure could not be recorded",
                extra={"run_id": str(run_id), "error": type(second).__name__},
            )
            failed_status = None
        await emit("error", run_failed_problem())
        shown_status = failed_status if failed_status is not None else TicketStatus.PROCESSING
        await emit("ticket.status", {"status": shown_status.value})
        return RunOutcome(run_id=run_id, ticket_status=failed_status, failed=True)
    if failed:
        await emit("error", run_failed_problem())
    await emit("ticket.status", {"status": status.value})
    return RunOutcome(run_id=run_id, ticket_status=status, failed=failed)


async def paused_at(pipeline: Pipeline, ticket_id: uuid.UUID, run_id: uuid.UUID) -> bool:
    """Whether the ticket's checkpoint holds this run, paused in `human_review`."""
    graph = await pipeline.checkpoints.graph(compile_graph)
    snapshot = await graph.aget_state(thread(ticket_id))
    values: dict[str, Any] = snapshot.values
    return values.get("run_id") == str(run_id) and bool(snapshot.interrupts)


async def resume_run(
    pipeline: Pipeline, *, ticket_id: uuid.UUID, run_id: uuid.UUID, decision: ReviewDecision
) -> bool:
    """Resume the paused run `run_id` with the operator's decision; True when it ended.

    Called by the console after its transaction committed, so it never raises: the decision is
    already sent. A run that does not reach its pause within `RESUME_WAIT_S` stays
    `interrupted` (logged). A failure after the resume is the run's last resort: the run is
    `failed` (the ticket is already with a human)."""
    try:
        deadline = asyncio.get_running_loop().time() + RESUME_WAIT_S
        while run_id in pipeline.live or not await paused_at(pipeline, ticket_id, run_id):
            if asyncio.get_running_loop().time() >= deadline:
                logger.warning("run not paused, not resumed", extra={"run_id": str(run_id)})
                return False
            await asyncio.sleep(RESUME_POLL_S)
        if not pipeline.live.claim(run_id):  # pragma: no cover - checked in the loop above
            return False
    except Exception as exc:
        logger.warning(
            "run not resumed", extra={"run_id": str(run_id), "error": type(exc).__name__}
        )
        return False
    try:
        graph = await pipeline.checkpoints.graph(compile_graph)
        final = await invoke(
            pipeline,
            graph,
            Command(resume=decision.model_dump(mode="json")),
            ticket_id=ticket_id,
            run_id=run_id,
            context=GraphContext(pipeline=pipeline, emit=ignore),
            how="resume",
        )
        return isinstance(final, dict) and not final.get("errors")
    except Exception as exc:
        await settle_failure(pipeline, ticket_id, run_id, exc)
        return False
    finally:
        pipeline.live.release(run_id)


async def settle_failure(
    pipeline: Pipeline, ticket_id: uuid.UUID, run_id: uuid.UUID, exc: Exception
) -> TicketStatus | None:
    """The last resort for a run without a stream (a resume, a recovery): the run is `failed`
    and the ticket goes to a human. Logs only the error class."""
    error = type(exc).__name__
    logger.warning("run failed", extra={"run_id": str(run_id), "error": error})
    try:
        return await fail(pipeline, ticket_id, run_id, error, {}, list(ERROR_REASONS))
    except Exception as second:
        logger.error(
            "run failure could not be recorded",
            extra={"run_id": str(run_id), "error": type(second).__name__},
        )
        return None
